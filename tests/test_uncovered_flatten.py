"""WO-UNCOVERED-FLATTEN — THE NAKED LEG DREW WATCHED (build 36).

Drew watched an uncovered FLIP leg ride 45→70→settlement doing nothing:
`_heal_uncovered` marked the leg's intent HEALED before a single exit
order confirmed resting, and every cover self-net-rejected into the void.

The law: COVER OR FLATTEN, NEVER BARE. A leg is healed only when a
resting exit CONFIRMS against the booked-held count. If cover cannot be
confirmed after a broker-truth reconcile + one retry, the leg is
FLATTENED at market (booked-net clamped). UNHEALABLE flattens FIRST, then
FATALs only if the flatten itself never registers a close.

HARD RAIL: cover/heal path only — no Kelly, cash-integrity, rate-halt,
HUNT, or OPEN-swing change."""

import json

import pytest

from relay_engine import config, failures, lane_flip
from relay_engine.book import OrderBook
from relay_engine.custodian import Custodian
from relay_engine.feed import DegradeLadder
from relay_engine.gateway import Order
from relay_engine.lane_flip import LaneFlip

TICKER = "KXBTC15M-02JAN251000-T99"
EVENT = TICKER.rsplit("-", 1)[0]
CLOSE = 1_000_000.0
GRAIN_YES2 = {"direction": "yes", "length": 2, "k": 4}


def _book(yes=40, no=49):
    b = OrderBook(market=TICKER)
    b.apply_snapshot({yes: 10}, {no: 10}, ts=1.0)
    return b


def _ctx(book, secs_left=850, grain=None, spot=None):
    return {"book": book, "now": CLOSE - secs_left, "close_ts": CLOSE,
            "spot": spot, "grain": grain, "spotlead": None}


@pytest.fixture(autouse=True)
def funnel(ledger):
    alerts = []
    failures._warn_last.clear()
    failures.configure(ledger, alert_fn=alerts.append, run_mode="TEST",
                       boot_id=1)
    yield alerts
    failures._ledger = None


@pytest.fixture
def flip(gateway, ledger, surface):
    return LaneFlip(gateway, custodian=Custodian(gateway, ledger, surface,
                                                 ladder=DegradeLadder()))


def _rows(ledger, tag):
    return ledger.db.execute(
        "SELECT COUNT(*) FROM failures WHERE why_tag=?", (tag,)).fetchone()[0]


def _open_leg(flip, gateway, ledger, entry=60):
    """A booked OPEN leg whose take rests — the healthy custody shape. Post
    WO-2026-07-22-E the entry is the FAVORED (higher) side: yes@entry > no@49,
    entry in [50,70]."""
    # WO-2026-07-22-F "wait for the pile": build the pile with two in-window
    # polls — a baseline (secs_into~65, small skew, spot low) then the entry poll
    # (secs_into~80, skew grown >=5, |trend|>=15 agreeing with favored yes, favored
    # depth >= other). _book(yes=entry) is favored-yes (entry>no=49) in [50,70].
    flip.evaluate(TICKER, _ctx(_book(yes=52), secs_left=835, spot=66000.0))
    props = flip.evaluate(TICKER, _ctx(_book(yes=entry), secs_left=820,
                                       spot=66020.0, grain=GRAIN_YES2))
    flip.on_submitted(props[0], "OID-E1", CLOSE - 800)
    ledger.record_fill(TICKER, "FLIP", "yes", "ENTRY", entry, 1, "PROBE")
    flip.note_fill(TICKER, "yes", entry, CLOSE - 790)
    return flip.windows[TICKER].opens["yes"]


# ── §4: normal path — cover confirms → healed, no page ─────────────────────
def test_cover_confirms_resting_heals_silently(flip, gateway, ledger):
    """A leg whose take confirms resting (take_oid registered) is healed —
    zero UNCOVERED page, zero flatten. The healthy path is untouched."""
    o = _open_leg(flip, gateway, ledger)
    p1 = flip.evaluate(TICKER, _ctx(_book(), secs_left=780))
    take = next(p for p in p1 if p.purpose == "EXIT")
    flip.on_submitted(take, "OID-T1", CLOSE - 780)     # CONFIRMS resting
    assert o["take_oid"] == "OID-T1"
    for s in (779, 778, 777, 776):                     # cycles pass, no drama
        flip.evaluate(TICKER, _ctx(_book(), secs_left=s))
    assert _rows(ledger, "FLIP_UNCOVERED_LEG") == 0
    assert _rows(ledger, "FLIP_UNCOVERED_FLATTENED") == 0


# ── §4: the 192145 replay — cover never confirms → FLATTEN, not ride ───────
def test_192145_self_net_void_flattens_not_rides(flip, gateway, ledger,
                                                 funnel):
    """The leg Drew watched: the take is PROPOSED every cycle but never
    confirms resting (the self-net void). It must not ride to settlement —
    within a bounded escalation it FLATTENS at market."""
    _open_leg(flip, gateway, ledger, entry=60)
    # the take proposes but we NEVER call on_submitted — it rejected into
    # the void, exactly as the live self-net storm did
    flatten = None
    for s in range(780, 770, -1):
        p = flip.evaluate(TICKER, _ctx(_book(yes=70), secs_left=s))
        flatten = next((x for x in p if x.purpose == "CUT" and
                        "FLIP_UNCOVERED_FLATTENED" in (x.reason or "")), None)
        if flatten:
            break
    assert flatten is not None and flatten.crossfire   # market close, NOW
    assert flatten.side == "yes" and flatten.count == 1
    # WO-2026-07-21-B Finding 3 (build 54): a 1-lot void LOOKS like the routine
    # cover-pending state at first (held 1 > covered 0, take proposed) → demoted
    # to FLIP_UNCOVERED_EXPECTED (debug), so it does NOT page at detect. It
    # reveals itself by ESCALATING — the FLATTEN is the real alarm and it pages.
    assert _rows(ledger, "FLIP_UNCOVERED_LEG") == 0     # looked routine; the FLATTEN is the page
    assert _rows(ledger, "FLIP_UNCOVERED_FLATTENED") == 1
    assert any("FLIP_UNCOVERED_FLATTENED" in a for a in funnel)


# ── §4: self-net reconcile — broker confirms held → cover recovers ─────────
def test_self_net_reconcile_cancels_stale_and_recovers(flip, gateway,
                                                       ledger):
    """A stale resting sell blocks the cover (self-net). The reconcile
    cancels it; broker confirms the held size; the revived record covers
    the whole leg. No flatten, no bare leg."""
    w = flip._window(TICKER, CLOSE)
    ledger.record_fill(TICKER, "FLIP", "yes", "ENTRY", 40, 1, "PROBE")
    gateway.positions[(EVENT, TICKER, "FLIP")] = 1
    w.fills["yes"] = 48          # rung-A leg: re-proposes its take, never
    w.first_fill_ts = CLOSE - 790  # confirms (the self-net void)
    w.trips = 1
    # a stale resting FLIP sell against the leg (the self-net artifact the
    # reconcile must cancel before a fresh cover can land)
    stale = gateway.submit(Order(
        lane="FLIP", event=EVENT, market=TICKER, side="yes", action="sell",
        price_cents=68, count=1, size_tier=config.TIER_PROBE,
        purpose="EXIT"), _book())
    assert stale.order_id in gateway.resting
    # drive past grace: the escalation reconciles, cancelling the stale
    # resting sell, and revives a fresh once-proposing cover
    for s in range(780, 772, -1):
        flip.evaluate(TICKER, _ctx(_book(yes=48), secs_left=s))
        if stale.order_id not in gateway.resting:
            break
    assert stale.order_id not in gateway.resting     # the stale sell cancelled
    o = flip.windows[TICKER].opens.get("yes")
    assert o is not None and o["count"] == 1         # revived whole leg
    assert _rows(ledger, "FLIP_UNCOVERED_FLATTENED") == 0


# ── §4: phantom gap — broker says smaller → no phantom cover, no bare ──────
def test_phantom_gap_reconciles_to_broker_no_flatten(flip, gateway, ledger):
    """Custody thinks it holds 2 but the broker booked an exit already
    (held=0). The reconcile corrects the count to broker truth — no
    phantom cover, no flatten, clean."""
    w = flip._window(TICKER, CLOSE)
    w.opens["yes"] = {"entry": 48, "fill_ts": CLOSE - 790, "count": 2,
                      "take_oid": None, "take_proposed": True,
                      "collapse_polls": 0, "det_ts": None,
                      "entry_oid": None, "defer_polls": 0, "done": True}
    ledger.record_fill(TICKER, "FLIP", "yes", "ENTRY", 40, 2, "PROBE")
    ledger.record_fill(TICKER, "FLIP", "yes", "EXIT", 50, 2, "PROBE")  # held 0
    for s in (780, 779, 778, 777):
        flip.evaluate(TICKER, _ctx(_book(), secs_left=s))
    assert _rows(ledger, "FLIP_UNCOVERED_FLATTENED") == 0
    assert _rows(ledger, "FLIP_UNCOVERED_UNHEALABLE") == 0


# ── §4: flatten is booked-net clamped (Engineer) ───────────────────────────
def test_flatten_clamped_to_booked_net(flip, gateway, ledger):
    """The flatten closes exactly what the broker says is held — never a
    phantom that re-triggers self-net. Memory says 2, broker holds 1."""
    w = flip._window(TICKER, CLOSE)
    w.opens["yes"] = {"entry": 48, "fill_ts": CLOSE - 1100, "count": 2,
                      "take_oid": None, "take_proposed": True,
                      "collapse_polls": 0, "det_ts": None,
                      "entry_oid": None, "defer_polls": 0}
    ledger.record_fill(TICKER, "FLIP", "yes", "ENTRY", 40, 2, "PROBE")
    ledger.record_fill(TICKER, "FLIP", "yes", "EXIT", 50, 1, "PROBE")  # held 1
    flatten = None
    for s in range(780, 772, -1):
        p = flip.evaluate(TICKER, _ctx(_book(yes=44), secs_left=s))
        flatten = next((x for x in p if x.purpose == "CUT" and
                        "FLIP_UNCOVERED_FLATTENED" in (x.reason or "")), None)
        if flatten:
            break
    assert flatten is not None and flatten.count == 1   # booked-net, not 2


# ── §4: flatten CONFIRMS → the FATAL never fires ───────────────────────────
def test_flatten_confirmed_prevents_fatal(flip, gateway, ledger):
    """When the flatten registers a close (on_submitted → flatten_oids),
    the leg is covered-pending-close and the UNHEALABLE FATAL never
    fires — the close is in the book."""
    w = flip._window(TICKER, CLOSE)
    w.opens["yes"] = {"entry": 48, "fill_ts": CLOSE - 1100, "count": 1,
                      "take_oid": None, "take_proposed": True,
                      "collapse_polls": 0, "det_ts": None,
                      "entry_oid": None, "defer_polls": 0}
    ledger.record_fill(TICKER, "FLIP", "yes", "ENTRY", 40, 1, "PROBE")
    for s in range(780, 772, -1):
        p = flip.evaluate(TICKER, _ctx(_book(yes=44), secs_left=s))
        flat = next((x for x in p if x.purpose == "CUT" and
                     "FLIP_UNCOVERED_FLATTENED" in (x.reason or "")), None)
        if flat:
            flip.on_submitted(flat, "FLAT-1", CLOSE - s)   # CONFIRMS
            break
    assert "yes" in w.flatten_oids
    # more cycles: the confirmed close holds the FATAL off
    for s in (770, 769, 768):
        flip.evaluate(TICKER, _ctx(_book(yes=44), secs_left=s))
    assert _rows(ledger, "FLIP_UNCOVERED_UNHEALABLE") == 0
    # and when it books, the leg is fully healed
    ledger.record_fill(TICKER, "FLIP", "yes", "EXIT", 44, 1, "PROBE")
    flip.note_exit(TICKER, "yes", 44, CLOSE - 760, count=1)
    assert "yes" not in w.flatten_oids


# ── WO-2026-07-23-B Part 2: the flatten has a price floor ──────────────────
def _at_flatten_deadline(flip, ledger, entry, count=1):
    """A booked, uncovered leg parked at the flatten deadline (esc2): its take
    is proposed but never confirms (the self-net void). Returns the window."""
    w = flip._window(TICKER, CLOSE)
    ledger.record_fill(TICKER, "FLIP", "yes", "ENTRY", entry, count, "PROBE")
    w.opens["yes"] = {"entry": entry, "fill_ts": CLOSE - 1100, "count": count,
                      "take_oid": None, "take_proposed": True, "done": False,
                      "collapse_polls": 0, "catastrophe_polls": 0, "det_ts": None,
                      "entry_oid": None, "defer_polls": 0, "hold": False}
    w.heal_attempts["yes"] = 2                    # jump to the flatten deadline
    w.heal_covered["yes"] = 0
    return w


def test_flatten_rests_at_floor_then_crosses_with_counted_breach(flip, ledger,
                                                                 funnel):
    """The 230230 leg: entry 61, the book already 45c (through the floor). The
    flatten must NOT dump at 45 on the first cross — it rests ONE poll at the
    floor (61−10−3 = 48), and only crosses below it on the next poll, counting
    the breach with its overshoot. (Acceptance #1.)"""
    floor = 61 - config.OPEN_MOMENTUM_STOP_C - config.SLIP_TOLERANCE_C   # 48
    w = _at_flatten_deadline(flip, ledger, entry=61)
    b = OrderBook(market=TICKER)
    b.apply_snapshot({45: 10}, {50: 10}, ts=1.0)      # best yes bid 45 < floor

    # poll 1: rest AT the floor, maker, no cross, no breach counted yet
    p1 = []
    flip._check_uncovered(w, TICKER, p1, book=b, event=EVENT, now=CLOSE - 700)
    sells1 = [p for p in p1 if p.action == "sell" and p.side == "yes"]
    assert len(sells1) == 1
    assert sells1[0].price_cents == floor and not sells1[0].crossfire
    assert _rows(ledger, "FLIP_FLOOR_BREACH") == 0
    assert _rows(ledger, "FLIP_UNCOVERED_FLATTENED") == 0
    assert w.flatten_floor_tried["yes"] is True

    # poll 2: still uncovered (the floor rest never filled) → cross at mark,
    # the breach counted with its overshoot (floor 48 − mark 45 = 3c)
    p2 = []
    flip._check_uncovered(w, TICKER, p2, book=b, event=EVENT, now=CLOSE - 699)
    sells2 = [p for p in p2 if p.action == "sell" and p.side == "yes"]
    assert len(sells2) == 1 and sells2[0].crossfire
    assert sells2[0].price_cents == 45
    assert _rows(ledger, "FLIP_FLOOR_BREACH") == 1        # counted, not silent
    assert any("FLIP_FLOOR_BREACH" in a for a in funnel)


def test_flatten_at_floor_that_fills_never_breaches(flip, ledger):
    """If the one-poll rest at the floor FILLS (the book covers it), the leg
    heals — no cross, no breach. The bounded ride did its job."""
    floor = 61 - config.OPEN_MOMENTUM_STOP_C - config.SLIP_TOLERANCE_C
    w = _at_flatten_deadline(flip, ledger, entry=61)
    b = OrderBook(market=TICKER)
    b.apply_snapshot({45: 10}, {50: 10}, ts=1.0)
    p1 = []
    flip._check_uncovered(w, TICKER, p1, book=b, event=EVENT, now=CLOSE - 700)
    rest = next(p for p in p1 if p.action == "sell" and not p.crossfire)
    assert rest.price_cents == floor
    # the rest fills: the booked EXIT lands, the leg covers
    flip.on_submitted(rest, "FLOOR-1", CLOSE - 700)
    ledger.record_fill(TICKER, "FLIP", "yes", "EXIT", floor, 1, "PROBE")
    p2 = []
    flip._check_uncovered(w, TICKER, p2, book=b, event=EVENT, now=CLOSE - 699)
    sells2 = [p for p in p2 if p.action == "sell" and p.side == "yes"]
    assert sells2 == []                               # healed — nothing to cross
    assert _rows(ledger, "FLIP_FLOOR_BREACH") == 0


def test_flatten_above_floor_crosses_immediately_unchanged(flip, ledger):
    """No behaviour change when the book is healthy: a mark AT or ABOVE the
    floor crosses at once with the ordinary FLATTENED tag — no needless
    one-poll delay, no breach."""
    w = _at_flatten_deadline(flip, ledger, entry=61)
    b = OrderBook(market=TICKER)
    b.apply_snapshot({55: 10}, {60: 10}, ts=1.0)      # bid 55 ≥ floor 48
    p1 = []
    flip._check_uncovered(w, TICKER, p1, book=b, event=EVENT, now=CLOSE - 700)
    sells = [p for p in p1 if p.action == "sell" and p.side == "yes"]
    assert len(sells) == 1 and sells[0].crossfire and sells[0].price_cents == 55
    assert _rows(ledger, "FLIP_UNCOVERED_FLATTENED") == 1
    assert _rows(ledger, "FLIP_FLOOR_BREACH") == 0


# ── HARD RAIL ──────────────────────────────────────────────────────────────
def test_rails_unchanged():
    assert config.RATE_HALT_LOSSES == 2 and config.RATE_HALT_WINDOW == 4
    assert config.CASH_SILENT_REBASE_CENTS == 5
    assert config.OPEN_BAND == (39, 56)          # OPEN swing gate untouched
    assert config.KELLY_FRACTION_CEILING == pytest.approx(1.0 / 12.0)
    assert config.SLIP_TOLERANCE_C == 3          # Part 2 floor tolerance
