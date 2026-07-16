# tests/test_orderbook_fp.py — F2: the blank-book bug and its accomplices.
# The desk was gating every window against (None,None,None,None) because the live API
# returns `orderbook_fp` with dollar-string prices and the pre-fp parser only read
# `orderbook` with numeric cents. These fixtures are the shape verbatim off the tape.
import unittest

import f_worker.lib.spot as spotmod
from f_worker.config import Config
from f_worker.ledger import Ledger
from f_worker.fgateway import FGateway
from f_worker.manager import Manager
from f_worker.pricebrain import PriceBrain, GateDecision
from f_worker.desk import Desk
from f_worker.lib.marketutil import parse_best_yes_no, raw_book_sample, _price_to_cents
from tests.fakes import FakeClient, FakeNotifier

# LIVE fp response shape (envelope `orderbook_fp`, dollar-string prices) — the exact
# shape the old engine's tape logged as `[OB] shape: ['orderbook_fp']`.
FP_FIXTURE = {"orderbook_fp": {
    "yes": [["0.4700", "120"], ["0.4600", "50"]],
    "no":  [["0.5100", "80"],  ["0.5000", "30"]],
}}
# Legacy shape (envelope `orderbook`, integer cents) — must keep parsing unchanged.
LEGACY_FIXTURE = {"orderbook": {"yes": [[47, 120], [46, 50]], "no": [[51, 80], [50, 30]]}}


class TestFpParse(unittest.TestCase):
    def test_price_coercion(self):
        self.assertEqual(_price_to_cents("0.4700"), 47)
        self.assertEqual(_price_to_cents("47"), 47)
        self.assertEqual(_price_to_cents(47), 47)
        self.assertEqual(_price_to_cents(0.47), 47)
        self.assertIsNone(_price_to_cents(""))
        self.assertIsNone(_price_to_cents(None))

    def test_fp_shape_parses(self):
        yb, ya, nb, na = parse_best_yes_no(FP_FIXTURE)
        self.assertEqual(yb, 47)
        self.assertEqual(nb, 51)
        self.assertEqual(ya, 49)   # 100 - no_bid
        self.assertEqual(na, 53)   # 100 - yes_bid

    def test_legacy_shape_unchanged(self):
        self.assertEqual(parse_best_yes_no(LEGACY_FIXTURE), (47, 49, 51, 53))

    def test_blank_book_still_blank(self):
        self.assertEqual(parse_best_yes_no({"orderbook_fp": {"yes": [], "no": []}}),
                         (None, None, None, None))

    def test_raw_sample_for_evidence(self):
        keys, sample = raw_book_sample(FP_FIXTURE)
        self.assertEqual(keys, ["orderbook_fp"])
        self.assertEqual(sample, ["0.4700", "120"])


class TestSigmaHonesty(unittest.TestCase):
    def setUp(self):
        self._orig = spotmod.realized_sigma_usd_per_sqrt_sec

    def tearDown(self):
        spotmod.realized_sigma_usd_per_sqrt_sec = self._orig

    def test_none_when_feed_dead(self):
        spotmod.realized_sigma_usd_per_sqrt_sec = lambda s: None
        sc = spotmod.SigmaCache(floor=6.0, ceil=40.0, refresh_sec=0.0)
        self.assertIsNone(sc.get(session=None, now=100.0))   # no fabricated 12.0
        self.assertIsNone(sc.last_raw)

    def test_clamp_is_tagged(self):
        spotmod.realized_sigma_usd_per_sqrt_sec = lambda s: 3.0   # below floor
        sc = spotmod.SigmaCache(floor=6.0, ceil=40.0, refresh_sec=0.0)
        self.assertEqual(sc.get(session=None, now=100.0), 6.0)
        self.assertEqual(sc.last_raw, 3.0)
        self.assertTrue(sc.last_clamped)

    def test_unclamped_not_tagged(self):
        spotmod.realized_sigma_usd_per_sqrt_sec = lambda s: 10.0
        sc = spotmod.SigmaCache(floor=6.0, ceil=40.0, refresh_sec=0.0)
        self.assertEqual(sc.get(session=None, now=100.0), 10.0)
        self.assertFalse(sc.last_clamped)


class TestBlindReachable(unittest.TestCase):
    def test_dead_sigma_reaches_blind_with_raw_keys(self):
        pb = PriceBrain(Config(), gate_path="")
        pb.sigma = lambda now=None: None            # feed dead -> None (now reachable)
        g = pb.gate_window(FP_FIXTURE)
        self.assertFalse(g.ok)
        self.assertTrue(g.reason.startswith("BLIND"))
        self.assertEqual(g.evidence["ob_keys"], ["orderbook_fp"])   # evidence can convict parser

    def test_gate_evidence_tags_clamp_and_sample(self):
        pb = PriceBrain(Config(), gate_path="")
        pb._sigma.last_raw = 3.0
        pb._sigma.last_clamped = True
        g = pb.gate_window(FP_FIXTURE, sigma=6.0)   # explicit sigma bypasses fetch
        self.assertEqual(g.evidence["sigma_raw"], 3.0)
        self.assertTrue(g.evidence["clamped"])
        self.assertEqual(g.evidence["sample"], ["0.4700", "120"])

    def test_fp_book_now_passes_the_gate(self):
        # the whole point: with the real shape readable, a healthy book yields one shot
        pb = PriceBrain(Config(), gate_path="")
        g = pb.gate_window(FP_FIXTURE, sigma=10.0)
        self.assertTrue(g.ok)
        self.assertEqual((g.yes_price, g.no_price), (47, 51))


class TestStuckGauge(unittest.TestCase):
    def _desk(self):
        cfg = Config()
        led = Ledger(":memory:")
        notif = FakeNotifier()
        gw = FGateway(FakeClient(), led, cfg, notif)
        mgr = Manager(gw, led, PriceBrain(cfg, gate_path=""), cfg, notif)
        return Desk(FakeClient(), gw, mgr, PriceBrain(cfg, gate_path=""), led, cfg, notif), notif

    def test_ledger_streak_and_reset(self):
        led = Ledger(":memory:")
        for _ in range(3):
            s = led.record_gate_reason("book missing a side")
        self.assertEqual(s, 3)
        self.assertEqual(led.record_gate_reason("book missing a side"), 4)
        self.assertEqual(led.record_gate_reason("one shot"), 1)          # different resets
        self.assertEqual(led.record_gate_reason("book missing a side"), 1)

    def test_fires_on_fourth_not_third(self):
        desk, notif = self._desk()
        refusal = GateDecision(False, "book missing a side", regime="mid")
        for _ in range(3):
            desk._check_stuck_gauge(refusal)
        self.assertEqual([a for a in notif.alerts if "sensor suspect" in a], [])
        desk._check_stuck_gauge(refusal)   # the 4th
        self.assertEqual(len([a for a in notif.alerts if "sensor suspect" in a]), 1)

    def test_healthy_pass_does_not_trip(self):
        desk, notif = self._desk()
        ok = GateDecision(True, "one shot", regime="mid")
        for _ in range(6):
            desk._check_stuck_gauge(ok)
        self.assertEqual([a for a in notif.alerts if "sensor suspect" in a], [])


if __name__ == "__main__":
    unittest.main()
