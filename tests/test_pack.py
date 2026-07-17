"""Pack tests: 8-section format ≤30 lines, flood budget, hourly send."""

import os
import pytest
import time
from unittest.mock import patch, MagicMock
from d_worker import dstore, pack


@pytest.fixture(autouse=True)
def _setup_db(tmp_path):
    db_path = str(tmp_path / "test_kal_d.db")
    os.environ["DW_DB_PATH"] = db_path
    dstore.DB_PATH = db_path
    dstore.init_db()
    yield
    dstore._conn.close()
    dstore._conn = None


@pytest.fixture(autouse=True)
def _reset_pack():
    pack._msg_count_this_hour = 0
    pack._msg_hour = -1
    pack._overflow_count = 0
    yield


def _seed_cycle(markets=50, seeds=2, skips=45, errs=1):
    cid = dstore.start_cycle(30)
    dstore.finish_cycle(cid, markets, seeds, skips, errs, 5, "test")
    return cid


class TestBuildPack:
    @patch("d_worker.pack.notify")
    def test_eight_sections_present(self, mock_notify):
        _seed_cycle()
        text = pack.build_pack()
        lines = text.strip().split("\n")
        for i in range(1, 9):
            assert any(line.strip().startswith(f"{i}.") for line in lines), \
                f"Section {i} missing from pack"

    @patch("d_worker.pack.notify")
    def test_pack_starts_with_prefix(self, mock_notify):
        _seed_cycle()
        text = pack.build_pack()
        assert text.startswith("🅳")

    @patch("d_worker.pack.notify")
    def test_pack_under_30_lines(self, mock_notify):
        _seed_cycle()
        text = pack.build_pack()
        lines = text.strip().split("\n")
        assert len(lines) <= 30, f"Pack has {len(lines)} lines, expected ≤30"

    @patch("d_worker.pack.notify")
    def test_quiet_mode_no_activity(self, mock_notify):
        text = pack.build_pack()
        assert "quiet" in text.lower()

    @patch("d_worker.pack.notify")
    def test_hourly_header(self, mock_notify):
        _seed_cycle()
        text = pack.build_pack()
        assert "HOURLY" in text

    @patch("d_worker.pack.notify")
    def test_alerts_section_halt(self, mock_notify):
        dstore.set_state("halt_promotion", "1")
        _seed_cycle()
        text = pack.build_pack()
        assert "halt_promotion" in text

    @patch("d_worker.pack.notify")
    def test_fills_uninstrumented(self, mock_notify):
        _seed_cycle()
        text = pack.build_pack()
        assert "UNINSTRUMENTED" in text


class TestFloodBudget:
    @patch("d_worker.pack.notify")
    def test_over_budget_suppressed(self, mock_notify):
        pack.MSG_BUDGET_PER_HOUR = 3
        for i in range(5):
            pack.send_event(f"msg {i}")
        assert mock_notify.send.call_count == 3
        pack.MSG_BUDGET_PER_HOUR = 20

    @patch("d_worker.pack.notify")
    def test_within_budget_sent(self, mock_notify):
        pack.MSG_BUDGET_PER_HOUR = 10
        for i in range(5):
            pack.send_event(f"msg {i}")
        assert mock_notify.send.call_count == 5
        pack.MSG_BUDGET_PER_HOUR = 20


class TestHourlySend:
    @patch("d_worker.pack.notify")
    def test_send_hourly_writes_to_db_and_sends(self, mock_notify):
        _seed_cycle()
        pack.send_hourly_pack()
        packs = dstore.latest_packs(1)
        assert len(packs) == 1
        assert "🅳" in packs[0]["rendered_text"]
        assert mock_notify.send.call_count == 1

    @patch("d_worker.pack.notify")
    def test_send_hourly_idempotent(self, mock_notify):
        _seed_cycle()
        pack.send_hourly_pack()
        pack.send_hourly_pack()
        packs = dstore.latest_packs(10)
        assert len(packs) == 1
        assert mock_notify.send.call_count == 1
