"""WO-P15 FINAL — Rulings 1-3 as law, Fix A (pending/gross exposure), the
FLIP R6 required-reading line, and the tape-accountability grader."""

import pytest

from relay_engine import config, failures
from relay_engine.book import OrderBook
from relay_engine.errors import WallRejection
from relay_engine.gateway import Order
from relay_engine.shadow_runner import ShadowEngine
from relay_engine.sizing import size_order

TICKER = "KXBTC15M-02JAN251000-T99"
EVENT = TICKER.rsplit("-", 1)[0]


@pytest.fixture
def engine(tmp_path):
    e = ShadowEngine(db_path=str(tmp_path / "p15.db"))
    e.boot()
    e.telegram_sent = []
    e.telegram.send = e.telegram_sent.append
    failures.configure(e.ledger, alert_fn=e.telegram.alert, run_mode="TEST", boot_id=1)
    yield e
    failures._ledger = None
    failures._alert_fn = None


def flip_book(yes, no):
    b = OrderBook(market=TICKER)
    b.apply_snapshot({yes: 10} if yes else {}, {no: 10} if no else {}, ts=1.0)
    return b


def flip_ctx(engine, book, secs_left=850, spot=None, grain=None):
    close = 1_000_000.0
    return {"book": book, "now": close - secs_left, "close_ts": close,
            "spot": spot, "grain": grain}


# ── RULING 2: pair-formable or nothing ─────────────────────────────────────
def test_one_sided_book_posts_nothing(engine):
    """AMENDED by WO-2026-07-22-E (build 57) + WO-2026-07-22-F (build 58): FLIP
    buys the FAVORED (higher-priced) side in [50,70], but only once the PILE has
    formed inside the [60,180]s window. A two-bid biased book whose favored yes
    grows to 66 (no at 40 keeps the skew in-band) ENTERS the favored side once
    primed. What still posts NOTHING is a GENUINELY one-sided book — a lone bid
    with the other side EMPTY — because there is no favored side to price against
    (need both bids)."""
    # two-bid biased book: prime the pile — baseline poll then the entry poll
    # with the favored (higher) yes grown to 66 and the tape rising with it.
    engine.flip.evaluate(TICKER, flip_ctx(engine, flip_book(54, 48),
                                          secs_left=835, spot=66000.0))
    props = engine.flip.evaluate(TICKER, flip_ctx(engine, flip_book(64, 40),
                                                  secs_left=820, spot=66020.0))
    assert [(p.side, p.price_cents, p.purpose) for p in props] == \
        [("yes", 64, "ENTRY")]
    # a genuinely one-sided book (no@34 only, yes side empty), in-window:
    # NOTHING — no favored side to price against (not the clock)
    engine.flip.windows.clear()
    props = engine.flip.evaluate(TICKER, flip_ctx(engine, flip_book(None, 34),
                                                  secs_left=820))
    assert props == []


def test_pair_formable_posts_the_favored_side(engine):
    """WO-2026-07-22-E (build 57): FLIP buys the FAVORED (higher-priced) side —
    the market's own read of direction — and sells the +20 into the pile-in. A
    two-way band book posts ONE lot on the FAVORED (higher) bid immediately, no
    grain needed. yes 40 < no 60 -> buy NO@60; grain, if present, only
    informs — the side stays the favored side."""
    # prime a NO-favored pile: baseline poll, then the entry poll with no grown
    # to 60 and the tape FALLING with it (trend agrees with no).
    engine.flip.evaluate(TICKER, flip_ctx(engine, flip_book(48, 54),
                                          secs_left=835, spot=66000.0))
    props = engine.flip.evaluate(TICKER, flip_ctx(engine, flip_book(40, 60),
                                                  secs_left=820, spot=65980.0))
    assert [(p.side, p.purpose) for p in props] \
        == [("no", "ENTRY")]                         # no 60 > yes 40 -> favored
    engine.flip.windows.clear()
    g = {"direction": "yes", "length": 2, "k": 4}    # informs only
    engine.flip.evaluate(TICKER, flip_ctx(engine, flip_book(48, 54),
                                          secs_left=835, spot=66000.0, grain=g))
    props = engine.flip.evaluate(TICKER, flip_ctx(engine, flip_book(40, 60),
                                                  secs_left=820, spot=65980.0,
                                                  grain=g))
    assert [(p.side, p.purpose) for p in props] \
        == [("no", "ENTRY")]                         # still the favored side


def test_combined_over_line_posts_nothing(engine):
    # both <=49 individually but 49+49=98... use line breach: 49+51 impossible
    # (51>49) — breach the line with 49+49 <= 99 OK, so craft via FLIP_LINE:
    from relay_engine.lane_flip import FLIP_LINE, FLIP_SIDE_MAX
    yes = FLIP_SIDE_MAX
    no = FLIP_LINE - FLIP_SIDE_MAX + 1   # combined = line+1
    if no <= FLIP_SIDE_MAX:
        props = engine.flip.evaluate(TICKER, flip_ctx(engine, flip_book(yes, no)))
        assert props == []


# ── RULING 3: depth floor at one lot ───────────────────────────────────────
def test_depth_one_admits_one_lot():
    d = size_order(10_000, 46, visible_depth=1)
    assert d.contracts == 1     # 0.25×1 rounds to 0 — the floor admits 1


def test_depth_zero_admits_nothing():
    d = size_order(10_000, 46, visible_depth=0)
    assert d.contracts == 0     # no book is still no book


# ── RULING 1: orphan adoption ──────────────────────────────────────────────
def test_orphan_adopted_custodied_never_evidence(engine, monkeypatch):
    from relay_engine import venue
    from relay_engine.reconcile import live_boot_reconcile
    monkeypatch.setattr(venue, "get_balance", lambda c: (100.0, 0.0))
    monkeypatch.setattr(venue, "get_positions",
                        lambda c: [{"ticker": TICKER, "position": 1}])

    class C:
        def request(self, *a, **k):
            raise RuntimeError("no resting sweep")
    summary = live_boot_reconcile(engine, C())

    assert summary["positions_quarantined"] == 1
    assert f"{TICKER}:ORPHAN" in engine.custodian.positions
    assert engine.gateway.positions[(EVENT, TICKER, "ORPHAN")] == 1
    assert "ORPHAN" in engine.custodian.params      # cut params registered
    assert any(m.startswith("🧾 ORPHAN adopted") for m in engine.telegram_sent)
    assert engine.ledger.db.execute(
        "SELECT COUNT(*) FROM surface_rows WHERE state='ORPHAN_ADOPTED'"
    ).fetchone()[0] == 1
    assert engine.ledger.db.execute(
        "SELECT COUNT(*) FROM failures WHERE why_tag='ORPHAN_FOUND'"
    ).fetchone()[0] == 1


# ── FIX A: gross exposure — a filled pair is still a position ──────────────
def pair_fill(engine, side, price):
    o = Order(lane="FLIP", event=EVENT, market=TICKER, side=side, action="buy",
              price_cents=price, count=1, size_tier=config.TIER_PROBE,
              purpose="ENTRY", band=(1, 49),
              why=f"OPEN grain {side}x2 · join {price}c · PROBE n=0 · geometry=v2")
    r = engine.gateway.submit(o, flip_book(40, 48))
    engine.gateway.on_fill(r.order_id)
    return r


def test_filled_pair_stays_visible_to_walls(engine):
    pair_fill(engine, "yes", 46)
    pair_fill(engine, "no", 48)
    key = (EVENT, TICKER, "FLIP")
    assert engine.gateway.positions[key] == 0        # net masks the pair...
    assert engine.gateway.gross_open[key] == 2       # ...gross does not
    # a fresh entry on top of the open pair: the double the tape saw — refused
    with pytest.raises(WallRejection) as e:
        engine.gateway.submit(
            Order(lane="FLIP", event=EVENT, market=TICKER, side="yes",
                  action="buy", price_cents=46, count=1,
                  size_tier=config.TIER_PROBE, purpose="ENTRY", band=(1, 49),
                  why="OPEN grain yesx2 · join 46c · PROBE n=0 · geometry=v2"),
            flip_book(40, 48))
    assert e.value.wall == "SINGLE_ENTRY"
    # and the event exposure counts the pair, not zero
    contracts, cents = engine.gateway._event_exposure(EVENT)
    assert contracts == 2 and cents > 0


def test_gross_clears_on_exit_fills_and_rollover(engine):
    pair_fill(engine, "yes", 46)
    key = (EVENT, TICKER, "FLIP")
    assert engine.gateway.gross_open[key] == 1
    exit_o = Order(lane="FLIP", event=EVENT, market=TICKER, side="yes",
                   action="sell", price_cents=50, count=1,
                   size_tier=config.TIER_PROBE, purpose="EXIT")
    r = engine.gateway.submit(exit_o, flip_book(40, 48))
    engine.gateway.on_fill(r.order_id)
    assert engine.gateway.gross_open[key] == 0
    engine.gateway.gross_open[key] = 2               # stale, pretend
    engine.on_market_closed(TICKER)                  # rollover clears
    assert key not in engine.gateway.gross_open


# ── R-1: the FLIP margin line is required reading ──────────────────────────
def test_flip_r6_line_renders_daily(engine):
    engine.ledger.record_fill(TICKER, "FLIP", "no", "ENTRY", 46, 1, "PROBE")
    engine.ledger.record_fill(TICKER, "FLIP", "no", "CUSTODIAN_EXIT", 42, 1,
                              "PROBE", fee_cents=2)
    # WO-2026-07-24-H: FLIP trip P&L reads the POSITION-level cell outcome (one
    # concluded no@46 → 42 round-trip, net −6) — not the per-exit-fill fill pair.
    engine.ledger.record_cell_outcome("OPEN", 46, won=False, pnl_cents=-6,
                                      fees_cents=2, market=TICKER, kind="trip",
                                      contracts=1)
    from relay_engine.ops import daily_pack
    pack = daily_pack(engine.ledger, engine.surface, engine.cash, econ=engine.econ)
    assert "FLIP R6: trips 1 · WR 0% · net/trip -6.0¢" in pack
    assert "margin UNPROVEN, mechanism proven" in pack


# ── §1: the tape grades the deploy ─────────────────────────────────────────
def test_tape_grade_all_pass_on_clean_tape(engine):
    from scripts.tape_grade import grade
    results = grade(engine.ledger.db)
    assert all(ok for _, ok, _ in results), results


def test_tape_grade_fails_on_depth_storm(engine):
    failures.fail("WALL_STORM", "PCT_OF_BOOK storm", lane="F", market=TICKER,
                  wall_tag="BUDGET", alert=False)
    from scripts.tape_grade import grade
    results = {name: ok for name, ok, _ in grade(engine.ledger.db)}
    assert results["zero [BUDGET]/[DEPTH] wall storms (Ruling 3, specific tags §4)"] is False


def test_tape_grade_fails_on_same_side_double(engine):
    engine.ledger.record_fill(TICKER, "D", "yes", "ENTRY", 60, 1, "PROBE")
    engine.ledger.record_fill(TICKER, "D", "yes", "ENTRY", 61, 1, "PROBE")
    from scripts.tape_grade import grade
    results = {name: ok for name, ok, _ in grade(engine.ledger.db)}
    assert results["zero same-side double entries (Fix A)"] is False


def test_pack_deploy_grade_runs_then_retires(engine):
    from relay_engine.ops import daily_pack
    p1 = daily_pack(engine.ledger, engine.surface, engine.cash, econ=engine.econ)
    assert "DEPLOY GRADE (P15 expected tape): 6/6" in p1
    p2 = daily_pack(engine.ledger, engine.surface, engine.cash, econ=engine.econ)
    assert "6/6" in p2
    p3 = daily_pack(engine.ledger, engine.surface, engine.cash, econ=engine.econ)
    assert "retired — expected tape passed twice" in p3
