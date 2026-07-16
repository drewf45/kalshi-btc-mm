# tests/test_pack.py — F1.5/F1.6: lived flip-rate query + hourly pack completeness.
import unittest

from f_worker.config import Config
from f_worker.ledger import Ledger, KIND_BUNDLE, KIND_FLOOR, KIND_SAT_OUT
from f_worker.fpack import render_pack


class TestPack(unittest.TestCase):
    def test_realized_flip_stats(self):
        led = Ledger(":memory:")
        led.record_pnl("w1", KIND_BUNDLE, 14, 0, 14, False, 1)
        led.record_pnl("w2", KIND_FLOOR, 3, 0, 3, False, 1)
        led.record_pnl("w3", KIND_SAT_OUT, 0, 0, 0, False, 1)
        stats = led.realized_flip_stats(0)
        self.assertEqual(stats["entered"], 2)     # bundle + floor, not sat_out
        self.assertEqual(stats["bundle"], 1)
        self.assertEqual(stats["flip_rate"], 0.5)  # 1 both-flip / 2 entered

    def test_pack_has_required_lines(self):
        led = Ledger(":memory:")
        led.record_pnl("w1", KIND_BUNDLE, 14, 0, 14, False, 1)
        pack = render_pack(led, Config(), client=None)
        for token in ("size ladder: rung", "halt:", "day P&L", "lived flip rate", "mem:"):
            self.assertIn(token, pack)
        self.assertLessEqual(len(pack.splitlines()), 30)


if __name__ == "__main__":
    unittest.main()
