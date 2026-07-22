"""WO-P26 "EVERY WHY IS A PROOF" — §1 one brain loaded-or-explained,
§2 the proof law (REJECT_UNPROVEN_WHY, per lane), §3 P25's OPEN fixes
(one shot per window · evacuations cross · yield to F · geometry gate ·
margin gate · page dedup)."""

import pytest

from relay_engine import config, delta, failures, lane_p, scoring
from relay_engine.book import OrderBook
from relay_engine.custodian import Custodian
from relay_engine.feed import DegradeLadder
from relay_engine.gateway import Order, WallRejection
from relay_engine.lane_flip import LaneFlip

TICKER = "KXBTC15M-02JAN251000-T99"
EVENT = TICKER.rsplit("-", 1)[0]
CLOSE = 1_000_000.0
GRAIN_YES2 = {"direction": "yes", "length": 2, "k": 4}


def _book(yes=40, no=49):
    b = OrderBook(market=TICKER)
    b.apply_snapshot({yes: 10}, {no: 10}, ts=1.0)
    return b


def _ctx(book, secs_left=850, grain=None, spotlead=None, spot=None):
    return {"book": book, "now": CLOSE - secs_left, "close_ts": CLOSE,
            "spot": spot, "grain": grain, "spotlead": spotlead}


@pytest.fixture(autouse=True)
def _funnel(ledger):
    failures.configure(ledger, alert_fn=lambda m: None, run_mode="TEST",
                       boot_id=1)
    yield
    failures._ledger = None


@pytest.fixture
def flip(gateway, ledger, surface):
    custodian = Custodian(gateway, ledger, surface, ladder=DegradeLadder())
    return LaneFlip(gateway, custodian=custodian)


def _entry(lane, why, price=48, side="yes", market=TICKER, event=EVENT):
    return Order(lane=lane, event=event, market=market, side=side,
                 action="buy", price_cents=price, count=1,
                 size_tier=config.TIER_PROBE, purpose="ENTRY", why=why)


# ── §2: REJECT_UNPROVEN_WHY — P27 §2(c): the NARRATION law only ───────────
def test_wall_rejects_only_empty_whys(gateway):
    """P27 §2(c) OVERTURNED P26's per-lane proof-field demands: whys
    REPORT, doctrine gates, process gates die. The wall now demands ONE
    thing — a non-empty why string (every dollar narrates) — and never a
    threshold. Any narrated why passes, any lane."""
    book = _book()
    for i, why in enumerate(("", "   ")):
        with pytest.raises(WallRejection) as e:
            gateway.submit(_entry("FLIP", why, market=f"M{i}",
                                  event=f"EV{i}"), book)
        assert e.value.wall == "REJECT_UNPROVEN_WHY"
    # narrated whys pass regardless of proof-field shape — even a new lane
    assert gateway.submit(_entry("MM", "a new lane, narrating its thesis",
                                 market="M9", event="EV9"), book).shadow


def test_wall_admits_proven_whys(gateway):
    book = _book()
    proven = [
        ("F", "F tier48 band45-56 confirms2 · surv~price"),
        ("H8", "H8 tier95 · surv0.97≥0.95"),
        ("P", "P fade yes · displ +4.0c x2 spot-flat"),
        ("D", "d-table verdict yes@48¢ · reserved"),
        ("FLIP", "OPEN grain yesx2 · join 48c · PROBE n=0 · geometry=v2"),
        ("FLIP", "HUNT needle +8pts (d 200→90, T-6:39) · fair 70 · gap 22 "
                 "· converging"),
    ]
    for i, (lane, why) in enumerate(proven):
        r = gateway.submit(_entry(lane, why, market=f"M{i}", event=f"EV{i}"),
                           _book())
        assert r.shadow, (lane, why)


def test_exits_and_cuts_never_reach_the_proof_wall(gateway):
    """Risk reduction never waits on paperwork — the wall is ENTRY-only."""
    book = _book()
    r = gateway.submit(_entry("F", "F tier48 · surv~price"), book)
    gateway.on_fill(r.order_id)
    gateway.submit(Order(lane="F", event=EVENT, market=TICKER, side="yes",
                         action="sell", price_cents=53, count=1,
                         size_tier=config.TIER_PROBE, purpose="EXIT"), book)


# ── §2: the lanes now PRINT their proofs ───────────────────────────────────
def test_p_fade_why_carries_displacement(monkeypatch):
    """The fade's proof — a book displacement the spot never made."""
    import inspect
    src = inspect.getsource(lane_p)
    assert "displ" in src and "spot-flat" in src


def test_fh8_nonreversal_gate_and_tag(monkeypatch):
    """§2 F/H8: with the table loaded, survival p >= the price paid or the
    lane passes; the why carries the arithmetic. Table absent -> tagged
    surv~price, never silently unproven."""
    from relay_engine.lanes import _PortedLane
    ctx = {"spot": 118_050.0, "close_ts": 1000.0, "now": 500.0,
           "boundary_hi": 118_000.0}
    # table absent -> None (price-implied path tags)
    monkeypatch.setattr(delta, "is_loaded", lambda: False)
    assert _PortedLane._table_survival("yes", ctx) is None
    # table loaded: (surv, d, t) — DIAG-1 rides the cell along for the
    # interrogator's histogram
    monkeypatch.setattr(delta, "is_loaded", lambda: True)
    monkeypatch.setattr(delta, "p_survive", lambda d, t, session="ALL": 0.9)
    surv, d_usd, t_rem = _PortedLane._table_survival("yes", ctx)
    assert surv == pytest.approx(0.9)
    assert d_usd == pytest.approx(50.0) and t_rem == pytest.approx(500.0)
    assert _PortedLane._table_survival("no", ctx)[0] == pytest.approx(0.1)


# ── §3.1: one shot per window (consumed on ANY exit, takes too) ────────────
def _open_position(flip, gateway, entry=60):
    # WO-2026-07-22-F: FLIP buys the FAVORED (higher-priced) side in [50,70],
    # but only after the PILE forms. Prime it: a baseline small-skew poll
    # in-window, then the entry poll with the skew grown (+14) and the tape
    # risen +20 (trend agrees with yes). props[0] is the favored yes@60 entry;
    # note_fill anchors the position at 60.
    flip.evaluate(TICKER, _ctx(_book(yes=54, no=48), secs_left=835,
                               spot=66000.0, grain=GRAIN_YES2))
    props = flip.evaluate(TICKER, _ctx(_book(yes=60, no=40), secs_left=820,
                                       spot=66020.0, grain=GRAIN_YES2))
    assert len(props) == 1
    flip.on_submitted(props[0], "OID-E", CLOSE - 800)
    gateway.positions[(EVENT, TICKER, "FLIP")] = 1
    flip.note_fill(TICKER, "yes", entry, CLOSE - 795)
    return flip.windows[TICKER]


def test_consumed_on_take_blocks_the_rebet(flip, gateway):
    """The Adversary's clause: a WON window is still a consumed window —
    the take exit sets open_consumed and re-proposals refuse until
    rollover (the note_exit pop was the located loophole)."""
    w = _open_position(flip, gateway)
    flip.note_exit(TICKER, "yes", 77, CLOSE - 700)   # the take FILLS (entry+17)
    assert w.open_consumed
    gateway.positions[(EVENT, TICKER, "FLIP")] = 0
    # a fresh FORMED pile (baseline then grown-skew poll), still inside the pile
    # window: it WOULD enter but for the consumed flag — so this proves consumed
    # blocks, not the band, the pile, or the clock (no tautology).
    flip.evaluate(TICKER, _ctx(_book(yes=54, no=48), secs_left=835,
                               spot=66000.0, grain=GRAIN_YES2))
    props = flip.evaluate(TICKER, _ctx(_book(yes=60, no=40), secs_left=820,
                                       spot=66020.0, grain=GRAIN_YES2))
    assert props == []
    assert w.open_consumed_logged
    # rollover clears: a NEW window gets its one shot (re-primed)
    flip.windows.clear()
    flip.evaluate(TICKER, _ctx(_book(yes=54, no=48), secs_left=835,
                               spot=66000.0, grain=GRAIN_YES2))
    props2 = flip.evaluate(TICKER, _ctx(_book(yes=60, no=40), secs_left=820,
                                        spot=66020.0, grain=GRAIN_YES2))
    assert len(props2) == 1


def test_consumed_on_determined_too(flip, gateway):
    w = _open_position(flip, gateway)
    flip.note_exit(TICKER, "yes", 50, CLOSE - 700)   # momentum-stop cut books
    assert w.open_consumed
    gateway.positions[(EVENT, TICKER, "FLIP")] = 0
    assert flip.evaluate(TICKER, _ctx(_book(yes=60, no=40), secs_left=840,
                                      grain=GRAIN_YES2)) == []


# ── §3.2/§3.4: evacuations cross NOW, within the geometry ──────────────────
def test_evacuation_crosses_at_the_mark(flip, gateway):
    """SPOT_DECIDED is RETIRED. WO-2026-07-22-E: the MOMENTUM STOP prices AT
    the mark when the book is already THROUGH the stop (mark < entry−10) — a
    CUT that crossfires, not a maker-grace slide to 31. (When mark still sits
    at the stop it rests maker-first; here the book has gone through us.)"""
    _open_position(flip, gateway, entry=60)          # favored yes@60, stop 50
    take = flip.evaluate(TICKER, _ctx(_book(yes=60, no=40), secs_left=780))[0]
    flip.on_submitted(take, "OID-T", CLOSE - 780)
    # book collapses THROUGH the stop (mark 44 < stop 50), sustained 2 polls
    flip.evaluate(TICKER, _ctx(_book(yes=44, no=56), secs_left=771))
    props = flip.evaluate(TICKER, _ctx(_book(yes=44, no=56), secs_left=770))
    assert len(props) == 1
    p = props[0]
    assert p.purpose == "CUT" and p.crossfire
    assert p.price_cents == 44 and "momentum stop" in p.reason


def test_t10_handoff_replaces_yield_to_f(flip, gateway):
    """P-FLIP-THESIS-1 §3.5 OVERTURNED §3.3's blind YIELD_TO_F: the T-10
    boundary is a book-aware handoff — this loser (47 < 48 basis) clears
    with crossfire; a winner would convert to hold-to-settle instead."""
    _open_position(flip, gateway)
    take = flip.evaluate(TICKER, _ctx(_book(), secs_left=780))[0]
    flip.on_submitted(take, "OID-T", CLOSE - 780)
    props = flip.evaluate(TICKER, _ctx(_book(yes=34, no=50),
                                       secs_left=config.FLIP_DECISION_S - 1))
    assert len(props) == 1 and props[0].crossfire
    assert "open decision point" in props[0].reason


def test_open_entry_pile_window(flip):
    """RE-ANCHORED by WO-2026-07-22-F "wait for the pile" (build 58): entries
    fire ONLY inside [OPEN_PILE_START_S (60), OPEN_PILE_END_S (180)]. secs_into
    < 60 is TOO EARLY (the pile has not had time — no entry); a formed pile
    in-window enters; past 180s the pile never came and the window is SKIPPED
    (one OPEN_SKIP line)."""
    # too early — secs_into 50 (secs_left 850), before the pile can form
    assert flip.evaluate(TICKER, _ctx(_book(yes=60, no=40), grain=GRAIN_YES2,
                                      secs_left=850)) == []
    # in-window with a FORMED pile: baseline then the grown-skew entry poll
    flip.windows.clear()
    flip.evaluate(TICKER, _ctx(_book(yes=54, no=48), grain=GRAIN_YES2,
                               secs_left=835, spot=66000.0))
    props = flip.evaluate(TICKER, _ctx(_book(yes=60, no=40), grain=GRAIN_YES2,
                                       secs_left=820, spot=66020.0))
    assert len(props) == 1
    # past the pile window (secs_into 200): SKIPPED, no entry, logged once
    flip.windows.clear()
    assert flip.evaluate(TICKER, _ctx(_book(yes=60, no=40), grain=GRAIN_YES2,
                                      secs_left=700)) == []
    assert flip.windows[TICKER].open_skip_logged


def test_no_geometry_gate_admits_the_band(flip):
    """§3.4 RETIRED by WO-FLIP-LIQUIDITY-HOLD (build 45): the risk/reward
    geometry gate is gone. A low mark is illiquidity to hold through, not a
    loss to bail from, so there is no pre-trade risk/reward rejection — the
    entry filter is band membership, and the reversion rate (Instrument 1) is
    the empirical gate. An in-band thesis entry is ADMITTED, never tagged
    OPEN_BAD_GEOMETRY."""
    # WO-2026-07-22-F: the favored band is [50,70]; a favored yes@60 with a
    # formed pile (prime baseline then grown-skew poll) is admitted
    flip.evaluate(TICKER, _ctx(_book(yes=54, no=48), secs_left=835,
                               spot=66000.0, grain=GRAIN_YES2))
    props = flip.evaluate(TICKER, _ctx(_book(yes=60, no=40), secs_left=820,
                                       spot=66020.0, grain=GRAIN_YES2))
    assert [p for p in props if p.purpose == "ENTRY"]
    assert not flip.windows[TICKER].open_geometry_logged


# ── §2: OPEN's margin — P27 §2(b): prints always, gates never ──────────────
def test_open_margin_prints_never_gates(flip, gateway, ledger):
    """P27 §2(b) OVERTURNED the margin gate (and OPEN_CELL_NEGATIVE's
    sit): entry proceeds on band + grain + geometry + one-shot; the cell
    margin prints on the why either way — informs daily, governs never."""
    # WO-2026-07-22-E: FLIP buys the favored yes@60 (cell 60); the margin cell
    # that prints on the why is scoring.price_cell(60) = 60, so we populate 60.
    # WO-2026-07-22-F: each entry needs a formed pile — baseline then grown-skew.
    def _prime_entry():
        flip.evaluate(TICKER, _ctx(_book(yes=54, no=48), secs_left=835,
                                   spot=66000.0, grain=GRAIN_YES2))
        return flip.evaluate(TICKER, _ctx(_book(yes=60, no=40), secs_left=820,
                                          spot=66020.0, grain=GRAIN_YES2))
    # virgin cell: enters, margin printed on the new favored-side why
    props = _prime_entry()
    assert len(props) == 1
    assert "margin" in props[0].why and "favored yes" in props[0].why
    # a mature NEGATIVE cell: STILL enters — the margin prints (info)
    flip.windows.clear()
    for i in range(config.OPEN_PROBE_MAX_N):
        ledger.record_cell_outcome("OPEN", 60, won=False, pnl_cents=-5,
                                   fees_cents=0, market=f"L{i}", kind="trip")
    props = _prime_entry()
    assert len(props) == 1 and "info" in props[0].why
    # a mature POSITIVE cell: enters with the positive margin printed
    ledger.db.execute("DELETE FROM cell_outcomes")
    ledger.db.commit()
    for i in range(60):
        ledger.record_cell_outcome("OPEN", 60, won=True, pnl_cents=5,
                                   fees_cents=0, market=f"W{i}", kind="trip")
    flip.windows.clear()
    props = _prime_entry()
    assert len(props) == 1 and "margin +" in props[0].why


# ── §1: one brain, loaded or explained ─────────────────────────────────────
def test_boot_pages_table_verdict_loaded(tmp_path, monkeypatch):
    from relay_engine.shadow_runner import ShadowEngine
    e = ShadowEngine(db_path=str(tmp_path / "p26.db"))
    sent = []
    e.telegram.send = sent.append
    monkeypatch.setattr(delta, "load", lambda path=None: True)
    monkeypatch.setattr(delta, "table_status",
                        lambda: {"status": "LOADED", "detail": "180d, built X"})
    monkeypatch.setattr(delta, "_TABLE", {(0, 0, "ALL"): {}}, raising=False)
    e.boot()
    assert any(m.startswith("🧠 TABLE: loaded") for m in sent)


def test_boot_starts_build_when_absent_and_pages_fail(tmp_path, monkeypatch):
    from relay_engine import delta_builder
    from relay_engine.shadow_runner import ShadowEngine
    e = ShadowEngine(db_path=str(tmp_path / "p26b.db"))
    sent = []
    e.telegram.send = sent.append
    monkeypatch.setattr(delta, "load", lambda path=None: False)
    monkeypatch.setattr(delta, "refusal_reason", lambda: "CSV file missing")
    monkeypatch.setattr(config, "TABLE_AUTOBUILD", True)
    started = {}
    monkeypatch.setattr(delta_builder, "start_background_build",
                        lambda notify_fn=None: started.update(fn=notify_fn))
    e.boot()
    assert any(m.startswith("🧠 TABLE: building") for m in sent)
    assert started["fn"] is not None
    # the builder's FAIL verdict pages ⛔ with the gate transcript
    started["fn"]("DELTA TABLE BUILD FAILED:\nA2: FAIL monotonicity")
    assert any(m.startswith("⛔ TABLE: FAILED — proven lanes mute")
               for m in sent)
    # autobuild off: the absence is still explained, loudly
    sent.clear()
    monkeypatch.setattr(config, "TABLE_AUTOBUILD", False)
    e2 = ShadowEngine(db_path=str(tmp_path / "p26c.db"))
    e2.telegram.send = sent.append
    e2.boot()
    assert any(m.startswith("⛔ TABLE: absent") for m in sent)


def test_loaded_table_cell_miss_logs_the_pair(monkeypatch, caplog):
    """§1.3: a (d,t) miss WITH a loaded table names the pair — grid gaps
    become Saturday data, not mysteries."""
    import logging
    monkeypatch.setattr(delta, "_LOADED", True)
    monkeypatch.setattr(delta, "_TABLE", {(50, 900, "ALL"): {
        "p_cross": 0.1, "n": 10, "effective_n": 5, "wilson_ub": 0.2}})
    delta._MISS_LOGGED.clear()
    with caplog.at_level(logging.WARNING, logger="relay.delta_table"):
        assert delta._lookup(999, 333) is None
    assert any("cell miss with LOADED table" in r.message
               for r in caplog.records) or delta._MISS_LOGGED


def test_page_once_dedups_across_restart_shape(tmp_path):
    """§3.5: the 6:22 restart double-page class — deploy-scoped pages key
    on the ledger and fire once."""
    from relay_engine.shadow_runner import ShadowEngine
    e = ShadowEngine(db_path=str(tmp_path / "p26d.db"))
    sent = []
    e.telegram.send = sent.append
    assert e.page_once("page_test_key", "hello") is True
    assert e.page_once("page_test_key", "hello") is False
    assert sent.count("hello") == 1
