"""WO-P13 "SAY WHAT YOU DID" — narration grammar (§1), the field→form map
with the docs' own payloads as fixtures (§2), orientation sentinels (§3),
FLIP pair integrity + sit-out visibility (§4), plus the tape-0718 hygiene
fixes (foreign dedup, ephemeral-DB warning)."""

import json

import pytest

from relay_engine import config, failures
from relay_engine.book import OrderBook
from relay_engine.errors import FatalIntegrityError, WallRejection
from relay_engine.gateway import Order
from relay_engine.shadow_runner import ShadowEngine
from relay_engine.venue import parse_fill

TICKER = "KXBTC15M-02JAN251000-T99"
EVENT = TICKER.rsplit("-", 1)[0]


@pytest.fixture
def engine(tmp_path):
    e = ShadowEngine(db_path=str(tmp_path / "p13.db"))
    e.boot()
    e.telegram_sent = []
    e.telegram.send = e.telegram_sent.append
    failures.configure(e.ledger, alert_fn=e.telegram.alert, run_mode="TEST", boot_id=1)
    yield e
    failures._ledger = None
    failures._alert_fn = None


# ── §2a: FIELD → FORM, explicit — the boundary cases that kill the heuristic
def test_dollars_one_dollar_is_100_cents():
    """The heuristic's fatal case: a dollars '1.00' parsed as 1c. Dead."""
    cost, fee, count = parse_fill(
        {"no_price_dollars": "1.00", "count": 1}, our_side="no")
    assert cost == 100.0


def test_legacy_integer_keys_are_cents():
    cost, _, _ = parse_fill({"no_price": 46, "count": 1}, our_side="no")
    assert cost == 46.0
    cost, _, _ = parse_fill({"yes_price": 97}, our_side="yes")
    assert cost == 97.0


def test_dollars_keys_are_dollars():
    cost, _, _ = parse_fill({"no_price_dollars": "0.46"}, our_side="no")
    assert cost == 46.0


def test_side_correct_complement_fallback():
    """Our no-side fill carrying only the yes price: complement, form-mapped."""
    cost, _, _ = parse_fill({"yes_price_dollars": "0.54"}, our_side="no")
    assert cost == 46.0


def test_fee_forms_explicit():
    _, fee, _ = parse_fill({"no_price_dollars": "0.46",
                            "fee_cost": "0.070000"}, our_side="no")
    assert fee == 7          # tape-0709 dollars-fp form
    _, fee2, _ = parse_fill({"no_price": 46, "fee": 2}, our_side="no")
    assert fee2 == 2         # legacy cents form


# ── §2b: the wire's own payloads, verbatim (live tape 0718 / 0709) ─────────
def test_fixture_live_0718_fill_shape():
    """The 0715-15 window's fill, as the wire speaks it: no@46, count_fp."""
    record = {"fill_id": "t0718-1",
              "order_id": "9739039b-c79e-46b7-9120-4f87a488b1cf",
              "no_price_dollars": "0.4600", "yes_price_dollars": "0.5400",
              "count_fp": "1.00", "fee_cost": "0.000000"}
    cost, fee, count = parse_fill(record, our_side="no")
    assert (cost, fee, count) == (46.0, 0, 1)


def test_fixture_orderbook_fp_example(engine, monkeypatch):
    """The orderbook_fp doc example through the REST feed: fp arrays, bids
    only, ascending, best last."""
    from relay_engine import venue
    payload = {"yes": [["0.44", "50.00"], ["0.97", "5.00"]],
               "no": [["0.02", "10.00"]]}
    monkeypatch.setattr(venue, "fetch_orderbook_raw", lambda c, m: payload)
    engine.feed.poll_market(object(), TICKER, now=1.0)
    book = engine.feed.books[TICKER]
    assert book.best_yes_bid() == 97 and book.best_no_bid() == 2
    assert book.best_fp("yes") == "0.97"


# ── §1: fill pages say WHAT and WHY ────────────────────────────────────────
def entry_order(why="pair-post ≤49 · y46/n54"):
    return Order(lane="FLIP", event=EVENT, market=TICKER, side="no",
                 action="buy", price_cents=46, count=1,
                 size_tier=config.TIER_PROBE, purpose="ENTRY", why=why)


def test_entry_page_names_the_why(engine):
    engine._on_fill_booked(entry_order(), "ENTRY", 46, 1, 1000.0, 0)
    page = [m for m in engine.telegram_sent if m.startswith("✅ ENTRY")][0]
    assert "FLIP" in page and "buy no@46¢ x1" in page
    assert "why: pair-post ≤49" in page


def test_exit_page_names_reason_fee_and_the_net_line(engine):
    """The 0715-15 scratch, re-narrated: sell@42 after entry@46, fee 2¢. WO-H
    "ONE POSITION, ONE STORY": the ↔ line reads the POSITION — a full round-trip
    prints CLOSED with the blended basis and the position net, not a fill pair."""
    gw = engine.gateway
    en = Order(lane="FLIP", event=EVENT, market=TICKER, side="no", action="buy",
               price_cents=46, count=1, size_tier=config.TIER_PROBE,
               purpose="ENTRY", why="OPEN grain nox2 · join 46c")
    gw.order_index["E"] = en
    gw.resting["E"] = en
    gw.on_fill("E")                              # gateway tracks: gross 1, basis 46
    engine.ledger.record_fill(TICKER, "FLIP", "no", "ENTRY", 46, 1, "PROBE")
    exit_order = Order(lane="FLIP", event=EVENT, market=TICKER, side="no",
                       action="sell", price_cents=42, count=1,
                       size_tier=config.TIER_PROBE, purpose="CUT",
                       reason="PER_CONTRACT_STOP")
    gw.order_index["X"] = exit_order
    gw.resting["X"] = exit_order
    gw.on_fill("X", fee_cents=2)                 # concludes the position (flat)
    engine._on_fill_booked(exit_order, "CUSTODIAN_EXIT", 42, 1, 1001.0, 2)
    exit_page = [m for m in engine.telegram_sent if m.startswith("✂️ EXIT")][0]
    assert "sell no@42¢ x1 (fee 2¢) — PER_CONTRACT_STOP" in exit_page
    net_page = [m for m in engine.telegram_sent if m.startswith("↔")][0]
    assert "ROUND-TRIP CLOSED x1 basis 46¢" in net_page
    assert "net -6¢" in net_page                 # (42−46)*1 − 2 fee = −6c


def test_a_mute_fill_cannot_exist(engine):
    """Every booked fill page is ENTRY or EXIT — the old ✅ FILL grammar is gone."""
    engine._on_fill_booked(entry_order(why=""), "ENTRY", 46, 1, 1000.0, 0)
    engine.ledger.record_fill(TICKER, "FLIP", "no", "ENTRY", 46, 1, "PROBE")
    engine._on_fill_booked(
        Order(lane="FLIP", event=EVENT, market=TICKER, side="no",
              action="sell", price_cents=50, count=1,
              size_tier=config.TIER_PROBE, purpose="EXIT",
              reason="take entry+4"), "EXIT", 50, 1, 1001.0, 0)
    fills_pages = [m for m in engine.telegram_sent
                   if m.startswith(("✅", "✂️"))]
    assert fills_pages and all(("ENTRY" in m) or ("EXIT" in m)
                               for m in fills_pages)
    assert not any(m.startswith("✅ FILL") for m in engine.telegram_sent)


# ── §4: FLIP pair integrity + sit-out visibility ───────────────────────────
def make_book(yes=46, no=46):
    b = OrderBook(market=TICKER)
    b.apply_snapshot({yes: 100}, {no: 80}, ts=1.0)
    return b


def test_second_same_side_flip_entry_rejected(gateway):
    gateway.positions[(EVENT, TICKER, "FLIP")] = -1   # holding a no leg
    with pytest.raises(WallRejection) as e:
        gateway.submit(Order(lane="FLIP", event=EVENT, market=TICKER,
                             side="no", action="buy", price_cents=46, count=1,
                             size_tier=config.TIER_PROBE, purpose="ENTRY",
                             band=(1, 49)), make_book())
    assert e.value.wall == "FLIP_UNPAIRED"
    # the OPPOSITE side nets toward flat — risk-reducing at the canonical
    # layer, walls skipped entirely: the pair is legitimate by construction
    r = gateway.submit(Order(lane="FLIP", event=EVENT, market=TICKER,
                             side="yes", action="buy", price_cents=46, count=1,
                             size_tier=config.TIER_PROBE, purpose="ENTRY",
                             band=(1, 49)), make_book())
    assert r.order_id


def test_flip_sitout_pages_once(engine):
    from relay_engine.lane_flip import FLIP_SCRATCH_SITOUT
    lane = engine.flip
    close_ts = 1_000_000.0
    w = lane._window(TICKER, close_ts)
    w.scratches = FLIP_SCRATCH_SITOUT
    ctx = {"book": make_book(), "now": close_ts - 500, "close_ts": close_ts}
    lane.evaluate(TICKER, ctx)
    lane.evaluate(TICKER, ctx)   # second cycle: no repeat page
    pages = [m for m in engine.telegram_sent if m.startswith("🚪")]
    assert len(pages) == 1 and f"{FLIP_SCRATCH_SITOUT} scratches" in pages[0]


# ── §3: orientation sentinels ──────────────────────────────────────────────
def test_boot_selftest_fatal_on_mirror(engine, monkeypatch):
    """ORIENT-1 amended the conviction path: the stale discovery record
    ACCUSES; only a FRESH record convicts (the 12:22:59Z false FATAL).
    Here the fresh record confirms the mirror on both touches — still
    FATAL, as P13 always demanded for a truly inverted book."""
    from relay_engine import venue
    engine.market_meta[TICKER] = {"close_ts": 2000.0, "rec_yes_bid": 97}
    book = OrderBook(market=TICKER)
    book.apply_snapshot({3: 10}, {97: 10}, ts=1.0)   # OUR book is the mirror
    monkeypatch.setattr(venue, "get_market",
                        lambda c, t: {"yes_bid_dollars": "0.97",
                                      "yes_ask_dollars": "0.98"})
    engine.gateway.venue_client = object()
    with pytest.raises(FatalIntegrityError):
        engine.orientation_selftest(TICKER, book)
    assert engine.ledger.db.execute(
        "SELECT COUNT(*) FROM failures WHERE why_tag='ORIENTATION_MIRROR'"
    ).fetchone()[0] == 1


def test_boot_selftest_passes_honest_book(engine):
    engine.market_meta[TICKER] = {"close_ts": 2000.0, "rec_yes_bid": 97}
    book = OrderBook(market=TICKER)
    book.apply_snapshot({97: 10}, {2: 10}, ts=1.0)
    engine.orientation_selftest(TICKER, book)        # no raise
    assert engine._orientation_checked is True


def test_settlement_cross_check_flags_suspect(engine):
    engine.feed.handle_frame(json.dumps(
        {"type": "orderbook_snapshot",
         "msg": {"market_ticker": TICKER, "yes": [[30, 10]], "no": [[65, 10]]}}),
        now=1.0)
    engine.econ.open_bracket(TICKER, 10_000, now=1.0)
    engine.settle_traded_market(TICKER, settled_yes=True, now=1000.0)
    assert engine.ledger.db.execute(
        "SELECT COUNT(*) FROM failures WHERE why_tag='ORIENTATION_SUSPECT'"
    ).fetchone()[0] == 1


def test_divergence_watch_halts_after_three_strikes(engine, monkeypatch):
    """WO-HALT-ORPHAN §2C: the strike is cast by a FRESH record (re-pulled),
    never a stale one — a movement-lag stale record can no longer force a
    false halt. Here the fresh record still diverges 7¢, so 3 strikes halt
    (and §1.3: the halt stamps its market for auto-recovery)."""
    engine.feed.handle_frame(json.dumps(
        {"type": "orderbook_snapshot",
         "msg": {"market_ticker": TICKER, "yes": [[45, 10]], "no": [[30, 10]]}}),
        now=1000.0)
    # a FRESH record 7c apart (yes_bid 52 vs ours 45) casts the strike
    monkeypatch.setattr(engine, "_fresh_record_touches",
                        lambda market: (52, 55))
    engine._on_fill_booked(entry_order(), "ENTRY", 46, 1, 1000.0, 0)
    assert TICKER in engine.divergence_watches
    for i in range(3):
        engine.process_divergence_watches(object(), now=1005.0 + i * 10)
    assert "ORIENTATION_DIVERGENCE" in engine.gateway.entries_halted_reasons
    assert any("ORIENTATION_DIVERGENCE" in m for m in engine.telegram_sent)
    assert TICKER not in engine.divergence_watches
    assert engine._orientation_halt_market == TICKER   # §1.3 stamped


# ── tape-0718 hygiene: foreign dedup + ephemeral-DB warning ────────────────
def test_foreign_fills_counted_once_per_unique_id(engine):
    rec = {"fill_id": "foreign-1", "order_id": "NOT-OURS",
           "no_price_dollars": "0.46", "count": 1}
    engine.fills.sweep([rec], now=1000.0)
    engine.fills.sweep([rec], now=1003.0)   # the 3s re-scan
    engine.fills.sweep([rec], now=1006.0)
    assert engine.fills.foreign_seen == 1   # the counter means something again


def test_live_without_persistent_db_pages(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "live_submit_enabled", lambda: True)
    monkeypatch.delenv("RELAY_DB_PATH", raising=False)
    e = ShadowEngine(db_path=str(tmp_path / "eph.db"))
    e.telegram_sent = []
    e.telegram.send = e.telegram_sent.append
    e.boot()
    assert any("RELAY_DB_PATH" in m and "EPHEMERAL" in m
               for m in e.telegram_sent)


# ── CEO knob: tuition itemized in the pack ─────────────────────────────────
def test_pack_itemizes_scratches_and_fees(engine):
    engine.ledger.record_fill(TICKER, "FLIP", "no", "ENTRY", 46, 1, "PROBE")
    engine.ledger.record_fill(TICKER, "FLIP", "no", "CUSTODIAN_EXIT", 42, 1,
                              "PROBE", fee_cents=2)
    # WO-2026-07-24-H: scratches read the POSITION-level cell outcome now (one
    # concluded no@46 → 42 round-trip, net −6 = gross −4 − fee 2). The gross −4
    # is the scratch cost, fees 2 from the fills.
    engine.ledger.record_cell_outcome("OPEN", 46, won=False, pnl_cents=-6,
                                      fees_cents=2, market=TICKER, kind="trip",
                                      contracts=1)
    from relay_engine.ops import daily_pack
    pack = daily_pack(engine.ledger, engine.surface, engine.cash, econ=engine.econ)
    assert "scratches: 1 · scratch cost: 4¢ · fees: 2¢" in pack
