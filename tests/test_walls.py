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
def test_net_risk_cross_lane_cap(gateway):
    b = make_book()
    gateway.submit(entry(lane="D", price=61, count=1), b)
    gateway.submit(entry(lane="P", market="M2", price=30, count=1), b)
    gateway.submit(entry(lane="H8", market="M3", price=30, count=1), b)
    # 4th contract on the same settlement event crosses the <=3 cap
    with pytest.raises(WallRejection) as e:
        gateway.submit(entry(lane="F", market="M4", price=30, count=1), b)
    assert e.value.wall == "NET_RISK"
    # a different settlement event has its own cap
    assert gateway.submit(entry(lane="F", market="M9", event="EV2", price=61, count=1), b).shadow


def test_at_risk_cap_trips_independently(gateway, monkeypatch):
    """At the DREW-DEFAULT constants the count cap fires first (3 lots x 99c max
    loss == the 297c cap exactly), so lift the count cap to prove the $-at-risk
    wall enforces on its own."""
    monkeypatch.setattr(config, "NET_RISK_CROSS_LANE_CAP", 10)
    b = make_book()
    gateway.submit(entry(lane="D", price=99, count=1), b)
    gateway.submit(entry(lane="P", market="M2", price=99, count=1), b)
    gateway.submit(entry(lane="H8", market="M3", price=99, count=1), b)  # 297c == cap
    with pytest.raises(WallRejection) as e:
        gateway.submit(entry(lane="F", market="M4", price=1, count=1), b)  # 298c > cap
    assert e.value.wall == "DOLLAR_RISK"


def test_at_risk_cap_exact_boundary(gateway):
    b = make_book()
    gateway.submit(entry(lane="D", price=99, count=1), b)
    gateway.submit(entry(lane="P", market="M2", price=99, count=1), b)
    assert gateway.submit(entry(lane="H8", market="M3", price=99, count=1), b).shadow  # ==297 OK
    with pytest.raises(WallRejection):  # anything more breaks a cap
        gateway.submit(entry(lane="F", market="M4", price=1, count=1), b)


def test_resting_exit_does_not_consume_cap(gateway):
    """The gate-4 named case: a resting exit must NOT consume net-risk cap."""
    b = make_book()
    # lane D long 1 on EV1 via a fill
    r = gateway.submit(entry(lane="D", price=61, count=1), b)
    gateway.on_fill(r.order_id)
    # park a resting EXIT for that position
    exit_order = Order(lane="D", event="EV1", market="M1", side="yes", action="sell",
                       price_cents=80, count=1, size_tier=config.TIER_PROBE, purpose="EXIT")
    gateway.submit(exit_order, b)
    # exposure: 1 open position; the resting exit adds nothing.
    contracts, cents = gateway._event_exposure("EV1")
    assert contracts == 1
    # two more entries still fit under the <=3 cap (proving the exit isn't counted)
    gateway.submit(entry(lane="P", market="M2", price=30, count=1), b)
    gateway.submit(entry(lane="H8", market="M3", price=30, count=1), b)
    with pytest.raises(WallRejection):
        gateway.submit(entry(lane="F", market="M4", price=30, count=1), b)


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


def test_pct_of_book_budget(gateway, ledger):
    b = make_book()
    # book $100, cap 10% -> 1000c budget; CLEAR tier allows 3 contracts; 3*99=297c fits,
    # so shrink the budget: rebuild caps on a $2 book
    ledger.db.execute("DELETE FROM cash_movements")
    ledger.db.commit()
    ledger.baseline(200, confirmed_by="boot")
    ledger.snapshot_caps_at_boot()  # budget = 20c
    with pytest.raises(WallRejection) as e:
        gateway.submit(entry(price=61, count=1), b)
    assert e.value.wall == "BUDGET"


def test_sizing_tier_authorization(gateway):
    b = make_book()
    with pytest.raises(WallRejection) as e:
        gateway.submit(entry(price=61, tier=config.TIER_SUPPRESS), b)
    assert e.value.wall == "DEPTH"
    with pytest.raises(WallRejection) as e:
        gateway.submit(entry(price=61, count=2, tier=config.TIER_PROBE), b)  # PROBE max 1
    assert e.value.wall == "DEPTH"
    gw2_authorized = lambda lane, market: config.TIER_LEAN
    gateway.sizing_authorized_tier = gw2_authorized
    with pytest.raises(WallRejection) as e:
        gateway.submit(entry(price=61, count=1, tier=config.TIER_CLEAR), b)
    assert e.value.wall == "DEPTH"


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
