"""WO-2026-07-23-B Part 1 — SCALE F. F earns ~98% of the book's profit and was
capped at 3 contracts by NET_RISK_CROSS_LANE_CAP (a fixed count that turned
compound growth into linear growth). F now sizes to a % of book (self-scaling),
bounded only by real depth; the gateway keeps a PER-LANE, BOOK-PROPORTIONAL
dollar wall (Drew's ruling — keep the guard, don't exempt F), plus guard (a)
the 50%-of-book portfolio cap. WO-2026-07-26-Q DELETED guard (b) (the F per-event
day-long suppression) — a duplicate of the money rate halt; a big F loss now
PAGES (F_BIG_LOSS) and never suppresses the next window.

HARD RAIL: F behaviour unchanged OTHER than size (the WO's kill condition)."""

import pytest

from relay_engine import config, scoring
from relay_engine.book import OrderBook
from relay_engine.gateway import Order
from relay_engine.ledger import Ledger
from relay_engine.shadow_runner import ShadowEngine

EVENT = "KXBTC15M-02JAN251000"
TICKER = EVENT + "-T99"


def _book(price=97, qty=1000):
    b = OrderBook(market=TICKER)
    b.apply_snapshot({price: qty}, {1: qty}, ts=1.0)
    return b


def _engine(ledger):
    """A minimal engine carrying only the organs _score_and_size touches."""
    eng = object.__new__(ShadowEngine)
    eng.ledger = ledger
    eng.telegram = type("T", (), {"alert": staticmethod(lambda m: None)})()
    eng._size_zero_logged = set()
    return eng


def _f_entry(price=97):
    return Order(lane="F", event=EVENT, market=TICKER, side="yes", action="buy",
                 price_cents=price, count=1, size_tier=config.TIER_PROBE,
                 purpose="ENTRY", why="F tier97 · surv~price")


# ── §1.3: F sizes to a percentage of book, self-scaling (acceptance #3) ─────
def test_f_sizes_to_notional_percent_of_book():
    # WO-L P2: F dial 0.24 — $41.62 book at 97c: notional = int(4162*0.24//97) = 10
    n = int(4162 * config.F_NOTIONAL_PCT // 97)
    dec = scoring.size_order(4162, 97, 10_000, lane="F")
    assert dec.contracts == n
    assert f"notional={n}" in dec.reason and "→ notional bound" in dec.reason


def test_f_size_scales_with_the_book():
    """The decay fix: F's size grows with the book instead of staying flat at 3."""
    small = scoring.size_order(2000, 97, 10_000, lane="F").contracts
    big = scoring.size_order(8000, 97, 10_000, lane="F").contracts
    assert small == int(2000 * config.F_NOTIONAL_PCT // 97)   # 4
    assert big == int(8000 * config.F_NOTIONAL_PCT // 97)     # 16
    assert big > small > config.NET_RISK_CROSS_LANE_CAP       # the count cap no longer binds


def test_f_size_bounded_by_real_depth_and_logged():
    """Guard (d): when depth is the smaller term it binds, and the reason names
    all three terms so 'is depth ever real' is answered permanently."""
    n = int(4162 * config.F_NOTIONAL_PCT // 97)        # WO-L: 10 at dial 0.24
    dec = scoring.size_order(4162, 97, 20, lane="F")   # depth 20·0.25 = 5 < notional
    assert dec.contracts == 5 and "→ depth bound" in dec.reason
    assert "kelly=" in dec.reason and f"notional={n}" in dec.reason and "depth=5" in dec.reason


def test_non_f_lanes_keep_the_kelly_path():
    """Only F takes the notional path — every other lane is min(kelly, depth, cap)."""
    f = scoring.size_order(4162, 97, 10_000, lane="F").contracts
    h8 = scoring.size_order(4162, 97, 10_000, lane="H8").contracts
    assert f == int(4162 * config.F_NOTIONAL_PCT // 97)
    assert h8 == min(int(4162 / 12 // 97), 2500, config.NET_RISK_CROSS_LANE_CAP)


# ── Guard (a): the 50%-of-book portfolio cap (acceptance #4) ────────────────
def test_deployed_cents_sums_open_notional_across_lanes(tmp_path):
    led = Ledger(str(tmp_path / "d.db"))
    led.record_fill("MA", "F", "yes", "ENTRY", 90, 3, "PROBE")     # 270c
    led.record_fill("MB", "FLIP", "no", "ENTRY", 50, 2, "PROBE")   # 100c
    led.record_fill("MB", "FLIP", "no", "EXIT", 60, 1, "PROBE")    # 1 of the 2 closed
    # F: 3 held × 90 = 270; FLIP: (2−1)=1 held × 50 = 50
    assert led.deployed_cents() == 270 + 50


def test_portfolio_cap_clamps_an_entry_that_would_exceed_half_the_book(tmp_path):
    led = Ledger(str(tmp_path / "p.db"))
    led.baseline(1000, confirmed_by="test")            # book 1000c → 50% = 500c
    led.record_fill("MX", "D", "yes", "ENTRY", 90, 5, "PROBE")   # deployed 450c
    eng = _engine(led)
    p = _f_entry(price=50)                              # F notional wants 4 lots
    eng._score_and_size(p, _book(price=50))
    # room = 500 − 450 = 50c → 50 // 50 = 1 lot; the 4-lot F entry is clamped to 1
    assert p.count == 1
    # and once the book is fully deployed, the next entry is refused outright
    led.record_fill("MY", "D", "yes", "ENTRY", 50, 1, "PROBE")   # deployed 500c
    p2 = _f_entry(price=50)
    eng._score_and_size(p2, _book(price=50))
    assert p2.count == 0


# ── WO-2026-07-26-Q — guard (b) DELETED: the last silent governor ───────────
def test_q_big_f_loss_pages_but_never_suppresses(tmp_path, caplog):
    """A big F loss PAGES (F_BIG_LOSS) and has NO entry effect — the day-long
    suppression is deleted; the money rate halt is F's one governor."""
    import logging
    from relay_engine import failures
    led = Ledger(str(tmp_path / "t.db"))
    led.baseline(4162, confirmed_by="test")
    failures.configure(led, alert_fn=lambda m: None, run_mode="TEST", boot_id=1)
    eng = _engine(led)
    try:
        # a loss of exactly 60c/contract does NOT page (strictly greater)
        eng._page_f_big_loss(60.0, TICKER, "at the wire")
        assert led.db.execute(
            "SELECT COUNT(*) FROM failures WHERE why_tag='F_BIG_LOSS'"
        ).fetchone()[0] == 0
        # 61c/contract PAGES — and the very next F window still SIZES full
        eng._page_f_big_loss(61.0, TICKER, "past the wire")
        assert led.db.execute(
            "SELECT COUNT(*) FROM failures WHERE why_tag='F_BIG_LOSS'"
        ).fetchone()[0] == 1
        p = _f_entry(price=97)
        eng._score_and_size(p, _book())
        assert p.count >= 1                # F is NOT suppressed — it sizes normally
    finally:
        failures._ledger = None
        failures._alert_fn = None


def test_q_boot_migration_clears_a_live_tripwire_flag(tmp_path):
    """The resume-tonight test: a live f_tripwire_day flag in the DB is cleared
    on boot so the deploy that removed its reader actually resumes F."""
    led = Ledger(str(tmp_path / "c.db"))
    led.set_state("f_tripwire_day", "20260726")         # a flag from the old build
    assert led.clear_f_tripwire_migration() is True     # cleared
    assert led.get_state("f_tripwire_day") is None
    assert led.clear_f_tripwire_migration() is False    # idempotent, nothing left


def test_q_no_suppression_consumer_survives():
    """Sibling grep, in code: the flag family has zero surviving consumers."""
    import inspect
    from relay_engine import shadow_runner, ledger as ledger_mod
    for mod in (shadow_runner, ledger_mod):
        src = inspect.getsource(mod)
        assert "F_SUPPRESSED" not in src
        assert "_f_suppressed_today" not in src
        assert "def set_f_tripwire" not in src
        assert "def f_suppressed" not in src
        assert "def _trip_f_event" not in src
