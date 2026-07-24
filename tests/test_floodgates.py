"""WO-2026-07-24-G "THE FLOODGATES ORDER" — the dials to scale with the book
already existed; the WALL they pressed against measured imaginary risk in the
wrong scope, and it quietly halved F in the desk's two busiest windows (the
40115-class squeeze). Fix the meter, open the dials, let the halt grow with the
size it guards, give the stop its eyes. Parts 1 (wall), 2 (FLIP scales + halt
scales), 3 (sighted stop live), 4/6 (instrument + dial<wall).

HARD RAIL: F byte-identical (lane_fh8 untouched; F's notional path is not
touched — only its WALL rose 0.20→0.25, which loosens, never tightens)."""

import pytest

from relay_engine import config, failures, scoring
from relay_engine.book import OrderBook
from relay_engine.custodian import Custodian
from relay_engine.feed import DegradeLadder
from relay_engine.gateway import Gateway, Order
from relay_engine.ledger import Ledger
from relay_engine.lane_flip import LaneFlip
from relay_engine.shadow_runner import ShadowEngine

EVENT = "KXBTC15M-02JAN251000"
TICKER = EVENT + "-T99"
CLOSE = 1_000_000.0


def _book(yes=48, no=49, q=1000):
    b = OrderBook(market=TICKER)
    b.apply_snapshot({yes: q}, {no: q}, ts=1.0)
    return b


def _entry(lane, event, market, price, count):
    return Order(lane=lane, event=event, market=market, side="yes", action="buy",
                 price_cents=price, count=count, size_tier=config.TIER_PROBE,
                 purpose="ENTRY", why="OPEN grain yesx2 · join 48c"
                 if lane == "FLIP" else "F tier · surv~price")


def _hold(gw, lane, market, count, basis):
    """Book a held position directly (skips submit's unrelated entry walls)."""
    key = (EVENT, market, lane)
    gw.positions[key] = count
    gw.gross_open[key] = count
    gw.pos_basis[key] = basis


# ── PART 1a: the wall is LANE-SCOPED — FLIP's position can't squeeze F ───────
def test_flip_exposure_does_not_count_against_f_wall(tmp_path):
    """The 40115-class squeeze: FLIP holding a position in a market must NOT
    consume F's wall in that market. At equal conditions, the lane-scoped wall
    admits an F order that the old cross-lane sum would have refused."""
    led = Ledger(str(tmp_path / "w.db"))
    led.baseline(4000, confirmed_by="test")     # F wall 25% = 1000c
    gw = Gateway(led, surface=None)
    _hold(gw, "FLIP", TICKER, 8, 58)            # FLIP holds 8 @58 = 464c in-event
    # F wants 9 lots @97 in the same event: F's OWN exposure is 0, 9×97 = 873c <
    # 1000c → admitted. The old cross-lane sum (464c + 873c) would have refused.
    f = _entry("F", EVENT, "KXBTC15M-02JAN251000-T98", 97, 9)
    gw._wall_net_risk_and_at_risk(f)            # does NOT raise — F not squeezed
    # the exposure query proves the scoping
    f_only, _ = gw._event_exposure(EVENT, lane="F")
    all_c, _ = gw._event_exposure(EVENT)
    assert f_only == 0 and all_c == 8           # F alone sees nothing; total sees FLIP


# ── PART 1b: a held leg prices at its BASIS, not the 99¢ constant ────────────
def test_held_leg_prices_at_basis(tmp_path):
    """A FLIP 6-lot at 58¢ is 348¢ at risk (basis × lots), not 594¢ (the 99¢
    constant) — a +70% overstatement that consumed headroom that didn't exist."""
    led = Ledger(str(tmp_path / "b.db"))
    led.baseline(10_000, confirmed_by="test")
    gw = Gateway(led, surface=None)
    _hold(gw, "FLIP", TICKER, 6, 58)
    contracts, cents = gw._event_exposure(EVENT, lane="FLIP")
    assert contracts == 6 and cents == 6 * 58        # basis, not 6 * 99


def _register(gw, oid, order):
    gw.order_index[oid] = order
    gw.resting[oid] = order


def test_basis_clears_when_a_position_goes_flat(tmp_path):
    """on_fill tracks the weighted basis and forgets it when the key is flat."""
    led = Ledger(str(tmp_path / "bc.db"))
    led.baseline(10_000, confirmed_by="test")
    gw = Gateway(led, surface=None)
    en = _entry("FLIP", EVENT, TICKER, 58, 4)
    _register(gw, "E1", en)
    gw.on_fill("E1")
    assert gw.pos_basis[(EVENT, TICKER, "FLIP")] == 58
    ex = Order(lane="FLIP", event=EVENT, market=TICKER, side="yes", action="sell",
               price_cents=62, count=4, size_tier=config.TIER_PROBE, purpose="EXIT")
    _register(gw, "X1", ex)
    gw.on_fill("X1")                             # closes the position → flat
    assert (EVENT, TICKER, "FLIP") not in gw.pos_basis


def test_basis_is_weighted_across_re_entries(tmp_path):
    """A re-entry at a new price blends: 2@58 then 2@62 → basis 60."""
    led = Ledger(str(tmp_path / "wb.db"))
    led.baseline(10_000, confirmed_by="test")
    gw = Gateway(led, surface=None)
    _register(gw, "A", _entry("FLIP", EVENT, TICKER, 58, 2))
    gw.on_fill("A")
    _register(gw, "B", _entry("FLIP", EVENT, TICKER, 62, 2))
    gw.on_fill("B")
    assert gw.pos_basis[(EVENT, TICKER, "FLIP")] == 60   # (58*2 + 62*2) / 4


# ── PART 1 (ADVERSARY i): the cross-lane event total still pages over 40% ────
def test_event_total_over_forty_percent_pages(tmp_path):
    led = Ledger(str(tmp_path / "e.db"))
    led.baseline(4000, confirmed_by="test")   # 40% of book = 1600c
    paged = []
    gw = Gateway(led, surface=None)
    gw.alert_fn = paged.append
    failures.configure(led, alert_fn=lambda m: None, run_mode="TEST", boot_id=1)
    _hold(gw, "F", TICKER, 10, 97)            # F holds 10 @97 = 970c (< F wall 1000c)
    # a FLIP order UNDER its own 18% wall (638c < 720c) that pushes the EVENT
    # total over 40% (970 + 638 = 1608 > 1600) — pages, does not reject.
    fl = _entry("FLIP", EVENT, "KXBTC15M-02JAN251000-T98", 58, 11)
    gw._wall_net_risk_and_at_risk(fl)          # NOT rejected — the page is the backstop
    assert any("EVENT_TOTAL_AT_RISK" in m for m in paged)
    failures._ledger = None


# ── PART 2: FLIP scales with the book, no fixed cap; the halt scales too ─────
def test_flip_scales_with_book_no_fixed_cap():
    small = scoring.size_order(2000, 58, 10_000, lane="FLIP").contracts
    big = scoring.size_order(9000, 58, 10_000, lane="FLIP").contracts
    assert small == int(2000 * config.FLIP_NOTIONAL_PCT // 58)
    assert big == int(9000 * config.FLIP_NOTIONAL_PCT // 58)
    assert big > small                     # it SCALES, not a frozen 10-cap
    assert "no cap" in scoring.size_order(9000, 58, 10_000, lane="FLIP").reason


def test_rate_halt_drawdown_scales_with_the_book():
    """4 stop-outs at CURRENT FLIP size — a threshold frozen at yesterday's size
    is the count-vs-money bug reborn."""
    assert config.rate_halt_drawdown_c(0) == 4 * config.FLIP_SIZE_CAP * config.OPEN_MOMENTUM_STOP_C
    small = config.rate_halt_drawdown_c(4000)
    big = config.rate_halt_drawdown_c(9000)
    assert big > small                     # grows with the book it guards
    assert big == 4 * int(9000 * config.FLIP_NOTIONAL_PCT
                          / config.FLIP_HALT_REF_PRICE_C) * config.OPEN_MOMENTUM_STOP_C


# ── PART 3: the sighted stop is LIVE, every cut names its trigger ────────────
@pytest.fixture
def flip(gateway, ledger, surface):
    failures.configure(ledger, alert_fn=lambda m: None, run_mode="TEST", boot_id=1)
    return LaneFlip(gateway, custodian=Custodian(gateway, ledger, surface,
                                                 ladder=DegradeLadder()))


def _open(flip, entry=60):
    w = flip._window(TICKER, CLOSE)
    w.opens.clear()
    now = CLOSE - 700
    w.opens["yes"] = {"entry": entry, "fill_ts": now - 200, "count": 1,
                      "take_oid": None, "take_proposed": True, "collapse_polls": 0,
                      "catastrophe_polls": 0, "det_ts": None, "entry_oid": None,
                      "defer_polls": 0}
    return w.opens["yes"], now


def _poll(flip, mark, now):
    b = OrderBook(market=TICKER)
    if mark is not None:
        b.apply_snapshot({mark: 10}, {100 - mark: 10}, ts=1.0)
    else:
        b.apply_snapshot({}, {40: 10}, ts=1.0)
    ctx = {"book": b, "now": CLOSE - 700, "close_ts": CLOSE, "spot": None,
           "grain": None, "spotlead": None}
    return flip._open_custody(flip._window(TICKER, CLOSE), TICKER, EVENT, b, ctx,
                              700, now)


def test_sighted_stop_live_defers_a_recovery(flip):
    """Acceptance #3: a book off its low and climbing DEFERS — no cut (the
    reversion thesis working). OPEN_SIGHTED_STOP is on by default."""
    o, now = _open(flip, entry=60)
    _poll(flip, 20, now)                   # trough
    props = _poll(flip, 48, now)           # climbing → DEFER
    assert not any(p.purpose in ("CUT", "EXIT") for p in props)
    assert not o.get("done") and o["shadow_would_defer"] is True


def test_falling_cut_names_the_2poll_trigger(flip):
    """A book falling through the stop (not recovering) cuts — trigger '2-poll'
    named in the reason string (acceptance #3)."""
    o, now = _open(flip, entry=60)         # stop 50, floor 47
    _poll(flip, 49, now)                   # poll 1 (within slip, falling)
    props = _poll(flip, 48, now)           # poll 2 → cut at the mark
    cut = next(p for p in props if p.purpose in ("CUT", "EXIT"))
    assert "[2-poll]" in cut.reason and o.get("stop_trigger") == "2-poll"


def test_g1_hard_floor_cuts_and_names_its_trigger(flip):
    """G1: a deep 12→18 bounce is a dead position twitching — cut regardless of
    the recovery, trigger G1_HARD named."""
    o, now = _open(flip, entry=60)         # hard floor 50−3−8 = 39
    _poll(flip, 12, now)                   # poll 1 (deep)
    props = _poll(flip, 18, now)           # poll 2: off the low +6 but < 39 → cut
    assert not o.get("shadow_would_defer")
    cut = next(p for p in props if p.purpose in ("CUT", "EXIT"))
    assert "[G1_HARD]" in cut.reason and o.get("stop_trigger") == "G1_HARD"


def test_sighted_stop_revert_flag_restores_pure_level(flip, monkeypatch):
    """The revert lever: OPEN_SIGHTED_STOP=0 → the pure-level stop (cuts a
    recovery just like before). One env flip, Telegram-visible."""
    monkeypatch.setattr(config, "OPEN_SIGHTED_STOP", False)
    o, now = _open(flip, entry=60)
    _poll(flip, 20, now)
    props = _poll(flip, 48, now)           # climbing — but the sighted stop is OFF
    assert any(p.purpose in ("CUT", "EXIT") for p in props)   # cuts, level-only


# ── PART 4: instrument truth — boot per-lane sizes + per-contract cell stats ─
def test_boot_line_states_real_per_lane_sizes():
    from relay_engine.boot import sizing_line
    line = sizing_line(9000)
    assert "F @97¢ →" in line and "FLIP @58¢ →" in line
    assert "rate-halt bound" in line and "no fixed cap" in line


def test_cell_stats_are_per_contract(tmp_path):
    """Acceptance #5: the scoreboard's avg_win/avg_loss are PER-CONTRACT, so an
    18-lot era does not blend with the 1-lot era and fake Gate A. Two winning
    outcomes, same per-contract edge, different lot counts → the avg is the
    per-contract edge, not a lot-weighted blend."""
    led = Ledger(str(tmp_path / "c.db"))
    # +5c/contract on 1 lot, then +5c/contract on 8 lots (pnl 40) — per-contract
    # avg is 5, a lot-weighted-by-row avg would be (5+40)/2 = 22.5
    led.record_cell_outcome("F", 95, won=True, pnl_cents=5, fees_cents=0,
                            market="M1", kind="settle", contracts=1)
    led.record_cell_outcome("F", 95, won=True, pnl_cents=40, fees_cents=0,
                            market="M2", kind="settle", contracts=8)
    agg = {(r["lane"], r["cell"]): r for r in scoring.lifetime_cell_aggregates(led)}
    row = next(r for k, r in agg.items() if k[0] == "F")
    assert row["avg_win_c"] == 5.0         # per-contract, era-invariant


# ── PART 6: the dial < wall invariant, asserted (fail loud on inversion) ─────
def test_dials_sit_under_their_walls():
    assert config.dial_wall_violations() == []
    assert config.F_NOTIONAL_PCT < config.AT_RISK_PCT["F"]
    assert config.FLIP_NOTIONAL_PCT < config.AT_RISK_PCT["FLIP"]


def test_a_dial_over_its_wall_is_caught(monkeypatch):
    monkeypatch.setitem(config.DIAL_OF_LANE, "F", 0.30)   # dial 30% > wall 25%
    bad = config.dial_wall_violations()
    assert ("F", 0.30, config.AT_RISK_PCT["F"]) in bad


# ── HARD RAIL: F byte-identical (its notional path untouched) ────────────────
def test_f_sizing_byte_identical():
    f = scoring.size_order(4162, 97, 10_000, lane="F")
    assert f.contracts == int(4162 * config.F_NOTIONAL_PCT // 97)
    assert "cap n/a" in f.reason and "notional" in f.reason
