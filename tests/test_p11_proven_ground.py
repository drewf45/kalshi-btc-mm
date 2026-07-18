"""WO-P11 "PROVEN GROUND" + P11.1 fold-ins — the REST reversion, kept honest:
(a) RestFeed is a FeedLike drop-in (the runner never rewired);
(b) the token-bucket governor paces ALL REST calls, budget in the boot tape;
(c) the recorder format is the synthesized snapshot (replay-compatible by
    construction) and transport stamps ride surface rows + brackets;
(d) a failed fetch is NO book — tagged, counted, skipped, never a stale
    fabrication; custody's grace ends at the staleness bound; the ws path is
    import-inert under the flag.
"""

import ast
import json
import time as _time
from pathlib import Path

import pytest

import relay_engine.shadow_runner as shadow_runner_module
from relay_engine import config, failures
from relay_engine.feed import DegradeLadder, Feed
from relay_engine.rest_feed import RestFeed
from relay_engine.shadow_runner import ShadowEngine

TICKER = "KXBTC15M-02JAN251000-T99"


@pytest.fixture
def engine(tmp_path):
    e = ShadowEngine(db_path=str(tmp_path / "p11.db"))
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


RAW_BOOK = {"yes": [["0.4550", "100.00"], ["0.44", "50.00"]],
            "no": [["0.3025", "80.00"]]}


# ── P11.1-a: the FeedLike drop-in ──────────────────────────────────────────
def test_restfeed_is_the_feedlike_surface(engine):
    """The runner's 8+ Feed touchpoints all exist because RestFeed IS a Feed:
    books/book/drop_book/resync_needed/on_poison/frames_seen/handle_frame."""
    assert isinstance(engine.feed, RestFeed) and isinstance(engine.feed, Feed)
    for attr in ("books", "book", "drop_book", "resync_needed", "on_poison",
                 "frames_seen", "handle_frame", "poll_market",
                 "fetch_failed", "failed_fetches"):
        assert hasattr(engine.feed, attr), attr


def test_poll_builds_book_through_the_proven_pipeline(engine, monkeypatch):
    """One poll → the synthesized snapshot rides handle_frame: units asserted,
    fp retained (true touch), frames counted, recorder banked (P11.1-c)."""
    from relay_engine import venue
    monkeypatch.setattr(venue, "fetch_orderbook_raw", lambda c, m: dict(RAW_BOOK))
    assert engine.feed.poll_market(object(), TICKER, now=1000.0) is True
    book = engine.feed.books[TICKER]
    assert book.best_yes_bid() == 45 and book.best_no_bid() == 30
    assert book.best_fp("yes") == "0.4550"          # the parts' true-touch law
    assert book.has_snapshot and not book.poisoned
    assert engine.feed.book_units == "dollars"
    assert engine.feed.frames_seen == 1
    # the recorder format IS the synthesized snapshot — replay-compatible
    engine.recorder.flush()
    from relay_engine.replay import replay_from_db
    out = replay_from_db(engine.ledger)
    assert out and out[0][1] == "yes" and out[0][2] == 45.5


def test_poll_incoherent_rest_book_still_trips_the_invariant(engine, monkeypatch):
    """The coherence tripwire rides along: a crossed REST book (sum 152)
    poisons exactly as a crossed WS book would."""
    from relay_engine import venue
    monkeypatch.setattr(venue, "fetch_orderbook_raw",
                        lambda c, m: {"yes": [["0.97", "5.00"]],
                                      "no": [["0.55", "5.00"]]})
    engine.feed.poll_market(object(), TICKER, now=1000.0)
    assert engine.feed.books[TICKER].poisoned is True
    assert any("BOOK_INCOHERENT" in m for m in engine.telegram_sent)


# ── P11.1-d: a failed fetch is NO book, never a fabrication ────────────────
def test_failed_fetch_no_book_tagged_counted(engine, monkeypatch):
    from relay_engine import venue
    monkeypatch.setattr(venue, "fetch_orderbook_raw", lambda c, m: dict(RAW_BOOK))
    engine.feed.poll_market(object(), TICKER, now=1000.0)
    monkeypatch.setattr(venue, "fetch_orderbook_raw", lambda c, m: None)
    assert engine.feed.poll_market(object(), TICKER, now=1001.0) is False
    assert TICKER in engine.feed.fetch_failed
    assert engine.feed.failed_fetches == 1
    assert fail_rows(engine, "BOOK_FETCH_FAILED") == 1
    # the OLD book is untouched — old timestamp, no fabricated freshness
    assert engine.feed.books[TICKER].last_update_ts == 1000.0

    # lanes skip the market this cycle, tagged
    engine.market_meta[TICKER] = {"close_ts": 1101.0 + 120,
                                  "boundary_lo": None, "boundary_hi": None}
    engine.cycle([TICKER], now=1101.0)
    assert engine.gateway.shadow_orders == []
    n = engine.ledger.db.execute(
        "SELECT COUNT(*) FROM surface_rows WHERE detail='BOOK_FETCH_FAILED'"
        " AND market=?", (TICKER,)).fetchone()[0]
    assert n >= 1

    # recovery: the next successful poll clears the flag and lanes evaluate
    monkeypatch.setattr(venue, "fetch_orderbook_raw", lambda c, m: dict(RAW_BOOK))
    engine.feed.poll_market(object(), TICKER, now=1102.0)
    assert TICKER not in engine.feed.fetch_failed


def test_custody_grace_ends_at_the_staleness_bound(engine, monkeypatch):
    """Within the bound custody reads the last book; beyond it custody marks
    DEFER (the custodian receives no book and holds) — P9's deferral shape
    applied to books."""
    from relay_engine import venue
    from relay_engine.custodian import OpenPosition
    monkeypatch.setattr(venue, "fetch_orderbook_raw", lambda c, m: dict(RAW_BOOK))
    engine.feed.poll_market(object(), TICKER, now=1000.0)
    monkeypatch.setattr(venue, "fetch_orderbook_raw", lambda c, m: None)
    engine.feed.poll_market(object(), TICKER, now=1001.0)   # fetch now failing
    engine.custodian.adopt(OpenPosition(
        event=TICKER.rsplit("-", 1)[0], market=TICKER, lane="FLIP", side="yes",
        count=1, entry_price_cents=45, entry_p_win=0.45,
        size_tier=config.TIER_PROBE, entry_time=999.0))
    engine.market_meta[TICKER] = {"close_ts": 2000.0,
                                  "boundary_lo": None, "boundary_hi": None}
    seen = {}
    real_tick = engine.custodian.tick

    def spy_tick(books, **kw):
        seen["books"] = dict(books)
        return real_tick(books=books, **kw)
    monkeypatch.setattr(engine.custodian, "tick", spy_tick)

    # within the 30s bound: the last book still serves custody
    engine.cycle([TICKER], now=1010.0)
    assert TICKER in seen["books"]
    # beyond the bound: custody marks defer — no book key at all
    engine.cycle([TICKER], now=1000.0 + config.REST_BOOK_STALE_CUSTODY_S + 5)
    assert TICKER not in seen["books"]
    assert f"{TICKER}:FLIP" in engine.custodian.positions   # held, not guessed


def test_ws_path_import_inert_under_the_flag():
    """P11.1-d: with WS_ENABLED=false (the default), websockets is never
    imported at module level — the ws dialect is shelved, not lurking."""
    assert config.WS_ENABLED is False   # the A3 default
    src = Path(shadow_runner_module.__file__).read_text()
    tree = ast.parse(src)
    top_level_imports = {
        n.names[0].name.split(".")[0]
        for n in tree.body if isinstance(n, (ast.Import,))}
    top_level_imports |= {
        (n.module or "").split(".")[0]
        for n in tree.body if isinstance(n, ast.ImportFrom)}
    assert "websockets" not in top_level_imports


# ── P11.1-b: the governor around ALL REST calls ────────────────────────────
def test_rest_governor_paces_never_storms():
    from relay_engine.venue import _RestGovernor
    gov = _RestGovernor(capacity=2, refill_per_s=100.0)
    t0 = _time.monotonic()
    for _ in range(6):
        gov.pace()
    elapsed = _time.monotonic() - t0
    # 2 free tokens, then 4 paced at 100/s => >= ~0.04s of enforced waiting
    assert elapsed >= 0.03
    assert gov.tokens < 1.0


def test_governor_wraps_the_request_core_and_boot_tape_prints_it():
    import inspect

    from relay_engine import venue
    from relay_engine.boot import boot_tape
    src = inspect.getsource(venue.KalshiClient.request)
    assert "REST_GOVERNOR.pace()" in src   # every attempt takes a token
    tape = "\n".join(boot_tape())
    assert f"REST GOVERNOR: bucket={config.REST_BUCKET_CAPACITY}" in tape
    assert "TRANSPORT: REST 1s" in tape
    assert "A3 PROVEN GROUND" in tape      # the ruling, printed every boot


# ── P11.1-c: transport stamps on surface rows + brackets ───────────────────
def test_transport_stamps_rest(engine, monkeypatch):
    from relay_engine import venue
    monkeypatch.setattr(venue, "fetch_orderbook_raw", lambda c, m: dict(RAW_BOOK))
    engine.feed.poll_market(object(), TICKER, now=1000.0)
    engine.market_meta[TICKER] = {"close_ts": 1000.0 + 120,
                                  "boundary_lo": None, "boundary_hi": None}
    engine.cycle([TICKER], now=1000.0)
    transports = {t for (t,) in engine.ledger.db.execute(
        "SELECT DISTINCT transport FROM surface_rows WHERE market=?",
        (TICKER,)).fetchall()}
    assert transports == {"REST"}
    # brackets carry the stamp too
    engine.econ.open_bracket(TICKER, 10_000, now=1000.0, source="paper",
                             transport="REST")
    row = engine.ledger.db.execute(
        "SELECT transport FROM window_econ WHERE market=?", (TICKER,)).fetchone()
    assert row == ("REST",)


def test_pack_names_the_transport_and_the_fill_lag(engine):
    from relay_engine.ops import daily_pack
    pack = daily_pack(engine.ledger, engine.surface, engine.cash, econ=engine.econ)
    assert "TRANSPORT: REST 1s" in pack
    assert "not a bug" in pack


# ── the drop-in, end to end: poll → cycle → verdicts (runner unrewired) ────
def test_rest_poll_all_drives_the_cycle(engine, monkeypatch):
    from relay_engine import venue
    monkeypatch.setattr(venue, "fetch_orderbook_raw", lambda c, m: dict(RAW_BOOK))
    engine.market_meta[TICKER] = {"close_ts": _time.time() + 120,
                                  "boundary_lo": None, "boundary_hi": None}
    assert engine.rest_poll_all(object()) == 1
    engine.cycle(sorted(engine.market_meta))
    # a 45/30 book is not F's territory: the PASS is the discipline working
    states = {s for (s,) in engine.ledger.db.execute(
        "SELECT DISTINCT state FROM surface_rows WHERE lane='F' AND market=?",
        (TICKER,)).fetchall()}
    assert states & {"PASS", "WATCHING"}
