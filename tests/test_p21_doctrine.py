"""P21 "THE DOCTRINE ENGINE" — A1 the netting model, A2 REJECT_SELF_NET,
A3 the grain, A5 the patient hold, A6 hunt seniority, B1-B3 the registry.

The doctrine IS the code, and these tests keep the beliefs honest."""

import pytest

from relay_engine import config, failures, semantics
from relay_engine.book import OrderBook
from relay_engine.custodian import Custodian
from relay_engine.feed import DegradeLadder
from relay_engine.fills import FillBooker
from relay_engine.gateway import Order, WallRejection
from relay_engine.grain import grain
from relay_engine.lane_flip import FLIP_CURFEW, LaneFlip
from relay_engine.spotlead import Needle

TICKER = "KXBTC15M-02JAN251000-T99"
EVENT = TICKER.rsplit("-", 1)[0]
CLOSE = 1_000_000.0

GRAIN_NO3 = {"direction": "no", "length": 3, "k": 4}
GRAIN_YES2 = {"direction": "yes", "length": 2, "k": 4}


def _book(yes=48, no=49, yq=10, nq=10):
    b = OrderBook(market=TICKER)
    b.apply_snapshot({yes: yq} if yes is not None else {},
                     {no: nq} if no is not None else {}, ts=1.0)
    return b


def _ctx(book, secs_left=800, grain=None, spotlead=None):
    return {"book": book, "now": CLOSE - secs_left, "close_ts": CLOSE,
            "spot": None, "grain": grain, "spotlead": spotlead}


@pytest.fixture
def flip(gateway, ledger, surface):
    failures.configure(ledger, alert_fn=lambda m: None, run_mode="TEST",
                       boot_id=1)
    custodian = Custodian(gateway, ledger, surface, ladder=DegradeLadder())
    return LaneFlip(gateway, custodian=custodian)


def _open_position(flip, gateway, side="yes", entry=48, grain_ctx=None):
    """Drive an OPEN entry to a filled patient hold; returns the window."""
    grain_ctx = grain_ctx or (GRAIN_YES2 if side == "yes" else GRAIN_NO3)
    props = flip.evaluate(TICKER, _ctx(_book(), grain=grain_ctx))
    assert len(props) == 1 and props[0].side == side
    flip.on_submitted(props[0], "OID-E", CLOSE - 800)
    gateway.positions[(EVENT, TICKER, "FLIP")] = 1 if side == "yes" else -1
    flip.note_fill(TICKER, side, entry, CLOSE - 795)
    w = flip.windows[TICKER]
    assert side in w.opens
    return w


# ── A2: the wall — netting is an exit's job, not an entry's ────────────────
def test_wall_refuses_self_net_entry(gateway, ledger, surface):
    failures.configure(ledger, alert_fn=lambda m: None, run_mode="TEST",
                       boot_id=1)
    book = _book()
    r = gateway.submit(Order(lane="FLIP", event=EVENT, market=TICKER,
                             side="yes", action="buy", price_cents=48,
                             count=1, size_tier=config.TIER_PROBE,
                             purpose="ENTRY",
                             why="OPEN grain yesx2 · join 48c · PROBE n=0"),
                       book)
    gateway.on_fill(r.order_id, count=1)   # the account now holds +1 yes
    # ANY lane's opposite-side ENTRY buy is refused — the venue nets across
    # our lanes whether we like it or not (account scope, not lane scope).
    with pytest.raises(WallRejection) as ei:
        gateway.submit(Order(lane="D", event=EVENT, market=TICKER,
                             side="no", action="buy", price_cents=49,
                             count=1, size_tier=config.TIER_PROBE,
                             purpose="ENTRY",
                             why="d-table verdict no@49¢ · reserved"), book)
    assert ei.value.wall == "REJECT_SELF_NET"
    # custodian-routed exits pass: netting IS an exit's job
    gateway.submit(Order(lane="FLIP", event=EVENT, market=TICKER,
                         side="yes", action="sell", price_cents=53, count=1,
                         size_tier=config.TIER_PROBE, purpose="EXIT"), book)


# ── A1: the belt — a slipped self-net entry books as the exit it IS ────────
def test_netting_belt_books_exit_of_held_side(gateway, ledger, surface):
    failures.configure(ledger, alert_fn=lambda m: None, run_mode="TEST",
                       boot_id=1)
    booker = FillBooker(gateway, ledger, surface)
    book = _book()
    r = gateway.submit(Order(lane="FLIP", event=EVENT, market=TICKER,
                             side="yes", action="buy", price_cents=48,
                             count=1, size_tier=config.TIER_PROBE,
                             purpose="ENTRY",
                             why="OPEN grain yesx2 · join 48c · PROBE n=0"),
                       book)
    booker.sweep([{"fill_id": "fb-e", "order_id": r.order_id,
                   "yes_price_dollars": "0.4800", "count": 1}], now=1000.0)
    assert gateway.positions[(EVENT, TICKER, "FLIP")] == 1
    # a no-buy ENTRY that slipped past the wall (belt-and-suspenders)
    slip = Order(lane="FLIP", event=EVENT, market=TICKER, side="no",
                 action="buy", price_cents=60, count=1,
                 size_tier=config.TIER_PROBE, purpose="ENTRY")
    gateway.order_index["OID-SLIP"] = slip
    booker.sweep([{"fill_id": "fb-slip", "order_id": "OID-SLIP",
                   "yes_price_dollars": "0.4000", "count": 1}], now=1005.0)
    side, action, px = ledger.db.execute(
        "SELECT side, action, price_cents FROM fills"
        " ORDER BY id DESC LIMIT 1").fetchone()
    # economically a SELL of the held yes at 100−60=40 — booked as exactly that
    assert (side, action, px) == ("yes", "EXIT", 40)
    assert gateway.positions[(EVENT, TICKER, "FLIP")] == 0
    n = ledger.db.execute("SELECT COUNT(*) FROM failures WHERE"
                          " why_tag='SELF_NET_BOOKED'").fetchone()[0]
    assert n == 1


# ── A3: the grain — the herd's compass from OUR settled windows ────────────
def test_grain_reads_streak_from_window_outcomes(ledger):
    assert grain(ledger) is None                      # empty screen
    for i, settled_yes in enumerate([True, False, False, False, True]):
        ledger.record_outcome(f"M{i}", settled_yes, now=1000.0 + i)
    g = grain(ledger)
    # most recent (M4) is yes with streak length 1
    assert g == {"direction": "yes", "length": 1, "k": config.GRAIN_K}
    ledger.record_outcome("M5", True, now=1010.0)
    ledger.record_outcome("M6", True, now=1011.0)
    g = grain(ledger)
    assert g["direction"] == "yes" and g["length"] == 3
    # idempotent: re-recording a settled market changes nothing (A3 lineage)
    ledger.record_outcome("M6", False, now=1012.0)
    assert grain(ledger)["length"] == 3


# ── A5: THE PATIENT HOLD — no stop, no scratch, no time-box in the band ────
def test_patient_hold_ignores_wiggles(flip, gateway):
    """P26 §3.4 TIGHTENED the patient hold's geometry: the hold ignores
    wiggles INSIDE max(band floor, entry−6) — a 5¢ wiggle proposes NOTHING
    beyond the resting take. (P21's −12¢ tolerance was the leak: risk must
    fit the take, so the determined trigger is entry−6 now.)"""
    _open_position(flip, gateway, side="yes", entry=48)
    props = flip.evaluate(TICKER, _ctx(_book(yes=48, no=49), secs_left=780))
    assert [p.reason for p in props] == [f"open take entry+{config.OPEN_TAKE_CENTS}"]
    flip.on_submitted(props[0], "OID-T", CLOSE - 780)
    # mark wiggles to entry−5 (>= trigger 42): the hold HOLDS
    for secs in (770, 760, 750):
        assert flip.evaluate(TICKER, _ctx(_book(yes=43, no=54),
                                          secs_left=secs)) == []


def test_determined_against_band_exit(flip, gateway):
    """Exit two of three, P26 §3.2/§3.4: mark below the v2 trigger
    (max(band floor, entry−6)) → the evacuation CROSSES IMMEDIATELY — the
    maker-grace slide (tonight's 31/20/33 fills on ~35 triggers) is dead."""
    _open_position(flip, gateway, side="yes", entry=48)
    take = flip.evaluate(TICKER, _ctx(_book(), secs_left=780))[0]
    flip.on_submitted(take, "OID-T", CLOSE - 780)
    # P-FLIP-THESIS-1 §2: the patience floor gates the cut — this law-test
    # exercises the POST-window decision, so the fill ages past the floor
    flip.windows[TICKER].opens["yes"]["fill_ts"] = CLOSE - 1100
    # yes mark 41 < trigger 42: determined against us — crossfire NOW
    props = flip.evaluate(TICKER, _ctx(_book(yes=41, no=56), secs_left=770))
    assert len(props) == 1
    p = props[0]
    assert (p.purpose, p.action, p.price_cents) == ("CUT", "sell", 41)
    assert p.crossfire
    assert "open determined-against" in p.reason
    assert "evacuate now" in p.reason


def test_determined_against_needle_collapse_sustained(flip, gateway):
    """ΔP-collapse >= K points against the held side, 2 sustained polls →
    determined; one flicker poll is NOT determination."""
    _open_position(flip, gateway, side="yes", entry=48)
    take = flip.evaluate(TICKER, _ctx(_book(), secs_left=780))[0]
    flip.on_submitted(take, "OID-T", CLOSE - 780)
    # P-FLIP-THESIS-1 §2: post-patience-window law (the floor is its own test)
    flip.windows[TICKER].opens["yes"]["fill_ts"] = CLOSE - 1100
    collapse = Needle(side="no", d_before=10.0, d_after=80.0,
                      delta_p=config.OPEN_DETERMINED_K_POINTS + 3.0,
                      fair_cents=30.0, t_remaining=700.0)
    # poll 1: counted, not yet determined (mark above the v2 trigger)
    assert flip.evaluate(TICKER, _ctx(_book(yes=44, no=54), secs_left=770,
                                      spotlead=collapse)) == []
    # flicker clears the count
    assert flip.evaluate(TICKER, _ctx(_book(yes=44, no=54),
                                      secs_left=768)) == []
    assert flip.evaluate(TICKER, _ctx(_book(yes=44, no=54), secs_left=766,
                                      spotlead=collapse)) == []
    # two SUSTAINED polls → evacuate NOW (P26 §3.2: crossfire, no maker)
    props = flip.evaluate(TICKER, _ctx(_book(yes=44, no=54), secs_left=764,
                                       spotlead=collapse))
    assert len(props) == 1
    assert props[0].purpose == "CUT" and props[0].crossfire
    assert "ΔP-collapse" in props[0].reason


def test_open_yield_flattens(flip, gateway):
    """Exit three of three, P26 §3.3: YIELD — OPEN is flat by T-6
    (YIELD_TO_F, crossfire); F owns the endgame floor and the SELF_NET
    storm class dies by schedule. (Was CURFEW at T-4 pre-P26.)"""
    _open_position(flip, gateway, side="yes", entry=48)
    take = flip.evaluate(TICKER, _ctx(_book(), secs_left=780))[0]
    flip.on_submitted(take, "OID-T", CLOSE - 780)
    props = flip.evaluate(TICKER, _ctx(_book(yes=47, no=50),
                                       secs_left=config.OPEN_FLAT_BY - 1))
    assert len(props) == 1
    assert props[0].purpose == "CUT" and props[0].crossfire
    assert "open yield to F" in props[0].reason


def test_open_exit_realizes_no_scratch_count(flip, gateway):
    """A5: an OPEN exit realizes against its entry and counts NO scratch —
    the patient hold has no scratches, and the sit-out never feeds off it."""
    w = _open_position(flip, gateway, side="yes", entry=48)
    flip.note_exit(TICKER, "yes", 30, CLOSE - 700)
    assert "yes" not in w.opens
    assert w.window_realized == -18
    assert w.scratches == 0


# ── A6: HUNT seniority — a live needle voids OPEN's premise ────────────────
def test_confirmed_needle_suppresses_open_entry(flip):
    needle = Needle(side="yes", d_before=60.0, d_after=5.0,
                    delta_p=config.HUNT_NEEDLE_POINTS + 5.0,
                    fair_cents=70.0, t_remaining=700.0)
    props = flip.evaluate(TICKER, _ctx(_book(), grain=GRAIN_NO3,
                                       spotlead=needle))
    assert all("OPEN" not in (p.why or "") for p in props)
    # the same book with no needle posts the OPEN entry
    flip.windows.clear()
    props2 = flip.evaluate(TICKER, _ctx(_book(), grain=GRAIN_NO3))
    assert len(props2) == 1 and props2[0].why.startswith("OPEN")


# ── B1/B3: the registry and its own wall ───────────────────────────────────
def test_registry_parses_all_knowns():
    entries = semantics.parse_registry()
    # 16 at P21 + the score (P22) + fee truth and the anchor shield (P24)
    # + the proof law (P26) + the phone-is-the-console pattern (DIAG-1)
    # + the-governor-is-the-halt constitution (P27)
    assert len(entries) == 22
    for e in entries:
        assert e["law"] and e["code"] and e["tape"], e["answer"]


def test_registry_wall_every_entry_names_a_tape_line():
    """B3 (Adversary: 'the registry could rot — that rule is the wall')."""
    assert semantics.validate_registry() == []
    lines = semantics.tape_lines()
    for e in semantics.parse_registry():
        assert e["tape"] in lines


def test_registry_wall_catches_rot(tmp_path):
    rotten = tmp_path / "SEMANTICS.md"
    rotten.write_text("## KNOWN\n\n### 1. A hopeful belief\n"
                      "- LAW: hope is a strategy\n"
                      "- CODE: `relay_engine/nowhere.py:1`\n"
                      "- TAPE: `a tape line that does not exist`\n",
                      encoding="utf-8")
    errs = semantics.validate_registry(str(rotten))
    assert len(errs) == 1 and "unknown tape line" in errs[0]


# ── B2: KNOWLEDGE_DRIFT — reality outranks the registry ────────────────────
def test_knowledge_drift_demotes_and_pages_once(ledger, surface):
    failures.configure(ledger, alert_fn=lambda m: None, run_mode="TEST",
                       boot_id=1)
    # the registry's tape tests read window_econ too — create the organ
    from relay_engine.window_econ import WindowEcon
    WindowEcon(ledger, None, surface, None)
    import time as _t
    # a fresh registry run over a clean ledger: every KNOWN green
    lines = semantics.drift_section(ledger)
    assert any("KNOWN green" in ln for ln in lines)
    # answer 13's tape test fails: a WINDOW_ECON_DIVERGENCE page lands
    ledger.db.execute(
        "INSERT INTO failures (ts, why_tag, what) VALUES (?,?,?)",
        (_t.time(), "WINDOW_ECON_DIVERGENCE", "broker -7c vs fills -65c"))
    ledger.db.commit()
    lines2 = semantics.drift_section(ledger)
    demoted = [ln for ln in lines2 if "QUESTION (demoted)" in ln]
    assert len(demoted) == 1
    assert "The venue nets; our books net" in demoted[0]
    n = ledger.db.execute("SELECT COUNT(*) FROM failures WHERE"
                          " why_tag='KNOWLEDGE_DRIFT'").fetchone()[0]
    assert n == 1
    # the page fires ONCE per transition — a second pack does not re-page
    semantics.drift_section(ledger)
    n2 = ledger.db.execute("SELECT COUNT(*) FROM failures WHERE"
                           " why_tag='KNOWLEDGE_DRIFT'").fetchone()[0]
    assert n2 == 1
