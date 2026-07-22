"""WO-VERIFY-LOSSTERM-1 — verification pass over the shipped loss-term
fixes + Kelly-throttle legibility. NO trading-behavior change: no sizing
constant, gate, floor, band, or threshold moves in this file's laws (B5 —
if a constant would need to change to pass a test, the test is wrong).

B1: salvage registers the loss-term or FATALs at boot (never silence).
B2: the flip covers every contract — 2-lot lifecycle with ZERO
    FLIP_UNCOVERED_LEG pages; the alarm stays intact on a forced gap.
B3: the cash-fatal survives reboot at the ENGINE level (boot line + wall).
B4: the Kelly throttle is legible — the boot line says the binder in
    words; a favorite sizing to 0 logs SIZE_ZERO_BY_KELLY.
"""

import json
import logging

import pytest

from relay_engine import config, failures
from relay_engine.custodian import Custodian, OpenPosition, salvage_params
from relay_engine.errors import FatalIntegrityError
from relay_engine.feed import DegradeLadder

TICKER = "KXBTC15M-02JAN251000-T99"
EVENT = TICKER.rsplit("-", 1)[0]
CLOSE = 1_000_000.0


# ── B1: salvage computes the loss-term, provably, at boot ──────────────────
def test_b1_adoption_armed_with_anchor(ledger, surface, gateway):
    """A held position with an anchor registers SALVAGE_ARMED carrying the
    anchor values — a POSITIVE firing, not an absence of error."""
    c = Custodian(gateway, ledger, surface, ladder=DegradeLadder())
    c.set_lane_params("F", salvage_params())
    c.adopt(OpenPosition(event=EVENT, market=TICKER, lane="F", side="yes",
                         count=1, entry_price_cents=95, entry_p_win=0.95,
                         size_tier=config.TIER_PROBE, entry_time=CLOSE - 600,
                         d_entry=200.0, t_entry=500.0, p_entry=0.93))
    row = ledger.db.execute(
        "SELECT detail FROM surface_rows WHERE state='SALVAGE_ARMED'"
        " AND market=?", (TICKER,)).fetchone()
    d = json.loads(row[0])
    assert d["p_entry"] == 0.93 and d["d"] == 200.0 and d["t"] == 500.0


def test_b1_adoption_disabled_tagged_and_backstop_reachable(ledger, surface,
                                                            gateway):
    """Table unavailable -> SALVAGE_DISABLED_TAGGED with its reason (never
    silent), and the 5% catastrophic backstop still fires anchorless."""
    c = Custodian(gateway, ledger, surface, ladder=DegradeLadder())
    c.set_lane_params("F", salvage_params())
    pos = OpenPosition(event=EVENT, market=TICKER, lane="F", side="yes",
                       count=1, entry_price_cents=95, entry_p_win=0.95,
                       size_tier=config.TIER_PROBE, entry_time=CLOSE - 600,
                       p_entry=None)
    c.adopt(pos, disabled_reason="table")
    row = ledger.db.execute(
        "SELECT detail FROM surface_rows WHERE"
        " state='SALVAGE_DISABLED_TAGGED' AND market=?", (TICKER,)).fetchone()
    assert json.loads(row[0])["reason"] == "table"
    assert c.should_cut(pos, now=1.0, secs_remaining=400, p_win=0.04,
                        exit_bid_cents=4, spot=None, boundary_lo=None,
                        boundary_hi=None, balance_usd=100.0) == "CATASTROPHIC"


def test_b1_boot_selftest_prints_and_passes(tmp_path, capsys):
    from relay_engine.shadow_runner import ShadowEngine
    e = ShadowEngine(db_path=str(tmp_path / "b1.db"))
    e.boot()
    out = capsys.readouterr().out
    assert "SALVAGE SELF-TEST: ARMED fires" in out
    assert "loss-term wired (B1)" in out
    # the throwaway ledger's rows never touch the live surface
    assert e.ledger.db.execute(
        "SELECT COUNT(*) FROM surface_rows WHERE market LIKE 'SELFTEST%'"
    ).fetchone()[0] == 0
    failures._ledger = None


def test_b1_selftest_fatal_when_registration_unreachable(tmp_path,
                                                         monkeypatch):
    """The Adversary's guard: a verification that cannot fail is a lie.
    Sabotage the registration write -> the boot self-test FATALs loud
    rather than let a held position die silent."""
    from relay_engine.shadow_runner import ShadowEngine
    e = ShadowEngine(db_path=str(tmp_path / "b1f.db"))
    monkeypatch.setattr(Custodian, "adopt",
                        lambda self, pos, disabled_reason=None: None)
    with pytest.raises(FatalIntegrityError, match="SALVAGE_SELFTEST_FAILED"):
        e.salvage_selftest()
    failures._ledger = None


# ── B2: the flip covers every contract ─────────────────────────────────────
GRAIN_YES2 = {"direction": "yes", "length": 2, "k": 4}


def _flip_book(yes=40, no=49):
    from relay_engine.book import OrderBook
    b = OrderBook(market=TICKER)
    b.apply_snapshot({yes: 10}, {no: 10}, ts=1.0)
    return b


def _flip_ctx(book, secs_left=850, grain=None, spot=None):
    return {"book": book, "now": CLOSE - secs_left, "close_ts": CLOSE,
            "spot": spot, "grain": grain, "spotlead": None}


@pytest.fixture
def funnel(ledger):
    alerts = []
    failures._warn_last.clear()   # per-tag WARN throttle is cross-test state
    failures.configure(ledger, alert_fn=alerts.append, run_mode="TEST",
                       boot_id=1)
    yield alerts
    failures._ledger = None


def _uncovered(ledger):
    return ledger.db.execute(
        "SELECT COUNT(*) FROM failures WHERE why_tag='FLIP_UNCOVERED_LEG'"
    ).fetchone()[0]


def test_b2_two_lot_open_lifecycle_zero_uncovered_pages(ledger, surface,
                                                        gateway, funnel):
    """B2 acceptance, the POSITIVE case: a 2-lot same-side OPEN window —
    entry, fill, resting take, SECOND same-side fill, merged re-take,
    2-lot exit — runs its whole life with ZERO FLIP_UNCOVERED_LEG pages
    and exits at full size."""
    from relay_engine.lane_flip import LaneFlip
    flip = LaneFlip(gateway, custodian=Custodian(gateway, ledger, surface,
                                                 ladder=DegradeLadder()))
    # WO-2026-07-22-E: FLIP buys the FAVORED (higher-bid) side in [50,70]. A
    # favored yes@60 book enters; the held-side mark (60) stays above the
    # momentum stop (entry−10=50) all cycle, so the 2-lot lifecycle runs clean.
    fav = lambda **kw: _flip_book(yes=60, no=40)
    # WO-2026-07-22-F "wait for the pile": build the pile with two in-window
    # polls — a baseline (secs_into~65, small skew, spot low) then the entry poll
    # (secs_into~80, skew grown +14, trend +20 agreeing, favored depth).
    flip.evaluate(TICKER, _flip_ctx(_flip_book(yes=54, no=48), secs_left=835,
                                    spot=66000.0))
    props = flip.evaluate(TICKER, _flip_ctx(fav(), secs_left=820, spot=66020.0,
                                            grain=GRAIN_YES2))
    flip.on_submitted(props[0], "OID-E1", CLOSE - 800)
    ledger.record_fill(TICKER, "FLIP", "yes", "ENTRY", 60, 1, "PROBE")
    flip.note_fill(TICKER, "yes", 60, CLOSE - 790)
    p2 = flip.evaluate(TICKER, _flip_ctx(fav(), secs_left=780))
    take1 = next(p for p in p2 if p.purpose == "EXIT")
    flip.on_submitted(take1, "OID-T1", CLOSE - 780)
    flip.evaluate(TICKER, _flip_ctx(fav(), secs_left=775))  # resting, covered
    # the orphan-maker: the SECOND same-side fill
    ledger.record_fill(TICKER, "FLIP", "yes", "ENTRY", 60, 1, "PROBE")
    flip.note_fill(TICKER, "yes", 60, CLOSE - 770)
    w = flip.windows[TICKER]
    assert w.opens["yes"]["count"] == 2 and "yes" not in w.fills  # ONE record
    p3 = flip.evaluate(TICKER, _flip_ctx(fav(), secs_left=760))
    take2 = next(p for p in p3 if p.purpose == "EXIT")
    assert take2.count == 2                                # full-size exit
    flip.on_submitted(take2, "OID-T2", CLOSE - 760)
    flip.evaluate(TICKER, _flip_ctx(fav(), secs_left=755))
    # the 2-lot take fills (entry 60, exit 65 → +5/lot × 2 = +10)
    ledger.record_fill(TICKER, "FLIP", "yes", "EXIT", 65, 2, "PROBE")
    flip.note_exit(TICKER, "yes", 65, CLOSE - 750, count=2)
    assert w.window_realized == 10 and "yes" not in w.opens
    flip.evaluate(TICKER, _flip_ctx(fav(), secs_left=740))
    assert _uncovered(ledger) == 0                         # ZERO pages, ever


def test_b2_alarm_intact_on_forced_held2_covered1(ledger, surface, gateway,
                                                  funnel):
    """B2 acceptance, the alarm side: a forced held-2/covered-1 state still
    pages FLIP_UNCOVERED_LEG exactly once — verification must not have
    quietly killed the invariant."""
    from relay_engine.lane_flip import LaneFlip
    flip = LaneFlip(gateway, custodian=Custodian(gateway, ledger, surface,
                                                 ladder=DegradeLadder()))
    w = flip._window(TICKER, CLOSE)
    w.fills["no"] = 48
    w.first_fill_ts = CLOSE - 790
    w.trips = 1
    w.takes_posted["no"] = "OID-T1"
    w.take_counts["no"] = 1
    gateway.positions[(EVENT, TICKER, "FLIP")] = -2
    ledger.record_fill(TICKER, "FLIP", "no", "ENTRY", 48, 2, "PROBE")
    flip.evaluate(TICKER, _flip_ctx(_flip_book(), secs_left=700))
    flip.evaluate(TICKER, _flip_ctx(_flip_book(), secs_left=699))
    assert _uncovered(ledger) == 1                         # once, not zero, not two
    assert any("FLIP_UNCOVERED_LEG" in a for a in funnel)


def test_b2_restart_amnesia_shape_pages_with_no_bucket(ledger, surface,
                                                       gateway, funnel):
    """The 12:18/191230 candidate root cause, documented as law: FlipWindow
    custody lives in memory, so a restart mid-window (the FLIP-COUNT-1
    deploy itself restarts the engine) leaves booked contracts with NO
    lane bucket and NO resting take. The invariant's job is exactly this
    page — buckets=none says the custody dicts are empty, pointing at
    restart amnesia, not the merge. (The custodian still owns the risk
    via boot adoption; lane-level re-hydration is a future order.)"""
    from relay_engine.lane_flip import LaneFlip
    ledger.record_fill(TICKER, "FLIP", "no", "ENTRY", 48, 2, "PROBE")
    # a FRESH lane instance over the same ledger — what a reboot builds
    flip = LaneFlip(gateway, custodian=Custodian(gateway, ledger, surface,
                                                 ladder=DegradeLadder()))
    flip.evaluate(TICKER, _flip_ctx(_flip_book(), secs_left=700))
    assert _uncovered(ledger) == 1
    row = ledger.db.execute(
        "SELECT how_json FROM failures WHERE why_tag='FLIP_UNCOVERED_LEG'"
    ).fetchone()[0]
    d = json.loads(row)
    assert d["buckets"] == "none" and d["held"] == 2 and d["covered"] == 0


# ── B3: the cash-fatal survives reboot — ENGINE level ──────────────────────
def _entry_order():
    from relay_engine.gateway import Order
    return Order(lane="F", event=EVENT, market=TICKER, side="yes",
                 action="buy", price_cents=48, count=1,
                 size_tier=config.TIER_PROBE, purpose="ENTRY",
                 why="F tier48 · surv~price")


def test_b3_deny_reboot_boots_halted_and_walled(tmp_path, capsys):
    """B3 acceptance end-to-end: deny -> full engine reboot -> the boot
    tape prints the restored fatal AND the stop audit honors it AND the
    gateway wall refuses an ENTRY. No re-baseline: the book is unchanged
    across the reboot."""
    from relay_engine.errors import WallRejection
    from relay_engine.shadow_runner import ShadowEngine
    db = str(tmp_path / "b3.db")
    e1 = ShadowEngine(db_path=db)
    e1.boot()
    assert e1.cash.reconcile(e1.ledger.book_cents() - 99, 0, 0,
                             now=1000.0) == "PROMPTED"
    e1.cash.deny_cash()
    book_denied = e1.ledger.book_cents()
    capsys.readouterr()                                  # drop e1's tape

    e2 = ShadowEngine(db_path=db)                        # THE REBOOT
    alerts = []
    e2.telegram.send = alerts.append
    e2.boot()
    out = capsys.readouterr().out
    assert any("CASH FATAL restored from DB — manual /clear_cash_fatal"
               in a for a in alerts)                     # §B3 the exact line
    assert "cash-fatal=HONORED" in out                   # audit line on tape
    assert "CASH INTEGRITY:" in out                      # the profile states the law
    assert e2.cash.fatal
    assert e2.ledger.book_cents() == book_denied         # no re-baseline
    with pytest.raises(WallRejection) as w:
        e2.gateway.submit(_entry_order(), _flip_book())
    assert "CASH_FATAL" in w.value.detail
    failures._ledger = None


def test_b3_pending_prompt_reboot_still_prompted_engine_level(tmp_path,
                                                              capsys):
    from relay_engine.errors import WallRejection
    from relay_engine.shadow_runner import ShadowEngine
    db = str(tmp_path / "b3p.db")
    e1 = ShadowEngine(db_path=db)
    e1.boot()
    import time as _t
    assert e1.cash.reconcile(e1.ledger.book_cents() - 99, 0, 0,
                             now=_t.time()) == "PROMPTED"
    capsys.readouterr()

    e2 = ShadowEngine(db_path=db)                        # reboot mid-prompt
    e2.boot()
    out = capsys.readouterr().out
    assert "cash-pending=HONORED" in out
    assert e2.cash.pending is not None and not e2.cash.fatal
    with pytest.raises(WallRejection) as w:
        e2.gateway.submit(_entry_order(), _flip_book())
    assert "CASH_PROMPT" in w.value.detail
    # consent still works after the restore
    assert e2.cash.confirm_cash(now=_t.time())
    failures._ledger = None


# ── B4: the Kelly throttle is legible (pure logging) ───────────────────────
def test_b4_sizing_line_states_the_binder_in_words():
    """Today's exact book: $11.74 -> kelly budget 97c -> 1 lot @97c, 0
    @98c. The line says WHY and that it self-scales — computed from the
    live constants, never asserted."""
    from relay_engine.boot import sizing_line
    line = sizing_line(1174)
    assert "kelly-bound: 1 lot @97¢ (0 @98¢)" in line
    assert "throttle is book size, not a wall" in line
    # computed at the 97c reference: 2*97*12=$23.28->~$24, 3*97*12=$34.92
    # ->~$35 (the order's "~$36" example was 98c math; the PRINTED number
    # is the COMPUTED number — the read-rule outranks the illustration)
    assert "self-scales ~$24→2 @97¢, ~$35→3" in line
    # and it scales UP with the book, as the doctrine states
    assert "kelly-bound: 2 lot @97¢" in sizing_line(2400)
    assert "kelly-bound: 3 lot @97¢" in sizing_line(3600)


def test_b4_boot_tape_carries_the_legible_sizing_line(tmp_path, capsys):
    from relay_engine.shadow_runner import ShadowEngine
    e = ShadowEngine(db_path=str(tmp_path / "b4.db"))
    e.boot()
    out = capsys.readouterr().out
    assert "throttle is book size, not a wall" in out
    failures._ledger = None


def test_b4_size_zero_by_kelly_logs_once_and_changes_nothing(tmp_path,
                                                             caplog):
    """A 98c favorite on today's $11.74 book sizes to 0 by Kelly: the INFO
    line names price/book/kelly_budget ONCE per (market, price); the
    proposal still carries count=1 for the walls to refuse by name — NO
    contract count changes anywhere (B5 rail)."""
    from relay_engine.book import OrderBook
    from relay_engine.gateway import Order
    from relay_engine.shadow_runner import ShadowEngine
    e = ShadowEngine(db_path=str(tmp_path / "b4z.db"))
    e.ledger.baseline(1174, confirmed_by="boot")         # today's live book
    book = OrderBook(market=TICKER)
    book.apply_snapshot({98: 10}, {1: 10}, ts=1.0)

    def prop(price):
        return Order(lane="F", event=EVENT, market=TICKER, side="yes",
                     action="buy", price_cents=price, count=1,
                     size_tier=config.TIER_PROBE, purpose="ENTRY",
                     why="F tier98 · surv~price")
    with caplog.at_level(logging.INFO, logger="relay.shadow"):
        p = prop(98)
        e._score_and_size(p, book)
        assert p.count == 1                              # behavior unchanged
        e._score_and_size(prop(98), book)                # dedup: once only
        e._score_and_size(prop(39), book)                # 39c sizes fine
    zero_lines = [r.message for r in caplog.records
                  if "SIZE_ZERO_BY_KELLY" in r.message]
    assert len(zero_lines) == 1
    assert "price=98c" in zero_lines[0]
    assert "book=1174c" in zero_lines[0]
    assert "kelly_budget=97c" in zero_lines[0]
    # rollover clears the dedup key with the window
    e.on_market_closed(TICKER)
    assert (TICKER, 98) not in e._size_zero_logged
    failures._ledger = None
