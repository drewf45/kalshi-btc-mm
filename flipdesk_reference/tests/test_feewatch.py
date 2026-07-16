# tests/test_feewatch.py — F1.3: the fee tripwire. An injected change to the series fee
# schedule must trip flip_halt + alert; a stable schedule must not.
import unittest

from f_worker.config import Config
from f_worker.ledger import Ledger
from f_worker.feewatch import FeeTripwire, fee_fingerprint
from tests.fakes import FakeClient, FakeNotifier

MKT = "KXBTC15M-25JUL16T1415"


class TestFeeWatch(unittest.TestCase):
    def test_stable_schedule_no_halt(self):
        client = FakeClient(market={"taker_fee_rate": 0.07, "maker_fee_rate": 0.0})
        led = Ledger(":memory:")
        tw = FeeTripwire(client, led, Config(), FakeNotifier())
        tw.capture(MKT)
        self.assertFalse(tw.check(MKT))
        self.assertFalse(led.halt_state()[0])

    def test_injected_change_trips_halt(self):
        client = FakeClient(market={"taker_fee_rate": 0.07, "maker_fee_rate": 0.0})
        led = Ledger(":memory:")
        notif = FakeNotifier()
        tw = FeeTripwire(client, led, Config(), notif)
        tw.capture(MKT)
        # Kalshi revises the schedule under us
        client.market = {"taker_fee_rate": 0.10, "maker_fee_rate": 0.02}
        tripped = tw.check(MKT)
        self.assertTrue(tripped)
        self.assertTrue(led.halt_state()[0])
        self.assertTrue(any("FEE SCHEDULE CHANGED" in a for a in notif.alerts))

    def test_fingerprint_is_deterministic(self):
        a = fee_fingerprint({"taker_fee_rate": 0.07, "maker_fee_rate": 0.0})
        b = fee_fingerprint({"maker_fee_rate": 0.0, "taker_fee_rate": 0.07})  # order-independent
        self.assertEqual(a, b)
        self.assertNotEqual(a, fee_fingerprint({"taker_fee_rate": 0.10}))


if __name__ == "__main__":
    unittest.main()
