"""Gateway tests: rung gate, balance re-read, submit, abandon_ship."""

import os
import pytest
from unittest.mock import MagicMock, patch

from d_worker import dstore, budget, gateway


@pytest.fixture(autouse=True)
def _setup_db(tmp_path):
    db_path = str(tmp_path / "test_kal_d.db")
    os.environ["DW_DB_PATH"] = db_path
    dstore.DB_PATH = db_path
    dstore.init_db()
    budget.init_budget()
    yield
    dstore._conn.close()
    dstore._conn = None


def _mock_client():
    return MagicMock()


class TestRungGate:
    def test_submit_blocked_at_rung_0(self):
        dstore.set_state("current_rung", "0")
        res = budget.reserve("KXTEST-B97", 97, "D1_WX", 3600)
        result = gateway.submit(_mock_client(), "KXTEST-B97", "yes", 97,
                                res.reservation_id)
        assert result.status == "BLOCKED"
        assert "rung_gate" in result.error

    def test_submit_blocked_at_rung_1(self):
        dstore.set_state("current_rung", "1")
        res = budget.reserve("KXTEST-B97", 97, "D1_WX", 3600)
        result = gateway.submit(_mock_client(), "KXTEST-B97", "yes", 97,
                                res.reservation_id)
        assert result.status == "BLOCKED"
        assert "rung_gate" in result.error

    def test_is_live_enabled_at_rung_2(self):
        dstore.set_state("current_rung", "2")
        assert gateway.is_live_enabled()

    def test_not_live_at_rung_0(self):
        dstore.set_state("current_rung", "0")
        assert not gateway.is_live_enabled()


class TestLiveHaltGate:
    def test_submit_blocked_on_live_halt(self):
        dstore.set_state("current_rung", "2")
        dstore.set_state("live_halt", "1")
        res = budget.reserve("KXTEST-B97", 97, "D1_WX", 3600)
        result = gateway.submit(_mock_client(), "KXTEST-B97", "yes", 97,
                                res.reservation_id)
        assert result.status == "BLOCKED"
        assert "live_halt" in result.error


class TestBalanceReread:
    @patch("d_worker.gateway.kalshi")
    @patch("d_worker.gateway.notify")
    def test_blocked_on_balance_failure(self, mock_notify, mock_kalshi):
        dstore.set_state("current_rung", "2")
        mock_kalshi.get_balance.side_effect = Exception("network error")
        res = budget.reserve("KXTEST-B97", 97, "D1_WX", 3600)
        result = gateway.submit(_mock_client(), "KXTEST-B97", "yes", 97,
                                res.reservation_id)
        assert result.status == "BLOCKED"
        assert "balance_read_failed" in result.error

    @patch("d_worker.gateway.kalshi")
    @patch("d_worker.gateway.notify")
    def test_blocked_on_insufficient_balance(self, mock_notify, mock_kalshi):
        dstore.set_state("current_rung", "2")
        mock_kalshi.get_balance.return_value = (0.50, 0.50)
        res = budget.reserve("KXTEST-B97", 97, "D1_WX", 3600)
        result = gateway.submit(_mock_client(), "KXTEST-B97", "yes", 97,
                                res.reservation_id)
        assert result.status == "BLOCKED"
        assert "insufficient_balance" in result.error

    @patch("d_worker.gateway._place_taker_order")
    @patch("d_worker.gateway.kalshi")
    @patch("d_worker.gateway.notify")
    def test_submit_placed_on_sufficient_balance(self, mock_notify,
                                                  mock_kalshi, mock_place):
        dstore.set_state("current_rung", "2")
        mock_kalshi.get_balance.return_value = (10.00, 10.00)
        mock_place.return_value = "order-abc-123"
        res = budget.reserve("KXTEST-B97", 97, "D1_WX", 3600)
        client = _mock_client()
        result = gateway.submit(client, "KXTEST-B97", "yes", 97,
                                res.reservation_id)
        assert result.status == "PLACED"
        assert result.order_id == "order-abc-123"


class TestAbandonShip:
    def test_abandon_shadow_only_at_rung_0(self):
        dstore.set_state("current_rung", "0")
        result = gateway.abandon_ship(_mock_client(), "KXTEST-B97", "yes", 1)
        assert result.status == "SHADOW_ONLY"

    @patch("d_worker.gateway._place_taker_order")
    @patch("d_worker.gateway.kalshi")
    @patch("d_worker.gateway.notify")
    def test_abandon_executes_at_rung_2(self, mock_notify, mock_kalshi,
                                        mock_place):
        dstore.set_state("current_rung", "2")
        from k_worker.kalshi import Book
        mock_kalshi.fetch_orderbook.return_value = Book(yes_bid=90)
        mock_place.return_value = "exit-order-456"
        res = budget.reserve("KXTEST-B97", 97, "D1_WX", 3600)
        budget.convert(res.reservation_id)
        client = _mock_client()
        result = gateway.abandon_ship(client, "KXTEST-B97", "yes",
                                      res.reservation_id)
        assert result.status == "ABANDONED"
        assert result.fill_price == 90
