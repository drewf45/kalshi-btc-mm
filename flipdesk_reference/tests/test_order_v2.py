# tests/test_order_v2.py — WO-F4: the PROVEN V2 order grammar. The translation table is
# pinned against k_worker/kalshi.py:place_order_maker (which placed hundreds of live
# fills). YES-leg bid/ask only; price = dollar string; count = string.
import time
import unittest

from f_worker.config import Config
from f_worker.ledger import Ledger, OK_MARKET_OUT
from f_worker.fgateway import FGateway
from f_worker.lib.order_v2 import yes_bidask, build_v2_order
from f_worker.lib.marketutil import parse_fill
from tests.fakes import FakeClient, FakeNotifier

MKT = "KXBTC15M-26JUL161600-00"


class TestTranslationTable(unittest.TestCase):
    def test_four_intents(self):
        self.assertEqual(yes_bidask("buy", "yes", 48), ("bid", 48))
        self.assertEqual(yes_bidask("buy", "no", 49), ("ask", 51))    # buy NO == ask YES @100-49
        self.assertEqual(yes_bidask("sell", "yes", 54), ("ask", 54))  # flip a YES leg
        self.assertEqual(yes_bidask("sell", "no", 55), ("bid", 45))   # flip a NO leg

    def test_price_is_dollar_string_2dp(self):
        b = build_v2_order(MKT, "buy", "yes", 48, 1, True, "coid")
        self.assertEqual(b["side"], "bid")
        self.assertEqual(b["price"], "0.48")
        self.assertEqual(b["count"], "1")           # STRING
        b2 = build_v2_order(MKT, "buy", "no", 49, 1, True, "coid")
        self.assertEqual((b2["side"], b2["price"]), ("ask", "0.51"))

    def test_body_matches_proven_field_set(self):
        b = build_v2_order(MKT, "buy", "yes", 47, 2, True, "coid-x")
        self.assertEqual(b["ticker"], MKT)
        self.assertEqual(b["client_order_id"], "coid-x")
        self.assertEqual(b["time_in_force"], "good_till_canceled")
        self.assertTrue(b["post_only"])
        self.assertEqual(b["self_trade_prevention_type"], "taker_at_cross")
        self.assertNotIn("action", b)               # V2 has no action/type
        self.assertNotIn("type", b)
        self.assertNotIn("yes_price", b)

    def test_taker_is_post_only_false(self):
        b = build_v2_order(MKT, "sell", "yes", 40, 1, False, "coid")
        self.assertFalse(b["post_only"])

    def test_subpenny_v2_price_str(self):
        # the free edge: rest at the exact orderbook_fp touch
        b = build_v2_order(MKT, "buy", "yes", 47, 1, True, "coid", v2_price_str="0.4750")
        self.assertEqual(b["price"], "0.4750")
        # NO side converts to YES terms exactly via 1 - x
        b2 = build_v2_order(MKT, "buy", "no", 47, 1, True, "coid", v2_price_str="0.4750")
        self.assertEqual(b2["side"], "ask")
        self.assertEqual(b2["price"], "0.5250")

    def test_expiration_time_when_present(self):
        b = build_v2_order(MKT, "buy", "yes", 48, 1, True, "coid", expiration_ts=1_800_000_000)
        self.assertEqual(b["expiration_time"], 1_800_000_000)
        b2 = build_v2_order(MKT, "buy", "yes", 48, 1, True, "coid")
        self.assertNotIn("expiration_time", b2)


class TestParseFill(unittest.TestCase):
    def test_fp_dollar_strings(self):
        f = {"yes_price": "0.4700", "count_fp": "1.00", "fee_cost": "0.02"}
        price, fee, count = parse_fill(f, "yes")
        self.assertEqual(price, 47.0)
        self.assertEqual(fee, 2)
        self.assertEqual(count, 1)

    def test_complement_from_other_side(self):
        f = {"no_price": "0.5100"}         # only NO price present; our side is yes
        price, fee, count = parse_fill(f, "yes")
        self.assertEqual(price, 49.0)      # 100 - 51

    def test_int_cents(self):
        price, _, _ = parse_fill({"yes_price": 47}, "yes")
        self.assertEqual(price, 47.0)

    def test_unparseable_returns_none(self):
        price, fee, count = parse_fill({"garbage": 1}, "yes")
        self.assertIsNone(price)


class TestW4bExpiration(unittest.TestCase):
    def _gw(self):
        led = Ledger(":memory:")
        return FGateway(FakeClient(), led, Config(), FakeNotifier())

    def test_future_expiry_is_set(self):
        gw = self._gw()
        exp = int(time.time()) + 1000
        gw.post_flip_ask(MKT, MKT, "yes", 48, 54, is_lone=True, seconds_to_close=300,
                         expiration_ts=exp)
        self.assertEqual(gw.client.placed[-1]["expiration_time"], exp)

    def test_past_expiry_is_dropped(self):
        gw = self._gw()
        gw.post_flip_ask(MKT, MKT, "yes", 48, 54, is_lone=True, seconds_to_close=300,
                         expiration_ts=int(time.time()) - 1000)   # already past
        self.assertNotIn("expiration_time", gw.client.placed[-1])


if __name__ == "__main__":
    unittest.main()
