"""Tests for env-var configuration (tg.py): rung and live_halt."""

import os
import pytest

from d_worker import dstore, tg


@pytest.fixture(autouse=True)
def _setup_db(tmp_path):
    db_path = str(tmp_path / "test_kal_d.db")
    os.environ["DW_DB_PATH"] = db_path
    dstore.DB_PATH = db_path
    dstore.init_db()
    yield
    dstore._conn.close()
    dstore._conn = None
    for key in ("DW_RUNG", "DW_CLEAR_LIVE_HALT"):
        os.environ.pop(key, None)


class TestApplyEnvRung:
    def test_sets_rung(self):
        os.environ["DW_RUNG"] = "2"
        assert tg.apply_env_rung()
        assert dstore.get_state("current_rung") == "2"

    def test_ignores_empty(self):
        os.environ.pop("DW_RUNG", None)
        assert not tg.apply_env_rung()

    def test_rejects_out_of_range(self):
        os.environ["DW_RUNG"] = "5"
        assert not tg.apply_env_rung()

    def test_rejects_negative(self):
        os.environ["DW_RUNG"] = "-1"
        assert not tg.apply_env_rung()

    def test_rejects_non_integer(self):
        os.environ["DW_RUNG"] = "abc"
        assert not tg.apply_env_rung()

    def test_rung_3_accepted(self):
        os.environ["DW_RUNG"] = "3"
        assert tg.apply_env_rung()
        assert dstore.get_state("current_rung") == "3"


class TestApplyEnvClearLiveHalt:
    def test_clears_active_live_halt(self):
        dstore.set_state("live_halt", "1")
        dstore.set_state("live_halt_reason", "test break")
        os.environ["DW_CLEAR_LIVE_HALT"] = "1"
        assert tg.apply_env_clear_live_halt()
        assert dstore.get_state("live_halt") == "0"
        assert dstore.get_state("live_halt_reason") == ""

    def test_ignores_no_halt(self):
        dstore.set_state("live_halt", "0")
        os.environ["DW_CLEAR_LIVE_HALT"] = "1"
        assert not tg.apply_env_clear_live_halt()

    def test_noop_without_env(self):
        dstore.set_state("live_halt", "1")
        os.environ.pop("DW_CLEAR_LIVE_HALT", None)
        assert not tg.apply_env_clear_live_halt()
        assert dstore.get_state("live_halt") == "1"
