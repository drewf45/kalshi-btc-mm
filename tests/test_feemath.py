# tests/test_feemath.py — W8 pinned fee cases + entry/leg predicates.
import unittest

from f_worker import feemath


class TestFeeMath(unittest.TestCase):
    def test_pinned_roundup(self):
        # Kalshi's own worked example: a single 50c contract rounds up to 2c.
        self.assertEqual(feemath.fee_cents(50, 1), 2)
        self.assertEqual(feemath.fee_cents(1, 1), 1)      # 0.0693c -> ceil 1
        self.assertEqual(feemath.fee_cents(99, 1), 1)     # symmetric with 1c
        self.assertEqual(feemath.fee_cents(40, 1), 2)     # 1.68c -> 2
        self.assertEqual(feemath.fee_cents(10, 1), 1)     # 0.63c -> 1
        self.assertEqual(feemath.fee_cents(50, 4), 7)     # exactly 7.0c, no over-round
        self.assertEqual(feemath.fee_cents(50, 10), 18)   # 17.5c -> 18
        self.assertEqual(feemath.fee_cents(50, 0), 0)     # no contracts, no fee

    def test_entry_ok_is_wall_W1(self):
        self.assertTrue(feemath.entry_ok(48, 49, 99))     # 97 <= 99
        self.assertTrue(feemath.entry_ok(49, 50, 99))     # 99 == line
        self.assertFalse(feemath.entry_ok(50, 50, 99))    # 100 > line

    def test_single_leg_ok_is_wall_W2(self):
        self.assertTrue(feemath.single_leg_ok(49, 49))
        self.assertFalse(feemath.single_leg_ok(50, 49))

    def test_taker_exit_prices_fee(self):
        # lone YES@47 flipped by crossing to 53: 6c gross - 2c fee = 4c net
        self.assertEqual(feemath.net_after_taker_exit(47, 53, 1), 4)
        self.assertTrue(feemath.exit_clears_hurdle(47, 53, 1, 2))
        self.assertFalse(feemath.exit_clears_hurdle(47, 53, 1, 5))


if __name__ == "__main__":
    unittest.main()
