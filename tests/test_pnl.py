# tests/test_pnl.py — knee accounting on guaranteed numbers (§3). Salvage is booked as
# recovered capital; flip/floor legs are fee-free; taker exits pay the exact roundup fee.
import unittest

from f_worker import window as W, feemath
from f_worker.config import Config
from f_worker.manager import (compute_window_pnl, FLIP, FLOOR, SALVAGE, MARKET_OUT)
from f_worker.ledger import KIND_BUNDLE, KIND_FLOOR, KIND_LONE, KIND_SALVAGE, KIND_SAT_OUT


def _win():
    return W.Window(window_id="w", market_ticker="m", event_ticker="e",
                    open_ts=None, close_ts=1_700_000_000, rung=1, lots=1)


class TestPnL(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()

    def test_bundle_both_flip(self):
        w = _win()
        w.add_fill("yes", 48, 1); w.add_fill("no", 49, 1)
        w.resolve_leg("yes", 55, FLIP); w.resolve_leg("no", 56, FLIP)
        p = compute_window_pnl(w, self.cfg)
        self.assertEqual(p["kind"], KIND_BUNDLE)
        self.assertEqual(p["gross_cents"], (55 - 48) + (56 - 49))  # 14
        self.assertEqual(p["fees_cents"], 0)                        # flips are maker
        self.assertEqual(p["net_cents"], 14)
        self.assertFalse(p["stopped"])

    def test_bundle_rides_floor(self):
        w = _win()
        w.add_fill("yes", 48, 1); w.add_fill("no", 49, 1)
        w.resolve_leg("yes", None, FLOOR); w.resolve_leg("no", None, FLOOR)
        p = compute_window_pnl(w, self.cfg)
        self.assertEqual(p["kind"], KIND_FLOOR)
        self.assertEqual(p["net_cents"], 100 - (48 + 49))          # +3 floor
        self.assertEqual(p["fees_cents"], 0)

    def test_lone_flip(self):
        w = _win()
        w.add_fill("yes", 47, 1)
        w.resolve_leg("yes", 53, FLIP)
        p = compute_window_pnl(w, self.cfg)
        self.assertEqual(p["kind"], KIND_LONE)
        self.assertEqual(p["net_cents"], 6)

    def test_salvage_booked_as_recovery(self):
        w = _win()
        w.add_fill("yes", 47, 1)
        w.resolve_leg("yes", 40, SALVAGE)         # crossed to recover
        p = compute_window_pnl(w, self.cfg)
        self.assertEqual(p["kind"], KIND_SALVAGE)
        self.assertEqual(p["fees_cents"], feemath.fee_cents(40, 1))  # taker fee (2)
        self.assertEqual(p["net_cents"], (40 - 47) - feemath.fee_cents(40, 1))  # -9
        self.assertFalse(p["stopped"])            # -9 is not past the -25 stop

    def test_stopped_window_flag(self):
        w = _win()
        w.add_fill("yes", 47, 1)
        w.resolve_leg("yes", 20, MARKET_OUT)      # dumped at T-90
        p = compute_window_pnl(w, self.cfg)
        self.assertTrue(p["stopped"])             # net -29 <= -25 stop

    def test_sat_out_is_zero(self):
        w = _win()
        p = compute_window_pnl(w, self.cfg)
        self.assertEqual(p["kind"], KIND_SAT_OUT)
        self.assertEqual(p["net_cents"], 0)


if __name__ == "__main__":
    unittest.main()
