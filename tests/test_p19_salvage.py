"""WO-P19 FINAL "SALVAGE, SEAL, AND LET IT RUN" — the salvage machine (§2),
the P-suppression lane consult (§3.1), state pruning (§3.2), and the
unattended-run tape (§4)."""

import json

import pytest

from relay_engine import config, delta, failures
from relay_engine.book import OrderBook
from relay_engine.custodian import OpenPosition, salvage_params
from relay_engine.shadow_runner import ShadowEngine

TICKER = "KXBTC15M-02JAN251000-T99"
EVENT = TICKER.rsplit("-", 1)[0]
STRIKE = 118_000.0
CLOSE = 1_000_000.0


@pytest.fixture
def engine(tmp_path):
    e = ShadowEngine(db_path=str(tmp_path / "p19.db"))
    e.boot()
    e.telegram_sent = []
    e.telegram.send = e.telegram_sent.append
    failures.configure(e.ledger, alert_fn=e.telegram.alert, run_mode="TEST", boot_id=1)
    e.gateway.alert_fn = e.telegram.alert
    yield e
    failures._ledger = None
    failures._alert_fn = None


@pytest.fixture
def table(monkeypatch):
    def p_survive(d, t, session="ALL"):
        edge = min(1.0, d / (0.3 * max(1.0, t)))
        return 0.5 + 0.43 * edge
    monkeypatch.setattr(delta, "p_survive", p_survive)
    return p_survive


def make_book(yes=62, no=30):
    b = OrderBook(market=TICKER)
    b.apply_snapshot({yes: 10}, {no: 10}, ts=1.0)
    return b


def f_pos(entry=95, p_entry=0.93, d_entry=200.0):
    return OpenPosition(event=EVENT, market=TICKER, lane="F", side="yes",
                        count=1, entry_price_cents=entry, entry_p_win=0.95,
                        size_tier=config.TIER_PROBE, entry_time=CLOSE - 600,
                        d_entry=d_entry, t_entry=500.0, p_entry=p_entry)


def tick(engine, pos, spot, now, yes=62):
    return engine.custodian.tick(
        books={TICKER: make_book(yes=yes)},
        close_ts_of=lambda m: CLOSE, now=now, balance_usd=100.0, spot=spot,
        boundaries={TICKER: (None, STRIKE)})


# ── §2.5: the promotion ────────────────────────────────────────────────────
def test_promotion_registered_and_paged(tmp_path):
    e = ShadowEngine(db_path=str(tmp_path / "promo.db"))
    sent = []
    e.telegram.send = sent.append   # hook BEFORE boot — the page is at boot
    e.boot()
    assert e.custodian.params["F"].salvage_enabled is True
    assert e.custodian.params["H8"].salvage_enabled is True
    assert any(m.startswith("👑 custodian promoted") for m in sent)


# ── §2.2/2.3: the trigger and the maker-first execution ────────────────────
def test_needle_collapse_salvages_maker_first(engine, table):
    """Entry anchored at p=0.93; spot collapses through the strike — 2
    sustained ticks -> maker EXIT at the held side's bid, casefile paged."""
    pos = f_pos()
    engine.custodian.adopt(pos)
    engine.ledger.record_fill(TICKER, "F", "yes", "ENTRY", 95, 1, "PROBE")
    spot_bad = STRIKE - 90       # through the strike: p_held collapses
    tick(engine, pos, spot_bad, now=CLOSE - 400)      # strike 1
    assert pos.salvage_oid is None                    # sustained, not a knife
    tick(engine, pos, spot_bad, now=CLOSE - 399)      # strike 2 -> TRIGGER
    assert pos.salvage_attempted is True
    assert pos.salvage_oid is not None                # maker resting
    order = engine.gateway.order_index[pos.salvage_oid]
    assert order.purpose == "EXIT" and order.price_cents == 62  # at the bid
    assert "SALVAGE maker" in order.reason
    page = [m for m in engine.telegram_sent if m.startswith("✂️ SALVAGE F")][0]
    assert "needle" in page and "est save" in page and "(cost 95)" in page
    row = engine.ledger.db.execute(
        "SELECT detail FROM surface_rows WHERE state='SALVAGE'").fetchone()[0]
    d = json.loads(row)
    assert d["entry"] == 95 and "delta_p" in d and "est_save" in d


def test_unfilled_maker_crossfires_after_r(engine, table):
    pos = f_pos()
    engine.custodian.adopt(pos)
    engine.ledger.record_fill(TICKER, "F", "yes", "ENTRY", 95, 1, "PROBE")
    spot_bad = STRIKE - 90
    tick(engine, pos, spot_bad, now=CLOSE - 400)
    tick(engine, pos, spot_bad, now=CLOSE - 399)
    oid = pos.salvage_oid
    cuts = tick(engine, pos, spot_bad, now=CLOSE - 399 + config.SALVAGE_R_S + 1)
    assert cuts and cuts[0][2] == "SALVAGE_CROSSFIRE"
    assert oid not in engine.gateway.resting          # maker canceled first
    assert f"{TICKER}:F" not in engine.custodian.positions
    cut_row = engine.ledger.db.execute(
        "SELECT action FROM fills WHERE action='CUSTODIAN_EXIT'").fetchone()
    assert cut_row is not None


def test_one_attempt_per_position(engine, table):
    pos = f_pos()
    engine.custodian.adopt(pos)
    engine.ledger.record_fill(TICKER, "F", "yes", "ENTRY", 95, 1, "PROBE")
    spot_bad = STRIKE - 90
    tick(engine, pos, spot_bad, now=CLOSE - 400)
    tick(engine, pos, spot_bad, now=CLOSE - 399)
    first_oid = pos.salvage_oid
    # the maker FILLS (booking clears custody via the P14 belt) — simulate a
    # re-adoption of the same shape: attempted stays latched on the pos object
    tick(engine, pos, spot_bad, now=CLOSE - 398)
    assert pos.salvage_oid == first_oid               # no second maker


def test_no_anchor_no_salvage(engine, table):
    pos = f_pos(p_entry=None)
    engine.custodian.adopt(pos)
    tick(engine, pos, STRIKE - 90, now=CLOSE - 400)
    tick(engine, pos, STRIKE - 90, now=CLOSE - 399)
    assert pos.salvage_attempted is False             # disabled, tagged


def test_blind_spot_and_floor_never_salvage(engine, table):
    pos = f_pos()
    engine.custodian.adopt(pos)
    tick(engine, pos, None, now=CLOSE - 400)          # BLIND
    tick(engine, pos, None, now=CLOSE - 399)
    assert pos.salvage_attempted is False
    pos2 = f_pos()
    engine.custodian.positions.clear()
    engine.custodian.adopt(pos2)
    t_inside = CLOSE - config.SALVAGE_T_FLOOR_S + 5   # inside the floor
    tick(engine, pos2, STRIKE - 90, now=t_inside)
    tick(engine, pos2, STRIKE - 90, now=t_inside + 1)
    assert pos2.salvage_attempted is False


def test_recovery_resets_strikes(engine, table):
    pos = f_pos()
    engine.custodian.adopt(pos)
    tick(engine, pos, STRIKE - 90, now=CLOSE - 400)   # strike 1
    tick(engine, pos, STRIKE + 200, now=CLOSE - 399)  # recovered
    assert pos.salvage_strikes == 0
    tick(engine, pos, STRIKE - 90, now=CLOSE - 398)   # needs 2 fresh again
    assert pos.salvage_attempted is False


# ── §2.1: the anchor at custody registration ───────────────────────────────
def test_anchor_computed_at_fill_booking(engine, table, monkeypatch):
    from relay_engine import venue
    from relay_engine.gateway import Order
    import time as _t
    engine.market_meta[TICKER] = {"close_ts": _t.time() + 500,
                                  "boundary_lo": None, "boundary_hi": STRIKE}
    engine.record_spot(STRIKE + 200, _t.time())
    monkeypatch.setattr(venue, "parse_fill", lambda r, s: (97.0, 0, 1))
    entry = Order(lane="F", event=EVENT, market=TICKER, side="yes",
                  action="buy", price_cents=97, count=1,
                  size_tier=config.TIER_PROBE, purpose="ENTRY", band=(95, 99))
    r = engine.gateway.submit(entry, make_book(yes=97, no=2))
    engine.fills.sweep([{"fill_id": "a1", "order_id": r.order_id, "count": 1}])
    pos = engine.custodian.positions[f"{TICKER}:F"]
    assert pos.p_entry is not None and pos.d_entry == 200.0


# ── §2.4: mutual suppression ───────────────────────────────────────────────
def test_hunt_suppressed_while_salvage_active(engine, table):
    pos = f_pos()
    pos.salvage_attempted = True
    engine.custodian.adopt(pos)
    assert engine.custodian.salvage_in_progress(TICKER) is True
    from relay_engine.spotlead import Needle
    sl = Needle(side="no", d_before=200, d_after=90, delta_p=40,
                fair_cents=90, t_remaining=500)
    props = engine.flip.evaluate(TICKER, {
        "book": make_book(), "now": CLOSE - 500, "close_ts": CLOSE,
        "spot": STRIKE - 90, "spotlead": sl, "salvage_active": True})
    assert [p for p in props if p.purpose == "ENTRY"] == []


# ── §3.1: P consults the ctx flag (the gap, closed in the lane) ────────────
def test_p_lane_itself_yields_on_confirmed_needle(engine):
    from relay_engine.lanes import PLaneWrapper
    from relay_engine.spotlead import Needle
    wrapper = engine.lanes[4]
    assert isinstance(wrapper, PLaneWrapper)
    sl = Needle(side="yes", d_before=100, d_after=20, delta_p=22,
                fair_cents=80, t_remaining=400)
    d = wrapper.evaluate(TICKER, {"close_ts": CLOSE, "now": CLOSE - 400,
                                  "needle_confirmed": True, "spotlead": sl})
    assert d.proposal is None
    assert d.pass_reason.startswith("P_SUPPRESSED_SPOTLEAD")
    assert "+22pts" in d.pass_reason


# ── §3.2: state pruning at rollover ────────────────────────────────────────
def test_per_market_state_prunes_on_close(engine):
    engine.flip._window(TICKER, CLOSE)
    engine.fh8_shared.ladders[TICKER] = object()
    engine.fh8_shared._cache[TICKER] = (1.0, ("PASS", "x"))
    engine.lanes[4].p.states[TICKER] = object()
    engine.on_market_closed(TICKER)
    assert TICKER not in engine.flip.windows
    assert TICKER not in engine.fh8_shared.ladders
    assert TICKER not in engine.fh8_shared._cache
    assert TICKER not in engine.lanes[4].p.states


# ── §2.6: the settlement counterfactual ────────────────────────────────────
def test_salvage_verdict_lands_at_settlement(engine, table):
    engine.market_meta[TICKER] = {"close_ts": CLOSE}
    engine.econ.open_bracket(TICKER, 10_000, now=CLOSE - 500)
    engine.ledger.record_fill(TICKER, "F", "yes", "ENTRY", 95, 1, "PROBE")
    engine.surface.write_row("F", TICKER, f"w-{TICKER}", "SALVAGE",
                             detail=json.dumps({"side": "yes", "entry": 95,
                                                "mark": 62, "delta_p": -18,
                                                "fair": 5, "est_save": 57}))
    engine.ledger.record_fill(TICKER, "F", "yes", "CUSTODIAN_EXIT", 62, 1,
                              "PROBE")
    engine.settle_traded_market(TICKER, settled_yes=False, now=CLOSE + 20)
    verdict = engine.ledger.db.execute(
        "SELECT detail FROM surface_rows WHERE state='SALVAGE_VERDICT'"
    ).fetchone()[0]
    d = json.loads(verdict)
    # sold at 62 (realized −33) vs holding to a NO settle (−95): dodged +62
    assert d["verdict"] == "DODGED_LOSS" and d["dodged_cents"] == 62


# ── §4: the unattended tape ────────────────────────────────────────────────
def test_p19_tape_grade_clean_and_catches_retired_fatals(engine):
    from scripts.tape_grade import CHECKS_P19, grade
    assert all(ok for _, ok, _ in grade(engine.ledger.db, checks=CHECKS_P19))
    failures.fail("BATON_VIOLATION", "synthetic", market=TICKER, alert=False)
    results = {n: ok for n, ok, _ in grade(engine.ledger.db, checks=CHECKS_P19)}
    assert results["zero retired-class FATALs (the tuition already paid)"] is False
