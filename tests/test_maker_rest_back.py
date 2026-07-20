"""WO-MAKER-REST-BACK (build 48) — stop posting into the cross.

F (and now FLIP) kept getting VENUE_REJECTED "post only cross": a maker BUY
posted at a price the fast book had already crossed, and post_only=True made
Kalshi reject it. The fix: at LIVE placement, re-price a maker BUY entry to
rest PASSIVELY — at/inside the held-side bid, strictly below the derived ask —
so it can never post_only into a cross. FLIP rests a cushion below the cheap
side (its liquidity doctrine). A rest-back that would breach the lane band
SKIPS (a maker who can't rest in-band waits, never chases). The deliberate
taker crossing stays CUT-only. LIVE placement only; shadow has no venue."""

import pytest

from relay_engine import config, venue
from relay_engine.book import OrderBook
from relay_engine.gateway import Order, WallRejection


def _book(yb, nb):
    b = OrderBook(market="M1")
    b.apply_snapshot({yb: 10}, {nb: 10}, ts=1.0)
    return b


def _entry(lane, side="yes", price=61, band=None):
    return Order(lane=lane, event="EV1", market="M1", side=side, action="buy",
                 price_cents=price, count=1, size_tier=config.TIER_PROBE,
                 purpose="ENTRY", band=band, why=f"{lane} tier{price} · surv~price")


@pytest.fixture
def live(gateway, monkeypatch):
    placed = []
    monkeypatch.setattr(config, "live_submit_enabled", lambda: True)
    gateway.venue_client = object()
    monkeypatch.setattr(venue, "get_balance", lambda c: (1000.0, 0.0))

    def _place(client, ticker, side, price_cents, count=1, expiration_ts=None,
               v2_price_str=None, post_only=True):
        placed.append({"side": side, "price": price_cents,
                       "post_only": post_only, "fp": v2_price_str})
        return "OID-1", {"ok": True}
    monkeypatch.setattr(venue, "place_order_maker", _place)
    gateway._placed = placed
    return gateway


# ── the unit: the rested price is passive by construction ──────────────────
def test_rest_back_joins_the_bid_for_F(gateway):
    # F intended yes@61, book yes_bid 59 / no_bid 38 (ask_yes 62) -> rest at 59
    assert gateway._rest_back_price(_entry("F", price=61), _book(59, 38)) == 59


def test_rest_back_flip_cushions_below_the_cheap_bid(gateway):
    # FLIP intended yes@44, book yes_bid 44 / no_bid 54 -> 44 − 2 cushion = 42
    r = gateway._rest_back_price(_entry("FLIP", price=44, band=config.OPEN_BAND),
                                 _book(44, 54))
    assert r == 44 - config.FLIP_REST_BACK_CENTS


def test_rest_back_is_strictly_below_the_ask(gateway):
    # a momentarily-crossed book (yes_bid 96, no_bid 5 -> ask_yes 95, bid > ask):
    # the rest must sit strictly below the ask, never at/through it
    assert gateway._rest_back_price(_entry("F", price=97), _book(96, 5)) == 94


def test_rest_back_skips_when_it_breaches_the_band(gateway):
    # FLIP intended 40, cheap bid 40 − 2 = 38 < band floor 39 -> SKIP, never chase
    with pytest.raises(WallRejection) as e:
        gateway._rest_back_price(_entry("FLIP", price=40, band=config.OPEN_BAND),
                                 _book(40, 59))
    assert e.value.wall == "REST_BACK_SKIP"


def test_rest_back_leaves_an_in_band_passive_price_alone(gateway):
    # already passive and in-band: intended 55, bid 59, ask 62 -> stays 55
    assert gateway._rest_back_price(_entry("F", price=55), _book(59, 38)) == 55


# ── the placement: rest-back applies LIVE, at the venue ────────────────────
def test_live_entry_rests_back_at_placement(live):
    live.submit(_entry("F", price=61), _book(59, 38))   # ask 62, bid 59
    assert live._placed[0]["price"] == 59               # rested to the bid
    assert live._placed[0]["post_only"] is True         # still a maker
    assert live._placed[0]["fp"] is None                # stale touch cleared


def test_shadow_entry_is_not_rested(gateway):
    # shadow (default): rest-back does NOT apply — the price is untouched
    r = gateway.submit(_entry("F", price=61), _book(59, 38))
    assert r.shadow
    assert gateway.resting[r.order_id].price_cents == 61


def test_through_price_entry_still_rejects_loudly(gateway):
    # a lane pricing an entry THROUGH the ask is a bug -> the wall rejects it;
    # rest-back never silently fixes it (the REJECT_TAKER_ENTRY wall stays)
    with pytest.raises(WallRejection) as e:
        gateway.submit(_entry("F", price=71), _book(45, 30))   # ask 70
    assert e.value.wall == "TAKER_ENTRY"


# ── the deliberate taker CUT is unchanged ──────────────────────────────────
def test_cut_still_crosses_deliberately(live):
    cut = Order(lane="FLIP", event="EV1", market="M1", side="yes", action="sell",
                price_cents=34, count=1, size_tier=config.TIER_PROBE,
                purpose="CUT", crossfire=True, reason="evacuate now")
    live.submit(cut, _book(40, 55))
    assert live._placed[0]["post_only"] is False        # crosses on purpose


# ── HARD RAIL ──────────────────────────────────────────────────────────────
def test_rails_unchanged():
    assert config.FLIP_REST_BACK_CENTS == 2
    assert config.KELLY_FRACTION_CEILING == pytest.approx(1.0 / 12.0)
