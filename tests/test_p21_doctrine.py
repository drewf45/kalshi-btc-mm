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


def _book(yes=40, no=49, yq=10, nq=10):
    b = OrderBook(market=TICKER)
    b.apply_snapshot({yes: yq} if yes is not None else {},
                     {no: nq} if no is not None else {}, ts=1.0)
    return b


def _ctx(book, secs_left=850, grain=None, spotlead=None, spot=None):
    return {"book": book, "now": CLOSE - secs_left, "close_ts": CLOSE,
            "spot": spot, "grain": grain, "spotlead": spotlead}


@pytest.fixture
def flip(gateway, ledger, surface):
    failures.configure(ledger, alert_fn=lambda m: None, run_mode="TEST",
                       boot_id=1)
    custodian = Custodian(gateway, ledger, surface, ladder=DegradeLadder())
    return LaneFlip(gateway, custodian=custodian)


def _favored_book(side):
    """WO-2026-07-22-E: a book whose FAVORED (higher-bid) side is `side` at
    60c — inside the buyable [50,70] band, so FLIP buys THAT side."""
    return _book(yes=60, no=40) if side == "yes" else _book(yes=40, no=60)


def _open_position(flip, gateway, side="yes", entry=60, grain_ctx=None):
    """Drive an OPEN entry (the FAVORED side) to a filled position; returns
    the window. WO-2026-07-22-E: entry is the favored 60c join."""
    grain_ctx = grain_ctx or (GRAIN_YES2 if side == "yes" else GRAIN_NO3)
    # WO-2026-07-22-F: prime the pile — a baseline small-skew poll in-window,
    # then the entry poll with the skew grown (+14) and the tape moving in the
    # favored direction (yes rises, no falls) so the trend agrees.
    if side == "yes":
        flip.evaluate(TICKER, _ctx(_book(yes=54, no=48), secs_left=835,
                                   spot=66000.0, grain=grain_ctx))
        props = flip.evaluate(TICKER, _ctx(_favored_book("yes"),
                                           secs_left=820, spot=66020.0,
                                           grain=grain_ctx))
    else:
        flip.evaluate(TICKER, _ctx(_book(yes=48, no=54), secs_left=835,
                                   spot=66000.0, grain=grain_ctx))
        props = flip.evaluate(TICKER, _ctx(_favored_book("no"),
                                           secs_left=820, spot=65980.0,
                                           grain=grain_ctx))
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
    """WO-2026-07-22-E: the patient hold is RETIRED — the favored side carries
    the MOMENTUM STOP from the first poll (entry−OPEN_MOMENTUM_STOP_C). A wiggle
    that stays ABOVE the stop (mark > entry−10) proposes NOTHING beyond the
    resting take; only a SUSTAINED mark at/below the stop (2 polls) exits."""
    from relay_engine.lane_flip import LaneFlip
    _open_position(flip, gateway, side="yes", entry=60)
    props = flip.evaluate(TICKER, _ctx(_book(yes=60, no=40), secs_left=780))
    # WO-2026-07-22-F: the resting take is entry + OPEN_GOUGE_C (17) → 60 → 77c
    take_px = LaneFlip._take_price(60)
    assert take_px == 70
    assert [p.reason for p in props] == \
        [f"open take → middle {take_px}c (entry 60, gouge +{take_px - 60})"]
    flip.on_submitted(props[0], "OID-T", CLOSE - 780)
    # mark wiggles to 55 (> stop 50): the momentum stop does NOT arm
    for secs in (770, 760, 750):
        assert flip.evaluate(TICKER, _ctx(_book(yes=55, no=45),
                                          secs_left=secs)) == []
    # but a SUSTAINED mark at/below the stop (50) exits on the 2nd poll
    assert flip.evaluate(TICKER, _ctx(_book(yes=50, no=50),
                                      secs_left=740)) == []
    props2 = flip.evaluate(TICKER, _ctx(_book(yes=50, no=50), secs_left=730))
    assert len(props2) == 1 and "momentum stop" in props2[0].reason


def test_determined_against_spot_evacuation(flip, gateway):
    """WO-2026-07-22-E: SPOT_DECIDED is retired. An adverse move that carries
    the held-side mark THROUGH the stop (mark < entry−OPEN_MOMENTUM_STOP_C),
    sustained 2 polls, evacuates MAKER-FIRST — but the book is already through
    us, so the resting sell crosses at the mark (CUT, crossfire)."""
    w = _open_position(flip, gateway, side="yes", entry=60)
    take = flip.evaluate(TICKER, _ctx(_book(yes=60, no=40), secs_left=780))[0]
    flip.on_submitted(take, "OID-T", CLOSE - 780)
    # mark drops through the stop (40 < entry−10 = 50), sustained 2 polls
    flip.evaluate(TICKER, _ctx(_book(yes=40, no=60), secs_left=771))
    props = flip.evaluate(TICKER, _ctx(_book(yes=40, no=60), secs_left=770))
    assert len(props) == 1
    p = props[0]
    assert (p.purpose, p.action, p.price_cents) == ("CUT", "sell", 40)
    assert p.crossfire
    assert "momentum stop" in p.reason
    assert w.opens["yes"].get("exit_reason") == "MOMENTUM_STOP"


def test_determined_against_needle_collapse_sustained(flip, gateway):
    """WO-2026-07-22-E: the momentum stop needs 2 SUSTAINED polls at/below
    entry−OPEN_MOMENTUM_STOP_C — one flicker poll (mark back above the stop)
    clears the count, so a single adverse print is NOT an exit."""
    _open_position(flip, gateway, side="yes", entry=60)
    take = flip.evaluate(TICKER, _ctx(_book(yes=60, no=40), secs_left=780))[0]
    flip.on_submitted(take, "OID-T", CLOSE - 780)
    # poll 1: mark at the stop (50 <= 50), counted — not yet 2 polls
    assert flip.evaluate(TICKER, _ctx(_book(yes=50, no=50),
                                      secs_left=770)) == []
    # a flicker back above the stop (55 > 50) clears the count
    assert flip.evaluate(TICKER, _ctx(_book(yes=55, no=45),
                                      secs_left=768)) == []
    assert flip.evaluate(TICKER, _ctx(_book(yes=50, no=50),
                                      secs_left=766)) == []
    # two SUSTAINED polls at/below the stop → exit (maker-first, at the stop)
    props = flip.evaluate(TICKER, _ctx(_book(yes=50, no=50), secs_left=764))
    assert len(props) == 1
    assert props[0].purpose == "EXIT" and not props[0].crossfire
    assert "momentum stop" in props[0].reason and props[0].price_cents == 50


def test_t10_handoff_clears_the_loser(flip, gateway):
    """P-FLIP-THESIS-1 §3.5 OVERTURNED the blind T-6 YIELD_TO_F flat: at
    T-10 the handoff is BOOK-AWARE — a side below basis is SOLD before
    F's window (crossfire), never dumped into the settlement zone."""
    _open_position(flip, gateway, side="yes", entry=60)
    take = flip.evaluate(TICKER, _ctx(_book(yes=60, no=40), secs_left=780))[0]
    flip.on_submitted(take, "OID-T", CLOSE - 780)
    props = flip.evaluate(TICKER, _ctx(_book(yes=50, no=50),
                                       secs_left=config.FLIP_DECISION_S - 1))
    assert len(props) == 1
    assert props[0].purpose == "CUT" and props[0].crossfire
    assert "open decision point" in props[0].reason
    assert props[0].price_cents == 50                 # sold at the mark, now


def test_open_exit_realizes_no_scratch_count(flip, gateway):
    """A5: an OPEN exit realizes against its entry and counts NO scratch —
    the patient hold has no scratches, and the sit-out never feeds off it."""
    w = _open_position(flip, gateway, side="yes", entry=60)
    flip.note_exit(TICKER, "yes", 30, CLOSE - 700)
    assert "yes" not in w.opens
    assert w.window_realized == -30
    assert w.scratches == 0


# ── A6: HUNT seniority — a live needle voids OPEN's premise ────────────────
def test_confirmed_needle_suppresses_open_entry(flip):
    needle = Needle(side="yes", d_before=60.0, d_after=5.0,
                    delta_p=config.HUNT_NEEDLE_POINTS + 5.0,
                    fair_cents=70.0, t_remaining=700.0)
    props = flip.evaluate(TICKER, _ctx(_book(yes=60, no=40), grain=GRAIN_NO3,
                                       spotlead=needle))
    assert all("OPEN" not in (p.why or "") for p in props)
    # the same favored book with no needle, primed into a formed pile, posts
    # the OPEN entry
    flip.windows.clear()
    flip.evaluate(TICKER, _ctx(_book(yes=54, no=48), secs_left=835,
                               spot=66000.0, grain=GRAIN_NO3))
    props2 = flip.evaluate(TICKER, _ctx(_book(yes=60, no=40), secs_left=820,
                                        spot=66020.0, grain=GRAIN_NO3))
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
