"""Budget ledger tests: reserve/convert/release, gates, live_halt."""

import os
import pytest
import time
from d_worker import dstore, budget


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


class TestReserve:
    def test_grant_within_limits(self):
        res = budget.reserve("KXTEST-B97", 97, "D1_WX", 3600)
        assert res.granted
        assert res.reservation_id is not None

    def test_deny_on_halt_promotion(self):
        dstore.set_state("halt_promotion", "1")
        res = budget.reserve("KXTEST-B97", 97, "D1_WX", 3600)
        assert not res.granted
        assert "halt_promotion" in res.reason

    def test_deny_on_live_halt(self):
        dstore.set_state("live_halt", "1")
        res = budget.reserve("KXTEST-B97", 97, "D1_WX", 3600)
        assert not res.granted
        assert "live_halt" in res.reason

    def test_deny_on_book_cap(self):
        dstore.set_state("budget_book_cap_usd", "1.00")
        res1 = budget.reserve("KXTEST-B97", 97, "D1_WX", 3600)
        assert res1.granted
        res2 = budget.reserve("KXTEST-B98", 97, "D1_WX", 3600)
        assert not res2.granted
        assert "book_cap" in res2.reason

    def test_deny_on_class_cap(self):
        dstore.set_state("budget_per_class_cap_usd", "0.50")
        res = budget.reserve("KXTEST-B97", 97, "D1_WX", 3600)
        assert not res.granted
        assert "class_cap" in res.reason

    def test_deny_on_per_market_cap(self):
        dstore.set_state("budget_per_market_cap_lots", "1")
        res1 = budget.reserve("KXTEST-B97", 97, "D1_WX", 3600)
        assert res1.granted
        res2 = budget.reserve("KXTEST-B97", 97, "D1_WX", 3600)
        assert not res2.granted
        assert "per_market_cap" in res2.reason


class TestConvertRelease:
    def test_convert_and_release(self):
        res = budget.reserve("KXTEST-B97", 97, "D1_WX", 3600)
        assert res.granted
        assert budget.convert(res.reservation_id)
        ledger = budget.get_ledger_summary()
        assert ledger["at_risk_usd"] > 0
        assert budget.release(res.reservation_id, "SETTLED")
        ledger = budget.get_ledger_summary()
        assert ledger["at_risk_usd"] == 0

    def test_release_without_convert(self):
        res = budget.reserve("KXTEST-B97", 97, "D1_WX", 3600)
        assert budget.release(res.reservation_id, "CANCELLED")


class TestLiveHalt:
    def test_deny_all_sets_halt(self):
        budget.deny_all("test invariant break")
        assert dstore.get_state("live_halt") == "1"
        res = budget.reserve("KXTEST-B97", 97, "D1_WX", 3600)
        assert not res.granted

    def test_clear_halt(self):
        budget.deny_all("test")
        budget.clear_halt()
        assert dstore.get_state("live_halt") == "0"
        res = budget.reserve("KXTEST-B97", 97, "D1_WX", 3600)
        assert res.granted


class TestLedgerSummary:
    def test_empty_ledger(self):
        ledger = budget.get_ledger_summary()
        assert ledger["at_risk_usd"] == 0
        assert ledger["reserved_usd"] == 0
        assert ledger["lockup_days"] == 0

    def test_reserved_shows_in_ledger(self):
        budget.reserve("KXTEST-B97", 200, "D1_WX", 3600)
        ledger = budget.get_ledger_summary()
        assert ledger["reserved_usd"] == 2.00

    def test_denial_count(self):
        dstore.set_state("live_halt", "1")
        budget.reserve("A", 97, "D1_WX", 3600)
        budget.reserve("B", 97, "D1_WX", 3600)
        count = dstore.budget_denial_count_since(time.time() - 60)
        assert count == 2
