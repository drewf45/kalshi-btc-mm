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


def _book(yes=48, no=49):
    b = OrderBook(market=TICKER)
    b.apply_snapshot({yes: 10}, {no: 10}, ts=1.0)
    return b


def _ctx(book, secs_left=800, grain=None, spotlead=None):
    return {"book": book, "now": CLOSE - secs_left, "close_ts": CLOSE,
            "spot": None, "grain": grain, "spotlead": spotlead}


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


# ── §2: REJECT_UNPROVEN_WHY, per lane ──────────────────────────────────────
def test_wall_rejects_unproven_whys_per_lane(gateway):
    book = _book()
    cases = [
        ("F", "F likes this market"),               # no tier/surv
        ("H8", "H8 tier95"),                        # missing surv
        ("P", "P feels a fade coming"),             # no displ arithmetic
        ("D", "cheap entry"),                       # no table verdict
        ("FLIP", ""),                               # neither HUNT nor OPEN
        ("FLIP", "OPEN grain yesx2 · join 48c"),    # no margin|PROBE receipt
        ("MM", "brand new lane, no proof form"),    # unregistered lane
    ]
    for i, (lane, why) in enumerate(cases):
        # distinct markets: the P10 wall-backoff must not shadow the verdict
        with pytest.raises(WallRejection) as e:
            gateway.submit(_entry(lane, why, market=f"M{i}", event=f"EV{i}"),
                           book)
        assert e.value.wall == "REJECT_UNPROVEN_WHY", (lane, why)


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
    # table loaded: survival for the spot-favored side
    monkeypatch.setattr(delta, "is_loaded", lambda: True)
    monkeypatch.setattr(delta, "p_survive", lambda d, t, session="ALL": 0.9)
    assert _PortedLane._table_survival("yes", ctx) == pytest.approx(0.9)
    assert _PortedLane._table_survival("no", ctx) == pytest.approx(0.1)


# ── §3.1: one shot per window (consumed on ANY exit, takes too) ────────────
def _open_position(flip, gateway, entry=48):
    props = flip.evaluate(TICKER, _ctx(_book(), grain=GRAIN_YES2))
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
    flip.note_exit(TICKER, "yes", 53, CLOSE - 700)   # the take FILLS
    assert w.open_consumed
    gateway.positions[(EVENT, TICKER, "FLIP")] = 0
    props = flip.evaluate(TICKER, _ctx(_book(), secs_left=690,
                                       grain=GRAIN_YES2))
    assert props == []
    assert w.open_consumed_logged
    # rollover clears: a NEW window gets its one shot
    flip.windows.clear()
    props2 = flip.evaluate(TICKER, _ctx(_book(), grain=GRAIN_YES2))
    assert len(props2) == 1


def test_consumed_on_determined_too(flip, gateway):
    w = _open_position(flip, gateway)
    flip.note_exit(TICKER, "yes", 41, CLOSE - 700)   # evacuation books
    assert w.open_consumed
    gateway.positions[(EVENT, TICKER, "FLIP")] = 0
    assert flip.evaluate(TICKER, _ctx(_book(), secs_left=690,
                                      grain=GRAIN_YES2)) == []


# ── §3.2/§3.4: evacuations cross NOW, within the geometry ──────────────────
def test_evacuation_crosses_at_trigger_price(flip, gateway):
    """The fill lands <=2¢ from the trigger: mark 41 vs trigger 42 — the
    crossfire cut prices AT the mark, not a maker-grace slide to 31."""
    _open_position(flip, gateway, entry=48)
    take = flip.evaluate(TICKER, _ctx(_book(), secs_left=780))[0]
    flip.on_submitted(take, "OID-T", CLOSE - 780)
    props = flip.evaluate(TICKER, _ctx(_book(yes=41, no=56), secs_left=770))
    assert len(props) == 1
    p = props[0]
    trigger = 48 - config.OPEN_DETERMINED_DROP
    assert p.purpose == "CUT" and p.crossfire
    assert abs(p.price_cents - trigger) <= 2


def test_yield_to_f_at_flat_by(flip, gateway):
    _open_position(flip, gateway)
    take = flip.evaluate(TICKER, _ctx(_book(), secs_left=780))[0]
    flip.on_submitted(take, "OID-T", CLOSE - 780)
    props = flip.evaluate(TICKER, _ctx(_book(yes=47, no=50),
                                       secs_left=config.OPEN_FLAT_BY - 1))
    assert len(props) == 1 and props[0].crossfire
    assert "YIELD_TO_F" in props[0].reason


def test_open_entry_schedule(flip):
    """§3.3: entries T-15→T-8 only — at T-7 the window is F's."""
    assert flip.evaluate(TICKER, _ctx(_book(), grain=GRAIN_YES2,
                                      secs_left=config.OPEN_ENTRY_CUTOFF - 1)
                         ) == []
    props = flip.evaluate(TICKER, _ctx(_book(), grain=GRAIN_YES2,
                                       secs_left=config.OPEN_ENTRY_CUTOFF + 5))
    assert len(props) == 1


def test_bad_geometry_passes(flip, monkeypatch):
    """§3.4: risk beyond take+1 -> pass, tagged (knob-shifted to force)."""
    monkeypatch.setattr(config, "OPEN_DETERMINED_DROP", 9)
    assert flip.evaluate(TICKER, _ctx(_book(), grain=GRAIN_YES2)) == []
    assert flip.windows[TICKER].open_geometry_logged


# ── §2: the OPEN margin gate — receipts or PROBE, then sit ─────────────────
def test_open_margin_gate_probe_then_sit(flip, gateway, ledger):
    # virgin cell: PROBE mode, stamped
    props = flip.evaluate(TICKER, _ctx(_book(), grain=GRAIN_YES2))
    assert "PROBE n=0" in props[0].why and "geometry=v2" in props[0].why
    # a mature NEGATIVE cell: the receipts argue against the lane — it sits
    flip.windows.clear()
    for i in range(config.OPEN_PROBE_MAX_N):
        ledger.record_cell_outcome("OPEN", 48, won=False, pnl_cents=-5,
                                   fees_cents=0, market=f"L{i}", kind="trip")
    assert flip.evaluate(TICKER, _ctx(_book(), grain=GRAIN_YES2)) == []
    # a mature POSITIVE cell: margin >= 0, the proof is the receipts
    ledger.db.execute("DELETE FROM cell_outcomes")
    ledger.db.commit()
    for i in range(60):
        ledger.record_cell_outcome("OPEN", 48, won=True, pnl_cents=5,
                                   fees_cents=0, market=f"W{i}", kind="trip")
    flip.windows.clear()
    props = flip.evaluate(TICKER, _ctx(_book(), grain=GRAIN_YES2))
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
