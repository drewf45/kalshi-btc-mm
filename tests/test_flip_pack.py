"""tests/test_flip_pack.py — WO-LANE-FLIP-3 §4 hourly-pack pure formatters (crypto-free).

The account renders in DOLLARS, and any unexplained book move ≥ 2¢ is flagged. These pure
functions import nothing crypto-bound, so they run in the offline sandbox."""

import unittest

from k_worker import flip_pack as fp


class TestFlipPack(unittest.TestCase):
    def _w(self, **kw):
        base = dict(tag="20:00", outcome_tag="NETTED_2R", bundle_cost=98,
                    realized_cents=8, broker_flat=1)
        base.update(kw)
        return base

    # ── the units-bug fix: dollars, never cents ──────────────────────
    def test_balance_renders_dollars(self):
        text, _u, _f = fp.build_pack(
            account_usd=9.85, midnight_usd=9.77, hour_windows=[self._w()],
            day_windows=[self._w()], settled_pnl_usd=0.08, fees_usd=0.0)
        self.assertIn("account: $9.85", text)      # dollars, not $985 / $None
        self.assertIn("Δ +0.08", text)
        self.assertNotIn("$985", text)

    def test_window_line(self):
        line = fp.fmt_window(self._w())
        self.assertIn("W20:00", line)
        self.assertIn("NETTED_2R", line)
        self.assertIn("98→", line)
        self.assertIn("+8¢", line)
        self.assertIn("flat✓", line)

    def test_window_line_inventory_mark(self):
        line = fp.fmt_window(self._w(broker_flat=0, outcome_tag="NETTED_1R"))
        self.assertIn("🚨inv", line)

    def test_window_line_lone_no_bundle(self):
        line = fp.fmt_window(self._w(bundle_cost=None, outcome_tag="LONE_FLIP",
                                     realized_cents=4))
        self.assertIn("lone→", line)
        self.assertIn("+4¢", line)

    # ── the explained delta: unexplained ≥ 2¢ fires ─────────────────
    def test_reconcile_flags_three_cent_gap(self):
        unexplained, flagged = fp.reconcile(0.05, 0.02)   # Δ 5¢, settled 2¢ → 3¢ gap
        self.assertAlmostEqual(unexplained, 0.03, places=6)
        self.assertTrue(flagged)

    def test_reconcile_ok_when_explained(self):
        unexplained, flagged = fp.reconcile(0.02, 0.02)
        self.assertAlmostEqual(unexplained, 0.0, places=6)
        self.assertFalse(flagged)

    def test_reconcile_one_cent_not_flagged(self):
        _u, flagged = fp.reconcile(0.03, 0.02)            # 1¢ < 2¢ threshold
        self.assertFalse(flagged)

    def test_pack_unexplained_prints_red(self):
        text, unexplained, flagged = fp.build_pack(
            account_usd=10.05, midnight_usd=10.00, hour_windows=[],
            day_windows=[self._w()], settled_pnl_usd=0.02, fees_usd=0.0)
        self.assertTrue(flagged)
        self.assertAlmostEqual(unexplained, 0.03, places=6)
        self.assertIn("🔴", text)

    def test_pack_explained_prints_check(self):
        text, _u, flagged = fp.build_pack(
            account_usd=10.02, midnight_usd=10.00, hour_windows=[],
            day_windows=[self._w()], settled_pnl_usd=0.02, fees_usd=0.0)
        self.assertFalse(flagged)
        self.assertIn("✓", text)

    def test_pack_day_rollup_counts(self):
        days = [self._w(outcome_tag="NETTED_2R"), self._w(outcome_tag="SAT_line"),
                self._w(outcome_tag="LONE_FLIP")]
        text, _u, _f = fp.build_pack(
            account_usd=10.0, midnight_usd=10.0, hour_windows=[],
            day_windows=days, settled_pnl_usd=0.0, fees_usd=0.0)
        self.assertIn("2 entered", text)      # NETTED_2R + LONE_FLIP
        self.assertIn("1 sat", text)          # SAT_line


if __name__ == "__main__":
    unittest.main()
