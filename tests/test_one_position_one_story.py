"""WO-2026-07-24-H "ONE POSITION, ONE STORY" — the exchange pieces intents into
fills; the law says the engine must piece them back into POSITIONS before it
speaks. The 12:18 window (entry pieced no@61 ×7+×10 = 17) then EXIT ×7 @65 and
the ↔ line declared "round-trip +28¢" as a concluded story while 10 contracts
still rode. The unit of account is the position — P&L and "concluded" exist only
at count→0.

P1 the ↔ line reads custody (PARTIAL while riding, CLOSED when flat); P2 the cell
outcome books once at conclusion (blended basis, total count); P3 the take
re-sizes on a partial exit + a standing EXIT_OVERSIZE assert.

HARD RAIL: F byte-identical; no entry-gate/target/One-Shot change."""

import pytest

from relay_engine import config, failures
from relay_engine.book import OrderBook
from relay_engine.custodian import Custodian
from relay_engine.feed import DegradeLadder
from relay_engine.gateway import Gateway, Order
from relay_engine.ledger import Ledger
from relay_engine.lane_flip import LaneFlip
from relay_engine.shadow_runner import ShadowEngine

EVENT = "KXBTC15M-02JAN251230"
TICKER = EVENT + "-T30"
CLOSE = 1_000_000.0


@pytest.fixture
def engine(tmp_path):
    e = ShadowEngine(db_path=str(tmp_path / "h.db"))
    e.boot()
    e.telegram_sent = []
    e.telegram.send = e.telegram_sent.append
    failures.configure(e.ledger, alert_fn=e.telegram.alert, run_mode="TEST",
                       boot_id=1)
    yield e
    failures._ledger = None
    failures._alert_fn = None


def _entry(side, price, count):
    return Order(lane="FLIP", event=EVENT, market=TICKER, side=side,
                 action="buy", price_cents=price, count=count,
                 size_tier=config.TIER_PROBE, purpose="ENTRY",
                 why="OPEN grain nox2 · join 61c")


def _exit(side, price, count):
    return Order(lane="FLIP", event=EVENT, market=TICKER, side=side,
                 action="sell", price_cents=price, count=count,
                 size_tier=config.TIER_PROBE, purpose="EXIT",
                 reason="OPEN take entry+4")


def _book(oid, gw, order):
    gw.order_index[oid] = order
    gw.resting[oid] = order


# ── P1: the ↔ line — PARTIAL while riding, CLOSED when flat ─────────────────
def test_pieced_entry_partial_then_closed(engine):
    """The 12:18 tape reproduced: pieced no@61 ×7 then ×10 (17 total), then a
    partial EXIT ×7 @65 — the line says PARTIAL x7, 10 riding (NOT a concluded
    round-trip); only when the last 10 exit does it print CLOSED with the blended
    math. The old line declared +28¢ concluded on the first exit."""
    gw = engine.gateway
    _book("A", gw, _entry("no", 61, 7))
    gw.on_fill("A")
    _book("B", gw, _entry("no", 61, 10))
    gw.on_fill("B")                                # blended basis 61, 17 held
    assert gw.pos_basis[(EVENT, TICKER, "FLIP")] == 61
    # the partial exit ×7 @65 — GOOD news, honestly narrated
    _book("X1", gw, _exit("no", 65, 7))
    gw.on_fill("X1")
    engine._on_fill_booked(gw.order_index["X1"], "EXIT", 65, 7, 1000.0, 0)
    line = [m for m in engine.telegram_sent if m.startswith("↔")][-1]
    assert "PARTIAL x7 @65¢ (basis 61¢) +28¢ — 10 riding" in line
    assert "ROUND-TRIP CLOSED" not in line          # NOT concluded — 10 ride
    # the final 10 conclude the position → CLOSED with blended totals
    _book("X2", gw, _exit("no", 65, 10))
    gw.on_fill("X2")
    engine._on_fill_booked(gw.order_index["X2"], "EXIT", 65, 10, 1010.0, 0)
    closed = [m for m in engine.telegram_sent if "CLOSED" in m][-1]
    assert "ROUND-TRIP CLOSED x17 basis 61¢ → avg exit 65¢, net +68¢" in closed


# ── P2: the cell outcome books ONCE, at conclusion, with position totals ────
def test_cell_outcome_books_at_conclusion_with_totals(engine):
    """One cell row per concluded position — blended basis, TOTAL count — never
    per-exit-fill against one entry's price (the fiction that contaminated the
    Gate A stats on pieced entries)."""
    gw = engine.gateway
    _book("A", gw, _entry("no", 60, 8))
    gw.on_fill("A")
    _book("B", gw, _entry("no", 64, 8))            # a re-entry at a DIFFERENT price
    gw.on_fill("B")                                # blended basis 62, 16 held
    rows = engine.ledger.db.execute(
        "SELECT COUNT(*) FROM cell_outcomes WHERE kind='trip'").fetchone()[0]
    assert rows == 0                               # nothing books mid-position
    _book("X", gw, _exit("no", 66, 16))
    gw.on_fill("X", fee_cents=3)                   # concludes flat
    row = engine.ledger.db.execute(
        "SELECT price_cell, pnl_cents, contracts FROM cell_outcomes"
        " WHERE kind='trip'").fetchone()
    # ONE row: blended basis 62 (cell 60), total 16 contracts, net = (66−62)*16−3
    assert row == (60, (66 - 62) * 16 - 3, 16)


# ── P3: the take re-sizes on a partial exit; EXIT_OVERSIZE is fireable ──────
@pytest.fixture
def flip(gateway, ledger, surface):
    failures.configure(ledger, alert_fn=lambda m: None, run_mode="TEST", boot_id=1)
    return LaneFlip(gateway, custodian=Custodian(gateway, ledger, surface,
                                                 ladder=DegradeLadder()))


def test_partial_exit_cancels_the_oversized_take(flip, gateway):
    """P3: a resting take sized to the OLD (larger) count is cancelled the moment
    a partial exit shrinks the position — custody re-proposes at the remainder
    (the merge's cancel+re-propose, reused). Never a resting exit > the position."""
    w = flip._window(TICKER, CLOSE)
    w.opens.clear()
    w.opens["no"] = {"entry": 61, "fill_ts": CLOSE - 700, "count": 17,
                     "take_oid": "TAKE17", "take_proposed": True,
                     "take_count": 17, "collapse_polls": 0,
                     "catastrophe_polls": 0, "det_ts": None, "entry_oid": None,
                     "defer_polls": 0}
    cancelled = []
    gateway.cancel = lambda oid: cancelled.append(oid)
    # a partial ×7 exit leaves 10 held — the ×17 take is now oversized
    flip.note_exit(TICKER, "no", 65, CLOSE - 690, count=7)
    o = flip.windows[TICKER].opens["no"]
    assert o["count"] == 10
    assert cancelled == ["TAKE17"]                 # the oversized take cancelled
    assert o["take_oid"] is None and o["take_proposed"] is False


def test_exit_oversize_assert_is_fireable(tmp_path):
    """The standing assert: a resting EXIT larger than the held position pages
    EXIT_OVERSIZE (the case P3's cancel prevents — the belt if a cancel-reject
    race ever leaves one). Never fires on honest tape."""
    led = Ledger(str(tmp_path / "o.db"))
    led.baseline(10_000, confirmed_by="test")
    failures.configure(led, alert_fn=lambda m: None, run_mode="TEST", boot_id=1)
    gw = Gateway(led, surface=None)
    _book("E", gw, _entry("no", 61, 10))
    gw.on_fill("E")                                # 10 held
    # a resting take sized 17 (oversized — a later partial would have shrunk it)
    _book("T", gw, _exit("no", 65, 17))
    gw.check_exit_oversize()
    n = led.db.execute(
        "SELECT COUNT(*) FROM failures WHERE why_tag='EXIT_OVERSIZE'").fetchone()[0]
    assert n == 1
    # honest tape: a take sized to the position (10) never fires
    gw.resting.pop("T")
    _book("T2", gw, _exit("no", 65, 10))
    gw.check_exit_oversize()
    n2 = led.db.execute(
        "SELECT COUNT(*) FROM failures WHERE why_tag='EXIT_OVERSIZE'").fetchone()[0]
    assert n2 == 1                                 # unchanged — honest, no page


# ── HARD RAIL: F byte-identical ─────────────────────────────────────────────
def test_f_sizing_untouched():
    from relay_engine import scoring
    f = scoring.size_order(4162, 97, 10_000, lane="F")
    assert f.contracts == int(4162 * config.F_NOTIONAL_PCT // 97)
    assert "cap n/a" in f.reason
