"""VERIFY GATE 4: wrong-way-tick + net-risk walls exercised synthetically —
including a resting exit NOT consuming cap — plus the rest of the wall order."""

import threading

import pytest

from relay_engine import config
from relay_engine.book import OrderBook, to_yes_terms
from relay_engine.errors import FatalIntegrityError, WallRejection
from relay_engine.gateway import Gateway, Order


def make_book(market="M1", no_bid=1):
    # wide derived ask (no_bid=1 -> yes ask 99) so entry prices below 99 rest
    b = OrderBook(market=market)
    b.apply_snapshot({45: 100, 44: 50}, {no_bid: 80}, ts=1.0)
    return b


# P26 §2: every ENTRY carries its lane's proof — the wall demands it, so
# the fixtures print it (the law is part of the furniture now).
PROOF_WHYS = {
    "D": "d-table verdict yes@61¢ · reserved",
    "P": "P fade yes · displ +4.0c x2 spot-flat",
    "F": "F tier61 · surv~price",
    "H8": "H8 tier95 · surv~price",
    "FLIP": "OPEN grain yesx2 · join 48c · PROBE n=0 · geometry=v2",
}


def entry(lane="D", market="M1", event="EV1", price=61, count=1,
          tier=config.TIER_PROBE, **kw):
    kw.setdefault("why", PROOF_WHYS.get(lane, ""))
    return Order(lane=lane, event=event, market=market, side="yes", action="buy",
                 price_cents=price, count=count, size_tier=tier, purpose="ENTRY", **kw)


# ---------------------------------------------------------------- canonical conversion
def test_canonical_yes_terms():
    assert to_yes_terms("yes", 45) == 45
    assert to_yes_terms("no", 52) == 48  # bids-only reciprocal
    with pytest.raises(ValueError):
        to_yes_terms("maybe", 50)


def test_book_derived_ask():
    b = OrderBook(market="M1")
    b.apply_snapshot({45: 100, 44: 50}, {52: 80, 51: 40}, ts=1.0)
    assert b.best_yes_bid() == 45
    assert b.best_no_bid() == 52
    assert b.best_yes_ask() == 48  # 100 - best_no_bid, derived, never quoted


# ---------------------------------------------------------------- wrong-way tick
def test_wrong_way_tick_buy_must_improve_up(gateway):
    b = make_book()
    # improving a BUY = +1 toward ask: 45 -> 46 OK
    r = gateway.submit(entry(price=46, improve_from=45), b)
    assert r.shadow
    # 45 -> 44 is the wrong way
    with pytest.raises(WallRejection) as e:
        gateway.submit(entry(lane="P", price=44, improve_from=45), b)
    assert e.value.wall == "WRONG_WAY"
    # zero-tick "improvement" also rejected
    with pytest.raises(WallRejection):
        gateway.submit(entry(lane="P", price=45, improve_from=45), b)


def test_wrong_way_tick_sell_must_improve_down(gateway):
    b = make_book()
    # P21 A2 overturned the old setup (a held +2 made ANY sell-entry a
    # self-net — REJECT_SELF_NET now fires first, account scope): the
    # market must be FLAT so the short-sell entry reaches the tick wall.
    sell = Order(lane="P", event="EV1", market="M1", side="yes", action="sell",
                 price_cents=53, count=1, size_tier=config.TIER_PROBE, purpose="ENTRY",
                 improve_from=52, why=PROOF_WHYS["P"])
    with pytest.raises(WallRejection) as e:
        gateway.submit(sell, b)
    assert e.value.wall == "WRONG_WAY"  # sell improved UP = wrong way
    ok = Order(lane="P", event="EV1", market="M1", side="yes", action="sell",
               price_cents=51, count=1, size_tier=config.TIER_PROBE, purpose="ENTRY",
               improve_from=52, why=PROOF_WHYS["P"])
    assert gateway.submit(ok, b).shadow  # -1 toward bid = correct sign


# ---------------------------------------------------------------- net-risk + $-at-risk
def _rebook(ledger, cents):
    """Re-baseline the book (and the boot caps) to a chosen size, for the
    proportional-wall math (WO-2026-07-23-B: the caps scale with the book)."""
    ledger.db.execute("DELETE FROM cash_movements")
    ledger.db.commit()
    ledger.baseline(cents, confirmed_by="boot")
    ledger.snapshot_caps_at_boot()


def test_per_lane_proportional_risk_wall(gateway, ledger):
    """DREW RULING 2026-07-23: the flat cross-lane count cap (<=3) is retired for
    a PER-LANE, BOOK-PROPORTIONAL dollar wall. On a $41.62 book F's cap is 20%
    (832c) — 8 lots at 97c — while D's is 2% (83c). F scales with the book; the
    small lanes stay bounded. The NET_RISK/DOLLAR_RISK reason names are kept so
    WALL_STORM telemetry stays comparable."""
    _rebook(ledger, 4162)
    b = make_book()
    # F: 8 lots @97 (776c) fit under the 832c (20%) cap; 9 (873c) trip.
    assert gateway.submit(entry(lane="F", event="EF1", market="MF1",
                                price=97, count=8), b).shadow
    with pytest.raises(WallRejection) as e:
        gateway.submit(entry(lane="F", event="EF2", market="MF2",
                             price=97, count=9), b)
    assert e.value.wall in ("NET_RISK", "DOLLAR_RISK")
    # D's cap is a tenth of F's (2% vs 20%): 1 lot @60 (60c) fits, 2 (120c) trip.
    assert gateway.submit(entry(lane="D", event="ED1", market="MD1",
                                price=60, count=1), b).shadow
    with pytest.raises(WallRejection) as e2:
        gateway.submit(entry(lane="D", event="ED2", market="MD2",
                             price=60, count=2), b)
    assert e2.value.wall in ("NET_RISK", "DOLLAR_RISK")


def test_risk_wall_scales_with_the_book(gateway, ledger):
    """The CEO lens made a wall: a FIXED cap holds F flat as the book rises. The
    same 5-lot F order that a $20 book refuses, a $40 book admits — the ceiling
    grows with the money instead of throttling it."""
    b = make_book()
    _rebook(ledger, 2000)          # $20 → F cap 400c; 5 lots @97 = 485c > 400
    with pytest.raises(WallRejection):
        gateway.submit(entry(lane="F", event="EA", market="MA",
                             price=97, count=5), b)
    _rebook(ledger, 4000)          # $40 → F cap 800c; the same 485c now fits
    assert gateway.submit(entry(lane="F", event="EB", market="MB",
                                price=97, count=5), b).shadow


def test_resting_exit_does_not_consume_cap(gateway, ledger):
    """The gate-4 named case: a resting exit must NOT consume the risk cap."""
    _rebook(ledger, 4162)
    b = make_book()
    r = gateway.submit(entry(lane="D", price=60, count=1), b)
    gateway.on_fill(r.order_id)
    exit_order = Order(lane="D", event="EV1", market="M1", side="yes", action="sell",
                       price_cents=80, count=1, size_tier=config.TIER_PROBE, purpose="EXIT")
    gateway.submit(exit_order, b)
    contracts, cents = gateway._event_exposure("EV1")
    assert contracts == 1          # the resting exit adds nothing


def test_risk_reducing_orders_exempt_from_walls(gateway):
    b = make_book()
    r = gateway.submit(entry(lane="D", price=61, count=1), b)
    gateway.on_fill(r.order_id)
    gateway.tripwire.observe(multiplier=0.99)  # trip every entry wall
    gateway.halt_entries("TEST_HALT")
    # risk-reducing sell sails through walls AND the halt (classified at the canonical layer)
    sell = Order(lane="D", event="EV1", market="M1", side="yes", action="sell",
                 price_cents=40, count=1, size_tier=config.TIER_PROBE, purpose="EXIT")
    assert gateway.is_risk_reducing(sell)
    assert gateway.submit(sell, b).shadow


# ---------------------------------------------------------------- remaining walls
def test_band_and_single_entry(gateway):
    b = make_book()
    with pytest.raises(WallRejection) as e:
        gateway.submit(entry(price=55, band=(config.LANE_D_FLOOR_CENTS, 99)), b)
    assert e.value.wall == "BAND"  # 55c under the 60c DREW-DEFAULT floor
    gateway.submit(entry(price=61, band=(config.LANE_D_FLOOR_CENTS, 99)), b)
    with pytest.raises(WallRejection) as e:
        gateway.submit(entry(price=62), b)  # same lane, same market, second entry
    assert e.value.wall == "SINGLE_ENTRY"


def test_per_order_budget_is_lane_aware(gateway, ledger):
    """WO-2026-07-23-B (DREW RULING): the per-order budget is the same fixed-
    fraction cap that would hold F flat, so it too is lane-proportional — F's
    20% clears an order that the retired flat 10% floor would have blocked, while
    the per-lane RISK wall keeps every other lane bounded (it fires first for a
    small lane, so the ruling's dollar wall subsumes the old count/budget cap)."""
    b = make_book()
    _rebook(ledger, 4162)          # $41.62 — 10% floor = 416c, F's 20% = 832c
    # F's 776c order (8 lots @97) is OVER the retired 10% floor but under F's 20%
    # per-order budget — it clears (this is exactly the order F was capped from).
    assert gateway.submit(entry(lane="F", event="EF", market="MF",
                                price=97, count=8), b).shadow
    # the identical 776c notional on a small lane (D, 2%) is refused by the
    # per-lane dollar wall — the gateway backstop F kept, not lost.
    with pytest.raises(WallRejection) as e:
        gateway.submit(entry(lane="D", event="EG", market="MG",
                             price=97, count=8), b)
    assert e.value.wall in ("NET_RISK", "DOLLAR_RISK")


def test_tier_never_walls(gateway):
    """P27 §1a OVERTURNED the sizing-tier wall: the tier is a REPORTING
    stamp — a 2-lot PROBE order and a SUPPRESS-stamped order both pass
    (the kept walls still bound risk; the ladder never votes)."""
    b = make_book()
    assert gateway.submit(entry(price=61, count=2,
                                tier=config.TIER_PROBE), b).shadow
    assert gateway.submit(entry(lane="P", market="M2", price=30, count=1,
                                tier=config.TIER_SUPPRESS), b).shadow


def test_fee_tripwire_multiplier_and_designation_list(gateway):
    b = make_book()
    gateway.tripwire.observe(multiplier=config.EXPECTED_FEE_MULTIPLIER)
    gateway.submit(entry(price=61), b)  # unchanged schedule passes
    gateway.tripwire.observe(maker_series=["KXBTC15M"])  # designation list changed
    with pytest.raises(WallRejection) as e:
        gateway.submit(entry(lane="P", market="M2", price=30), b)
    assert e.value.wall == "FEE_TRIPWIRE"


def test_post_only_everywhere(gateway):
    b = make_book()
    r = gateway.submit(entry(price=61), b)
    assert r.payload["post_only"] is True
    assert r.payload["type"] == "limit"


def test_shadow_mode_places_nothing(gateway):
    b = make_book()
    r = gateway.submit(entry(price=61), b)
    assert r.shadow and r.order_id.startswith("SHADOW-")
    assert len(gateway.shadow_orders) == 1  # recorded, not placed


def test_single_threaded_submit_path(gateway):
    b = make_book()
    gateway.submit(entry(price=61), b)
    errors = []

    def other_thread():
        try:
            gateway.submit(entry(lane="P", market="M2", price=30), b)
        except FatalIntegrityError as e:
            errors.append(e)

    t = threading.Thread(target=other_thread)
    t.start()
    t.join()
    assert len(errors) == 1  # second thread = FATAL


def test_rate_governor_printed_is_enforced(gateway):
    assert gateway.governor.capacity == config.RATE_BUCKET_CAPACITY
    assert gateway.governor.refill == config.RATE_REFILL_PER_SECOND
    b = make_book()
    now = 0.0
    gateway.governor.last = now
    gateway.governor.tokens = 1.0
    assert gateway.governor.take(now=now)
    assert not gateway.governor.take(now=now)  # bucket empty, no time passed
    assert "1 submissions rejected" in gateway.governor.alert_line()  # counts carried
