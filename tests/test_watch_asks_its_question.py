"""WO-2026-07-26-R — THE WATCH ASKS ITS OWN QUESTION.

The post-entry orientation watch used to halt entries on ANY >3¢ offset between
our orderbook read and a fresh market-record read — two venue endpoints on
different clocks. Its PURPOSE is catching orientation inversion (reading the
book upside-down), and the codebase already owns the right detector
(_mirror_signature). On quiet books the summary endpoint lags the orderbook by a
spread routinely, so a small offset was freshness noise wearing an orientation
alarm — tonight's two ORIENTATION_DIVERGENCE halts (y30-vs-y26) were exactly
that. This WO narrows the halt to inversion OR a gross non-mirror gap; a small
sub-gross offset demotes to BOOK_STALE info + a resync request, never a halt.

Four acceptance criteria, each a test.
"""

import json

import pytest

from relay_engine import config, failures

TICKER = "KXBTC15M-02JAN251000-T99"


@pytest.fixture
def engine(tmp_path):
    from relay_engine.shadow_runner import ShadowEngine
    e = ShadowEngine(db_path=str(tmp_path / "watch.db"))
    e.boot()
    e.telegram_sent = []
    e.telegram.send = e.telegram_sent.append
    failures.configure(e.ledger, alert_fn=e.telegram.alert, run_mode="TEST",
                       boot_id=1)
    yield e
    failures._ledger = None
    failures._alert_fn = None


def _arm(e, ours_yes_bid, now=1000.0):
    """Snapshot a book whose best yes-bid is `ours` and arm a post-entry watch."""
    e.feed.handle_frame(json.dumps(
        {"type": "orderbook_snapshot",
         "msg": {"market_ticker": TICKER,
                 "yes": [[ours_yes_bid, 10]], "no": [[30, 10]]}}), now=now)
    e.divergence_watches[TICKER] = {"until": now + 30.0, "strikes": 0}


def _book_stale_rows(e):
    return e.ledger.db.execute(
        "SELECT COUNT(*) FROM failures WHERE why_tag='BOOK_STALE'").fetchone()[0]


def _halted(e):
    return "ORIENTATION_DIVERGENCE" in e.gateway.entries_halted_reasons


# ── ACCEPTANCE 1 — tonight's two events: BOOK_STALE, no halt ─────────────────
def test_tonights_y30_vs_y26_is_book_stale_not_a_halt(engine, monkeypatch):
    """ours y30 vs a FRESH record y26 (4¢, sub-gross, non-mirror) is endpoint
    lag — it demotes to BOOK_STALE + a resync request, entries CONTINUE, even
    across three consecutive checks (the old code halted at strike 3)."""
    _arm(engine, 30)
    monkeypatch.setattr(engine, "_fresh_record_touches", lambda m: (26, 28))
    # arm one watch and hit it three consecutive times on the same object
    engine.divergence_watches[TICKER] = {"until": 2000.0, "strikes": 0}
    for _ in range(3):
        engine.process_divergence_watches(object(), now=1010.0)
    assert not _halted(engine), "a 4¢ endpoint lag must NOT halt entries"
    assert _book_stale_rows(engine) == 3          # each check logged, both values
    assert TICKER in engine.feed.resync_needed    # resync requested
    # strikes never accumulated toward a halt
    assert engine.divergence_watches.get(TICKER, {}).get("strikes", 0) == 0


# ── ACCEPTANCE 2 — a real inversion still HALTS (x3), recovery intact ────────
def test_synthetic_inversion_halts_x3_and_arms_recovery(engine, monkeypatch):
    """ours y30 vs record y70 → the mirror signature (100−70=30, exact) → an
    INVERSION halt after 3 strikes; the auto-recovery ceiling is armed (STUCK
    page reachable)."""
    _arm(engine, 30)
    monkeypatch.setattr(engine, "_fresh_record_touches", lambda m: (70, 72))
    engine.divergence_watches[TICKER] = {"until": 2000.0, "strikes": 0}
    engine.process_divergence_watches(object(), now=1010.0)
    engine.process_divergence_watches(object(), now=1011.0)
    assert not _halted(engine)                    # not yet — needs 3
    engine.process_divergence_watches(object(), now=1012.0)
    assert _halted(engine)                        # inversion caught
    assert engine._orientation_halt_market == TICKER
    assert engine._orientation_halt_ts is not None  # ceiling armed → STUCK reachable
    assert _book_stale_rows(engine) == 0          # never a BOOK_STALE
    assert any("INVERSION" in m for m in engine.telegram_sent)


def test_inversion_auto_recovers_on_a_clean_fresh_read(engine, monkeypatch):
    """The recovery path is intact: once a fresh read agrees, the halt clears."""
    _arm(engine, 30)
    monkeypatch.setattr(engine, "_fresh_record_touches", lambda m: (70, 72))
    engine.divergence_watches[TICKER] = {"until": 2000.0, "strikes": 0}
    for t in (1010.0, 1011.0, 1012.0):
        engine.process_divergence_watches(object(), now=t)
    assert _halted(engine)
    # a fresh record now AGREES with ours (30 vs 30) → auto-resume
    monkeypatch.setattr(engine, "_fresh_record_touches", lambda m: (30, 32))
    engine.process_divergence_watches(object(), now=1020.0)
    assert not _halted(engine)
    assert engine._orientation_halt_market is None


# ── ACCEPTANCE 3 — a gross non-mirror gap HALTS (unknown-unknown) ────────────
def test_gross_non_mirror_divergence_halts(engine, monkeypatch):
    """ours y30 vs record y50 → offset 20¢ ≥ gross, NOT a mirror (100−50=50 is
    20¢ off, not ≤3) → a GROSS halt after 3 strikes (the unknown-unknown catch,
    not endpoint lag)."""
    _arm(engine, 30)
    monkeypatch.setattr(engine, "_fresh_record_touches", lambda m: (50, 52))
    engine.divergence_watches[TICKER] = {"until": 2000.0, "strikes": 0}
    for t in (1010.0, 1011.0, 1012.0):
        engine.process_divergence_watches(object(), now=t)
    assert _halted(engine)
    assert _book_stale_rows(engine) == 0
    assert any("GROSS" in m for m in engine.telegram_sent)


def test_a_sub_gross_non_mirror_offset_is_stale_not_halt(engine, monkeypatch):
    """An 8¢ offset (>3, <15, non-mirror) is still endpoint lag → BOOK_STALE,
    never a halt (the boundary between demote and halt)."""
    _arm(engine, 40)
    monkeypatch.setattr(engine, "_fresh_record_touches", lambda m: (32, 34))
    engine.divergence_watches[TICKER] = {"until": 2000.0, "strikes": 0}
    for t in (1010.0, 1011.0, 1012.0):
        engine.process_divergence_watches(object(), now=t)
    assert not _halted(engine)
    assert _book_stale_rows(engine) == 3


# ── ACCEPTANCE 4 — pack by hour, constants tagged, mirror detector reused ────
def test_pack_counts_book_stale_by_hour(engine, monkeypatch):
    from relay_engine.ops import book_stale_by_hour
    _arm(engine, 30)
    monkeypatch.setattr(engine, "_fresh_record_touches", lambda m: (26, 28))
    engine.divergence_watches[TICKER] = {"until": 2000.0, "strikes": 0}
    engine.process_divergence_watches(object(), now=1010.0)
    lines = book_stale_by_hour(engine.ledger)
    body = "\n".join(lines)
    assert "BOOK_STALE" in body and "by UTC hour" in body
    assert "1 demoted read" in body


def test_both_constants_tagged_and_new_this_deploy():
    names = {t.name for t in config.constant_tags()}
    assert "BOOK_STALE_OFFSET_C" in names
    assert "ORIENTATION_GROSS_DIVERGENCE_C" in names
    changed = {t.name for t in config.changed_constants()}
    assert "BOOK_STALE_OFFSET_C" in changed
    assert "ORIENTATION_GROSS_DIVERGENCE_C" in changed


def test_the_watch_reuses_the_existing_mirror_detector():
    """The inversion question is answered by the detector the codebase already
    owned — not a new, divergent copy (Engineer: reuse, don't re-derive)."""
    import inspect
    from relay_engine import shadow_runner
    src = inspect.getsource(shadow_runner.ShadowEngine.process_divergence_watches)
    assert "_mirror_signature" in src
    assert "config.ORIENTATION_GROSS_DIVERGENCE_C" in src
    assert "config.BOOK_STALE_OFFSET_C" in src


def test_book_stale_registered_as_a_data_question():
    from relay_engine import registry
    q = registry.get("BOOK_STALE")
    assert q is not None and q.is_complete()
    assert "BOOK_STALE" in registry.SEED_SURFACES
