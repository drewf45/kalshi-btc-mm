"""P3.1 — THE WALL (Adversary): the fills-booking path proves itself before
any lane beyond F/H8 lands. Every fill books exactly once, attributed by the
gateway's order index, never inferred; foreign fills are counted, not claimed;
partial fills accumulate; the reconcile sweep is idempotent."""

import pytest

from relay_engine import config
from relay_engine.book import OrderBook
from relay_engine.custodian import Custodian, OpenPosition
from relay_engine.errors import WallRejection
from relay_engine.feed import DegradeLadder
from relay_engine.fills import FillBooker
from relay_engine.gateway import Gateway, Order


def make_book():
    b = OrderBook(market="M1")
    b.apply_snapshot({61: 100}, {1: 80}, ts=1.0)
    return b


def entry(lane="F", price=61, count=1, market="M1", event="EV1"):
    return Order(lane=lane, event=event, market=market, side="yes", action="buy",
                 price_cents=price, count=count, size_tier=config.TIER_PROBE,
                 purpose="ENTRY")


def venue_fill(order_id, fill_id, yes_price_cents=61, count=1):
    """A synthetic venue fill record in the live wire shape (fp strings)."""
    return {"fill_id": fill_id, "order_id": order_id,
            "yes_price": f"{yes_price_cents / 100:.4f}",
            "count_fp": f"{count}.00", "fee_cost": "0.010000"}


@pytest.fixture
def booker(gateway, ledger, surface):
    alerts = []
    b = FillBooker(gateway, ledger, surface,
                   custodian=Custodian(gateway, ledger, surface, ladder=DegradeLadder()),
                   alert_fn=alerts.append)
    b.test_alerts = alerts
    return b


def test_fill_books_exactly_once_with_attribution(booker, gateway, ledger):
    r = gateway.submit(entry(lane="F"), make_book())
    stats = booker.sweep([venue_fill(r.order_id, "f1")], now=1000.0)
    assert stats == {"booked": 1, "duplicate": 0, "foreign": 0, "unparsed_price": 0}

    lane, market, side, action, price, count, tier = ledger.db.execute(
        "SELECT lane, market, side, action, price_cents, count, size_tier FROM fills").fetchone()
    # attribution from the ORDER INDEX — lane recorded at submit, never inferred
    assert (lane, market, side, action, price, count, tier) == \
        ("F", "M1", "yes", "ENTRY", 61, 1, "PROBE")
    # position effect landed
    assert gateway.positions[("EV1", "M1", "F")] == 1
    # custodian adopted the position (opening lane owns it)
    assert "M1:F" in booker.custodian.positions

    # duplicate sweep: zero double-booking
    stats2 = booker.sweep([venue_fill(r.order_id, "f1")], now=1001.0)
    assert stats2["duplicate"] == 1 and stats2["booked"] == 0
    assert ledger.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1


def test_foreign_fills_counted_never_claimed(booker, ledger):
    stats = booker.sweep([venue_fill("SOMEONE-ELSES-ORDER", "fx")], now=1000.0)
    assert stats["foreign"] == 1 and stats["booked"] == 0
    assert ledger.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0
    assert booker.foreign_seen == 1  # the pack surfaces this after cutover


def test_partial_fills_accumulate(booker, gateway, monkeypatch, ledger):
    monkeypatch.setattr(config, "NET_RISK_CROSS_LANE_CAP", 10)
    order = Order(lane="F", event="EV1", market="M1", side="yes", action="buy",
                  price_cents=61, count=3, size_tier=config.TIER_CLEAR, purpose="ENTRY")
    r = gateway.submit(order, make_book())
    booker.sweep([venue_fill(r.order_id, "p1", count=1)], now=1000.0)
    assert r.order_id in gateway.resting  # 1/3 filled: still resting
    booker.sweep([venue_fill(r.order_id, "p2", count=2)], now=1001.0)
    assert r.order_id not in gateway.resting  # fully filled
    assert gateway.positions[("EV1", "M1", "F")] == 3
    total = ledger.db.execute("SELECT SUM(count) FROM fills").fetchone()[0]
    assert total == 3


def test_unparsed_price_books_at_rest_price_and_alerts(booker, gateway, ledger):
    r = gateway.submit(entry(), make_book())
    bad = {"fill_id": "u1", "order_id": r.order_id, "count": 1}  # no price keys
    stats = booker.sweep([bad], now=1000.0)
    assert stats["unparsed_price"] == 1 and stats["booked"] == 1
    assert ledger.db.execute("SELECT price_cents FROM fills").fetchone()[0] == 61
    assert any("unparsed" in a for a in booker.test_alerts)


def test_exit_fill_books_as_exit(booker, gateway, ledger):
    b = make_book()
    r = gateway.submit(entry(), b)
    booker.sweep([venue_fill(r.order_id, "e0")], now=1000.0)
    exit_order = Order(lane="F", event="EV1", market="M1", side="yes", action="sell",
                       price_cents=80, count=1, size_tier=config.TIER_PROBE, purpose="EXIT")
    r2 = gateway.submit(exit_order, b)
    booker.sweep([venue_fill(r2.order_id, "e1", yes_price_cents=80)], now=1001.0)
    action = ledger.db.execute(
        "SELECT action FROM fills WHERE price_cents=80").fetchone()[0]
    assert action == "EXIT"
    assert gateway.positions[("EV1", "M1", "F")] == 0  # flat after exit


# ── the P3.1 walls around the live door ────────────────────────────────────
def test_reject_taker_entry_through_price(gateway):
    b = OrderBook(market="M1")
    b.apply_snapshot({45: 100}, {30: 80}, ts=1.0)  # derived yes ask 70
    with pytest.raises(WallRejection) as e:
        gateway.submit(entry(price=71), b)  # strictly through the ask
    assert e.value.wall == "REJECT_TAKER_ENTRY"
    # AT the boundary is touch-joining — allowed (venue post_only owns exact cross)
    assert gateway.submit(entry(price=70), b).shadow


def test_crossfire_confined_to_cut(gateway):
    b = make_book()
    with pytest.raises(WallRejection) as e:
        gateway.submit(Order(lane="F", event="EV1", market="M1", side="yes",
                             action="buy", price_cents=61, count=1,
                             size_tier=config.TIER_PROBE, purpose="ENTRY",
                             crossfire=True), b)
    assert e.value.wall == "REJECT_TAKER_ENTRY"
    # even a passive EXIT may not crossfire
    gateway.positions[("EV1", "M1", "F")] = 1
    with pytest.raises(WallRejection):
        gateway.submit(Order(lane="F", event="EV1", market="M1", side="yes",
                             action="sell", price_cents=40, count=1,
                             size_tier=config.TIER_PROBE, purpose="EXIT",
                             crossfire=True), b)
    # a CUT may
    cut = Order(lane="F", event="EV1", market="M1", side="yes", action="sell",
                price_cents=40, count=1, size_tier=config.TIER_PROBE,
                purpose="CUT", crossfire=True)
    assert gateway.submit(cut, b).shadow


def test_live_door_stays_shut_in_shadow(gateway):
    """RUN_MODE=SHADOW: submit never touches a venue client."""
    r = gateway.submit(entry(), make_book())
    assert r.shadow and gateway.venue_client is None and not gateway.live_order_ids