# tests/test_settlement.py — F1.2: settlement broker-truth. A clean yes/no result
# matches the floor arithmetic; a void/refund is a mismatch that must alert.
import unittest

from f_worker import window as W
from f_worker.config import Config
from f_worker.ledger import Ledger
from f_worker.manager import FLOOR
from f_worker.settlement import Settler, rode_to_settlement
from tests.fakes import FakeClient, FakeNotifier


def _floor_win():
    w = W.Window(window_id="w", market_ticker="KXBTC15M-25JUL16T1415", event_ticker="e",
                 open_ts=None, close_ts=1_700_000_000, rung=1, lots=1)
    w.add_fill("yes", 48, 1); w.add_fill("no", 49, 1)
    w.resolve_leg("yes", None, FLOOR); w.resolve_leg("no", None, FLOOR)
    return w


def _settler(market):
    client = FakeClient(market=market)
    led = Ledger(":memory:")
    notif = FakeNotifier()
    return Settler(client, led, Config(), notif, sleep=lambda s: None), led, notif


class TestSettlement(unittest.TestCase):
    def test_rode_to_settlement_detection(self):
        self.assertTrue(rode_to_settlement(_floor_win()))

    def test_clean_result_matches_floor(self):
        settler, led, notif = _settler({"status": "settled", "result": "yes"})
        w = _floor_win()
        booked = 100 - (48 + 49)   # +3
        out = settler.verify_floor(w, booked)
        self.assertFalse(out["mismatch"])
        self.assertEqual(out["broker_net"], booked)
        self.assertTrue(any("settled" in s for s in notif.sent))

    def test_void_market_is_mismatch_alert(self):
        settler, led, notif = _settler({"status": "canceled", "result": ""})
        w = _floor_win()
        out = settler.verify_floor(w, 3)
        self.assertTrue(out["mismatch"])
        self.assertEqual(out["broker_net"], 0)          # premium refunded, not +3
        self.assertTrue(any("settlement mismatch" in a for a in notif.alerts))

    def test_settlement_row_is_appended(self):
        settler, led, notif = _settler({"status": "settled", "result": "no"})
        settler.verify_floor(_floor_win(), 3)
        n = led._conn.execute("SELECT COUNT(*) c FROM settlements").fetchone()["c"]
        self.assertEqual(n, 1)


if __name__ == "__main__":
    unittest.main()
