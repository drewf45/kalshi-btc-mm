"""WO-2026-07-21-B (build 54) — HOT FIX: the reboot bypassed the 4-minute hold.

  Finding 1 (ROOT): the self-heal rebuilt an adopted (reboot-orphan) record with
    fill_ts=0.0 → age = now − 0.0 ≈ 56 YEARS → the hard-hold guard evaluated
    False and the 4-minute hold (+ the poll resets) was BYPASSED on every
    reboot. Fix: recover the real fill_ts; if unrecoverable, fail SAFE (fill_ts
    = now → treat as FRESH, full protection), never 0.0, and PAGE.
  Finding 2: FLOOR_BREACH fired on a correctly-bounded 8c salvage — the alarm's
    expectation must be pinned to OPEN_SALVAGE_BUDGET_C (A1's budget), and the
    message must quote the live constant, not a stale "−15c".
  Finding 3: don't silence the uncovered page — SPLIT it. The routine 1-lot
    cover-pending state is FLIP_UNCOVERED_EXPECTED (debug); the reboot orphan is
    FLIP_ORPHAN_ADOPTED (page, with recovered entry + fill_ts).
"""

import json
import pytest

from relay_engine import config, failures
from relay_engine.book import OrderBook
from relay_engine.custodian import Custodian
from relay_engine.feed import DegradeLadder
from relay_engine.lane_flip import LaneFlip

TICKER = "KXBTC15M-02JAN251000-T99"
EVENT = TICKER.rsplit("-", 1)[0]
CLOSE = 1_000_000.0


@pytest.fixture
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


def _paged(ledger, tag):
    return ledger.db.execute(
        "SELECT COUNT(*) FROM failures WHERE why_tag=?", (tag,)).fetchone()[0]


# ── Finding 1: the reboot orphan adopts a REAL fill_ts, and the hold holds ──
def test_f1_orphan_adopts_the_real_fill_ts_not_zero(flip, gateway, ledger, funnel):
    """The self-heal recovers the fill TIMESTAMP from the DB — never the 0.0
    that read as ~56 years old and bypassed the 4-minute hold."""
    ledger.record_fill(TICKER, "FLIP", "no", "ENTRY", 36, 1, "PROBE")
    (ts,) = ledger.db.execute(
        "SELECT ts FROM fills WHERE market=? ORDER BY id DESC LIMIT 1",
        (TICKER,)).fetchone()
    w = flip._window(TICKER, CLOSE)
    flip._heal_uncovered(w, TICKER, "no", 1, now=ts + 5)
    o = w.opens["no"]
    assert o["fill_ts"] == ts and o["fill_ts"] != 0.0    # recovered, not the 56yr default
    assert o["entry"] == 36                               # entry recovered too


def test_f1_reboot_orphan_honors_the_4_minute_hold(flip, gateway, ledger, funnel):
    """The whole point: an adopted position filled ~5s ago does NOT exit on the
    first poll — the hard-hold is honored across the restart (was bypassed)."""
    ledger.record_fill(TICKER, "FLIP", "no", "ENTRY", 36, 1, "PROBE")
    (ts,) = ledger.db.execute(
        "SELECT ts FROM fills WHERE market=? ORDER BY id DESC LIMIT 1",
        (TICKER,)).fetchone()
    w = flip._window(TICKER, CLOSE)
    flip._heal_uncovered(w, TICKER, "no", 1, now=ts + 5)   # 5s old
    o = w.opens["no"]
    o["take_proposed"] = True                              # take already resting
    b = OrderBook(market=TICKER)
    b.apply_snapshot({72: 10}, {24: 10}, ts=1.0)           # held-side (no) bid 24: would cut if armed
    ctx = {"book": b, "now": ts + 5, "close_ts": ts + 705, "spot": None,
           "grain": None, "spotlead": None}
    props = flip._open_custody(w, TICKER, EVENT, b, ctx, 700, ts + 5)
    assert [p for p in props if p.purpose in ("CUT", "EXIT")] == []  # HELD, not cut


def test_f1_unrecoverable_ts_fails_safe_and_pages(flip, gateway, ledger, funnel):
    """No recoverable timestamp → fill_ts = now (FRESH, full hold), NEVER 0.0,
    and FLIP_ORPHAN_ADOPTED pages (unknown age is never silent)."""
    w = flip._window(TICKER, CLOSE)
    now = 1_700_000_000.0
    flip._heal_uncovered(w, TICKER, "no", 1, now=now)      # no fills row
    assert w.opens["no"]["fill_ts"] == now                 # fresh, not 0.0
    assert _paged(ledger, "FLIP_ORPHAN_ADOPTED") == 1
    assert any("FLIP_ORPHAN_ADOPTED" in a for a in funnel)


# ── Finding 2: FLOOR_BREACH no longer fires on a correct salvage ────────────
def test_f2_bounded_salvage_does_not_trip_floor_breach(flip, ledger, surface,
                                                        funnel):
    """A perfectly-bounded 8c salvage (exactly OPEN_SALVAGE_BUDGET_C) on a 36c
    entry is OK — the breach test is pinned to the budget, not entry−35."""
    flip._log_swing_outcome(TICKER, 36, 28, 1000.0, 900.0)   # exit 28, loss 8
    (d,) = ledger.db.execute(
        "SELECT detail FROM surface_rows WHERE state='FLIP_LOSER_CUT'"
        " ORDER BY id DESC LIMIT 1").fetchone()
    row = json.loads(d)
    assert row["ok"] is True and row["loss_cents"] == 8
    assert row["floor_expected"] == config.OPEN_SALVAGE_BUDGET_C == 8
    assert _paged(ledger, "FLIP_FLOOR_BREACH") == 0          # no false positive


def test_f2_a_genuine_over_budget_cut_still_breaches(flip, ledger, surface, funnel):
    """The alarm still means something: a cut past budget+slip still fires, and
    the message quotes the LIVE budget constant (not a stale −15c)."""
    flip._log_swing_outcome(TICKER, 40, 20, 1000.0, 900.0)   # loss 20 > 8+5
    assert _paged(ledger, "FLIP_FLOOR_BREACH") == 1
    msg = next(a for a in funnel if "FLIP_FLOOR_BREACH" in a)
    assert f"{config.OPEN_SALVAGE_BUDGET_C}c salvage assumption" in msg
    assert "-15c" not in msg and "−15c" not in msg


# ── Finding 3: split the tag — routine debug, orphan pages ──────────────────
def test_f3_orphan_pages_with_entry_and_fill_ts(flip, gateway, ledger, funnel):
    w = flip._window(TICKER, CLOSE)
    flip._heal_uncovered(w, TICKER, "no", 1, now=1_700_000_000.0)
    msg = next(a for a in funnel if "FLIP_ORPHAN_ADOPTED" in a)
    assert "in-memory record" in msg and "fill_ts" in msg
