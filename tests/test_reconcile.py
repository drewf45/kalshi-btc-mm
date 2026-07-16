# tests/test_reconcile.py — F1.1: boot reconcile / orphan sweep. Simulated crash mid-
# HOLDING: a restart must cancel every resting order and flatten any held leg through
# the manager's normal exit path, leaving nothing stranded and placing no duplicate.
import unittest

from f_worker.config import Config
from f_worker.ledger import Ledger, KIND_SALVAGE
from f_worker.fgateway import FGateway
from f_worker.manager import Manager
from f_worker.pricebrain import PriceBrain
from f_worker.reconcile import Reconciler
from tests.fakes import FakeClient, FakeNotifier

SERIES = "KXBTC15M"
MKT = "KXBTC15M-25JUL16T1415"


def _stack(**client_kw):
    client = FakeClient(**client_kw)
    cfg = Config()
    led = Ledger(":memory:")
    notif = FakeNotifier()
    gw = FGateway(client, led, cfg, notif)
    mgr = Manager(gw, led, PriceBrain(cfg, gate_path=""), cfg, notif)
    rec = Reconciler(client, gw, mgr, led, cfg, notif)
    return client, cfg, led, notif, rec


class TestReconcile(unittest.TestCase):
    def test_cancels_resting_orders_on_series(self):
        client, cfg, led, notif, rec = _stack(
            open_orders=[{"ticker": MKT, "order_id": "A"},
                         {"ticker": MKT, "order_id": "B"},
                         {"ticker": "KXETH15M-x", "order_id": "C"}])  # other series ignored
        summary = rec.boot_reconcile()
        self.assertEqual(summary["canceled"], 2)
        self.assertIn("A", client.canceled)
        self.assertIn("B", client.canceled)
        self.assertNotIn("C", client.canceled)

    def test_orphan_leg_is_flattened(self):
        client, cfg, led, notif, rec = _stack(
            positions=[{"ticker": MKT, "position": 1, "average_price": 47}],
            orderbook={"orderbook": {"yes": {"bids": [[46, 5]], "asks": [[48, 5]]},
                                     "no": {"bids": [[52, 5]], "asks": [[54, 5]]}}})
        summary = rec.boot_reconcile()
        self.assertEqual(summary["orphans"], 1)
        # a market-out order was placed to flatten the held YES leg
        self.assertEqual(len(client.placed), 1)
        self.assertEqual(client.placed[0]["side"], "yes")
        self.assertEqual(client.placed[0]["type"], "market")
        # the recovery is booked and Drew was paged
        self.assertEqual(led.state_counts(0).get(KIND_SALVAGE), 1)
        self.assertTrue(any("ORPHAN RECOVERED" in a for a in notif.alerts))

    def test_flat_book_no_orphans_no_orders(self):
        client, cfg, led, notif, rec = _stack(positions=[{"ticker": MKT, "position": 0}])
        summary = rec.boot_reconcile()
        self.assertEqual(summary, {"canceled": 0, "orphans": 0})
        self.assertEqual(client.placed, [])

    def test_short_no_leg_flattened(self):
        # negative net position = long NO
        client, cfg, led, notif, rec = _stack(
            positions=[{"ticker": MKT, "position": -1, "average_price": 45}],
            orderbook={"orderbook": {"yes": {"bids": [[50, 5]], "asks": [[52, 5]]},
                                     "no": {"bids": [[46, 5]], "asks": [[48, 5]]}}})
        rec.boot_reconcile()
        self.assertEqual(client.placed[0]["side"], "no")


if __name__ == "__main__":
    unittest.main()
