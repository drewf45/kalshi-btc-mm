# tests/test_integration.py — a window handled end-to-end through the REAL manager,
# gateway, window machine and ledger (only the exchange client + notifier are fakes).
# This is the acceptance "first window handled end-to-end + one-line story" in-process.
import unittest

from f_worker import window as W
from f_worker.config import Config
from f_worker.ledger import Ledger, KIND_BUNDLE, KIND_SAT_OUT
from f_worker.fgateway import FGateway
from f_worker.manager import Manager
from f_worker.pricebrain import PriceBrain
from tests.fakes import FakeClient, FakeNotifier

MKT = "KXBTC15M-25JUL16T1415"


def _stack():
    client = FakeClient()
    cfg = Config()
    led = Ledger(":memory:")
    notif = FakeNotifier()
    gw = FGateway(client, led, cfg, notif)
    pb = PriceBrain(cfg, gate_path="")   # no gate file -> boot defaults, no network at init
    mgr = Manager(gw, led, pb, cfg, notif)
    return client, cfg, led, notif, gw, pb, mgr


def _win(led, cfg):
    rung, lots = led.current_rung()
    return W.Window(window_id=MKT, market_ticker=MKT, event_ticker="E", open_ts=None,
                    close_ts=1_700_000_000, rung=rung, lots=lots).bind_ledger(led)


class TestIntegration(unittest.TestCase):
    def test_bundle_both_flip_end_to_end(self):
        client, cfg, led, notif, gw, pb, mgr = _stack()
        w = _win(led, cfg)
        led.record_window(w.window_id, w.market_ticker, w.event_ticker, None, w.close_ts, w.rung)

        # SEEKING: post the entry pair (join best bids 48/49 -> bundle 97)
        w.transition(W.SEEKING)
        from f_worker.pricebrain import GateDecision
        gate = GateDecision(True, "one shot", yes_price=48, no_price=49)
        mgr.post_entry(w, gate, seconds_to_close=300)
        self.assertEqual(len(client.placed), 2)

        # both legs fill -> HOLDING(bundle)
        mgr.settle_entry_phase(w, {"yes": 48, "no": 49}, seconds_to_close=250)
        self.assertEqual(w.state, W.HOLDING)
        self.assertEqual(w.mode, "bundle")

        # post flips, then both get taken
        mgr.post_flips(w, seconds_to_close=200)
        mgr.on_flip_fill(w, "yes", 54)
        mgr.on_flip_fill(w, "no", 55)
        self.assertEqual(w.state, W.DONE)

        # a window_pnl row was booked and a one-line story sent
        counts = led.state_counts(0)
        self.assertEqual(counts.get(KIND_BUNDLE), 1)
        self.assertTrue(any("DONE" in s and "🔁" in s for s in notif.sent))

    def test_no_fill_sats_out(self):
        client, cfg, led, notif, gw, pb, mgr = _stack()
        w = _win(led, cfg)
        w.transition(W.SEEKING)
        from f_worker.pricebrain import GateDecision
        mgr.post_entry(w, GateDecision(True, "x", 48, 49), seconds_to_close=300)
        mgr.settle_entry_phase(w, {}, seconds_to_close=250)   # nothing filled
        self.assertEqual(w.state, W.SAT_OUT)
        self.assertEqual(led.state_counts(0).get(KIND_SAT_OUT), 1)
        self.assertTrue(any("SAT_OUT" in s for s in notif.sent))

    def test_unholdable_lone_goes_exiting(self):
        client, cfg, led, notif, gw, pb, mgr = _stack()
        w = _win(led, cfg)
        w.transition(W.SEEKING)
        from f_worker.pricebrain import GateDecision
        mgr.post_entry(w, GateDecision(True, "x", 48, 49), seconds_to_close=300)
        # only the NO leg fills, at 55 (> single_leg_max 49) -> not holdable
        mgr.settle_entry_phase(w, {"no": 55}, seconds_to_close=250)
        self.assertEqual(w.state, W.EXITING)
        mgr.flat_out(w, marks={"no": 52})
        self.assertEqual(w.state, W.DONE)

    def test_gate_rejects_calm_tape(self):
        _, cfg, _, _, _, pb, _ = _stack()
        ob = {"orderbook": {"yes": {"bids": [[48, 10]], "asks": [[50, 10]]},
                            "no": {"bids": [[49, 10]], "asks": [[51, 10]]}}}
        calm = pb.gate_window(ob, sigma=1.0)     # below floor
        self.assertFalse(calm.ok)
        good = pb.gate_window(ob, sigma=10.0)    # mid regime, tight book
        self.assertTrue(good.ok)
        self.assertEqual((good.yes_price, good.no_price), (48, 49))


if __name__ == "__main__":
    unittest.main()
