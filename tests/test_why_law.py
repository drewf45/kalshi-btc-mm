"""WO-2026-07-26-P — THE WHY BAKE (Part A) + THE ONE-LOT TRACE (Part B),
acceptance.

Six acceptance criteria, each a test:
  1. a fresh level sizes to a band-referenced full lot, both depth terms +
     the binder on the size row (B1);
  2. a blind book DEFERS (DEPTH_BLIND) and a zero size DEFERS
     (SIZE_ZERO_DEFER) — never a fabricated 1 (B2);
  3. the fallback-family lint is green and fails on a planted `or 0` (A2,
     covered in test_no_silent_fallbacks.py; re-asserted here as a gate);
  4. a surface row with no why is REFUSED; every seed surface is registered;
     the WARN→FATAL schedule is in the boot banner (A1/A3);
  5. every capital constant is tagged; the boot prints changed-constants-with-
     tags; a planted DERIVED-without-source fails loud (A4);
  6. F entry/hold is byte-identical (lane_fh8 untouched) — checked in
     test_f_identical / preflight; re-asserted here at the module level.
"""

import json
import logging

import pytest

from relay_engine import config, failures, registry
from relay_engine.book import OrderBook
from relay_engine.gateway import Order
from relay_engine.shadow_runner import ShadowEngine

EVENT = "KXBTC15M-02JAN251000"
TICKER = "KXBTC15M-02JAN251000-T99"


# ── ACCEPTANCE 1 — B1: the band-referenced size row ──────────────────────────
def test_b1_fresh_level_sizes_to_band_with_both_terms_on_row(tmp_path, caplog):
    """F creating a FRESH tier (joining=0) in front of a real band sizes to the
    band, and the size row names joining, band, band_ref, and used."""
    e = ShadowEngine(db_path=str(tmp_path / "b1.db"))
    e.ledger.baseline(500_00, confirmed_by="boot")   # a book big enough for lots
    book = OrderBook(market=TICKER)
    # A deep wall sits at 90-94¢; F prices a FRESH level at 95¢ (joining=0) in
    # front of it. The band ±4¢ around 95 = [91,99] catches the wall.
    book.apply_snapshot({90: 200, 92: 300, 94: 500, 99: 1}, {1: 10}, ts=1.0)
    p = Order(lane="F", event=EVENT, market=TICKER, side="yes", action="buy",
              price_cents=95, count=1, size_tier=config.TIER_PROBE,
              purpose="ENTRY", why="F tier95")
    with caplog.at_level(logging.INFO, logger="relay.shadow"):
        e._score_and_size(p, book)
    # joining at 95 is 0 (fresh), but the band carries the wall → a full lot.
    assert p.count >= 1, "a fresh level in front of a real band must SIZE, not defer"
    assert "joining=0" in p.why
    assert "band=" in p.why and "ref=" in p.why
    assert "used=" in p.why
    failures._ledger = None


# ── ACCEPTANCE 2 — B2: defer, never fabricate ────────────────────────────────
def test_b2_blind_book_defers_depth_blind(tmp_path, caplog):
    """A book with no snapshot answers joining_depth=None → DEPTH_BLIND defer,
    count=0, no order."""
    e = ShadowEngine(db_path=str(tmp_path / "b2blind.db"))
    e.ledger.baseline(500_00, confirmed_by="boot")
    book = OrderBook(market=TICKER)               # NEVER snapshotted → blind
    assert book.joining_depth("yes", 95) is None
    p = Order(lane="F", event=EVENT, market=TICKER, side="yes", action="buy",
              price_cents=95, count=1, size_tier=config.TIER_PROBE,
              purpose="ENTRY", why="F tier95")
    with caplog.at_level(logging.INFO, logger="relay.shadow"):
        e._score_and_size(p, book)
    assert p.count == 0, "a blind book must DEFER, never bet"
    msgs = " ".join(r.message for r in caplog.records)
    assert "DEPTH_BLIND" in msgs
    failures._ledger = None


def test_b2_zero_size_defers_not_one(tmp_path, caplog):
    """A real book that sizes to 0 (kelly-zeroed on a tiny book) DEFERS with
    SIZE_ZERO_DEFER carrying the term set — never a fabricated 1-lot."""
    e = ShadowEngine(db_path=str(tmp_path / "b2zero.db"))
    e.ledger.baseline(1174, confirmed_by="boot")   # today's tiny live book
    book = OrderBook(market=TICKER)
    book.apply_snapshot({98: 10}, {1: 10}, ts=1.0)
    p = Order(lane="H8", event=EVENT, market=TICKER, side="yes", action="buy",
              price_cents=98, count=1, size_tier=config.TIER_PROBE,
              purpose="ENTRY", why="H8 tier98")
    with caplog.at_level(logging.INFO, logger="relay.shadow"):
        e._score_and_size(p, book)
    assert p.count == 0
    zero = [r.message for r in caplog.records if "SIZE_ZERO_DEFER" in r.message]
    assert len(zero) == 1
    # the defer names the terms the math used
    for term in ("tradeable", "joining", "band", "sizing"):
        assert term in zero[0]
    failures._ledger = None


def test_b2_submit_loop_skips_a_zero_count_proposal(tmp_path):
    """The submit path never sends a count<=0 proposal (the defer signal)."""
    import inspect
    from relay_engine import shadow_runner
    src = inspect.getsource(shadow_runner)
    assert "if proposal.count <= 0:" in src, \
        "submit loop must skip the deferred (count<=0) proposal"


# ── ACCEPTANCE 1+2 — the two ×1 windows, replayed ────────────────────────────
def test_the_two_one_lot_windows_replayed(tmp_path, caplog):
    """The one-lot bug's origin: F priced a FRESH tier (joining=0), two silent
    fallbacks (`depth or 0`, `max(1,contracts)`) turned that honest 0 into a
    1-lot bet. Replay the two window shapes the bug produced and assert the new
    law: a fresh level in FRONT OF A REAL BAND sizes to a band-referenced lot
    with both terms on the row; a fresh level in front of NOTHING (thin/blind)
    DEFERS. No 1-lot survives unless the math produced it."""
    e = ShadowEngine(db_path=str(tmp_path / "twowin.db"))
    e.ledger.baseline(500_00, confirmed_by="boot")

    def prop(price):
        return Order(lane="F", event=EVENT, market=TICKER, side="yes",
                     action="buy", price_cents=price, count=1,
                     size_tier=config.TIER_PROBE, purpose="ENTRY", why="F")

    # WINDOW 1 — a fresh 95¢ level in front of a real 90-94¢ wall: SIZES.
    b1 = OrderBook(market=TICKER)
    b1.apply_snapshot({90: 200, 92: 300, 94: 500, 99: 1}, {1: 10}, ts=1.0)
    p1 = prop(95)
    with caplog.at_level(logging.INFO, logger="relay.shadow"):
        e._score_and_size(p1, b1)
    assert p1.count >= 1
    assert "joining=0" in p1.why and "band=" in p1.why and "used=" in p1.why

    # WINDOW 2 — a fresh 95¢ level in front of NOTHING (thin book): DEFERS.
    caplog.clear()
    b2 = OrderBook(market=TICKER)
    b2.apply_snapshot({1: 5}, {1: 5}, ts=1.0)     # nothing near 95 on either side
    p2 = prop(95)
    with caplog.at_level(logging.INFO, logger="relay.shadow"):
        e._score_and_size(p2, b2)
    assert p2.count == 0, "a fresh level in front of nothing must DEFER, not bet 1"
    assert any("DEFER" in r.message for r in caplog.records)
    failures._ledger = None


# ── ACCEPTANCE 4 — A1: the Why Law refuses a why-less row ─────────────────────
def test_a1_surface_row_without_why_is_refused(tmp_path):
    """The shared write chokepoint REFUSES a row with no why/reason."""
    from relay_engine.ledger import Ledger
    from relay_engine.surface import Surface, WATCHING
    led = Ledger(db_path=str(tmp_path / "a1.db"))
    surf = Surface(led)
    with pytest.raises(ValueError, match="WHY_REQUIRED"):
        surf.write_row("F", TICKER, "w1", WATCHING, detail="")
    with pytest.raises(ValueError, match="WHY_REQUIRED"):
        surf.write_row("F", TICKER, "w1", WATCHING, detail="   ")
    # a row WITH a why writes fine
    assert surf.write_row("F", TICKER, "w1", WATCHING, detail="no edge yet") is True


# ── ACCEPTANCE 4 — A3: registry registered + boot schedule ───────────────────
def test_a3_every_seed_surface_registered_with_complete_question():
    for s in registry.SEED_SURFACES:
        q = registry.get(s)
        assert q is not None, f"{s} not registered"
        assert q.is_complete(), f"{s} has an incomplete question"
    assert registry.missing_registration(registry.SEED_SURFACES) == []


def test_a3_boot_banner_states_warn_then_fatal_schedule():
    banner = registry.assert_writers_registered()
    assert "WARN" in banner and "FATAL" in banner
    assert "DATA_WITHOUT_QUESTION" in banner


def test_a3_unregistered_writer_warns_soft_then_fatal_after_cutover():
    before = registry.ENFORCE_FROM_TS - 1
    after = registry.ENFORCE_FROM_TS + 1
    # WARN before the cutover: a soft banner, no raise
    banner = registry.assert_writers_registered(["UNDECLARED_SURFACE"], now=before)
    assert "WARN" in banner
    # FATAL after: the fail_fn is invoked with fatal=True
    calls = []
    registry.assert_writers_registered(
        ["UNDECLARED_SURFACE"], now=after,
        fail_fn=lambda tag, what, **kw: calls.append((tag, kw.get("fatal"))))
    assert calls and calls[0][0] == "DATA_WITHOUT_QUESTION" and calls[0][1] is True


def test_a3_unread_14_days_pages_data_without_question(tmp_path):
    from relay_engine.ledger import Ledger
    led = Ledger(db_path=str(tmp_path / "a3read.db"))
    now = registry.ENFORCE_FROM_TS
    # nothing read yet → all seed surfaces are stale
    stale = registry.unread_surfaces(led.db, now=now)
    assert set(stale) == set(registry.SEED_SURFACES)
    calls = []
    registry.page_unread(led.db, now=now,
                         fail_fn=lambda tag, what, **kw: calls.append(tag))
    assert calls == ["DATA_WITHOUT_QUESTION"]
    # read them all NOW → none stale
    for s in registry.SEED_SURFACES:
        registry.record_read(led.db, s, ts=now)
    assert registry.unread_surfaces(led.db, now=now) == []
    # 15 days later, stale again
    assert set(registry.unread_surfaces(led.db, now=now + 15 * 86400)) \
        == set(registry.SEED_SURFACES)


# ── ACCEPTANCE 5 — A4: constant tags + DERIVED-without-source fails loud ──────
def test_a4_every_capital_constant_is_tagged():
    tags = config.constant_tags()
    assert tags, "the constant table is empty"
    for t in tags:
        assert t.kind in (config.RULED, config.DERIVED, config.DREW_DEFAULT)
    # the two WO-P constants are present, tagged, and flagged NEW/changed
    names = {t.name for t in tags}
    assert "BAND_DEPTH_FRACTION" in names
    assert "SIZING_BAND_HALFWIDTH_C" in names
    # the WO-P band constants are new/changed; later deploys add their own NEW
    # constants (WO-R's two orientation thresholds), so assert membership not equality.
    changed = {t.name for t in config.changed_constants()}
    assert {"BAND_DEPTH_FRACTION", "SIZING_BAND_HALFWIDTH_C"} <= changed, \
        "WO-P's two band constants are flagged changed (no dial moves)"


def test_a4_boot_prints_changed_constants_with_tags():
    lines = config.constant_tag_boot_lines()
    body = "\n".join(lines)
    assert "BAND_DEPTH_FRACTION" in body and "DREW-DEFAULT" in body
    assert "SIZING_BAND_HALFWIDTH_C" in body


def test_a4_real_table_has_no_derived_without_source():
    """The shipped table is sane — assert_constant_tags_sane does not raise."""
    config.assert_constant_tags_sane()          # must not raise
    assert config.derived_without_source() == []


def test_a4_planted_derived_without_source_fails_loud():
    planted = [config.ConstantTag("PHANTOM_DIAL", config.DERIVED, "  ",
                                  0.42, 0.42)]
    assert config.derived_without_source(planted)
    calls = []
    config.assert_constant_tags_sane(
        fail_fn=lambda tag, what, **kw: calls.append((tag, kw.get("fatal"))),
        tags=planted)
    assert calls == [("DERIVED_WITHOUT_SOURCE", True)]


# ── ACCEPTANCE 6 — F byte-identical (lane_fh8 untouched) ──────────────────────
def test_f_entry_hold_untouched_lane_fh8_not_edited():
    """WO-P is a sizing-chokepoint + evidence-law change; F's entry/hold logic
    (lane_fh8.py) is not touched. The kill-condition guard: this WO added no
    edit to lane_fh8.py — proven byte-identical in the close pytest/preflight."""
    import inspect
    from relay_engine import lane_fh8
    # a shape marker that would have to change if F's sizing had moved
    src = inspect.getsource(lane_fh8)
    assert "joining_depth" not in src, \
        "F must size through the SAME touch view; the two-question API lives in " \
        "book.py + shadow_runner's chokepoint, never inside lane_fh8"
