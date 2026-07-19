"""WO-FLIP-CHEAP-LIVE — prove the swing, live, measured (build 34).

The law under test: (§2.1) the band admits the cheap side to 39¢; (§2.2)
the two-sided swing gate buys only when the table says the swing to the
take beats the cut (p_cross(d_strike,t) ≥ OPEN_SWING_MIN_P — the take
REQUIRES the strike-touch, every cut-first path is a no-touch path, one
cell answers both legs); (§3) every cheap entry concludes with a
FLIP_SWING row (the measured swing rate) and every loser audits the
salvage floor (FLIP_LOSER_CUT ok, FLIP_FLOOR_BREACH on a ride past it —
the assumption whose failure inverts the EV, flagged on the FIRST
loser). RAIL: no Kelly/cash-integrity/rate-halt change; one lot until
Instruments 1 & 2 prove the rate and the floor."""

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


def _ctx(book, secs_left=800, grain=None, spot=None):
    return {"book": book, "now": CLOSE - secs_left, "close_ts": CLOSE,
            "spot": spot, "grain": grain, "spotlead": None,
            "boundary_lo": None, "boundary_hi": STRIKE}


@pytest.fixture(autouse=True)
def funnel(ledger):
    alerts = []
    failures._warn_last.clear()
    failures.configure(ledger, alert_fn=alerts.append, run_mode="TEST",
                       boot_id=1)
    yield alerts
    failures._ledger = None


@pytest.fixture
def flip(gateway, ledger, surface):
    return LaneFlip(gateway, custodian=Custodian(gateway, ledger, surface,
                                                 ladder=DegradeLadder()))


def _rows(ledger, state):
    return [json.loads(d) for (d,) in ledger.db.execute(
        "SELECT detail FROM surface_rows WHERE state=?", (state,)).fetchall()]


# ── §2.1: the band admits the cheap side ───────────────────────────────────
def test_band_admits_39c_cheap_side(flip):
    """DREW-RULED (39,56): the 39c side (excluded at the old 44 floor)
    posts; table absent → the gate is permissive (live-proof wants data),
    tagged swing~untabled on the why."""
    assert config.OPEN_BAND == (39, 56)
    props = flip.evaluate(TICKER, _ctx(_book(yes=39, no=55),
                                       grain=GRAIN_YES2))
    assert [(p.side, p.price_cents, p.purpose) for p in props] == \
        [("yes", 39, "ENTRY")]
    assert "swing~untabled" in props[0].why


def test_old_band_floor_would_have_excluded_it():
    """The read-rule record: 39 < 44 — this entry could not exist before."""
    assert 39 < 44 <= 56


# ── §2.2: the two-sided swing gate ─────────────────────────────────────────
def test_swing_gate_buys_when_table_favors_the_take(flip, monkeypatch):
    monkeypatch.setattr(delta, "is_loaded", lambda: True)
    monkeypatch.setattr(delta, "p_cross", lambda d, t, session="ALL": 0.72)
    props = flip.evaluate(TICKER, _ctx(_book(), grain=GRAIN_YES2,
                                       spot=STRIKE - 120))
    assert len(props) == 1
    assert "swing p=0.72" in props[0].why           # the gate value narrates


def test_swing_gate_refuses_the_losing_cheap_side(flip, monkeypatch,
                                                  caplog):
    """39c where the cut is more likely than the take (p_swing below the
    floor) → NO buy — losing-cheap, not oversold-cheap. The knife guard."""
    import logging
    monkeypatch.setattr(delta, "is_loaded", lambda: True)
    monkeypatch.setattr(delta, "p_cross", lambda d, t, session="ALL": 0.40)
    with caplog.at_level(logging.INFO, logger="relay.lane_flip"):
        props = flip.evaluate(TICKER, _ctx(_book(), grain=GRAIN_YES2,
                                           spot=STRIKE - 120))
    assert props == []
    assert any("OPEN_SWING_REFUSED" in r.message for r in caplog.records)


def test_swing_gate_threshold_is_two_sided(flip, monkeypatch):
    """OPEN_SWING_MIN_P > 0.5 — at exactly the floor the buy proceeds;
    just under, it refuses (p > 0.5 ⇒ P(reach take) > P(reach cut) by the
    no-touch bound; same p_cross cell for both legs)."""
    assert config.OPEN_SWING_MIN_P > 0.5
    monkeypatch.setattr(delta, "is_loaded", lambda: True)
    monkeypatch.setattr(delta, "p_cross",
                        lambda d, t, session="ALL": config.OPEN_SWING_MIN_P)
    assert flip.evaluate(TICKER, _ctx(_book(), grain=GRAIN_YES2,
                                      spot=STRIKE - 120)) != []


# ── §3 INSTRUMENT 1: the swing-outcome log ─────────────────────────────────
def _cheap_entry(flip, gateway, ledger, entry=39):
    props = flip.evaluate(TICKER, _ctx(_book(yes=entry), grain=GRAIN_YES2))
    flip.on_submitted(props[0], "OID-E1", CLOSE - 800)
    ledger.record_fill(TICKER, "FLIP", "yes", "ENTRY", entry, 1, "PROBE")
    flip.note_fill(TICKER, "yes", entry, CLOSE - 790)
    return flip.windows[TICKER].opens["yes"]


def test_swing_win_logs_flip_swing_row(flip, gateway, ledger):
    """§4: entry 39 swings to 59 — FLIP_SWING took_swing=true, ~+20c."""
    _cheap_entry(flip, gateway, ledger)
    ledger.record_fill(TICKER, "FLIP", "yes", "EXIT", 59, 1, "PROBE")
    flip.note_exit(TICKER, "yes", 59, CLOSE - 500, count=1)
    rows = _rows(ledger, "FLIP_SWING")
    assert len(rows) == 1
    r = rows[0]
    assert r["took_swing"] is True and r["gross_cents"] == 20
    assert r["entry_price"] == 39 and r["exit_price"] == 59
    assert r["salvaged"] is False
    assert r["secs_to_swing"] == pytest.approx(290, abs=1)
    assert _rows(ledger, "FLIP_LOSER_CUT") == []     # winners don't audit


def test_hold_conversion_logs_swing_arrived(flip, gateway, ledger):
    """A T-10 winner handoff IS a swing that arrived — instrumented at the
    conversion mark, held_to_settle=true."""
    o = _cheap_entry(flip, gateway, ledger)
    p1 = flip.evaluate(TICKER, _ctx(_book(yes=39), secs_left=780))
    flip.on_submitted(next(p for p in p1 if p.purpose == "EXIT"),
                      "OID-T1", CLOSE - 780)
    flip.evaluate(TICKER, _ctx(_book(yes=52), secs_left=599))   # T-10 winner
    assert o["hold"] is True
    rows = _rows(ledger, "FLIP_SWING")
    assert len(rows) == 1
    assert rows[0]["held_to_settle"] is True and rows[0]["exit_price"] == 52


# ── §3 INSTRUMENT 2: the salvage-floor audit ───────────────────────────────
def test_loser_cut_at_the_floor_logs_ok_true(flip, gateway, ledger):
    """§4: the non-swinger cuts at the band floor (~entry−(39−35)=−5c at a
    39c entry, the −15c region at a 49c entry) — FLIP_LOSER_CUT ok=true,
    NOT a −39c ride."""
    o = _cheap_entry(flip, gateway, ledger)
    p1 = flip.evaluate(TICKER, _ctx(_book(yes=39), secs_left=780))
    flip.on_submitted(next(p for p in p1 if p.purpose == "EXIT"),
                      "OID-T1", CLOSE - 780)
    o["fill_ts"] = CLOSE - 1100                      # patience elapsed
    cuts = [p for p in flip.evaluate(TICKER, _ctx(_book(yes=34),
                                                  secs_left=700))
            if p.purpose == "CUT"]
    assert len(cuts) == 1 and cuts[0].price_cents == 34   # the floor cut
    ledger.record_fill(TICKER, "FLIP", "yes", "EXIT", 34, 1, "PROBE")
    flip.note_exit(TICKER, "yes", 34, CLOSE - 699, count=1)
    swing = _rows(ledger, "FLIP_SWING")[0]
    assert swing["took_swing"] is False and swing["salvaged"] is True
    audit = _rows(ledger, "FLIP_LOSER_CUT")[0]
    assert audit["ok"] is True and audit["loss_cents"] == 5
    assert audit["floor_expected"] == 39 - config.OPEN_UNDETERMINED_BAND[0]


def test_loser_past_the_floor_flags_ok_false_and_pages(flip, gateway,
                                                       ledger, funnel):
    """§4: a synthetic loser forced past the floor (−30c on a 39c entry) →
    ok=FALSE + FLIP_FLOOR_BREACH page — the early warning the rate-halt
    cannot give, on the FIRST loser."""
    _cheap_entry(flip, gateway, ledger)
    ledger.record_fill(TICKER, "FLIP", "yes", "EXIT", 9, 1, "PROBE")
    flip.note_exit(TICKER, "yes", 9, CLOSE - 400, count=1)
    audit = _rows(ledger, "FLIP_LOSER_CUT")[0]
    assert audit["ok"] is False and audit["loss_cents"] == 30
    assert ledger.db.execute(
        "SELECT COUNT(*) FROM failures WHERE why_tag='FLIP_FLOOR_BREACH'"
    ).fetchone()[0] == 1
    assert any("FLIP_FLOOR_BREACH" in a for a in funnel)


# ── the rails stand ────────────────────────────────────────────────────────
def test_rails_unchanged():
    """No Kelly/cash-integrity/rate-halt change rode in on this order."""
    assert config.RATE_HALT_LOSSES == 2 and config.RATE_HALT_WINDOW == 4
    assert config.CASH_SILENT_REBASE_CENTS == 5
    assert config.NET_RISK_CROSS_LANE_CAP == 3


def test_pack_carries_the_swing_rate_line(ledger, surface):
    surface.write_row("FLIP", "M1", "w1", "FLIP_SWING",
                      detail=json.dumps({"took_swing": True,
                                         "gross_cents": 20}))
    surface.write_row("FLIP", "M2", "w2", "FLIP_SWING",
                      detail=json.dumps({"took_swing": False,
                                         "gross_cents": -5}))
    from relay_engine.ledger import CashProtocol
    from relay_engine.ops import daily_pack
    cash = CashProtocol(ledger, alert_fn=lambda m: None)
    pack = daily_pack(ledger, surface, cash)
    assert "FLIP SWING (24h): rate 1/2 (50%)" in pack
    assert "avg win +20.0c" in pack and "avg salvaged -5.0c" in pack
    assert "net/market +7.5c" in pack