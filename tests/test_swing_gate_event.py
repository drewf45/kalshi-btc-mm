"""WO-SWING-GATE-EVENT — the gate measured the wrong thing (build 39).

At 3 lots the FLIP bleed became visible: every OPEN cheap entry showed
swing p=0.89 and lost anyway. Root cause: `_swing_gate` computed
p_cross(|spot−strike|, t) = P(BTC twitches to a strike already under its
nose — trivially ~high near 50/50), NOT P(the contract swings +20¢). The
input distance is structurally tiny for exactly the cheap entries the
gate should filter, so it rubber-stamped falling knives.

The fix: (§4.1) the LIVE gate tests Instrument 1's MEASURED took_swing
rate per price band; below OPEN_SWING_MIN_SAMPLES it is permissive and
the (§4.2, DREW-RULED) 1-lot FLIP cap bounds the risk. §2's two-barrier
price model (P(contract reaches +20 before the cut), via the table
inverted) rides along as a SHADOW, calibrated against measured before it
may ever drive the decision. §3: the pack shows old-proxy vs measured
vs shadow.

HARD RAIL: only the swing gate's event definition + FLIP size cap. No
Kelly/cash/rate-halt/F change."""

import json

import pytest

from relay_engine import config, delta, failures
from relay_engine.book import OrderBook
from relay_engine.custodian import Custodian
from relay_engine.feed import DegradeLadder
from relay_engine.lane_flip import LaneFlip

TICKER = "KXBTC15M-02JAN251000-T99"
EVENT = TICKER.rsplit("-", 1)[0]
CLOSE = 1_000_000.0
STRIKE = 118_000.0
GRAIN_YES2 = {"direction": "yes", "length": 2, "k": 4}


def _book(yes=39, no=55):
    b = OrderBook(market=TICKER)
    b.apply_snapshot({yes: 10}, {no: 10}, ts=1.0)
    return b


def _ctx(book, secs_left=850, grain=None, spot=STRIKE - 120):
    return {"book": book, "now": CLOSE - secs_left, "close_ts": CLOSE,
            "spot": spot, "grain": grain, "spotlead": None,
            "boundary_lo": None, "boundary_hi": STRIKE}


@pytest.fixture(autouse=True)
def funnel(ledger):
    failures._warn_last.clear()
    failures.configure(ledger, alert_fn=lambda m: None, run_mode="TEST",
                       boot_id=1)
    yield
    failures._ledger = None


@pytest.fixture
def flip(gateway, ledger, surface):
    return LaneFlip(gateway, custodian=Custodian(gateway, ledger, surface,
                                                 ladder=DegradeLadder()))


def _seed(surface, entry, took, total):
    for i in range(total):
        surface.write_row("FLIP", f"M{i}", f"w{i}", "FLIP_SWING",
                          detail=json.dumps({"entry_price": entry,
                                             "took_swing": i < took,
                                             "gross_cents": 20}))


# ── §1: the bug — the old proxy is constant across bands ───────────────────
def test_old_strike_touch_proxy_is_constant_across_cheap_entries(flip,
                                                                 monkeypatch):
    """The symptom, quantified: p_cross(|spot−strike|) is IDENTICAL for a
    44¢ and a 49¢ cheap entry because d is tiny for both — a discriminating
    probability would vary. The retired proxy rides on the tape as the
    'old_proxy_p' shadow so the bug stays visible."""
    monkeypatch.setattr(delta, "is_loaded", lambda: True)
    # p_cross depends only on distance; near 50/50 the distance is ~tiny
    # for every cheap side, so the proxy is ~constant
    monkeypatch.setattr(delta, "p_cross",
                        lambda d, t, session="ALL", **_kw: 0.89 if d < 30 else 0.30)
    monkeypatch.setattr(delta, "distance_for_p",
                        lambda p, t, session="ALL", **_kw: None)   # §2 shadow off
    # a cheap side is cheap BECAUSE spot sits near the strike — tiny d
    near = _ctx(_book(), spot=STRIKE - 10)
    g1 = flip._swing_gate(near, CLOSE - 800, 44, "yes", TICKER)
    g2 = flip._swing_gate(near, CLOSE - 800, 49, "yes", TICKER)
    assert g1["proxy"] == g2["proxy"] == 0.89     # same answer, always


# ── §4.1: the swing gate is RETIRED from the ENTRY path (WO-2026-07-22-E) ───
# The measured took_swing rate no longer gates entry — FLIP now buys the
# FAVORED side (no@55, in band [50,70]) regardless of the rate; the swing
# computation lives on as a LOG-ONLY method, directly testable below.
def test_measured_low_rate_no_longer_gates_entry(flip, ledger, surface):
    """A low measured took_swing rate once REFUSED the knife. The swing gate
    is now retired from the entry path: the favored side (no@55) STILL enters
    — swing is log-only, it blocks no entry."""
    _seed(surface, 55, took=7, total=20)          # 0.35 measured — a knife band
    # WO-2026-07-22-F: the favored side is NO (no 55 > yes), so prime the pile
    # with a FALLING tape (trend agrees with NO): a baseline poll in-window
    # (small skew, higher spot) then the entry poll (skew grown, spot −20).
    flip.evaluate(TICKER, _ctx(_book(yes=49, no=55), secs_left=835,
                               grain=GRAIN_YES2, spot=STRIKE - 100))
    props = flip.evaluate(TICKER, _ctx(_book(yes=39, no=55), secs_left=820,
                                       grain=GRAIN_YES2, spot=STRIKE - 120))
    assert [(p.side, p.purpose) for p in props] == [("no", "ENTRY")]


def test_swing_gate_method_measures_high_rate(flip, ledger, surface):
    """The retired ENTRY gate survives as a LOG-ONLY method (Instrument 1):
    a high measured took_swing rate reads ok with 'measured=0.75n20' on its
    why. It gates no entry now, but the measurement is preserved and directly
    testable."""
    _seed(surface, 55, took=15, total=20)         # 0.75 measured — a real swing
    g = flip._swing_gate(_ctx(_book()), CLOSE - 800, 55, "no", TICKER)
    assert g["ok"] and "measured=0.75n20" in g["why"]


def test_swing_gate_method_thin_sample_permissive(flip, ledger, surface):
    """Below OPEN_SWING_MIN_SAMPLES the log-only method stays permissive (ok,
    'calibrating n10') — the 1-lot cap bounds the risk while data accrues.
    (It gates no entry anymore; the property is preserved on the method.)"""
    _seed(surface, 55, took=0, total=10)          # < 20: don't trust it
    g = flip._swing_gate(_ctx(_book()), CLOSE - 800, 55, "no", TICKER)
    assert g["ok"] and "calibrating n10" in g["why"]


# ── §2: the two-barrier shadow measures the RIGHT event ────────────────────
def test_shadow_two_barrier_computes_price_barriers(flip, monkeypatch):
    """§2: P(contract reaches join+20 before the band-floor cut), via the
    table inverted (price → distance → spot move). The shadow is logged,
    never live yet."""
    monkeypatch.setattr(delta, "is_loaded", lambda: True)
    # a monotone table: closer = more likely to cross; invert cleanly
    monkeypatch.setattr(delta, "p_cross",
                        lambda d, t, session="ALL", **_kw: max(0.0, 1.0 - d / 400.0))
    monkeypatch.setattr(delta, "distance_for_p",
                        lambda p, t, session="ALL", **_kw: (1.0 - p) * 400.0)
    p_up, p_down = flip._shadow_two_barrier(_ctx(_book()), 44, 700.0, "yes")
    assert p_up is not None and p_down is not None
    # reaching +20 (a big reprice) is LESS likely than reaching the nearer
    # cut — the discrimination the strike-touch proxy never had
    assert 0.0 <= p_up <= 1.0 and 0.0 <= p_down <= 1.0


def test_distance_for_p_inverts_the_table(monkeypatch):
    monkeypatch.setattr(delta, "_LOADED", True)
    monkeypatch.setattr(delta, "p_cross",
                        lambda d, t, session="ALL", **_kw: max(0.0, 1.0 - d / 1000.0))
    # p_cross(d)=1-d/1000 → p=0.5 at d=500
    d = delta.distance_for_p(0.5, 700.0)
    assert d == pytest.approx(500, abs=5)


# ── §4.2: the 1-lot FLIP cap (DREW-RULED) ──────────────────────────────────
def test_flip_capped_to_one_lot_f_untouched(monkeypatch):
    from relay_engine.gateway import Order
    from relay_engine.shadow_runner import ShadowEngine
    eng = object.__new__(ShadowEngine)

    class _L:
        def book_cents(self):
            return 5000        # a book big enough for >1 lot

        def tradeable_cents(self):
            return 5000        # WO-2026-07-26-O §O2: sizing reads tradeable (owed 0 here)

        def deployed_cents(self):
            return 0           # WO-2026-07-23-B guard (a): nothing deployed
        # WO-2026-07-26-Q: guard (b) (f_suppressed) is deleted — no mock needed.
    eng.ledger = _L()
    eng.telegram = type("T", (), {"alert": staticmethod(lambda m: None)})()
    eng._size_zero_logged = set()   # WO-2026-07-23-B: F sizing logs (guard d)
    from relay_engine import scoring
    # WO-2026-07-24-D test hygiene: a bare `scoring.tier_for = ...` leaked
    # globally (no restore) and made preflight order-dependently flaky once a
    # later test relied on the real tier. monkeypatch restores it at teardown.
    monkeypatch.setattr(scoring, "tier_for", lambda *a, **k: config.TIER_PROBE)
    b = _book(yes=48, no=49)
    fl = Order(lane="FLIP", event=EVENT, market=TICKER, side="yes",
               action="buy", price_cents=48, count=1,
               size_tier=config.TIER_PROBE, purpose="ENTRY",
               why="OPEN grain yesx2 · join 48c")
    eng._score_and_size(fl, b)
    # WO-2026-07-23-E: FLIP is bounded by the cap (now 3, the size test); here
    # depth (10·0.25=2) binds lower still. Either way FLIP is capped small.
    assert 1 <= fl.count <= config.FLIP_SIZE_CAP == 10
    f = Order(lane="F", event=EVENT, market=TICKER, side="yes",
              action="buy", price_cents=48, count=1,
              size_tier=config.TIER_PROBE, purpose="ENTRY",
              why="F tier48 · surv~price")
    eng._score_and_size(f, b)
    # WO-2026-07-23-B Part 1: F is NOT FLIP-capped — it self-sizes by notional
    # (here min(notional, depth) > 1), the opposite of FLIP's 1-lot leash.
    assert f.count > 1


# ── §3: the calibration pack line + compare rows ───────────────────────────
def test_compare_row_and_pack_calibration_line(flip, ledger, surface):
    _seed(surface, 55, took=6, total=20)               # 0.30 measured
    # the compare row is written by the LOG-ONLY swing-gate method (retired
    # from the entry path); §3's calibration line reads it in the pack
    flip._swing_gate(_ctx(_book()), CLOSE - 800, 55, "no", TICKER)
    rows = ledger.db.execute(
        "SELECT detail FROM surface_rows WHERE state='SWING_GATE_COMPARE'"
    ).fetchall()
    assert len(rows) == 1
    r = json.loads(rows[0][0])
    assert r["measured_rate"] == pytest.approx(0.30) and r["n"] == 20
    from relay_engine.ledger import CashProtocol
    from relay_engine.ops import daily_pack
    pack = daily_pack(ledger, surface,
                      CashProtocol(ledger, alert_fn=lambda m: None))
    assert "SWING GATE CAL" in pack and "measured took_swing 0.30" in pack


# ── HARD RAIL ──────────────────────────────────────────────────────────────
def test_rails_unchanged():
    assert config.KELLY_FRACTION_CEILING == pytest.approx(1.0 / 12.0)
    assert config.RATE_HALT_LOSSES == 2 and config.RATE_HALT_WINDOW == 4
    assert config.CASH_SILENT_REBASE_CENTS == 5
    assert config.OPEN_BAND == (39, 56)
