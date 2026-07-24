"""WO-2026-07-24-I "BLIND IS NOT BARE" — 1345-45 (1:32 PM): entry no@62 ×17 →
FLIP_UNCOVERED_LEG "held 17 > covered 0" → EXIT_OVERSIZE "resting x34 > held 17"
→ FLIP_UNCOVERED_FLATTENED ×17 @51 (−247¢). The leg was never bare — it was
DOUBLE-covered. Two instruments read 0 (in-memory buckets) and 34 (gateway
registry) for the same leg in the same minute, and the destructive close fired
on the reading that said zero.

THE LAW: a destructive close requires POSITIVE knowledge of bareness from broker
truth. Blindness PAUSES; it never fires. A position with a resting exit is
already bounded. P1 one coverage authority (the registry); P2 flatten on
positive bareness only, partial → maker top-up, surplus → cancel; P3 evidence-
clocked; P4 the merge respects cancel_tristate (kills the x34 at the root).

HARD RAIL: F byte-identical; stop/take/One-Shot untouched."""

import pytest

from relay_engine import config, failures
from relay_engine.book import OrderBook
from relay_engine.custodian import Custodian
from relay_engine.feed import DegradeLadder
from relay_engine.gateway import Gateway, Order
from relay_engine.lane_flip import LaneFlip

EVENT = "KXBTC15M-02JAN251230"
TICKER = EVENT + "-T30"
CLOSE = 1_000_000.0


@pytest.fixture(autouse=True)
def funnel(ledger):
    failures._warn_last.clear()
    failures.configure(ledger, alert_fn=lambda m: None, run_mode="TEST",
                       boot_id=1)
    yield
    failures._ledger = None


@pytest.fixture
def flip(gateway, ledger, surface):
    return LaneFlip(gateway, custodian=Custodian(gateway, ledger, surface,
                                                 ladder=DegradeLadder()))


def _book(yes=39, no=55):
    b = OrderBook(market=TICKER)
    b.apply_snapshot({yes: 100}, {no: 100}, ts=1.0)
    return b


def _rest(gw, oid, side, count, price=66):
    o = Order(lane="FLIP", event=EVENT, market=TICKER, side=side, action="sell",
              price_cents=price, count=count, size_tier=config.TIER_PROBE,
              purpose="EXIT")
    gw.order_index[oid] = o
    gw.resting[oid] = o


def _held17(flip, ledger, side="no"):
    """A booked no@62 ×17 leg (as the 1345 tape), with an open custody record."""
    ledger.record_fill(TICKER, "FLIP", side, "ENTRY", 62, 17, "PROBE")
    w = flip._window(TICKER, CLOSE)
    w.opens[side] = {"entry": 62, "fill_ts": CLOSE - 700, "count": 17,
                     "take_oid": None, "take_proposed": True,
                     "collapse_polls": 0, "catastrophe_polls": 0,
                     "det_ts": None, "entry_oid": None, "defer_polls": 0}
    return w


def _rows(ledger, tag):
    return ledger.db.execute(
        "SELECT COUNT(*) FROM failures WHERE why_tag=?", (tag,)).fetchone()[0]


# ── AC1: the 1345 replay (17 held, 34 resting) → surplus cancelled, no flatten ─
def test_replay_17_held_34_resting_cancels_surplus_no_flatten(flip, gateway,
                                                              ledger):
    """The exact 1345 inputs: held 17, resting 34 (a double take from the merge
    repost bug). The registry is the authority — COVERED, surplus 17 → cancel the
    duplicate, retain cover 17. No flatten order, no taker fee, escalation reset."""
    w = _held17(flip, ledger)
    _rest(gateway, "T1", "no", 17)          # the original take
    _rest(gateway, "T2", "no", 17)          # the phantom duplicate (merge repost)
    w.opens["no"]["take_oid"] = "T1"
    props = []
    flip._check_uncovered(w, TICKER, props, book=_book(), event=EVENT,
                          now=CLOSE - 690)
    # surplus cancelled, cover 17 retained — the ORIGINAL take (T1) survives
    remaining = gateway.resting_exits(TICKER, "no")
    assert sum(c for _, c in remaining) == 17 and ("T1", 17) in remaining
    assert _rows(ledger, "FLIP_COVER_SURPLUS") == 1
    assert _rows(ledger, "FLIP_UNCOVERED_FLATTENED") == 0    # never flattened
    assert not any(p.purpose == "CUT" for p in props)        # zero taker cross


# ── AC2: positive bareness (17 held, 0 resting confirmed) → the flatten survives ─
def test_replay_17_held_0_resting_still_flattens(flip, gateway, ledger):
    """The doctrine SURVIVES: a leg that reads ZERO resting from the registry
    (positively bare) after grace + reconcile still flattens — bounded loss beats
    an unbounded ride. Blind pauses; BARE fires."""
    w = _held17(flip, ledger)               # registry empty for this leg
    w.opens["no"]["take_proposed"] = True   # past the first-propose grace
    w.heal_attempts["no"] = 2               # at the deadline (grace + reconcile spent)
    props = []
    flip._check_uncovered(w, TICKER, props, book=_book(no=51), event=EVENT,
                          now=CLOSE - 690)
    flat = next((p for p in props if "FLIP_UNCOVERED_FLATTENED"
                 in (p.reason or "")), None)
    assert flat is not None                 # positively bare (covered 0) → flatten


# ── AC3: read-fail (17 held, registry unreadable) → HEAL_BLIND, no orders ────
def test_replay_read_fail_heal_blind_no_escalation(flip, gateway, ledger,
                                                   monkeypatch):
    """Blindness PAUSES: when the coverage read is unavailable, the heal ladder
    logs HEAL_BLIND, advances no escalation, and proposes NO orders — absence of
    confirmation is not absence of cover."""
    w = _held17(flip, ledger)
    monkeypatch.setattr(gateway, "resting_exits", lambda *a, **k: None)
    for s in range(700, 690, -1):
        props = []
        flip._check_uncovered(w, TICKER, props, book=_book(no=51), event=EVENT,
                              now=CLOSE - s)
        assert props == []                  # never an order while blind
    assert _rows(ledger, "FLIP_UNCOVERED_FLATTENED") == 0
    assert "no" not in w.heal_attempts      # no escalation advanced


# ── AC1b: partial cover (17 held, 7 resting) → maker top-up, never a flatten ──
def test_partial_cover_tops_up_maker_never_flattens(flip, gateway, ledger):
    """0 < covered < held is bounded — top up the gap with a resting MAKER exit
    at the take, never a market flatten of a partially-covered leg."""
    w = _held17(flip, ledger)
    _rest(gateway, "T1", "no", 7)           # only 7 of 17 covered
    w.opens["no"]["take_oid"] = "T1"
    w.heal_attempts["no"] = 2               # at the deadline
    props = []
    flip._check_uncovered(w, TICKER, props, book=_book(no=51), event=EVENT,
                          now=CLOSE - 690)
    topup = next((p for p in props if "FLIP_COVER_TOPUP" in (p.reason or "")), None)
    assert topup is not None and topup.count == 10 and not topup.crossfire
    assert _rows(ledger, "FLIP_UNCOVERED_FLATTENED") == 0


# ── AC4/P4: the merge respects cancel_tristate — no double-cover ─────────────
def test_merge_unknown_cancel_keeps_one_take(flip, gateway):
    """The x34 ROOT: a merge whose take-cancel returns UNKNOWN must NOT clear
    take_oid and repost (that doubled the cover). The old take is kept; no repost
    is armed."""
    w = flip._window(TICKER, CLOSE)
    w.opens["no"] = {"entry": 61, "fill_ts": CLOSE - 700, "count": 7,
                     "take_oid": "T-OLD", "take_proposed": True,
                     "collapse_polls": 0, "catastrophe_polls": 0,
                     "det_ts": None, "entry_oid": None, "defer_polls": 0}
    gateway.cancel_tristate = lambda oid: "UNKNOWN"    # cancel in flight
    flip.note_fill(TICKER, "no", 61, CLOSE - 690, count=10)   # merge 7+10 = 17
    o = flip.windows[TICKER].opens["no"]
    assert o["count"] == 17                            # blended
    assert o["take_oid"] == "T-OLD"                    # kept — NOT cleared
    assert o["take_proposed"] is True                  # no repost armed


def test_merge_confirmed_cancel_reposts(flip, gateway):
    """A CONFIRMED cancel (CANCELED/ALREADY_TERMINAL) clears take_oid and arms
    the repost at the merged size — the normal merge, now guarded by the answer."""
    w = flip._window(TICKER, CLOSE)
    w.opens["no"] = {"entry": 61, "fill_ts": CLOSE - 700, "count": 7,
                     "take_oid": "T-OLD", "take_proposed": True,
                     "collapse_polls": 0, "catastrophe_polls": 0,
                     "det_ts": None, "entry_oid": None, "defer_polls": 0}
    gateway.cancel_tristate = lambda oid: "CANCELED"
    flip.note_fill(TICKER, "no", 61, CLOSE - 690, count=10)
    o = flip.windows[TICKER].opens["no"]
    assert o["count"] == 17 and o["take_oid"] is None and o["take_proposed"] is False


# ── HARD RAIL: F byte-identical ─────────────────────────────────────────────
def test_f_sizing_untouched():
    from relay_engine import scoring
    f = scoring.size_order(4162, 97, 10_000, lane="F")
    assert f.contracts == int(4162 * config.F_NOTIONAL_PCT // 97)
    assert "cap n/a" in f.reason
