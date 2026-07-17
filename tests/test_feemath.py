"""Pinned fee-math tests from charter §2.2/§2.3."""

import pytest
from d_worker.feemath import order_fee_cents, entry_ok, taker_mult, maker_mult


class TestOrderFeeCents:
    def test_1lot_99c_taker_net_zero(self):
        """1-lot at 99¢ taker = 1¢ fee, gross 1¢, net 0."""
        fee = order_fee_cents(0.07, 1, 99)
        assert fee == 1
        gross = 100 - 99  # 1¢
        assert gross - fee == 0

    def test_10lot_97c_taker_27net(self):
        """10-lot at 97¢ taker = 3¢ fee, gross 30¢, net 27¢."""
        fee = order_fee_cents(0.07, 1, 97)
        fee_10 = order_fee_cents(0.07, 10, 97)
        assert fee_10 == 3
        gross_10 = (100 - 97) * 10  # 30¢
        assert gross_10 - fee_10 == 27

    def test_1lot_95c_taker(self):
        fee = order_fee_cents(0.07, 1, 95)
        # 0.07 × 1 × 0.95 × 0.05 × 100 = 0.3325 → ceil = 1
        assert fee == 1

    def test_1lot_50c_taker(self):
        fee = order_fee_cents(0.07, 1, 50)
        # 0.07 × 1 × 0.50 × 0.50 × 100 = 1.75 → ceil = 2
        assert fee == 2

    def test_maker_cheaper_than_taker(self):
        """Maker base (0.0175) is 4× cheaper than taker (0.07)."""
        t_fee = order_fee_cents(0.07, 10, 97)
        m_fee = order_fee_cents(0.0175, 10, 97)
        assert m_fee < t_fee

    def test_zero_contracts(self):
        assert order_fee_cents(0.07, 0, 99) == 0

    def test_edge_price_100(self):
        assert order_fee_cents(0.07, 1, 100) == 0

    def test_edge_price_0(self):
        assert order_fee_cents(0.07, 1, 0) == 0

    def test_series_multiplier(self):
        """Fee with 2× series multiplier should be ~2× base."""
        base = order_fee_cents(0.07, 10, 95)
        doubled = order_fee_cents(0.14, 10, 95)
        assert doubled >= base


class TestEntryOk:
    def test_99c_1lot_fails(self):
        """1-lot at 99¢ with min_net=1 fails (net=0)."""
        ok, net = entry_ok(99, 0.07, 1, 1.0)
        assert not ok
        assert net == 0

    def test_97c_10lot_passes(self):
        """10-lot at 97¢ with min_net=1 passes (net=27¢, 2.7¢/ct)."""
        ok, net = entry_ok(97, 0.07, 10, 1.0)
        assert ok
        assert net == 27

    def test_97c_1lot_passes(self):
        """1-lot at 97¢: gross 3¢, fee ceil(0.07×0.97×0.03×100)=1, net=2."""
        ok, net = entry_ok(97, 0.07, 1, 1.0)
        assert ok
        assert net == 2.0

    def test_high_min_net_rejects(self):
        ok, net = entry_ok(97, 0.07, 1, 5.0)
        assert not ok


class TestMultipliers:
    def test_taker_default(self):
        assert taker_mult() == 0.07

    def test_maker_default(self):
        assert maker_mult() == 0.0175

    def test_taker_with_series(self):
        assert taker_mult(2.0) == 0.14

    def test_maker_with_series(self):
        assert maker_mult(2.0) == 0.035
