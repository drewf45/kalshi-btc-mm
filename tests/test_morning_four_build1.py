"""WO-2026-07-24-D — THE MORNING FOUR, build 1/2 (no exposure change): the
four incomplete fixes, each a correct idea applied to one path and not its
sibling. Parts 2 (stop floor), 3 (orientation auto-recovery + ceiling), 4
(reconcile is instrumented, stale pv invalidated), 5 (wall-storm latch).

HARD RAIL: F byte-identical — none of these touch F sizing/logic (the stop
floor is FLIP's, the recon/orientation guards are engine-wide plumbing)."""

import pytest

from relay_engine import config, failures
from relay_engine.book import OrderBook
from relay_engine.custodian import Custodian
from relay_engine.feed import DegradeLadder
from relay_engine.gateway import Order
from relay_engine.lane_flip import LaneFlip
from relay_engine.shadow_runner import ShadowEngine


@pytest.fixture(autouse=True)
def _single_room_halt_keys():
    # WO-2026-07-27-W P3: this file tests the per-LANE halt mechanic, which is
    # series-agnostic. Pin a single-room roster so halt_scope stays the bare lane
    # (the byte-identical single-room path). Series-scoping (halt_scope keying on
    # {series}:{lane} once the roster grows) is covered in test_ensemble_governor
    # and test_three_rooms. conftest._roster_isolation restores the default after.
    from relay_engine import config
    config.SERIES[:] = ["KXBTC15M"]
    yield


TICKER = "KXBTC15M-02JAN251000-T99"
EVENT = TICKER.rsplit("-", 1)[0]
CLOSE = 1_000_000.0


# ── fixtures ───────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def funnel(ledger):
    failures._warn_last.clear()
    failures.configure(ledger, alert_fn=lambda m: None, run_mode="TEST",
                       boot_id=1)
    yield
    failures._ledger = None


@pytest.fixture
def flip(gateway, ledger, surface):
    return LaneFlip(gateway, custodian=Custodian(gateway, ledger, surface,
                                                 ladder=DegradeLadder()))


@pytest.fixture
def engine(tmp_path):
    e = ShadowEngine(db_path=str(tmp_path / "d.db"))
    e.boot()
    e.telegram_sent = []
    e.telegram.send = e.telegram_sent.append
    failures.configure(e.ledger, alert_fn=e.telegram.alert, run_mode="TEST",
                       boot_id=1)
    yield e
    failures._ledger = None
    failures._alert_fn = None


def _fail_rows(ledger, tag):
    return ledger.db.execute(
        "SELECT COUNT(*) FROM failures WHERE why_tag=?", (tag,)).fetchone()[0]


def _book(yes, no=40):
    b = OrderBook(market=TICKER)
    b.apply_snapshot({yes: 10}, {no: 10}, ts=1.0)
    return b


def _ctx(book, secs):
    return {"book": book, "now": CLOSE - secs, "close_ts": CLOSE,
            "spot": None, "grain": None, "spotlead": None}


def _pos(flip, entry=60):
    w = flip._window(TICKER, CLOSE)
    w.opens.clear()
    now = CLOSE - 700
    w.opens["yes"] = {"entry": entry, "fill_ts": now - 200, "count": 1,
                      "take_oid": None, "take_proposed": True,
                      "collapse_polls": 0, "catastrophe_polls": 0,
                      "det_ts": None, "entry_oid": None, "defer_polls": 0}
    return w.opens["yes"], now


# ── Part 2 (#1): the momentum stop has the floor, and a cross below it counts ─
def test_stop_rests_at_floor_then_crosses_below_with_a_counted_breach(flip, ledger):
    """Acceptance #1: no FLIP exit fills below stop_px − SLIP_TOLERANCE_C without
    a counted FLIP_FLOOR_BREACH. Entry 60, stop 50, floor 47; the book gaps to
    44 (through the floor). Poll 2 rests at the floor (maker EXIT); poll 3
    crosses at the mark and records the breach — the exact loss the unfloored
    stop gave back last night, now bounded-then-counted."""
    o, now = _pos(flip, entry=60)
    floor = 60 - config.OPEN_MOMENTUM_STOP_C - config.SLIP_TOLERANCE_C   # 47
    b = _book(yes=44, no=56)
    args = (flip._window(TICKER, CLOSE), TICKER, EVENT, b, _ctx(b, 700), 700, now)
    flip._open_custody(*args)                                  # poll 1: arm
    p2 = flip._open_custody(*args)                             # poll 2: rest@floor
    assert [p for p in p2 if p.purpose == "CUT"] == []
    rest = [p for p in p2 if p.purpose == "EXIT"]
    assert rest and rest[0].price_cents == floor and not rest[0].crossfire
    p3 = flip._open_custody(*args)                             # poll 3: cross@mark
    cuts = [p for p in p3 if p.purpose == "CUT"]
    assert len(cuts) == 1 and cuts[0].price_cents == 44 and cuts[0].crossfire
    # the breach is COUNTED (never a silent naked dump)
    assert _fail_rows(ledger, "FLIP_FLOOR_BREACH") == 1


def test_stop_within_slip_crosses_at_the_mark_no_breach(flip, ledger):
    """A stop-out WITHIN the slip tolerance (mark 48, floor 47) is not a breach —
    it crosses at the mark on 2 polls exactly as before, no floor rest, no
    FLIP_FLOOR_BREACH. The floor only engages BELOW stop−slip."""
    o, now = _pos(flip, entry=60)                             # stop 50, floor 47
    b = _book(yes=48, no=52)
    args = (flip._window(TICKER, CLOSE), TICKER, EVENT, b, _ctx(b, 700), 700, now)
    flip._open_custody(*args)                                  # poll 1
    p2 = flip._open_custody(*args)                             # poll 2: crosses
    cuts = [p for p in p2 if p.purpose == "CUT"]
    assert len(cuts) == 1 and cuts[0].price_cents == 48
    assert _fail_rows(ledger, "FLIP_FLOOR_BREACH") == 0


# ── Part 3 (#2): orientation recovers on a LIVE market, or pages if stuck ────
def test_orientation_recovers_on_a_live_market_not_the_dead_one(engine, monkeypatch):
    """Acceptance #2: recovery is no longer pinned to the halting market (which
    expires in minutes and gets pruned). A clean read on ANY currently-open
    market auto-resumes — the 161-minute dead-time bug closed."""
    g = engine.gateway
    g.halt_entries("ORIENTATION_DIVERGENCE")
    engine._orientation_halt_market = "KXBTC15M-DEAD"   # expired: not in books
    engine._orientation_halt_ts = 1000.0
    live = "KXBTC15M-02JAN251000-T99"
    engine.feed.book(live).apply_snapshot({46: 10}, {51: 10}, ts=1.0)
    monkeypatch.setattr(engine, "_fresh_record_touches",
                        lambda m: (46, 51) if m == live else None)
    engine.process_divergence_watches(client=None, now=1100.0)
    assert "ORIENTATION_DIVERGENCE" not in g.entries_halted_reasons
    assert engine._orientation_halt_market is None
    assert engine._orientation_halt_ts is None


def test_orientation_halt_stuck_pages_after_the_ceiling(engine, monkeypatch):
    """Acceptance #2 (the other arm): if NO live market reads clean past the
    ceiling, it pages ORIENTATION_HALT_STUCK rather than sitting silent behind a
    dead market forever — the promise the old code structurally could not keep."""
    g = engine.gateway
    g.halt_entries("ORIENTATION_DIVERGENCE")
    engine._orientation_halt_market = "KXBTC15M-DEAD"
    engine._orientation_halt_ts = 100.0
    monkeypatch.setattr(engine, "_fresh_record_touches", lambda m: None)
    engine.process_divergence_watches(
        client=None, now=100.0 + config.ORIENTATION_HALT_MAX_S + 1)
    assert "ORIENTATION_DIVERGENCE" in g.entries_halted_reasons   # still halted
    assert _fail_rows(engine.ledger, "ORIENTATION_HALT_STUCK") == 1


# ── Part 4 (#3/#4): the reconcile is visible, and a stale pv self-invalidates ─
def test_recon_status_line_shows_time_since_clean(engine):
    """Acceptance #3: the hourly carries recon_ok=<seconds> — a stale reconcile
    is visible without a log grep. '—' until the first clean cross-check."""
    assert "recon_ok=—" in engine.recon_status_line(now=5000.0)
    engine._recon_last_ok_ts = 4000.0
    engine._recon_deferred_streak = 3
    line = engine.recon_status_line(now=5000.0)
    assert "recon_ok=1000s" in line and "recon_deferred=3" in line


def test_failed_account_read_invalidates_the_pv(engine, monkeypatch):
    """Acceptance #4 (the source): a failed account_value read INVALIDATES the pv
    (sets it None) instead of preserving a stale non-zero number that pins the
    reconcile's boundary gate forever."""
    from relay_engine import venue
    monkeypatch.setattr(config, "live_submit_enabled", lambda: True)
    engine.gateway.venue_client = object()
    engine._last_venue_pv_cents = 98            # a stale read from a prior hold
    monkeypatch.setattr(venue, "get_balance", lambda c: (None, None))
    val, src = engine.account_value(now=2000.0)
    assert val is None and engine._last_venue_pv_cents is None


def test_none_pv_defers_as_no_pv_never_crashes(engine, monkeypatch):
    """Acceptance #4: a None pv defers (RECON_NO_PV) — distinguishable from a
    real settlement boundary and never an `abs(None - deployed)` crash."""
    monkeypatch.setattr(config, "live_submit_enabled", lambda: True)
    monkeypatch.setattr(engine, "account_value", lambda now=None: (500, "venue"))
    engine._last_venue_pv_cents = None
    assert engine.standing_reconcile(now=2000.0) == "DEFERRED"


def test_recon_stalled_pages_after_the_streak(engine, monkeypatch):
    """Acceptance #3 (the alarm): a run of un-cross-checked cycles pages
    RECON_STALLED — "nothing pending" can no longer masquerade as "not checked
    in an hour"."""
    from relay_engine import venue
    monkeypatch.setattr(config, "live_submit_enabled", lambda: True)
    engine.gateway.venue_client = object()
    monkeypatch.setattr(venue, "get_balance", lambda c: (None, None))
    for i in range(config.RECON_STALL_STREAK):
        engine.standing_reconcile(now=2000.0 + i * 100)   # each: unreadable
    assert engine._recon_deferred_streak == config.RECON_STALL_STREAK
    assert _fail_rows(engine.ledger, "RECON_STALLED") == 1


def test_a_clean_reconcile_resets_the_stall(engine, monkeypatch):
    """A completed cross-check clears the streak and stamps recon_ok — the stall
    only counts un-cross-checked cycles, not benign quiescence."""
    from relay_engine import venue
    monkeypatch.setattr(config, "live_submit_enabled", lambda: True)
    engine.gateway.venue_client = object()
    monkeypatch.setattr(venue, "get_balance", lambda c: (None, None))
    engine.standing_reconcile(now=2000.0)
    assert engine._recon_deferred_streak == 1
    # now the venue answers cleanly, pv agrees with deployed 0 → a real reconcile
    monkeypatch.setattr(venue, "get_balance", lambda c: (100.0, 0.0))
    engine.standing_reconcile(now=2100.0)
    assert engine._recon_deferred_streak == 0
    assert engine._recon_last_ok_ts == 2100.0


# ── Part 5 (#6): the closed-gate reject latches per (lane, market) ───────────
def test_entries_halted_reject_latches_until_the_gate_opens(engine, monkeypatch):
    """Acceptance #6: a halted lane re-proposing every poll counts ONCE per
    (lane, market) until the gate opens — hundreds of polls of noise can no
    longer bury a real reject in the hourly `rejects=` field."""
    mkt = "KXBTC15M-ZZ"
    engine.feed.book(mkt).apply_snapshot({48: 100}, {49: 100}, ts=1.0)

    class _Dec:
        multi = None
        pass_reason = "TEST"
        interim = False

        def __init__(self, proposal):
            self.proposal = proposal

    flip_lane = engine.lanes[2]
    assert flip_lane.name == "FLIP"

    def _flip_eval(market, ctx):
        return _Dec(Order(lane="FLIP", event=mkt.rsplit("-", 1)[0], market=market,
                          side="yes", action="buy", price_cents=48, count=1,
                          size_tier=config.TIER_PROBE, purpose="ENTRY",
                          why="OPEN grain yesx2 · join 48c"))

    monkeypatch.setattr(flip_lane, "evaluate", _flip_eval)
    engine.gateway.halt_entries("RATE_HALT:FLIP")     # FLIP's gate closed
    for i in range(4):
        engine.cycle([mkt], now=100.0 + i)
    # four polls into a closed gate → ONE counted reject, not four
    assert engine.gateway.reject_counts.get("ENTRIES_HALTED", 0) == 1
    assert ("FLIP", mkt) in engine._halt_reject_latched
    # the gate opens → the latch releases so the NEXT halt counts fresh
    engine.gateway.resume_entries("RATE_HALT:FLIP")
    engine.cycle([mkt], now=200.0)
    assert ("FLIP", mkt) not in engine._halt_reject_latched


# ── HARD RAIL: F byte-identical (the stop floor is FLIP-only) ────────────────
def test_f_sizing_untouched_by_the_stop_floor():
    from relay_engine import scoring
    f = scoring.size_order(4162, 97, 10_000, lane="F")
    assert f.contracts == int(4162 * config.F_NOTIONAL_PCT // 97)
    assert "cap n/a" in f.reason
