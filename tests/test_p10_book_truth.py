"""WO-P10 "THE BOOK CANNOT LIE" — parser holes convicted and closed (§1),
the coherence invariant + poison + quarantine (§2), reconcile-to-book (§3),
wall-reject backoff + WALL_STORM (§4), and the A4 end-to-end replay (§6).

§1 evidence note: the 01:25 banked tape lives in the DEPLOYED worker's DB
(RELAY_DB_PATH on Render), not in this checkout — scripts/autopsy_replay.py
runs the verbatim-frame conviction THERE. The frames below are RECONSTRUCTED
to the 01:25 shape (yes_bid=97 AND no_bid=55) through each convicted hole;
they are permanent regression tests for the classes, stated per the read-rule.
"""

import asyncio
import json

import pytest

from relay_engine import config, failures
from relay_engine.book import OrderBook
from relay_engine.errors import WallRejection
from relay_engine.feed import DegradeLadder, Feed
from relay_engine.gateway import Order
from relay_engine.shadow_runner import ShadowEngine

TICKER = "KXBTC15M-02JAN251000-T99"


@pytest.fixture
def engine(tmp_path):
    e = ShadowEngine(db_path=str(tmp_path / "p10.db"))
    e.boot()
    e.telegram_sent = []
    e.telegram.send = e.telegram_sent.append
    failures.configure(e.ledger, alert_fn=e.telegram.alert, run_mode="TEST", boot_id=1)
    yield e
    failures._ledger = None
    failures._alert_fn = None


def fail_rows(engine, tag):
    return engine.ledger.db.execute(
        "SELECT COUNT(*) FROM failures WHERE why_tag=?", (tag,)).fetchone()[0]


def snapshot(yes, no, seq=None):
    frame = {"type": "orderbook_snapshot",
             "msg": {"market_ticker": TICKER, "yes": yes, "no": no}}
    if seq is not None:
        frame["seq"] = seq
    return json.dumps(frame)


def delta(side, price, d, seq=None, omit_side=False):
    m = {"market_ticker": TICKER, "price": price, "delta": d}
    if not omit_side:
        m["side"] = side
    frame = {"type": "orderbook_delta", "msg": m}
    if seq is not None:
        frame["seq"] = seq
    return json.dumps(frame)


# ── §1: the parser holes, each a convicted class ───────────────────────────
def test_sideless_delta_refused_never_defaulted(engine):
    """THE 01:25 reconstruction: true book yes45/no55 (sum 100, honest).
    Pre-fix, a side-LESS delta at 0.97 defaulted onto the YES book →
    yes_bid=97 AND no_bid=55 (sum 152) — the exact lie on the tape.
    Post-fix the frame is refused, banked, and the book resyncs."""
    feed = engine.feed
    feed.handle_frame(snapshot([["0.45", "100"]], [["0.55", "80"]]), now=1.0)
    feed.handle_frame(delta("yes", "0.97", "5.00", omit_side=True), now=2.0)
    assert TICKER not in feed.books           # dropped, not corrupted
    assert fail_rows(engine, "FRAME_SHAPE_UNKNOWN") == 1


def test_side_value_garbage_refused(engine):
    feed = engine.feed
    feed.handle_frame(snapshot([["0.45", "100"]], [["0.55", "80"]]), now=1.0)
    feed.handle_frame(delta("maybe", "0.97", "5.00"), now=2.0)
    assert TICKER not in feed.books
    assert fail_rows(engine, "FRAME_SHAPE_UNKNOWN") == 1


def test_side_case_normalized_not_refused(engine):
    feed = engine.feed
    feed.handle_frame(snapshot([["0.45", "100"]], [["0.55", "80"]]), now=1.0)
    feed.handle_frame(delta("Yes", "0.46", "5.00"), now=2.0)
    assert feed.books[TICKER].best_yes_bid() == 46


def test_delta_without_snapshot_foundation_refused(engine):
    """The drop_book corruption path: a book rebuilt from deltas alone is
    fiction. Refused, resync requested, banked ONCE per episode."""
    feed = engine.feed
    feed.handle_frame(delta("yes", "0.97", "5.00"), now=1.0)
    feed.handle_frame(delta("no", "0.55", "5.00"), now=2.0)
    assert TICKER not in feed.books or not feed.books[TICKER].has_snapshot
    assert TICKER in feed.resync_needed
    assert fail_rows(engine, "DELTA_WITHOUT_SNAPSHOT") == 1  # once, not twice
    # the snapshot arrives: foundation restored, deltas land again
    feed.handle_frame(snapshot([["0.45", "100"]], [["0.30", "80"]]), now=3.0)
    feed.handle_frame(delta("yes", "0.46", "5.00"), now=4.0)
    assert feed.books[TICKER].best_yes_bid() == 46


def test_seq_gap_drops_book_and_resyncs(engine):
    feed = engine.feed
    feed.handle_frame(snapshot([["0.45", "100"]], [["0.30", "80"]], seq=10), now=1.0)
    feed.handle_frame(delta("yes", "0.46", "5.00", seq=11), now=2.0)
    assert feed.books[TICKER].best_yes_bid() == 46
    feed.handle_frame(delta("yes", "0.47", "5.00", seq=13), now=3.0)  # 12 missed
    assert TICKER not in feed.books
    assert TICKER in feed.resync_needed
    assert fail_rows(engine, "BOOK_SEQ_GAP") == 1


# ── §2: the coherence invariant ────────────────────────────────────────────
def poison_book(feed, now=1.0):
    """A VALID-side delta that crosses the book (whatever produced it live):
    yes 97 onto a no 55 book → sum 152."""
    feed.handle_frame(snapshot([["0.45", "100"]], [["0.55", "80"]]), now=now)
    feed.handle_frame(delta("yes", "0.97", "5.00"), now=now + 1)


def test_crossed_book_poisons_pages_once_and_resyncs(engine):
    poison_book(engine.feed)
    book = engine.feed.books[TICKER]
    assert book.poisoned is True
    assert TICKER in engine.feed.resync_needed
    assert fail_rows(engine, "BOOK_INCOHERENT") == 1
    pages = [m for m in engine.telegram_sent if "BOOK_INCOHERENT" in m]
    assert len(pages) == 1 and "y97+n55" in pages[0]
    # further incoherent applies in the SAME episode: row yes, page no
    engine.feed.handle_frame(delta("yes", "0.98", "5.00"), now=3.0)
    assert fail_rows(engine, "BOOK_INCOHERENT") == 2
    assert len([m for m in engine.telegram_sent if "BOOK_INCOHERENT" in m]) == 1


def test_lanes_skip_poisoned_book_never_fatal(engine):
    poison_book(engine.feed)
    engine.market_meta[TICKER] = {"close_ts": 1000.0 + 120,
                                  "boundary_lo": None, "boundary_hi": None}
    engine.cycle([TICKER], now=1000.0)          # does not raise
    assert engine.gateway.shadow_orders == []   # no entries off a lying book
    rows = engine.ledger.db.execute(
        "SELECT COUNT(*) FROM surface_rows WHERE detail='BOOK_POISONED'"
        " AND market=?", (TICKER,)).fetchone()[0]
    assert rows >= 1


def test_clean_snapshot_clears_poison(engine):
    poison_book(engine.feed)
    engine.feed.handle_frame(snapshot([["0.45", "100"]], [["0.55", "80"]]), now=5.0)
    assert engine.feed.books[TICKER].poisoned is False


def test_custodian_marks_flip_to_rest_on_poison(engine, monkeypatch):
    """§2.3 (A6): a poisoned book must not blind the custodian — its marks
    switch to the REST book; risk reduction never stalls."""
    from relay_engine.custodian import OpenPosition
    poison_book(engine.feed)
    engine.custodian.adopt(OpenPosition(
        event=TICKER.rsplit("-", 1)[0], market=TICKER, lane="FLIP", side="yes",
        count=1, entry_price_cents=45, entry_p_win=0.45,
        size_tier=config.TIER_PROBE, entry_time=0.0))
    rest = OrderBook(market=TICKER, transport="REST")
    rest.yes_bids[40] = 10
    rest.no_bids[58] = 10
    rest.has_snapshot = True
    monkeypatch.setattr(engine, "_rest_book", lambda m: rest)
    seen = {}
    real_tick = engine.custodian.tick

    def spy_tick(books, **kw):
        seen.update(books)
        return real_tick(books=books, **kw)
    monkeypatch.setattr(engine.custodian, "tick", spy_tick)
    engine.market_meta[TICKER] = {"close_ts": 1000.0 + 120,
                                  "boundary_lo": None, "boundary_hi": None}
    engine.cycle([TICKER], now=1000.0)
    assert seen[TICKER].transport == "REST"      # marks came from REST truth
    assert seen[TICKER].best_yes_bid() == 40     # not the poisoned 97


def test_three_episodes_quarantine_one_page(engine):
    close_ts = 99999.0
    engine.market_meta[TICKER] = {"close_ts": close_ts,
                                  "boundary_lo": None, "boundary_hi": None}
    for i in range(3):
        poison_book(engine.feed, now=10.0 * i + 1)   # snapshot clears, delta re-poisons
    assert engine.poison_episodes[TICKER] == 3
    assert engine.quarantined[TICKER] == close_ts
    q_pages = [m for m in engine.telegram_sent if "QUARANTINE" in m]
    assert len(q_pages) == 1 and "until" in q_pages[0]
    assert fail_rows(engine, "QUARANTINE") == 1
    # entries off for ALL lanes; custody continues (rows tagged QUARANTINED)
    engine.cycle([TICKER], now=1000.0)
    assert engine.gateway.shadow_orders == []
    rows = engine.ledger.db.execute(
        "SELECT COUNT(*) FROM surface_rows WHERE detail='QUARANTINED'").fetchone()[0]
    assert rows >= 1
    # rollover clears the quarantine and the episode count
    engine.on_market_closed(TICKER)
    assert TICKER not in engine.quarantined
    assert TICKER not in engine.poison_episodes


# ── §3: reconcile-to-book ──────────────────────────────────────────────────
class FakeRest:
    def __init__(self, yes_bid, no_bid):
        self.yes_bid, self.no_bid = yes_bid, no_bid
        self.yes_bid_qty = self.no_bid_qty = 10


def test_divergence_first_silent_second_pages(engine, monkeypatch):
    from relay_engine import venue
    engine.feed.handle_frame(snapshot([["0.45", "100"]], [["0.30", "80"]]), now=1.0)
    engine.gateway.venue_client = object()
    monkeypatch.setattr(venue, "fetch_orderbook", lambda c, m: FakeRest(50, 30))

    engine.book_check(now=100.0)   # ws yes 45 vs rest 50 → 5c divergence
    assert fail_rows(engine, "BOOK_DIVERGENCE") == 1     # the ROW every trip (R5)
    assert TICKER in engine.feed.resync_needed           # silent resync, first trip
    assert not any("BOOK_DIVERGENCE" in m for m in engine.telegram_sent)

    engine.book_check(now=160.0)   # second consecutive → the phone hears it
    assert fail_rows(engine, "BOOK_DIVERGENCE") == 2
    assert any("BOOK_DIVERGENCE" in m and "persists" in m
               for m in engine.telegram_sent)

    monkeypatch.setattr(venue, "fetch_orderbook", lambda c, m: FakeRest(45, 30))
    assert engine.book_check(now=220.0) == 1             # clean → heartbeat counts
    assert engine.book_checks_total == 1
    assert TICKER not in engine._divergence_pending


def test_book_check_unreadable_rest_gives_no_verdict(engine, monkeypatch):
    from relay_engine import venue
    engine.feed.handle_frame(snapshot([["0.45", "100"]], [["0.30", "80"]]), now=1.0)
    engine.gateway.venue_client = object()
    monkeypatch.setattr(venue, "fetch_orderbook", lambda c, m: FakeRest(None, None))
    engine.book_check(now=100.0)
    assert fail_rows(engine, "BOOK_DIVERGENCE") == 0
    assert TICKER not in engine.feed.resync_needed


# ── §4: wall-reject backoff + WALL_STORM ───────────────────────────────────
def entry(price=50, lane="F", count=1):
    return Order(lane=lane, event=TICKER.rsplit("-", 1)[0], market=TICKER,
                 side="yes", action="buy", price_cents=price, count=count,
                 size_tier=config.TIER_PROBE, purpose="ENTRY", band=(95, 99))


def make_book(yes=50, no=30):
    ob = OrderBook(market=TICKER)
    ob.apply_snapshot({yes: 100}, {no: 80}, ts=1.0)
    return ob


def test_backoff_suppresses_identical_repropose(gateway):
    book = make_book()
    with pytest.raises(WallRejection) as e1:
        gateway.submit(entry(price=50), book, now_mono=100.0)  # outside band 95-99
    orig_tag = e1.value.wall
    assert orig_tag != "WALL_BACKOFF"
    with pytest.raises(WallRejection) as e2:                   # identical → suppressed
        gateway.submit(entry(price=50), book, now_mono=101.0)
    assert e2.value.wall == "WALL_BACKOFF"
    assert gateway.suppressed_counts[orig_tag] == 1            # R5 counters count
    with pytest.raises(WallRejection) as e3:                   # changed price → re-walled
        gateway.submit(entry(price=51), book, now_mono=102.0)
    assert e3.value.wall == orig_tag
    with pytest.raises(WallRejection) as e4:                   # 30s elapsed → re-walled
        gateway.submit(entry(price=51), book, now_mono=140.0)
    assert e4.value.wall == orig_tag


def test_backoff_reopens_when_touch_moves(gateway):
    with pytest.raises(WallRejection):
        gateway.submit(entry(price=50), make_book(yes=50), now_mono=100.0)
    with pytest.raises(WallRejection) as e2:   # same proposal, moved touch
        gateway.submit(entry(price=50), make_book(yes=52), now_mono=101.0)
    assert e2.value.wall != "WALL_BACKOFF"


def test_wall_storm_pages_exactly_once(gateway):
    alerts = []
    gateway.alert_fn = alerts.append
    book = make_book()
    for i in range(25):
        with pytest.raises(WallRejection):
            gateway.submit(entry(price=50), book, now_mono=100.0 + i)
    storms = [a for a in alerts if "WALL_STORM" in a]
    assert len(storms) == 1
    assert "F" in storms[0] and TICKER in storms[0]


def test_exits_and_cuts_never_suppressed(gateway):
    """Risk reduction skips walls AND backoff — repeated exits all pass."""
    gateway.positions[(TICKER.rsplit("-", 1)[0], TICKER, "F")] = 1
    book = make_book()
    exit_order = Order(lane="F", event=TICKER.rsplit("-", 1)[0], market=TICKER,
                       side="yes", action="sell", price_cents=40, count=1,
                       size_tier=config.TIER_PROBE, purpose="EXIT", band=(95, 99))
    r1 = gateway.submit(exit_order, book, now_mono=100.0)
    r2 = gateway.submit(exit_order, book, now_mono=100.5)
    assert r1.order_id and r2.order_id


# ── §5: the pack renders the episodes ──────────────────────────────────────
def test_pack_episode_section(engine):
    for i in range(3):
        poison_book(engine.feed, now=10.0 * i + 1)
    from relay_engine.ops import daily_pack
    pack = daily_pack(engine.ledger, engine.surface, engine.cash, econ=engine.econ)
    assert "BOOK EPISODES" in pack
    assert f"BOOK_INCOHERENT {TICKER}: x3" in pack
    assert "FINDING" in pack


# ── §6 [A4]: the end-to-end replay through the DEPLOYED wiring ─────────────
def test_0125_replay_end_to_end_zero_lying_proposals(engine):
    """Feed.handle_frame → touch_view → FH8Shared.decide → gateway.submit —
    the full deployed pipeline. Both 01:25 corruption shapes (the side-less
    frame AND a crossed book from any other cause) must produce ZERO
    proposals priced above the book-derived cost. (The v7 property test
    passed while live failed — a test that bypasses the runner's wiring
    certifies the wrong machine; banked.)"""
    now = 1000.0
    engine.market_meta[TICKER] = {"close_ts": now + 120,
                                  "boundary_lo": None, "boundary_hi": None}
    # true book: yes 45 / no 55 — F's honest verdict on this book is PASS
    engine.feed.handle_frame(snapshot([["0.45", "100"]], [["0.55", "80"]]), now=now)

    # corruption shape 1: the side-less delta (refused → book dropped)
    engine.feed.handle_frame(delta("yes", "0.97", "5.00", omit_side=True),
                             now=now + 1)
    engine.cycle([TICKER], now=now + 2)
    assert engine.gateway.shadow_orders == []

    # corruption shape 2: a crossed book from a valid-side delta (poisoned)
    engine.feed.handle_frame(snapshot([["0.45", "100"]], [["0.55", "80"]]),
                             now=now + 3)
    engine.feed.handle_frame(delta("yes", "0.97", "5.00"), now=now + 4)
    engine.cycle([TICKER], now=now + 5)
    assert engine.gateway.shadow_orders == []   # zero proposals off the lie

    # and the honest book after resync: F PASSES a mid-priced book —
    # a quiet log is the discipline working, not a defect
    engine.feed.handle_frame(snapshot([["0.45", "100"]], [["0.55", "80"]]),
                             now=now + 6)
    engine.cycle([TICKER], now=now + 7)
    assert engine.gateway.shadow_orders == []
    verdicts = engine.ledger.db.execute(
        "SELECT DISTINCT state FROM surface_rows WHERE market=?"
        " AND lane='F'", (TICKER,)).fetchall()
    assert ("PASS",) in verdicts or ("WATCHING",) in verdicts


# ── §1b regression guard: async plumbing still supervised after P10 edits ──
def test_resync_cmds_shapes():
    from relay_engine.shadow_runner import ChannelSubscriber
    sub = ChannelSubscriber()
    # no sid learned yet → plain re-subscribe fallback
    cmds = sub.resync_cmds(TICKER)
    assert len(cmds) == 1 and cmds[0]["cmd"] == "subscribe"
    assert cmds[0]["id"] in sub.resync_ids
    # sid learned → delete + add on the existing subscription
    sub.sids["orderbook_delta"] = 7
    cmds = sub.resync_cmds(TICKER)
    assert [c["cmd"] for c in cmds] == ["update_subscription", "update_subscription"]
    assert [c["params"]["action"] for c in cmds] == ["delete", "add"]
    assert all(c["params"]["sids"] == [7] for c in cmds)
    assert all(c["params"]["market_tickers"] == [TICKER] for c in cmds)
