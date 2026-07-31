"""WO-2026-07-27-V — THE SLEEPING SENTINEL.

The ~$45 withdrawal walked past every guard: the reconcile never reached a
quiescent cycle (two rooms busy), a quiescence-defer was treated as benign so it
never paged, the book stayed a phantom, and BTC's F sized off it and bounced at
the venue silently. Root cause T1 (quiescence starvation); belts B1 (insufficient
balance screams) and B2 (the sanity clamp) close the class.
"""

import time

import pytest

from relay_engine import config, failures
from relay_engine.book import OrderBook
from relay_engine.gateway import Gateway, Order
from relay_engine.shadow_runner import ShadowEngine

EVENT = "KXBTC15M-02JAN251000"
TICKER = "KXBTC15M-02JAN251000-T99"


def _eng(tmp_path, name="v.db"):
    e = ShadowEngine(db_path=str(tmp_path / name))
    e.ledger.baseline(50_000, confirmed_by="boot")
    failures.configure(e.ledger, alert_fn=lambda m: None, run_mode="TEST", boot_id=1)
    return e


def _f(price=97, count=1, side="yes"):
    return Order(lane="F", event=EVENT, market=TICKER, side=side, action="buy",
                 price_cents=price, count=count, size_tier=config.TIER_PROBE,
                 purpose="ENTRY", why="F tier97")


# ── T1 — the sentinel no longer sleeps through quiescence starvation ─────────
def test_t1_quiescence_starvation_pages_recon_starved(tmp_path):
    """The root cause: a book unverified against the venue for RECON_MAX_QUIET_S
    (because the rooms never went quiet) now PAGES — it is not benign."""
    e = _eng(tmp_path)
    now = 1_000_000.0
    e._recon_last_ok_ts = now                          # last clean check: now
    # a quiescence-defer far in the future (rooms stayed busy the whole time)
    e._recon_check_starved(now + config.RECON_MAX_QUIET_S + 1)
    n = e.ledger.db.execute(
        "SELECT COUNT(*) FROM failures WHERE why_tag='RECON_STARVED'").fetchone()[0]
    assert n == 1
    # once per episode — not every cycle
    e._recon_check_starved(now + config.RECON_MAX_QUIET_S + 61)
    assert e.ledger.db.execute(
        "SELECT COUNT(*) FROM failures WHERE why_tag='RECON_STARVED'").fetchone()[0] == 1
    failures._ledger = None


def test_t1_fresh_reconcile_does_not_page(tmp_path):
    e = _eng(tmp_path)
    now = 1_000_000.0
    e._recon_last_ok_ts = now
    e._recon_check_starved(now + 60)                   # only a minute — fine
    assert e.ledger.db.execute(
        "SELECT COUNT(*) FROM failures WHERE why_tag='RECON_STARVED'").fetchone()[0] == 0
    failures._ledger = None


def test_t1_recon_cadence_recorded_and_packed(tmp_path):
    e = _eng(tmp_path)
    from relay_engine.ops import recon_cadence_lines
    base = 1_000_000.0
    for r in ("CLEAN", "DEFERRED", "DEFERRED", "CLEAN"):
        e.record_recon_cycle(r, now=base)
    body = "\n".join(recon_cadence_lines(e.ledger))
    assert "RECON CADENCE" in body and "the sentinel's pulse" in body
    failures._ledger = None


# ── B1 — insufficient-balance rejections scream ──────────────────────────────
def test_b1_insufficient_balance_pages_once_per_window(tmp_path, monkeypatch):
    from relay_engine import venue, gateway as gw_mod
    led = _eng(tmp_path).ledger
    led.snapshot_caps_at_boot()
    led.set_state("last_venue_cash_cents", "2070")
    gw = Gateway(led, surface=None)
    monkeypatch.setattr(config, "live_submit_enabled", lambda: True)
    gw.venue_client = object()

    def _boom(*a, **k):
        raise RuntimeError("HTTP 400 insufficient balance for this order")
    monkeypatch.setattr(venue, "get_balance", lambda c: (100.0, 0.0))
    monkeypatch.setattr(venue, "place_order_maker", _boom)
    from relay_engine.errors import WallRejection
    book = OrderBook(market=TICKER)
    book.apply_snapshot({97: 10}, {1: 10}, ts=1.0)
    for _ in range(3):                                 # 3 polls, same window
        with pytest.raises(WallRejection):
            gw.submit(_f(count=5), book)
    n = led.db.execute(
        "SELECT COUNT(*) FROM failures WHERE why_tag='BALANCE_REJECTED'").fetchone()[0]
    assert n == 1                                      # once per window (market ticker)
    failures._ledger = None


def test_b1_transient_500_does_not_page_balance(tmp_path, monkeypatch):
    from relay_engine import venue
    led = _eng(tmp_path).ledger
    led.snapshot_caps_at_boot()
    gw = Gateway(led, surface=None)
    monkeypatch.setattr(config, "live_submit_enabled", lambda: True)
    gw.venue_client = object()
    monkeypatch.setattr(venue, "get_balance", lambda c: (100.0, 0.0))
    monkeypatch.setattr(venue, "place_order_maker",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("HTTP 500 upstream timeout")))
    from relay_engine.errors import WallRejection
    book = OrderBook(market=TICKER)
    book.apply_snapshot({97: 10}, {1: 10}, ts=1.0)
    with pytest.raises(WallRejection):
        gw.submit(_f(), book)
    assert led.db.execute(
        "SELECT COUNT(*) FROM failures WHERE why_tag='BALANCE_REJECTED'").fetchone()[0] == 0
    failures._ledger = None


def test_b1_classifier_keys_on_insufficient_only():
    assert Gateway._is_balance_error("HTTP 400 insufficient_balance") is True
    assert Gateway._is_balance_error("insufficient funds available") is True
    assert Gateway._is_balance_error("HTTP 500 internal error") is False
    assert Gateway._is_balance_error("HTTP 429 rate limited") is False
    assert Gateway._is_balance_error("post only would cross") is False


# ── B2 — the sanity clamp: never spend ghost cash ────────────────────────────
def test_b2_cost_over_venue_cash_defers(tmp_path, monkeypatch):
    e = _eng(tmp_path)
    monkeypatch.setattr(config, "live_submit_enabled", lambda: True)
    e.ledger.set_state("last_venue_cash_cents", "2070")     # venue: $20.70 real
    e.ledger.set_state("last_venue_cash_ts", str(time.time()))
    p = _f(count=30)                                         # 30×97 = 2910c > 2070c
    p.count = 30
    e._cash_sanity_clamp(p)
    assert p.count == 0                                      # deferred — no ghost spend
    rows = e.ledger.db.execute(
        "SELECT detail FROM surface_rows WHERE detail LIKE '%CASH_SANITY%'").fetchall()
    assert rows
    failures._ledger = None


def test_b2_within_venue_cash_is_a_no_op(tmp_path, monkeypatch):
    e = _eng(tmp_path)
    monkeypatch.setattr(config, "live_submit_enabled", lambda: True)
    e.ledger.set_state("last_venue_cash_cents", "2070")
    e.ledger.set_state("last_venue_cash_ts", str(time.time()))
    p = _f(count=10)                                         # 10×97 = 970c < 2070c
    p.count = 10
    e._cash_sanity_clamp(p)
    assert p.count == 10                                     # fits real cash — untouched
    failures._ledger = None


def test_b2_stale_confirmation_defers_and_pages(tmp_path, monkeypatch):
    e = _eng(tmp_path)
    monkeypatch.setattr(config, "live_submit_enabled", lambda: True)
    e.ledger.set_state("last_venue_cash_cents", "99999")    # plenty of cash…
    e.ledger.set_state("last_venue_cash_ts",
                       str(time.time() - config.CASH_CONFIRM_MAX_AGE_S - 100))  # …but stale
    p = _f(count=1)
    p.count = 1
    e._cash_sanity_clamp(p)
    assert p.count == 0                                      # blind is not solvent
    assert e.ledger.db.execute(
        "SELECT COUNT(*) FROM failures WHERE why_tag='CASH_STALE'").fetchone()[0] == 1
    failures._ledger = None


def test_v_data_questions_registered():
    from relay_engine import registry
    for s in ("RECON_CADENCE", "BALANCE_REJECTED"):
        q = registry.get(s)
        assert q is not None and q.is_complete()
        assert s in registry.SEED_SURFACES


def test_v_constants_tagged_new():
    changed = {t.name for t in config.changed_constants()}
    assert "RECON_MAX_QUIET_S" in changed
    assert "CASH_CONFIRM_MAX_AGE_S" in changed
