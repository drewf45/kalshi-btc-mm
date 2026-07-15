"""Shadow tests: fee-true settlement nets, UNINSTRUMENTED maker fill, station cache."""

import os
import json
import pytest
import time
from unittest.mock import patch, MagicMock
from d_worker import dstore, shadow


@pytest.fixture(autouse=True)
def _setup_db(tmp_path):
    db_path = str(tmp_path / "test_kal_d.db")
    os.environ["DW_DB_PATH"] = db_path
    dstore.DB_PATH = db_path
    dstore.init_db()
    yield
    dstore._conn.close()
    dstore._conn = None


class TestSettleNet:
    def test_win_99_cent_mult1_net_zero(self):
        """1-lot @ 99¢, mult=1 → fee=ceil(1*0.07*0.99*0.01*100)=1¢, net=0¢."""
        net = shadow._settle_net(correct=True, price=99, fee=1)
        assert net == 0.0

    def test_win_50_cent_no_fee(self):
        net = shadow._settle_net(correct=True, price=50, fee=0)
        assert net == 50.0

    def test_loss_returns_negative_price_plus_fee(self):
        net = shadow._settle_net(correct=False, price=97, fee=2)
        assert net == -99.0

    def test_loss_99_cent_mult1(self):
        """1-lot @ 99¢, mult=1 loss → -(99+1) = -100¢."""
        net = shadow._settle_net(correct=False, price=99, fee=1)
        assert net == -100.0

    def test_zero_price_returns_zero(self):
        net = shadow._settle_net(correct=True, price=0, fee=0)
        assert net == 0.0


class TestExtract1LotFees:
    def test_normal_sizes_json(self):
        sizes = {"1": {"taker_fee": 1, "maker_fee": 0, "size": 1}}
        tf, mf = shadow._extract_1lot_fees(json.dumps(sizes))
        assert tf == 1
        assert mf == 0

    def test_empty_json(self):
        tf, mf = shadow._extract_1lot_fees("")
        assert tf == 0
        assert mf == 0

    def test_invalid_json(self):
        tf, mf = shadow._extract_1lot_fees("{bad json")
        assert tf == 0
        assert mf == 0

    def test_missing_lot_1(self):
        sizes = {"5": {"taker_fee": 3, "maker_fee": 1}}
        tf, mf = shadow._extract_1lot_fees(json.dumps(sizes))
        assert tf == 0
        assert mf == 0


class TestCheckMakerFill:
    def test_returns_none_uninstrumented(self):
        assert shadow._check_maker_fill({}) is None


class TestSettleSeeds:
    @patch("d_worker.shadow.kalshi")
    @patch("d_worker.shadow.notify")
    def test_settle_correct_fee_true(self, mock_notify, mock_kalshi):
        """Settling a correct seed subtracts seed-time fees from net."""
        past = time.time() - 300
        sizes = {"1": {"taker_fee": 1, "maker_fee": 0, "size": 1}}
        vid = dstore.insert_verdict(
            0, "KXTEST-WIN", "KXTEST", past,
            "D1_WX", "SEED", "yes", {}, 1.0, 1.0)
        sid = dstore.insert_seed(
            vid, "KXTEST-WIN", "D1_WX", "yes", "{}",
            99, True, 98, json.dumps(sizes), past)

        mock_kalshi.get_settlement_result.return_value = "yes"
        client = MagicMock()
        settled = shadow.settle_seeds(client)
        assert settled == 1

        seeds = dstore.get_unsettled_seeds()
        assert len(seeds) == 0

        with dstore._lock:
            row = dstore._conn.execute(
                "SELECT net_clip_taker_cents, net_clip_maker_cents, maker_filled_est "
                "FROM shadow_seeds WHERE id=?", (sid,)).fetchone()
        assert row[0] == 0.0  # 100-99-1 = 0
        assert row[1] == 2.0  # 100-98-0 = 2
        assert row[2] is None  # UNINSTRUMENTED

    @patch("d_worker.shadow.kalshi")
    @patch("d_worker.shadow.notify")
    def test_settle_wrong_sets_halt(self, mock_notify, mock_kalshi):
        past = time.time() - 300
        vid = dstore.insert_verdict(
            0, "KXTEST-LOSE", "KXTEST", past,
            "D1_WX", "SEED", "yes", {}, 1.0, 1.0)
        dstore.insert_seed(
            vid, "KXTEST-LOSE", "D1_WX", "yes", "{}",
            97, True, 96, "{}", past)

        mock_kalshi.get_settlement_result.return_value = "no"
        client = MagicMock()
        settled = shadow.settle_seeds(client)
        assert settled == 1
        assert dstore.get_state("halt_promotion") == "1"

    @patch("d_worker.shadow.kalshi")
    @patch("d_worker.shadow.notify")
    def test_settle_with_governor(self, mock_notify, mock_kalshi):
        """Governor.consume called for each settlement API check."""
        past = time.time() - 300
        vid = dstore.insert_verdict(
            0, "KXTEST-GOV", "KXTEST", past,
            "D1_WX", "SEED", "yes", {}, 1.0, 1.0)
        dstore.insert_seed(
            vid, "KXTEST-GOV", "D1_WX", "yes", "{}",
            97, True, 96, "{}", past)

        mock_kalshi.get_settlement_result.return_value = "yes"
        client = MagicMock()
        gov = MagicMock()
        shadow.settle_seeds(client, governor=gov)
        gov.consume.assert_called_once_with(1)
