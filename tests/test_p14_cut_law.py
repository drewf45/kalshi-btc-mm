"""WO-P14 "CUT ONLY WHAT YOU HOLD" — tri-state cancel (§1), re-derive before
every cut (§2), sizing narration (§3). Trigger: the 7:34 tape — the first
profitable round-trip followed by a protective FATAL when RAPID_DROP raced
the take-exit's fill. This suite makes the protection deliberate.
"""

import pytest

from relay_engine import config, failures
from relay_engine.book import OrderBook
from relay_engine.custodian import OpenPosition
from relay_engine.errors import FatalIntegrityError
from relay_engine.shadow_runner import ShadowEngine

TICKER = "KXBTC15M-02JAN251000-T99"
EVENT = TICKER.rsplit("-", 1)[0]


@pytest.fixture
def engine(tmp_path):
    e = ShadowEngine(db_path=str(tmp_path / "p14.db"))
    e.boot()
    e.telegram_sent = []
    e.telegram.send = e.telegram_sent.append
    failures.configure(e.ledger, alert_fn=e.telegram.alert, run_mode="TEST", boot_id=1)
    yield e
    failures._ledger = None
    failures._alert_fn = None


def make_book(yes=40, no=55):
    b = OrderBook(market=TICKER)
    b.apply_snapshot({yes: 100}, {no: 80}, ts=1.0)
    return b


def flip_pos(count=1, exit_id=None):
    return OpenPosition(event=EVENT, market=TICKER, lane="FLIP", side="no",
                        count=count, entry_price_cents=46, entry_p_win=0.46,
                        size_tier=config.TIER_PROBE, entry_time=0.0,
                        resting_exit_id=exit_id)


# ── §1: tri-state cancel ───────────────────────────────────────────────────
def test_cancel_tristate_shadow_resting_is_canceled(gateway):
    from relay_engine.gateway import Order
    r = gateway.submit(Order(lane="F", event=EVENT, market=TICKER, side="yes",
                             action="buy", price_cents=97, count=1,
                             size_tier=config.TIER_PROBE, purpose="ENTRY",
                             why="F tier97 · surv~price",
                             band=(95, 99)), make_book(yes=97, no=2))
    assert gateway.cancel_tristate(r.order_id) == "CANCELED"


def test_cancel_tristate_gone_order_is_terminal_not_violation(gateway):
    """The 7:34 shape: the exit FILLED before the cancel — it is GONE,
    the venue's word, never a violation."""
    assert gateway.cancel_tristate("FILLED-BEFORE-CANCEL") == "ALREADY_TERMINAL"


def test_cancel_tristate_unverifiable_is_unknown(gateway, monkeypatch):
    from relay_engine import venue
    from relay_engine.gateway import Order
    order = Order(lane="F", event=EVENT, market=TICKER, side="yes",
                  action="buy", price_cents=97, count=1,
                  size_tier=config.TIER_PROBE, purpose="ENTRY")
    gateway.resting["LIVE-X"] = order
    gateway.live_order_ids.add("LIVE-X")
    gateway.venue_client = object()
    monkeypatch.setattr(venue, "cancel_order",
                        lambda c, oid: (_ for _ in ()).throw(RuntimeError("timeout")))
    assert gateway.cancel_tristate("LIVE-X") == "UNKNOWN"
    assert "LIVE-X" in gateway.resting   # put back, un-verified, said so


# ── §2: re-derive before every cut ─────────────────────────────────────────
def test_filled_exit_race_skips_cut_no_fatal(engine, caplog):
    """THE 7:34 REPLAY: entry booked, take filled and booked (ledger flat),
    stale custodian pos still holds resting_exit_id, RAPID_DROP fires →
    ALREADY_TERMINAL → re-derive → flat → CUT SKIPPED. No FATAL, no new
    position, engine continues."""
    engine.ledger.record_fill(TICKER, "FLIP", "no", "ENTRY", 46, 1, "PROBE")
    engine.ledger.record_fill(TICKER, "FLIP", "no", "EXIT", 50, 1, "PROBE")
    pos = flip_pos(exit_id="TAKE-FILLED-ALREADY")   # not in gateway.resting
    engine.custodian.adopt(pos)
    before = len(engine.gateway.shadow_orders)

    result = engine.custodian.execute_cut(pos, 40, make_book(), "RAPID_DROP")

    assert result is None                                  # no cut submitted
    assert len(engine.gateway.shadow_orders) == before     # no NEW position
    assert f"{TICKER}:FLIP" not in engine.custodian.positions
    assert "CUT SKIPPED [RAPID_DROP]" in caplog.text
    assert engine.ledger.db.execute(
        "SELECT COUNT(*) FROM fills WHERE action='CUSTODIAN_EXIT'"
    ).fetchone()[0] == 0
    # no page (the round-trip line already told the story), and no FATAL row
    assert not any("BATON" in m for m in engine.telegram_sent)


def test_partial_fill_race_cuts_exactly_the_remainder(engine):
    """Entry x2 booked; the take HALF-filled (x1 booked) → the cut sells
    exactly the ledger's remaining 1, not the pos object's 2."""
    engine.ledger.record_fill(TICKER, "FLIP", "no", "ENTRY", 46, 2, "PROBE")
    engine.ledger.record_fill(TICKER, "FLIP", "no", "EXIT", 50, 1, "PROBE")
    pos = flip_pos(count=2, exit_id="HALF-FILLED-TAKE")
    engine.custodian.adopt(pos)
    engine.gateway.positions[(EVENT, TICKER, "FLIP")] = -1

    result = engine.custodian.execute_cut(pos, 40, make_book(), "RAPID_DROP")

    assert result is not None
    cut_order = engine.gateway.shadow_orders[-1]
    assert cut_order.purpose == "CUT" and cut_order.count == 1   # the remainder
    row = engine.ledger.db.execute(
        "SELECT count FROM fills WHERE action='CUSTODIAN_EXIT'").fetchone()
    assert row == (1,)
    assert engine.custodian.ledger_remaining(pos) == 0           # now flat


def test_genuinely_stuck_resting_still_fatal(engine, monkeypatch):
    from relay_engine import venue
    from relay_engine.gateway import Order
    engine.ledger.record_fill(TICKER, "FLIP", "no", "ENTRY", 46, 1, "PROBE")
    stuck = Order(lane="FLIP", event=EVENT, market=TICKER, side="no",
                  action="sell", price_cents=50, count=1,
                  size_tier=config.TIER_PROBE, purpose="EXIT")
    engine.gateway.resting["STUCK-1"] = stuck
    engine.gateway.live_order_ids.add("STUCK-1")
    engine.gateway.venue_client = object()
    monkeypatch.setattr(venue, "cancel_order",
                        lambda c, oid: (_ for _ in ()).throw(RuntimeError("HTTP 500")))
    pos = flip_pos(exit_id="STUCK-1")
    engine.custodian.adopt(pos)
    with pytest.raises(FatalIntegrityError):
        engine.custodian.execute_cut(pos, 40, make_book(), "RAPID_DROP")
    assert engine.ledger.db.execute(
        "SELECT COUNT(*) FROM failures WHERE why_tag='BATON_VIOLATION'"
    ).fetchone()[0] == 1


def test_adopted_pos_with_no_fill_rows_cuts_pos_count(engine):
    """No fills evidence at all (shadow-adopted): the pos object stands —
    there is no contrary ledger truth to outrank it."""
    pos = flip_pos(count=1)
    engine.custodian.adopt(pos)
    result = engine.custodian.execute_cut(pos, 40, make_book(), "REVERSAL")
    assert result is not None
    assert engine.gateway.shadow_orders[-1].count == 1


def test_resweep_hook_called_before_the_cut(engine):
    swept = []
    engine.custodian.resweep = swept.append
    engine.ledger.record_fill(TICKER, "FLIP", "no", "ENTRY", 46, 1, "PROBE")
    pos = flip_pos()
    engine.custodian.adopt(pos)
    engine.custodian.execute_cut(pos, 40, make_book(), "REVERSAL")
    assert swept == [TICKER]   # the on-demand sweep ran at the cut boundary


def test_exit_booking_ends_custody_of_flat_position(engine, monkeypatch):
    """The belt: when the take's fill BOOKS and the position nets flat, the
    custodian's stale pos object is cleared — the race cannot even arise."""
    from relay_engine import venue
    from relay_engine.gateway import Order
    monkeypatch.setattr(venue, "parse_fill", lambda r, s: (46.0, 0, 1))
    entry = Order(lane="FLIP", event=EVENT, market=TICKER, side="no",
                  action="buy", price_cents=46, count=1,
                  size_tier=config.TIER_PROBE, purpose="ENTRY", band=(1, 49),
                  why="OPEN grain nox2 · join 46c · PROBE n=0 · geometry=v2")
    r = engine.gateway.submit(entry, make_book(yes=40, no=46))
    engine.fills.sweep([{"fill_id": "e1", "order_id": r.order_id, "count": 1}])
    assert f"{TICKER}:FLIP" in engine.custodian.positions
    exit_order = Order(lane="FLIP", event=EVENT, market=TICKER, side="no",
                       action="sell", price_cents=50, count=1,
                       size_tier=config.TIER_PROBE, purpose="EXIT",
                       reason="take entry+4")
    r2 = engine.gateway.submit(exit_order, make_book(yes=40, no=46))
    monkeypatch.setattr(venue, "parse_fill", lambda r, s: (50.0, 0, 1))
    engine.fills.sweep([{"fill_id": "x1", "order_id": r2.order_id, "count": 1}])
    assert f"{TICKER}:FLIP" not in engine.custodian.positions   # custody ended


# ── §3: sizing narration — the budget is the invariant ─────────────────────
def test_sizing_line_says_both_prices():
    """WO-2026-07-24-G Part 4: the line now states the budget AND the REAL
    per-lane sizes F and FLIP actually trade (notional paths), each with its
    dial and wall — not the generic Kelly preview that lied about size."""
    from relay_engine.boot import sizing_line
    line = sizing_line(541)   # the 7:35 book: $5.41 (owed $0 → tradeable = book)
    # WO-2026-07-26-O §O2: the line now leads with book / owed / tradeable
    # WO-2026-07-28-X X4: the boot SIZING line labels its figure the internal
    # sizing-base (not an account value); the live account is the venue read.
    assert line.startswith("SIZING: sizing-base $5.41 (internal attribution")
    assert "tradeable $5.41" in line
    assert "Kelly fraction=0.0833 · budget/window 45¢ · " in line
    assert "F @97¢ →" in line and "FLIP @58¢ →" in line
    assert "throttle is book size + the at-risk wall" in line


def test_boot_tape_carries_the_two_price_readout(engine):
    from relay_engine.boot import boot_tape
    tape = "\n".join(boot_tape(boot_caps=engine.ledger.boot_caps))
    assert "budget/window" in tape and "F @97¢ →" in tape and "FLIP @58¢ →" in tape
