"""WO-2026-07-22-E "FLIP THE SIDE" (build 57) — re-anchored from the cheap-side
swing thesis to the FAVORED-side liquidity thesis.

The law under test: (§2.1) FLIP buys the FAVORED (higher-priced) side in
[OPEN_ENTRY_MIN_C, OPEN_ENTRY_MAX_C] = [50,70] — the cheap-side band and the
two-sided SWING GATE are RETIRED from the entry path; swing is LOG-ONLY now and
GATES ON NOTHING, so the favored entry fires regardless of the measured rate.
(§3) every entry still concludes with a FLIP_SWING row (the instrument stands),
and every loser audits the MOMENTUM-STOP budget (FLIP_LOSER_CUT ok,
FLIP_FLOOR_BREACH on a loss past OPEN_MOMENTUM_STOP_C+slip — the assumption
whose failure inverts the EV, flagged on the FIRST loser). RAIL: no
Kelly/cash-integrity/rate-halt change; one lot."""

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


def _book(yes=60, no=40):
    # WO-2026-07-22-E: the default is a FAVORED-yes book (yes_bid 60 > no_bid 40),
    # so the favored side is yes@60, inside [50,70].
    b = OrderBook(market=TICKER)
    b.apply_snapshot({yes: 10}, {no: 10}, ts=1.0)
    return b


def _ctx(book, secs_left=850, grain=None, spot=None):
    return {"book": book, "now": CLOSE - secs_left, "close_ts": CLOSE,
            "spot": spot, "grain": grain, "spotlead": None,
            "boundary_lo": None, "boundary_hi": STRIKE}


def _prime_favored(flip, side="yes", join=60, grain=GRAIN_YES2):
    """WO-2026-07-22-F 'wait for the pile' (build 58) two-poll prime: a baseline
    in-window poll (small skew) then the entry poll (skew grown >=5, agreeing
    trend >=$15, favored depth). Returns the entry poll's proposals — props[0]
    is the favored side@join ENTRY. yes favored → spot RISES; no favored →
    spot FALLS (trend agrees with the favored side)."""
    other = 100 - join
    if side == "yes":
        flip.evaluate(TICKER, _ctx(_book(yes=54, no=48), secs_left=835,
                                   spot=66000.0, grain=grain))
        return flip.evaluate(TICKER, _ctx(_book(yes=join, no=other),
                                          secs_left=820, spot=66020.0,
                                          grain=grain))
    flip.evaluate(TICKER, _ctx(_book(yes=48, no=54), secs_left=835,
                               spot=66000.0, grain=grain))
    return flip.evaluate(TICKER, _ctx(_book(yes=other, no=join),
                                      secs_left=820, spot=65980.0, grain=grain))


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


# ── §2.1: the band admits the FAVORED side ─────────────────────────────────
def test_band_admits_favored_side(flip):
    """WO-2026-07-22-F: FLIP buys the FAVORED (higher-priced) side in
    [OPEN_ENTRY_MIN_C, OPEN_ENTRY_MAX_C] = [50,70], once the PILE has formed. A
    favored no@60 (spot FALLING with the no side) posts as an ENTRY through the
    two-poll pile prime; the why now carries the all-of pile verdict."""
    assert (config.OPEN_ENTRY_MIN_C, config.OPEN_ENTRY_MAX_C) == (50, 70)
    props = _prime_favored(flip, side="no", join=60)
    assert [(p.side, p.price_cents, p.purpose) for p in props] == \
        [("no", 60, "ENTRY")]
    assert "OPEN50 favored no@60c" in props[0].why
    assert "pile: all-of met" in props[0].why


def test_favored_out_of_band_skips(flip):
    """The band is a real gate: even inside the pile window a favored side above
    70 (over-priced, the move already fully paid for) SKIPS on price_band — no
    entry, no post."""
    flip.evaluate(TICKER, _ctx(_book(yes=72, no=20), secs_left=835,
                               spot=66000.0, grain=GRAIN_YES2))    # baseline
    assert flip.evaluate(TICKER, _ctx(_book(yes=80, no=15), secs_left=820,
                                      spot=66020.0, grain=GRAIN_YES2)) == []


# ── WO-2026-07-22-E: the SWING GATE is RETIRED as an entry gate. Swing is
# LOG-ONLY now — it gates on NOTHING. The favored entry fires regardless of
# the measured took_swing rate; these tests seed the full range (high / low /
# thin) and prove rate-independence. ────────────────────────────────────────
def _seed_swing_rows(ledger, surface, band_entry, took, total):
    """Instrument 1 history in the entry's price band (no longer gates)."""
    for i in range(total):
        surface.write_row(
            "FLIP", f"M{i}", f"w{i}", "FLIP_SWING",
            detail=json.dumps({"entry_price": band_entry,
                               "took_swing": i < took, "gross_cents": 20}))


def test_favored_entry_fires_regardless_of_high_swing_rate(flip, gateway,
                                                           ledger, surface):
    """A seeded HIGH measured took_swing rate does not gate the entry (the gate
    is retired): the favored yes@60 still fires, one lot."""
    _seed_swing_rows(ledger, surface, 60, took=16, total=20)   # 0.80
    props = _prime_favored(flip)
    assert [(p.side, p.price_cents, p.purpose) for p in props] == \
        [("yes", 60, "ENTRY")]


def test_favored_entry_fires_even_on_low_swing_rate(flip, gateway, ledger,
                                                    surface, caplog):
    """The discrimination the old gate did on a LOW measured rate is GONE —
    swing no longer REFUSES an entry. A seeded low took_swing rate (the old
    falling-knife refusal) still admits the favored side, and there is NO
    OPEN_SWING_REFUSED."""
    import logging
    _seed_swing_rows(ledger, surface, 60, took=8, total=20)    # 0.40
    with caplog.at_level(logging.INFO, logger="relay.lane_flip"):
        props = _prime_favored(flip)
    assert [(p.side, p.price_cents, p.purpose) for p in props] == \
        [("yes", 60, "ENTRY")]
    assert not any("OPEN_SWING_REFUSED" in r.message for r in caplog.records)


def test_favored_entry_fires_below_sample_floor(flip, gateway, ledger,
                                                surface):
    """A thin swing history no longer factors at all — the gate is retired, not
    merely permissive-below-floor. The favored side fires on a thin all-loss
    sample."""
    _seed_swing_rows(ledger, surface, 60, took=0, total=5)     # thin, all-loss
    props = _prime_favored(flip)
    assert [(p.side, p.price_cents, p.purpose) for p in props] == \
        [("yes", 60, "ENTRY")]


# ── §3 INSTRUMENT 1: the swing-outcome log ─────────────────────────────────
def _favored_entry(flip, gateway, ledger, entry=60):
    """A booked favored-yes OPEN leg (yes@entry, favored over no) — entered
    through the two-poll pile prime (build 58)."""
    props = _prime_favored(flip, join=entry)
    flip.on_submitted(props[0], "OID-E1", CLOSE - 800)
    ledger.record_fill(TICKER, "FLIP", "yes", "ENTRY", entry, 1, "PROBE")
    flip.note_fill(TICKER, "yes", entry, CLOSE - 790)
    return flip.windows[TICKER].opens["yes"]


def test_swing_win_logs_flip_swing_row(flip, gateway, ledger):
    """§4: favored entry 60 swings to 80 (a +20 gross over the entry) —
    FLIP_SWING took_swing=true, +20c."""
    _favored_entry(flip, gateway, ledger)
    ledger.record_fill(TICKER, "FLIP", "yes", "EXIT", 80, 1, "PROBE")
    flip.note_exit(TICKER, "yes", 80, CLOSE - 500, count=1)
    rows = _rows(ledger, "FLIP_SWING")
    assert len(rows) == 1
    r = rows[0]
    assert r["took_swing"] is True and r["gross_cents"] == 20
    assert r["entry_price"] == 60 and r["exit_price"] == 80
    assert r["salvaged"] is False
    assert r["secs_to_swing"] == pytest.approx(290, abs=1)
    assert _rows(ledger, "FLIP_LOSER_CUT") == []     # winners don't audit


def test_curfew_winner_conversion_logs_swing_arrived(flip, gateway, ledger):
    """WO-2026-07-22-E: the F-agrees patience hold-conversion is RETIRED. The
    only winner-to-F handoff left is the CURFEW (unchanged): at
    secs <= FLIP_DECISION_S a winner (mark >= basis) converts to hold-to-settle
    — a swing that arrived, instrumented at the conversion mark,
    held_to_settle=true."""
    o = _favored_entry(flip, gateway, ledger)
    p1 = flip.evaluate(TICKER, _ctx(_book(yes=60, no=40), secs_left=780))
    flip.on_submitted(next(p for p in p1 if p.purpose == "EXIT"),
                      "OID-T1", CLOSE - 780)
    flip.evaluate(TICKER, _ctx(_book(yes=65, no=35),
                               secs_left=config.FLIP_DECISION_S - 1))  # decision winner
    assert o["hold"] is True
    rows = _rows(ledger, "FLIP_SWING")
    assert len(rows) == 1
    assert rows[0]["held_to_settle"] is True and rows[0]["exit_price"] == 65


# ── §3 INSTRUMENT 2: the salvage-floor audit ───────────────────────────────
def test_loser_cleared_at_endgame_logs_ok_true(flip, gateway, ledger):
    """CURFEW (unchanged): an unreverted loser is cleared at the mark by the
    FLIP_DECISION_S endgame handoff (purpose CUT), never ridden to the bell —
    FLIP_LOSER_CUT ok=true. A 60c entry cleared at 55c is a bounded −5, well
    inside the momentum-stop budget the audit now polices."""
    o = _favored_entry(flip, gateway, ledger)
    p1 = flip.evaluate(TICKER, _ctx(_book(yes=60, no=40), secs_left=780))
    flip.on_submitted(next(p for p in p1 if p.purpose == "EXIT"),
                      "OID-T1", CLOSE - 780)
    # at the endgame decision the unreverted loser is cleared at the mark
    cuts = [p for p in flip.evaluate(TICKER, _ctx(_book(yes=55, no=45),
                                                  secs_left=config.FLIP_DECISION_S - 1))
            if p.purpose == "CUT"]
    assert len(cuts) == 1 and cuts[0].price_cents == 55   # cleared at the mark
    ledger.record_fill(TICKER, "FLIP", "yes", "EXIT", 55, 1, "PROBE")
    flip.note_exit(TICKER, "yes", 55, CLOSE - 699, count=1)
    swing = _rows(ledger, "FLIP_SWING")[0]
    assert swing["took_swing"] is False and swing["salvaged"] is True
    audit = _rows(ledger, "FLIP_LOSER_CUT")[0]
    assert audit["ok"] is True and audit["loss_cents"] == 5
    # WO-2026-07-22-E: floor_expected is now the MOMENTUM-STOP budget
    assert audit["floor_expected"] == config.OPEN_MOMENTUM_STOP_C


def test_loser_past_the_floor_flags_ok_false_and_pages(flip, gateway,
                                                       ledger, funnel):
    """WO-2026-07-22-E: FLIP_FLOOR_BREACH now polices the MOMENTUM STOP. A loser
    forced past the budget (−30c on a 60c entry, well beyond entry−10=50) →
    ok=FALSE + FLIP_FLOOR_BREACH page — the stop is fiction (kill condition #1),
    the early warning the rate-halt cannot give, on the FIRST loser."""
    _favored_entry(flip, gateway, ledger)
    ledger.record_fill(TICKER, "FLIP", "yes", "EXIT", 30, 1, "PROBE")
    flip.note_exit(TICKER, "yes", 30, CLOSE - 400, count=1)
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