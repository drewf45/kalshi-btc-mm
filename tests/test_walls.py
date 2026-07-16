# tests/test_walls.py — every wall W1..W8 has a test that TRIES to violate it and
# proves it can't. A blocked wall raises WallViolation and (for risk-adding requests)
# nothing reaches the exchange (FakeClient.placed stays empty) + a BUG alert fires.
import unittest

from f_worker.config import Config
from f_worker.ledger import Ledger, OK_SALVAGE
from f_worker.fgateway import FGateway, WallViolation
from f_worker import feemath
from tests.fakes import FakeClient, FakeNotifier

MKT = "KXBTC15M-25JUL16T1415"
WID = MKT


def _gw(client=None, cfg=None):
    client = client or FakeClient()
    cfg = cfg or Config()
    led = Ledger(":memory:")
    notif = FakeNotifier()
    return FGateway(client, led, cfg, notif), client, led, notif


class TestWalls(unittest.TestCase):
    # ---- W1: combined bundle cost must be <= entry_line ----
    def test_W1_bundle_over_line_blocked(self):
        gw, client, led, notif = _gw()
        with self.assertRaises(WallViolation) as ctx:
            gw.post_entry_pair(WID, MKT, 50, 50, seconds_to_close=300)   # 100 > 99
        self.assertEqual(ctx.exception.wall, "W1")
        self.assertEqual(client.placed, [])
        self.assertTrue(any("W1" in a for a in notif.alerts))

    def test_W1_at_line_allowed(self):
        gw, client, _, _ = _gw()
        gw.post_entry_pair(WID, MKT, 49, 50, seconds_to_close=300)       # 99 == line
        self.assertEqual(len(client.placed), 2)

    # ---- W2: a lone leg above single_leg_max may never be held/flipped ----
    def test_W2_lone_leg_over_max_blocked(self):
        gw, client, _, notif = _gw()
        self.assertFalse(gw.may_hold_lone(50))
        with self.assertRaises(WallViolation) as ctx:
            gw.post_flip_ask(WID, MKT, "yes", entry_price=55, ask_price=61,
                             is_lone=True, seconds_to_close=300)
        self.assertEqual(ctx.exception.wall, "W2")
        self.assertEqual(client.placed, [])

    def test_W2_lone_leg_at_max_ok(self):
        gw, client, _, _ = _gw()
        self.assertTrue(gw.may_hold_lone(49))
        gw.post_flip_ask(WID, MKT, "yes", entry_price=49, ask_price=55,
                         is_lone=True, seconds_to_close=300)
        self.assertEqual(len(client.placed), 1)

    # ---- W3: 1 lot/side until the ladder says otherwise; lone legs ALWAYS 1 lot ----
    def test_W3_lone_never_scales(self):
        gw, _, led, _ = _gw()
        led.set_rung(3, lots=5)
        self.assertEqual(gw.current_lots(is_lone=False), 5)   # bundle legs follow ladder
        self.assertEqual(gw.current_lots(is_lone=True), 1)    # lone is ALWAYS 1

    def test_W3_bundle_uses_ladder_lots(self):
        gw, client, led, _ = _gw()
        led.set_rung(2, lots=4)
        gw.post_entry_pair(WID, MKT, 48, 49, seconds_to_close=300)
        self.assertTrue(all(o["count"] == "4" for o in client.placed))   # V2 count is a STRING

    # ---- W4: no NEW risk inside the flat zone (T-90) ----
    def test_W4_entry_in_flat_zone_blocked(self):
        gw, client, _, _ = _gw()
        with self.assertRaises(WallViolation) as ctx:
            gw.post_entry_pair(WID, MKT, 48, 49, seconds_to_close=30)   # inside T-90
        self.assertEqual(ctx.exception.wall, "W4")
        self.assertEqual(client.placed, [])

    def test_W4_flip_in_flat_zone_blocked(self):
        gw, client, _, _ = _gw()
        with self.assertRaises(WallViolation) as ctx:
            gw.post_flip_ask(WID, MKT, "yes", 48, 54, is_lone=False, seconds_to_close=45)
        self.assertEqual(ctx.exception.wall, "W4")

    def test_W4_market_out_allowed_in_flat_zone(self):
        gw, client, _, _ = _gw()
        gw.market_out(WID, MKT, "yes", is_lone=False, entry_price=48, mark_price=47)
        self.assertEqual(len(client.placed), 1)   # exits ALWAYS allowed

    # ---- W5: one entry phase per window ----
    def test_W5_second_entry_blocked(self):
        gw, client, _, notif = _gw()
        gw.post_entry_pair(WID, MKT, 48, 49, seconds_to_close=300)
        with self.assertRaises(WallViolation) as ctx:
            gw.post_entry_pair(WID, MKT, 48, 49, seconds_to_close=300)
        self.assertEqual(ctx.exception.wall, "W5")
        self.assertEqual(len(client.placed), 2)   # only the first pair ever placed

    # ---- W6: live-balance re-read before EVERY order; insufficient blocks the buy ----
    def test_W6_balance_reread_every_order(self):
        gw, client, _, _ = _gw()
        gw.post_entry_pair(WID, MKT, 48, 49, seconds_to_close=300)
        self.assertGreaterEqual(client.balance_calls, 2)   # re-read per leg

    def test_W6_insufficient_balance_blocks(self):
        client = FakeClient(avail=0.0, total=0.0)
        gw, client, _, notif = _gw(client=client)
        with self.assertRaises(WallViolation) as ctx:
            gw.post_entry_pair(WID, MKT, 48, 49, seconds_to_close=300)
        self.assertEqual(ctx.exception.wall, "W6")
        self.assertEqual(client.placed, [])

    # ---- W7: flip_halt blocks new risk; exits still allowed to flatten ----
    def test_W7_entry_blocked_while_halted(self):
        gw, client, led, _ = _gw()
        led.set_halt(True, "two stops")
        with self.assertRaises(WallViolation) as ctx:
            gw.post_entry_pair(WID, MKT, 48, 49, seconds_to_close=300)
        self.assertEqual(ctx.exception.wall, "W7")
        self.assertEqual(client.placed, [])

    def test_W7_exit_allowed_while_halted(self):
        gw, client, led, _ = _gw()
        led.set_halt(True, "two stops")
        gw.market_out(WID, MKT, "yes", is_lone=True, entry_price=48, mark_price=47)
        self.assertEqual(len(client.placed), 1)   # flatten inventory even in halt

    # ---- W8: a crossing exit prices its exact taker fee BEFORE the order goes out ----
    def test_W8_salvage_prices_fee_first(self):
        gw, client, led, _ = _gw()
        oid, fee = gw.salvage_cross(WID, MKT, "yes", entry_price=47, exit_price=40, is_lone=True)
        self.assertEqual(fee, feemath.fee_cents(40, 1))    # exact roundup
        # the recorded order carries the fee + net computed pre-submit
        row = led._conn.execute(
            "SELECT detail FROM orders WHERE kind=? ORDER BY id DESC LIMIT 1", (OK_SALVAGE,)
        ).fetchone()
        import json
        detail = json.loads(row["detail"])
        self.assertEqual(detail["taker_fee_cents"], fee)
        self.assertEqual(detail["net_cents"], feemath.net_after_taker_exit(47, 40, 1))


if __name__ == "__main__":
    unittest.main()
