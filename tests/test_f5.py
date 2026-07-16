# tests/test_f5.py — WO-F5 "THE EXACT DESK": arm-at-birth, async settlement, taker
# short-fuse, one budget, sigma self-sampling, pinned W4b expiry.
import time
import unittest

import f_worker.lib.spot as spotmod
from f_worker import window as W
from f_worker.config import Config
from f_worker.ledger import Ledger, OK_SALVAGE
from f_worker.fgateway import FGateway
from f_worker.manager import Manager
from f_worker.pricebrain import PriceBrain
from f_worker.desk import Desk, TokenBucket
from f_worker.settlement import Settler, SettlementWorker
from tests.fakes import FakeClient, FakeNotifier

MKT = "KXBTC15M-26JUL161600-00"


def _stack(cfg=None, client=None):
    cfg = cfg or Config()
    client = client or FakeClient()
    led = Ledger(":memory:")
    notif = FakeNotifier()
    gw = FGateway(client, led, cfg, notif)
    pb = PriceBrain(cfg, gate_path="")
    mgr = Manager(gw, led, pb, cfg, notif)
    desk = Desk(client, gw, mgr, pb, led, cfg, notif)
    return desk, led, notif, gw, mgr, cfg, client


def _win(led, close_ts=1_800_000_000):
    return W.Window(window_id=MKT, market_ticker=MKT, event_ticker="E", open_ts=None,
                    close_ts=close_ts, rung=1, lots=1).bind_ledger(led)


# ---- F5.1 ARM AT BIRTH ----
class TestArmedState(unittest.TestCase):
    def test_armed_edges(self):
        led = Ledger(":memory:")
        w = _win(led)
        w.transition(W.ARMED)
        w.transition(W.SEEKING)          # ARMED -> SEEKING legal
        self.assertEqual(w.state, W.SEEKING)

    def test_armed_to_sat_out(self):
        led = Ledger(":memory:")
        w = _win(led); w.transition(W.ARMED); w.transition(W.SAT_OUT)
        self.assertTrue(w.is_terminal)

    def test_armed_cannot_skip_to_holding(self):
        w = _win(Ledger(":memory:")); w.transition(W.ARMED)
        with self.assertRaises(W.IllegalTransition):
            w.transition(W.HOLDING)

    def test_run_window_arms_then_gates_on_fresh_book(self):
        # wall_open in the past (close in 30s) -> gate fires immediately on a blank book
        desk, led, notif, gw, mgr, cfg, client = _stack()
        desk.pb.sigma = lambda now=None: 10.0        # feed alive, mid regime
        client.orderbook = {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}}  # blank
        w = _win(led, close_ts=int(time.time()) + 30)
        desk.run_window(w)
        self.assertEqual(w.state, W.SAT_OUT)         # book missing a side, gated fast
        states = [r["to_state"] for r in led._conn.execute(
            "SELECT to_state FROM window_events WHERE window_id=? ORDER BY id", (MKT,))]
        self.assertIn("ARMED", states)               # armed before it fired


# ---- F5.2 ASYNC SETTLEMENT ----
class TestAsyncSettlement(unittest.TestCase):
    def test_settle_floor_enqueues_not_blocks(self):
        desk, led, notif, gw, mgr, cfg, client = _stack()
        calls = {"get_market": 0}
        client.get_market = lambda t: calls.__setitem__("get_market", calls["get_market"] + 1)
        enq = []
        desk.settle_worker = type("W", (), {"enqueue": lambda self, w, n: enq.append((w, n))})()
        w = _win(led)
        w.add_fill("yes", 48, 1); w.add_fill("no", 49, 1)
        w.resolve_leg("yes", None, "floor"); w.resolve_leg("no", None, "floor")
        desk._settle_floor(w)
        self.assertEqual(len(enq), 1)                # handed off
        self.assertEqual(calls["get_market"], 0)     # did NOT block on the poll

    def test_worker_processes_queue(self):
        led = Ledger(":memory:")
        client = FakeClient(market={"status": "settled", "result": "yes"})
        settler = Settler(client, led, Config(), FakeNotifier(), sleep=lambda s: None)
        worker = SettlementWorker(settler)
        w = _win(led)
        w.add_fill("yes", 48, 1); w.add_fill("no", 49, 1)
        w.resolve_leg("yes", None, "floor"); w.resolve_leg("no", None, "floor")
        worker.enqueue(w, 3)
        win, booked = worker._q.get_nowait()
        settler.verify_floor(win, booked)            # what the thread does
        n = led._conn.execute("SELECT COUNT(*) c FROM settlements").fetchone()["c"]
        self.assertEqual(n, 1)


# ---- F5.3 TAKER SHORT-FUSE ----
class TestTakerShortFuse(unittest.TestCase):
    def test_salvage_carries_short_fuse(self):
        desk, led, notif, gw, mgr, cfg, client = _stack()
        gw.salvage_cross(MKT, MKT, "yes", entry_price=47, exit_price=40, is_lone=True)
        body = client.placed[-1]
        self.assertIn("expiration_time", body)
        self.assertLessEqual(body["expiration_time"] - int(time.time()), cfg.taker_fuse_sec + 1)

    def test_cross_once_recross_once_then_alert(self):
        desk, led, notif, gw, mgr, cfg, client = _stack()
        w = _win(led); w.mode = "lone"; w.add_fill("yes", 47, 1)
        w.transition(W.SEEKING); w.transition(W.HOLDING)
        t0 = 1000.0
        mgr.salvage_walk(w, best_bid=40, seconds_to_close=100, now=t0)          # cross 1
        self.assertEqual(w.legs[0].salvage_attempts, 1)
        mgr.salvage_walk(w, best_bid=40, seconds_to_close=100, now=t0 + 1)      # within fuse: no-op
        self.assertEqual(w.legs[0].salvage_attempts, 1)
        mgr.salvage_walk(w, best_bid=40, seconds_to_close=100, now=t0 + 25)     # fuse passed: recross
        self.assertEqual(w.legs[0].salvage_attempts, 2)
        mgr.salvage_walk(w, best_bid=40, seconds_to_close=100, now=t0 + 60)     # cap: alert, no 3rd
        self.assertEqual(w.legs[0].salvage_attempts, 2)
        self.assertTrue(any("exit unfilled after recross" in a for a in notif.alerts))

    def test_filled_first_try_no_recross(self):
        desk, led, notif, gw, mgr, cfg, client = _stack()
        w = _win(led); w.mode = "lone"; w.add_fill("yes", 47, 1)
        w.transition(W.SEEKING); w.transition(W.HOLDING)
        mgr.salvage_walk(w, best_bid=40, seconds_to_close=100, now=1000.0)      # cross 1
        mgr.on_salvage_fill(w, "yes", 40)                                       # it FILLED
        self.assertTrue(w.is_terminal)
        self.assertEqual(led.state_counts(0).get(OK_SALVAGE, None) or
                         led.state_counts(0).get("salvage"), 1)


# ---- F5.4 ONE BUDGET ----
class TestOneBudget(unittest.TestCase):
    def test_fills_poll_lives_under_budget(self):
        cfg = Config()
        cfg.req_per_min = 5
        client = FakeClient()
        n_calls = {"fills": 0}
        client.get_fills = lambda mt=None, limit=200: (n_calls.__setitem__("fills", n_calls["fills"] + 1) or [])
        desk, led, notif, gw, mgr, _, _ = _stack(cfg=cfg, client=client)
        desk._api_bucket = TokenBucket(cfg.req_per_min, cfg.req_per_min, now=0.0)
        w = _win(led)
        for _ in range(100):
            desk._poll_fills(w, {"yes": "A"})       # hammered
        self.assertLessEqual(n_calls["fills"], cfg.req_per_min)   # bucket capped it


# ---- F5.5 SIGMA SELF-SAMPLING ----
class TestSigmaSelfSampling(unittest.TestCase):
    def setUp(self):
        self._orig = spotmod.realized_sigma_usd_per_sqrt_sec

    def tearDown(self):
        spotmod.realized_sigma_usd_per_sqrt_sec = self._orig

    def test_sigma_flows_from_spot_when_candles_dead(self):
        spotmod.realized_sigma_usd_per_sqrt_sec = lambda s: None    # candles down
        sc = spotmod.SigmaCache(floor=1.0, ceil=100.0, min_samples=5, min_span_sec=10.0)
        for i in range(10):
            sc.add_sample(100000.0 + (i % 2) * 20.0, now=1000.0 + i * 3)  # oscillating spot
        v = sc.get(session=None, now=1100.0)
        self.assertIsNotNone(v)
        self.assertEqual(sc.last_source, "spot_samples")

    def test_blind_when_both_dead(self):
        spotmod.realized_sigma_usd_per_sqrt_sec = lambda s: None    # candles down
        sc = spotmod.SigmaCache(min_samples=5, min_span_sec=10.0)   # no spot samples
        self.assertIsNone(sc.get(session=None, now=1000.0))         # -> BLIND


# ---- F5.6 PINNED W4b ENTRY EXPIRY ----
class TestEntryExpiryPinned(unittest.TestCase):
    def test_entry_body_carries_close_anchored_expiration(self):
        desk, led, notif, gw, mgr, cfg, client = _stack()
        # close far enough out that expiry is in the future
        w = _win(led, close_ts=int(time.time()) + 800)
        from f_worker.pricebrain import GateDecision
        mgr.post_entry(w, GateDecision(True, "one shot", 48, 49), seconds_to_close=800)
        exp = w.close_ts - cfg.flat_at_t
        for body in client.placed:
            self.assertEqual(body["expiration_time"], exp)      # W4b proven, not intent


if __name__ == "__main__":
    unittest.main()
