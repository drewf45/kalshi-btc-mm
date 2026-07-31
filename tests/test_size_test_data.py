"""WO-2026-07-24-C Part 4 — COLLECT THE DATA. The +4×10 size test is only
worth running if it emits the data that answers whether it worked. Two
instruments, on EVERY FLIP trade:

  #7  the BINDING SIZING TERM — a FLIP_SIZE log on every entry naming which of
      {kelly, depth, cap} bound the lot count, so "did the 10-cap ever bite, or
      was depth the ceiling the whole time" is read from the tape, never argued.
  #6  target_touched / target_filled — did the book reach entry+OPEN_GOUGE_C
      (our posted take), and did our RESTING take fill there. touched-not-filled
      is the central question: the middle came to us and we missed the fill.

HARD RAIL: F byte-identical — the FLIP_SIZE log is gated to lane=="FLIP"; the
F_SIZE block is untouched. Part 3 dials (OPEN_GOUGE_C=4, FLIP_SIZE_CAP=10,
wall 15%) assert alongside."""

import json
import logging

import pytest

from relay_engine import config
from relay_engine.book import OrderBook
from relay_engine.custodian import Custodian
from relay_engine.feed import DegradeLadder
from relay_engine.gateway import Order
from relay_engine.lane_flip import LaneFlip
from relay_engine.shadow_runner import ShadowEngine

TICKER = "KXBTC15M-02JAN251000-T99"
EVENT = TICKER.rsplit("-", 1)[0]
CLOSE = 1_000_000.0
GRAIN = {"direction": "yes", "length": 2, "k": 4}


def _book(yes=40, no=49, yq=10, nq=10):
    b = OrderBook(market=TICKER)
    b.apply_snapshot({yes: yq} if yes is not None else {},
                     {no: nq} if no is not None else {}, ts=1.0)
    return b


def _ctx(book, secs_left, spot=None, grain=None):
    return {"book": book, "now": CLOSE - secs_left, "close_ts": CLOSE,
            "spot": spot, "grain": grain, "spotlead": None}


@pytest.fixture
def flip(gateway, ledger, surface):
    return LaneFlip(gateway, custodian=Custodian(gateway, ledger, surface,
                                                 ladder=DegradeLadder()))


def _engine(ledger):
    eng = object.__new__(ShadowEngine)
    eng.ledger = ledger
    eng.telegram = type("T", (), {"alert": staticmethod(lambda m: None)})()
    eng._size_zero_logged = set()
    return eng


def _swing_row(ledger):
    (d,) = ledger.db.execute(
        "SELECT detail FROM surface_rows WHERE state='FLIP_SWING'"
        " ORDER BY id DESC LIMIT 1").fetchone()
    return json.loads(d)


# ── Part 3: the dials are set (acceptance #5, #9) ──────────────────────────
def test_part3_dials_are_set():
    assert config.OPEN_GOUGE_C == 4          # target = entry + 4
    # WO-2026-07-24-G: the fixed cap is retired as a binder; the FLIP wall rose
    # to 18% (dial 14% sits under it), and the halt is book-derived.
    assert config.AT_RISK_PCT["FLIP"] == 0.18
    assert config.FLIP_NOTIONAL_PCT < config.AT_RISK_PCT["FLIP"]   # dial < wall
    # acceptance #9: the revert levers live in the config comment
    import inspect
    src = inspect.getsource(config)
    assert "REVERT" in src


# ── #7: the binding sizing term is logged on EVERY FLIP entry ──────────────
def test_flip_size_logs_the_binding_term(ledger, caplog):
    """The FLIP_SIZE log names the term that bound the lot count. WO-2026-07-24-G:
    the fixed cap is RETIRED — on a deep book NOTIONAL is the ceiling (FLIP scales
    with the book), and the reason says '→ notional bound (no cap …)'."""
    ledger.baseline(8_000, confirmed_by="test")   # $80 book
    eng = _engine(ledger)
    p = Order(lane="FLIP", event=EVENT, market=TICKER, side="yes",
              action="buy", price_cents=48, count=1, size_tier=config.TIER_PROBE,
              purpose="ENTRY", why="OPEN grain yesx2 · join 48c")
    with caplog.at_level(logging.INFO, logger="relay.shadow"):
        eng._score_and_size(p, _book(yes=48, no=49, yq=1000, nq=1000))
    line = next((r.getMessage() for r in caplog.records
                 if r.getMessage().startswith("FLIP_SIZE")), None)
    assert line is not None
    # WO-2026-07-25-K: FLIP defaults to TUITION (0.04) until the desk earns full
    # by conversion; here 8000*0.04//48 = 6 notional binds below ample depth.
    assert "→ notional bound" in line and "no cap" in line
    assert f"count={int(8000 * config.FLIP_NOTIONAL_PCT // 48)}" in line


def test_flip_size_log_names_depth_when_depth_binds(ledger, caplog):
    """A thin book: depth is the smaller term — the same log names it, so the
    experiment reads that the 10-cap never bit here."""
    ledger.baseline(80_000, confirmed_by="test")
    eng = _engine(ledger)
    p = Order(lane="FLIP", event=EVENT, market=TICKER, side="yes",
              action="buy", price_cents=48, count=1, size_tier=config.TIER_PROBE,
              purpose="ENTRY", why="OPEN grain yesx2 · join 48c")
    with caplog.at_level(logging.INFO, logger="relay.shadow"):
        eng._score_and_size(p, _book(yes=48, no=49, yq=12, nq=12))  # depth 12·.25=3
    line = next((r.getMessage() for r in caplog.records
                 if r.getMessage().startswith("FLIP_SIZE")), None)
    assert line is not None and "→ depth bound" in line


def test_flip_size_log_is_flip_only_f_untouched(ledger, caplog):
    """HARD RAIL: an F entry writes F_SIZE, never FLIP_SIZE — the new block is
    gated to lane=='FLIP' and does not touch the F path."""
    ledger.baseline(4162, confirmed_by="test")
    eng = _engine(ledger)
    f = Order(lane="F", event=EVENT, market=TICKER, side="yes", action="buy",
              price_cents=97, count=1, size_tier=config.TIER_PROBE,
              purpose="ENTRY", why="F tier97 · surv~price")
    with caplog.at_level(logging.INFO, logger="relay.shadow"):
        eng._score_and_size(f, _book(yes=97, no=1, yq=1000, nq=1000))
    msgs = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("F_SIZE") for m in msgs)
    assert not any(m.startswith("FLIP_SIZE") for m in msgs)


# ── #6: target_touched / target_filled on EVERY FLIP trade ─────────────────
def _run_take_fill(flip, ledger, entry=60):
    """A full trade that concludes on the resting take (TAKE_FILL) at entry+4."""
    flip.evaluate(TICKER, _ctx(_book(yes=54, no=48), secs_left=835,
                               spot=66_380, grain=GRAIN))
    props = flip.evaluate(TICKER, _ctx(_book(yes=entry, no=40), secs_left=820,
                                       spot=66_400, grain=GRAIN))
    flip.on_submitted(props[0], "E1", CLOSE - 820)
    ledger.record_fill(TICKER, "FLIP", "yes", "ENTRY", entry, 1, "PROBE")
    flip.note_fill(TICKER, "yes", entry, CLOSE - 818)
    take = flip._take_price(entry)            # entry + 4
    # a custody poll where the book has RUN UP to the take → high-water touched
    flip.evaluate(TICKER, _ctx(_book(yes=take, no=100 - take), secs_left=790,
                               spot=66_460))
    ledger.record_fill(TICKER, "FLIP", "yes", "EXIT", take, 1, "PROBE")
    flip.note_exit(TICKER, "yes", take, CLOSE - 700, count=1)


def test_take_fill_trade_touched_and_filled(flip, ledger):
    """The resting take concludes the trade: target_touched AND target_filled,
    take_target = entry+OPEN_GOUGE_C."""
    _run_take_fill(flip, ledger, entry=60)
    r = _swing_row(ledger)
    assert r["take_target"] == 64            # _take_price(60) = 60+4
    assert r["target_touched"] is True
    assert r["target_filled"] is True


def test_momentum_stop_trade_not_filled(flip, ledger):
    """A stop-out: the book never reached the take and the resting take did not
    conclude the trade — target_filled False (touched False here too, the book
    fell). The touched-not-filled column stays honest."""
    w = flip._window(TICKER, CLOSE)
    w.opens["yes"] = {
        "entry": 60, "count": 1, "take_oid": None, "take_proposed": True,
        "take_px": 64, "collapse_polls": 0, "catastrophe_polls": 0,
        "det_ts": None, "entry_oid": None, "defer_polls": 0, "hold": False,
        "fill_ts": CLOSE - 850, "entry_meta": {"spot": 1, "secs_into": 50}}
    for _ in range(2):    # mark 50 <= stop_px (60−10) two polls → the stop fires
        flip._open_custody(w, TICKER, EVENT, _book(yes=50, no=45),
                           _ctx(_book(yes=50, no=45), 500), 500, CLOSE - 500)
    ledger.record_fill(TICKER, "FLIP", "yes", "EXIT", 50, 1, "PROBE")
    flip.note_exit(TICKER, "yes", 50, CLOSE - 500, count=1)
    r = _swing_row(ledger)
    assert r["exit_reason"] == "MOMENTUM_STOP"
    assert r["target_filled"] is False
    assert r["target_touched"] is False


def test_touched_but_not_filled_is_recorded(flip, ledger):
    """The +4×10 test's central question: the book REACHED the take (high-water
    set) but a later path — not the resting take — concluded the trade. touched
    True, filled False: the middle came to us and we did not get the fill."""
    w = flip._window(TICKER, CLOSE)
    w.opens["yes"] = {
        "entry": 60, "count": 1, "take_oid": None, "take_proposed": True,
        "take_px": 64, "collapse_polls": 0, "catastrophe_polls": 0,
        "det_ts": None, "entry_oid": None, "defer_polls": 0, "hold": False,
        "fill_ts": CLOSE - 850, "entry_meta": {"spot": 1, "secs_into": 50}}
    # one poll where the book runs THROUGH the take (mark 64) → high-water touched
    flip._open_custody(w, TICKER, EVENT, _book(yes=64, no=36),
                       _ctx(_book(yes=64, no=36), 700), 700, CLOSE - 700)
    assert w.opens["yes"].get("target_touched") is True
    # then the book collapses and the momentum stop concludes it, NOT the take
    for _ in range(2):
        flip._open_custody(w, TICKER, EVENT, _book(yes=50, no=45),
                           _ctx(_book(yes=50, no=45), 500), 500, CLOSE - 500)
    ledger.record_fill(TICKER, "FLIP", "yes", "EXIT", 50, 1, "PROBE")
    flip.note_exit(TICKER, "yes", 50, CLOSE - 500, count=1)
    r = _swing_row(ledger)
    assert r["target_touched"] is True and r["target_filled"] is False
    assert r["exit_reason"] == "MOMENTUM_STOP"
