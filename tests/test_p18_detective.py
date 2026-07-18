"""WO-P18 FINAL "THE DETECTIVE" — the needle-move equation (§2), the two
jobs (§3), shared eyes + P-suppression (§4), instrumentation (§5).
The spot-lead law: there was never a second price — only a second clock,
and ours runs first."""

import pytest

from relay_engine import config, delta, failures, spotlead
from relay_engine.book import OrderBook
from relay_engine.lane_flip import FLIP_CURFEW
from relay_engine.shadow_runner import ShadowEngine

TICKER = "KXBTC15M-02JAN251000-T99"
EVENT = TICKER.rsplit("-", 1)[0]
STRIKE = 118_000.0
CLOSE = 1_000_000.0


@pytest.fixture
def engine(tmp_path):
    e = ShadowEngine(db_path=str(tmp_path / "p18.db"))
    e.boot()
    e.telegram_sent = []
    e.telegram.send = e.telegram_sent.append
    failures.configure(e.ledger, alert_fn=e.telegram.alert, run_mode="TEST", boot_id=1)
    yield e
    failures._ledger = None
    failures._alert_fn = None


@pytest.fixture
def table(monkeypatch):
    """A deterministic delta-table stand-in: survival rises with distance,
    scaled by time (near strike + clock on it = volatile; far/late = frozen)."""
    def p_survive(d, t, session="ALL"):
        # reachability shrinks with clock: ~$0.30/s of plausible travel
        edge = min(1.0, d / (0.3 * max(1.0, t)))
        return 0.5 + 0.43 * edge
    monkeypatch.setattr(delta, "p_survive", p_survive)
    return p_survive


def hunt_book(side_price=60, other_price=30):
    b = OrderBook(market=TICKER)
    b.apply_snapshot({side_price: 10}, {other_price: 10}, ts=1.0)
    return b


def hunt_ctx(book, spot, secs_left=500):
    return {"book": book, "now": CLOSE - secs_left, "close_ts": CLOSE,
            "spot": spot,
            "spotlead": spotlead.needle(118_000.0 - 80, spot, STRIKE,
                                        secs_left)}


# ── §2 gate A: the needle, as arithmetic ───────────────────────────────────
def test_needle_clock_and_distance_law(table):
    """The same $80 jump: a big needle near the strike with clock on it, a
    nothing far away — no hand-tuned time rules."""
    near = spotlead.needle(STRIKE - 40, STRIKE + 40, STRIKE, 300)
    far = spotlead.needle(STRIKE + 5_000, STRIKE + 5_080, STRIKE, 300)
    assert near is not None and near.side == "yes"
    assert near.delta_p > 20                       # crossing the strike: huge
    assert far is not None and far.delta_p < 1     # deep-safe: nothing moved


def test_needle_none_without_evidence(table):
    assert spotlead.needle(None, 118_100.0, STRIKE, 300) is None   # spot blind
    assert spotlead.needle(118_000.0, 118_000.0, STRIKE, 300) is None  # no move
    assert spotlead.needle(118_000.0, 118_100.0, None, 300) is None    # no strike


def test_needle_none_when_table_absent(monkeypatch):
    monkeypatch.setattr(delta, "p_survive", lambda d, t, session="ALL": None)
    assert spotlead.needle(117_920.0, 118_080.0, STRIKE, 300) is None


def test_needle_side_is_always_with_spot(table):
    up = spotlead.needle(117_900.0, 118_100.0, STRIKE, 300)
    down = spotlead.needle(118_100.0, 117_900.0, STRIKE, 300)
    assert up.side == "yes" and down.side == "no"  # never fades the move


# ── §2 the full trigger through the lane ───────────────────────────────────
def drive_hunt(engine, spot=STRIKE + 80, join=60, cycles=2, secs=500):
    """Two confirming evaluations of a qualifying needle."""
    props = []
    for i in range(cycles):
        book = hunt_book(side_price=join)
        props = engine.flip.evaluate(TICKER, hunt_ctx(book, spot,
                                                      secs_left=secs - i))
    return props


def test_confirmed_needle_hunts_with_casefile(engine, table):
    props = drive_hunt(engine)
    assert len(props) == 1
    o = props[0]
    assert o.purpose == "ENTRY" and o.side == "yes" and o.action == "buy"
    assert o.price_cents == 60          # MAKER JOIN at the touch, no chasing
    assert o.band == config.HUNT_BAND   # no side-max, no price cap
    for tok in ("HUNT needle +", "d ", "fair", "gap", "converging"):
        assert tok in o.why, (tok, o.why)


def test_single_frame_never_hunts(engine, table):
    props = drive_hunt(engine, cycles=1)
    assert props == []                  # no single-tick knives (gate C)


def test_gap_under_g_never_hunts(engine, table):
    # fair ≈ 93 for this needle; a join at 91 leaves gap < 4 — invisible lag
    props = drive_hunt(engine, join=91)
    assert props == []


def test_book_repricing_against_kills_the_hunt(engine, table):
    book1 = hunt_book(side_price=60)
    engine.flip.evaluate(TICKER, hunt_ctx(book1, STRIKE + 80, secs_left=500))
    book2 = hunt_book(side_price=57)    # the crowd fights the move
    props = engine.flip.evaluate(TICKER, hunt_ctx(book2, STRIKE + 80,
                                                  secs_left=499))
    assert props == []


def test_curfew_and_blind_spot_never_hunt(engine, table):
    assert drive_hunt(engine, secs=FLIP_CURFEW - 10) == []
    book = hunt_book()
    ctx = {"book": book, "now": CLOSE - 500, "close_ts": CLOSE,
           "spot": None, "spotlead": None}   # BLIND: no signal computes
    assert engine.flip.evaluate(TICKER, ctx) == []


# ── §3 the two jobs ────────────────────────────────────────────────────────
def hunted(engine, table, entry=60):
    props = drive_hunt(engine, join=entry)
    assert props
    engine.flip.on_submitted(props[0], "HUNT-1", CLOSE - 500)
    engine.flip.note_fill(TICKER, "yes", entry, CLOSE - 498)
    return engine.flip.windows[TICKER]


def test_job_a_take_posts_on_fill(engine, table):
    w = hunted(engine, table)
    props = engine.flip.evaluate(TICKER, hunt_ctx(hunt_book(60), STRIKE + 80,
                                                  secs_left=497))
    takes = [p for p in props if p.purpose == "EXIT"]
    assert len(takes) == 1
    assert takes[0].price_cents == 60 + config.HUNT_TAKE_CENTS
    assert "hunt take" in takes[0].reason
    engine.flip.on_submitted(takes[0], "TAKE-1", CLOSE - 497)
    assert w.hunts["yes"]["take_oid"] == "TAKE-1"


def test_job_b_breakeven_reprice_then_bail(engine, table):
    w = hunted(engine, table)
    engine.flip.evaluate(TICKER, hunt_ctx(hunt_book(61), STRIKE + 80, 497))
    # (the take proposed above; register it so custody can cancel it)
    w.hunts["yes"]["take_oid"] = "TAKE-1"
    props = engine.flip.evaluate(TICKER, hunt_ctx(hunt_book(59), STRIKE + 80,
                                                  secs_left=495))
    be = [p for p in props if "breakeven reprice" in p.reason]
    assert len(be) == 1 and be[0].price_cents == 60 and be[0].purpose == "EXIT"
    # not out within R seconds → crossfire flatten NOW at best
    now_late = CLOSE - 495 + config.HUNT_BAIL_R_S + 1
    props = engine.flip.evaluate(
        TICKER, {"book": hunt_book(59), "now": now_late, "close_ts": CLOSE,
                 "spot": STRIKE + 80, "spotlead": None})
    cuts = [p for p in props if p.purpose == "CUT"]
    assert len(cuts) == 1 and cuts[0].crossfire is True
    assert "not-out-in" in cuts[0].reason


def test_job_b_hard_bail_at_entry_minus_2(engine, table):
    w = hunted(engine, table)
    w.hunts["yes"]["take_oid"] = "TAKE-1"
    w.hunts["yes"]["take_proposed"] = True
    props = engine.flip.evaluate(TICKER, hunt_ctx(hunt_book(58), STRIKE + 80,
                                                  secs_left=496))
    cuts = [p for p in props if p.purpose == "CUT"]
    assert len(cuts) == 1 and cuts[0].crossfire is True
    assert "mark<=entry-2" in cuts[0].reason


def test_timebox_flattens_at_m(engine, table):
    w = hunted(engine, table)
    w.hunts["yes"]["take_oid"] = "TAKE-1"
    w.hunts["yes"]["take_proposed"] = True
    now_late = CLOSE - 498 + config.HUNT_TIMEBOX_M_S + 1
    props = engine.flip.evaluate(
        TICKER, {"book": hunt_book(61), "now": now_late, "close_ts": CLOSE,
                 "spot": STRIKE + 80, "spotlead": None})
    cuts = [p for p in props if p.purpose == "CUT"]
    assert len(cuts) == 1 and "time-box" in cuts[0].reason


def test_hunt_exit_realizes_and_feeds_the_ratchet(engine, table):
    w = hunted(engine, table)
    engine.flip.note_exit(TICKER, "yes", 58, CLOSE - 400)   # bailed −2
    assert w.window_realized == -2 and w.scratches == 1
    assert "yes" not in w.hunts


# ── §4: shared eyes + P yields the floor ───────────────────────────────────
def test_p_suppressed_on_confirmed_needle(engine, table, monkeypatch):
    engine.market_meta[TICKER] = {"close_ts": CLOSE,
                                  "boundary_lo": None, "boundary_hi": STRIKE}
    engine.feed.handle_frame(
        __import__("json").dumps(
            {"type": "orderbook_snapshot",
             "msg": {"market_ticker": TICKER, "yes": [[60, 10]], "no": [[30, 10]]}}),
        now=CLOSE - 500)
    engine.hunt_anchor[TICKER] = STRIKE - 80
    engine.cycle([TICKER], now=CLOSE - 500, spot=STRIKE + 80)
    row = engine.ledger.db.execute(
        "SELECT detail FROM surface_rows WHERE lane='P' AND"
        " detail LIKE 'P_SUPPRESSED%'").fetchone()
    assert row is not None and "needle +" in row[0]


def test_f_why_carries_the_spotlead_tag(engine, table):
    from relay_engine.lanes import infer_close_ts_from_ticker
    close = infer_close_ts_from_ticker(TICKER)
    engine.market_meta[TICKER] = {"close_ts": close,
                                  "boundary_lo": None, "boundary_hi": STRIKE}
    engine.feed.handle_frame(
        __import__("json").dumps(
            {"type": "orderbook_snapshot",
             "msg": {"market_ticker": TICKER, "yes": [[97, 10]], "no": [[2, 10]]}}),
        now=close - 120)
    engine.hunt_anchor[TICKER] = STRIKE + 70
    engine.cycle([TICKER], now=close - 120, spot=STRIKE + 80)
    row = engine.ledger.db.execute(
        "SELECT detail FROM surface_rows WHERE lane='F' AND state='PROPOSED'"
    ).fetchone()
    assert row is not None and "spotlead:" in row[0]


# ── §5/§7: instrumentation + the graded tape ───────────────────────────────
def test_pack_prints_the_bar(engine):
    from relay_engine.ops import daily_pack
    pack = daily_pack(engine.ledger, engine.surface, engine.cash, econ=engine.econ)
    assert "bar ≥50%" in pack and "HUNT volume expected LOW" in pack


def test_p18_tape_grade_clean_and_catches_sub_n(engine):
    from scripts.tape_grade import CHECKS_P18, grade
    assert all(ok for _, ok, _ in grade(engine.ledger.db, checks=CHECKS_P18))
    engine.surface.write_row("FLIP", TICKER, "w-x", "PROPOSED",
                             detail="order=1 ENTRY @60c why=HUNT needle +3pts "
                                    "(d 80→10, T-8:20) · fair 64 · gap 4 · converging")
    results = {n: ok for n, ok, _ in grade(engine.ledger.db, checks=CHECKS_P18)}
    assert results["zero HUNT entries with ΔP < N (gate A graded)"] is False
