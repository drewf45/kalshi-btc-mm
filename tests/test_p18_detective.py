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
    scaled by time (near strike + clock on it = volatile; far/late = frozen).
    WO-2026-07-24-J: the SETTLE surface (p_end / p_end_wilson_lb) is stubbed
    too — HUNT's forward gate reads it, and a high LB (0.70) means a 60c join
    clears the +6 edge floor while a 91c join does not."""
    def p_survive(d, t, session="ALL"):
        # reachability shrinks with clock: ~$0.30/s of plausible travel
        edge = min(1.0, d / (0.3 * max(1.0, t)))
        return 0.5 + 0.43 * edge
    monkeypatch.setattr(delta, "p_survive", p_survive)
    monkeypatch.setattr(delta, "p_end", lambda d, t, session="ALL": 0.72)
    monkeypatch.setattr(delta, "p_end_wilson_lb",
                        lambda d, t, session="ALL": 0.70)
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
    # WO-2026-07-24-J §P4: the forward casefile — every probability names ITS
    # question (settle / touch), distances carry $, the gate quantity is EDGE,
    # the info-only touch is marked, and the EV tag rides. "fair" is banned.
    for tok in ("HUNT ↑", "spot $", "settle ", "(LB ", "book ", "edge +",
                "touch ", "(info)", "EV "):
        assert tok in o.why, (tok, o.why)
    assert "fair" not in o.why
    # edge = p_end_lb(0.70)*100 − join(60) = +10
    assert "edge +10" in o.why


def test_single_frame_never_hunts(engine, table):
    props = drive_hunt(engine, cycles=1)
    assert props == []                  # no single-tick knives (gate C)


def test_edge_under_floor_never_hunts(engine, table):
    # WO-J §P2: settle-LB 70%, a join at 91c leaves edge = 70−91 = −21 < 6 —
    # the market has caught up; there is no forward edge to buy.
    props = drive_hunt(engine, join=91)
    assert props == []


def test_blind_settle_surface_never_hunts(engine, monkeypatch):
    # WO-J §P2: a legacy touch-only table (p_end absent) → the forward gate is
    # BLIND → HUNT sits out. A qualifying needle is NOT enough on its own.
    monkeypatch.setattr(delta, "p_survive",
                        lambda d, t, session="ALL": 0.93)
    monkeypatch.setattr(delta, "p_end_wilson_lb",
                        lambda d, t, session="ALL": None)
    props = drive_hunt(engine, join=60)
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


def test_job_b_thesis_exit_when_edge_gone(engine, table, monkeypatch):
    """WO-2026-07-24-J §P3: the two-tick level bail (`mark<=entry-2`) RETIRES.
    A hunt is not wrong because the mark ticked; it is wrong when the SETTLE
    edge is gone — wilson_LB(p_end) at CURRENT geometry ≤ the mark, sustained
    N polls — and the exit logs BOTH numbers."""
    w = hunted(engine, table)
    w.hunts["yes"]["take_oid"] = "TAKE-1"
    w.hunts["yes"]["take_proposed"] = True
    # the settle LB collapses below the 60c mark → the edge that bought it is gone
    monkeypatch.setattr(delta, "p_end_wilson_lb",
                        lambda d, t, session="ALL": 0.45)

    def ctx(secs):
        return {"book": hunt_book(60), "now": CLOSE - secs, "close_ts": CLOSE,
                "spot": STRIKE + 80, "boundary_hi": STRIKE, "boundary_lo": None,
                "spotlead": None}
    cuts = []
    for i in range(config.HUNT_EDGE_GONE_POLLS):     # sustained N polls
        props = engine.flip.evaluate(TICKER, ctx(496 - i))
        cuts = [p for p in props if p.purpose == "CUT"]
    assert len(cuts) == 1 and cuts[0].crossfire is True
    assert "edge gone" in cuts[0].reason
    assert "settle 45%" in cuts[0].reason and "cost 60¢" in cuts[0].reason


def test_thesis_exit_blind_never_fires(engine, table):
    """The thesis exit NEVER fires on blindness: no boundaries → no strike →
    the settle LB is unreadable → the edge-gone bail cannot trigger (the named
    guardrails still can)."""
    w = hunted(engine, table)
    w.hunts["yes"]["take_oid"] = "TAKE-1"
    w.hunts["yes"]["take_proposed"] = True
    for i in range(config.HUNT_EDGE_GONE_POLLS + 2):
        props = engine.flip.evaluate(TICKER, hunt_ctx(hunt_book(60),
                                                      STRIKE + 80, 496 - i))
        assert not [p for p in props if p.purpose == "CUT"]


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


def test_p18_tape_grade_clean_and_catches_sub_floor(engine):
    from scripts.tape_grade import CHECKS_P18, grade
    assert all(ok for _, ok, _ in grade(engine.ledger.db, checks=CHECKS_P18))
    # WO-2026-07-24-J §P2: a HUNT entry printed with edge below the floor is a
    # graded violation (the forward gate is the law).
    engine.surface.write_row(
        "FLIP", TICKER, "w-x", "PROPOSED",
        detail="HUNT ↑ spot $80 off strike, T-8:20 · settle 62% (LB 58) · "
               "book 55¢ · edge +3 · touch 70% (info) · EV +7")
    results = {n: ok for n, ok, _ in grade(engine.ledger.db, checks=CHECKS_P18)}
    assert results["zero HUNT entries with edge < floor (gate B graded)"] is False
