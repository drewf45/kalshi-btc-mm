"""tests/test_flip_math.py — WO-LANE-FLIP pinned complement math (crypto-free).

The four flip intents, pinned to the numbers the doctrine requires. Runs without the
cryptography-bound client (flip_math imports nothing but typing)."""

import unittest

from k_worker import flip_math as fm


class TestFlipComplementMath(unittest.TestCase):
    # ---- ENTRIES (buys) ----
    def test_entry_yes(self):
        self.assertEqual(fm.entry_args("yes", 48), ("yes", 48))
        self.assertEqual(fm.v2_of("yes", 48), ("bid", 48))          # bid YES @48

    def test_entry_no(self):
        self.assertEqual(fm.entry_args("no", 49), ("no", 49))       # buy NO @49
        self.assertEqual(fm.v2_of("no", 49), ("ask", 51))           # == ask YES @51

    # ---- EXITS (complement buys that net flat) ----
    def test_exit_held_yes_at_q(self):
        # flip a held YES at 52  ->  buy NO @48  ->  ask YES @52 (sells the YES at 52)
        self.assertEqual(fm.exit_args("yes", 52), ("no", 48))
        self.assertEqual(fm.v2_of("no", 48), ("ask", 52))

    def test_exit_held_no_at_q(self):
        # flip a held NO at 52   ->  buy YES @48  ->  bid YES @48 (sells the NO at 52)
        self.assertEqual(fm.exit_args("no", 52), ("yes", 48))
        self.assertEqual(fm.v2_of("yes", 48), ("bid", 48))

    # ---- round trips: net = FLIP_X ----
    def test_roundtrip_yes(self):
        entry = 48
        flip_q = entry + 4
        es, ep = fm.exit_args("yes", flip_q)      # ('no', 48)
        v2side, v2px = fm.v2_of(es, ep)           # ('ask', 52) -> sells YES @52
        self.assertEqual((v2side, v2px), ("ask", flip_q))
        self.assertEqual(v2px - entry, 4)         # realized +4c

    def test_roundtrip_no(self):
        entry = 49
        flip_q = entry + 4                        # 53
        es, ep = fm.exit_args("no", flip_q)       # ('yes', 47)
        self.assertEqual((es, ep), ("yes", 47))
        self.assertEqual(fm.v2_of(es, ep), ("bid", 47))   # bid YES @47 sells the NO @53

    def test_bundle_cost_and_line(self):
        self.assertEqual(fm.bundle_cost(48, 49), 97)
        self.assertLessEqual(fm.bundle_cost(48, 49), 99)

    def test_bad_side_raises(self):
        with self.assertRaises(ValueError):
            fm.entry_args("maybe", 50)
        with self.assertRaises(ValueError):
            fm.exit_args("maybe", 50)


class TestModeSwitch(unittest.TestCase):
    """F-2: this branch flips by default; KW_MODE=OFF is the one-variable kill switch."""

    def test_default_enabled(self):
        self.assertTrue(fm.mode_enabled(None))      # unset -> runs
        self.assertTrue(fm.mode_enabled(""))        # empty  -> runs
        self.assertTrue(fm.mode_enabled("FLIP"))
        self.assertTrue(fm.mode_enabled("flip"))
        self.assertTrue(fm.mode_enabled("anything"))

    def test_off_kill_switch(self):
        self.assertFalse(fm.mode_enabled("OFF"))
        self.assertFalse(fm.mode_enabled("off"))
        self.assertFalse(fm.mode_enabled("  Off  "))


if __name__ == "__main__":
    unittest.main()
