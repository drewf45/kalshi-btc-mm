"""Registry tests: approve flow, version bump, fee-change alert, env-var approval."""

import os
import pytest
import tempfile
from d_worker import dstore, registry, tg


@pytest.fixture(autouse=True)
def _setup_db(tmp_path):
    db_path = str(tmp_path / "test_kal_d.db")
    os.environ["DW_DB_PATH"] = db_path
    dstore.DB_PATH = db_path
    dstore.init_db()
    yield
    dstore._conn.close()
    dstore._conn = None


class TestApproveFlow:
    def test_draft_and_approve(self):
        dstore.draft_registry("KXHIGHNY", "NWS", "KNYC", "America/New_York",
                              "F", "0.5")
        reg = dstore.get_registry("KXHIGHNY")
        assert reg is not None
        assert reg["approved_ts"] is None
        assert reg["version"] == 1

        ok = dstore.approve_registry("KXHIGHNY", "drew")
        assert ok
        reg = dstore.get_registry("KXHIGHNY")
        assert reg["approved_ts"] is not None
        assert reg["approved_by"] == "drew"

    def test_approve_nonexistent(self):
        ok = dstore.approve_registry("NOPE", "drew")
        assert not ok

    def test_version_bump_clears_approval(self):
        dstore.draft_registry("KXHIGHNY", "NWS", "KNYC", "America/New_York",
                              "F", "0.5")
        dstore.approve_registry("KXHIGHNY", "drew")
        reg = dstore.get_registry("KXHIGHNY")
        assert reg["approved_ts"] is not None

        dstore.draft_registry("KXHIGHNY", "NWS", "KNYC_V2", "America/New_York",
                              "F", "1.0", notes="updated station")
        reg = dstore.get_registry("KXHIGHNY")
        assert reg["version"] == 2
        assert reg["approved_ts"] is None
        assert reg["station_or_ref"] == "KNYC_V2"


class TestFeeChange:
    def test_new_fee_no_alert(self):
        result = dstore.upsert_fee("KXHIGHNY", 1.0, 1.0)
        assert result is None

    def test_fee_change_returns_old(self):
        dstore.upsert_fee("KXHIGHNY", 1.0, 1.0)
        old = dstore.upsert_fee("KXHIGHNY", 2.0, 1.5)
        assert old is not None
        assert old["maker_mult"] == 1.0
        assert old["taker_mult"] == 1.0

    def test_same_fee_no_alert(self):
        dstore.upsert_fee("KXHIGHNY", 1.0, 1.0)
        old = dstore.upsert_fee("KXHIGHNY", 1.0, 1.0)
        assert old is None


class TestBlacklist:
    def test_crypto_blacklisted(self):
        assert registry.is_blacklisted("KXBTC15M")
        assert registry.is_blacklisted("KXETH15M")

    def test_non_crypto_not_blacklisted(self):
        assert not registry.is_blacklisted("KXHIGHNY")

    def test_user_blacklist(self):
        registry.add_blacklist("KXTEST")
        assert registry.is_blacklisted("KXTEST")
        registry.remove_blacklist("KXTEST")
        assert not registry.is_blacklisted("KXTEST")


class TestEnvApproval:
    def test_env_approves_drafted_series(self):
        dstore.draft_registry("KXHIGHNY", "NWS", "KNYC", "America/New_York",
                              "F", "0.5")
        os.environ["DW_APPROVED_SERIES"] = "KXHIGHNY"
        approved = tg.apply_env_approvals()
        assert "KXHIGHNY" in approved
        reg = dstore.get_registry("KXHIGHNY")
        assert reg["approved_ts"] is not None
        assert reg["approved_by"] == "env"
        os.environ.pop("DW_APPROVED_SERIES", None)

    def test_env_skips_already_approved(self):
        dstore.draft_registry("KXHIGHNY", "NWS", "KNYC", "America/New_York",
                              "F", "0.5")
        dstore.approve_registry("KXHIGHNY", "drew")
        os.environ["DW_APPROVED_SERIES"] = "KXHIGHNY"
        approved = tg.apply_env_approvals()
        assert approved == []
        os.environ.pop("DW_APPROVED_SERIES", None)

    def test_env_skips_undrafted(self):
        os.environ["DW_APPROVED_SERIES"] = "KXNOPE"
        approved = tg.apply_env_approvals()
        assert approved == []
        os.environ.pop("DW_APPROVED_SERIES", None)

    def test_env_empty_is_noop(self):
        os.environ["DW_APPROVED_SERIES"] = ""
        approved = tg.apply_env_approvals()
        assert approved == []
        os.environ.pop("DW_APPROVED_SERIES", None)


class TestEnvBlacklist:
    def test_env_blacklist(self):
        os.environ["DW_BLACKLIST_EXTRA"] = "KXFOO,KXBAR"
        added = tg.apply_env_blacklist()
        assert "KXFOO" in added
        assert "KXBAR" in added
        assert registry.is_blacklisted("KXFOO")
        assert registry.is_blacklisted("KXBAR")
        registry.remove_blacklist("KXFOO")
        registry.remove_blacklist("KXBAR")
        os.environ.pop("DW_BLACKLIST_EXTRA", None)


class TestEnvClearHalt:
    def test_clears_active_halt(self):
        dstore.set_state("halt_promotion", "1")
        os.environ["DW_CLEAR_HALT"] = "1"
        assert tg.apply_env_clear_halt()
        assert dstore.get_state("halt_promotion") == "0"
        os.environ.pop("DW_CLEAR_HALT", None)

    def test_ignores_no_halt(self):
        os.environ["DW_CLEAR_HALT"] = "1"
        assert not tg.apply_env_clear_halt()
        os.environ.pop("DW_CLEAR_HALT", None)

    def test_noop_without_env(self):
        dstore.set_state("halt_promotion", "1")
        os.environ.pop("DW_CLEAR_HALT", None)
        assert not tg.apply_env_clear_halt()
        assert dstore.get_state("halt_promotion") == "1"
