"""Scanner tests: streaming discovery, per-market error isolation, 429 handling."""

import os
import pytest
import time
from unittest.mock import patch, MagicMock, call
from d_worker import dstore, scanner


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
def _reset_scanner_state():
    scanner._governor = None
    scanner._sweep_counter = 0
    scanner._dead_series.clear()
    yield
    scanner._governor = None
    scanner._sweep_counter = 0
    scanner._dead_series.clear()


def _make_market(ticker, series="KXTEST", close_time="2026-07-15T00:00:00Z"):
    return {
        "ticker": ticker,
        "series_ticker": series,
        "subtitle": "Test market",
        "floor_strike": 80.0,
        "status": "active",
        "close_time": close_time,
    }


class TestTokenBucket:
    def test_initial_tokens(self):
        tb = scanner.TokenBucket(60)
        assert tb.rate == 60
        assert tb.tokens == 60.0

    def test_consume_reduces_tokens(self):
        tb = scanner.TokenBucket(60)
        tb.consume(5)
        assert tb.tokens < 60.0
        assert tb.total_consumed == 5

    def test_halve_reduces_rate(self):
        tb = scanner.TokenBucket(60)
        tb.halve()
        assert tb.rate == 30
        assert tb.max_tokens == 30.0

    def test_halve_floor_at_5(self):
        tb = scanner.TokenBucket(8)
        tb.halve()
        assert tb.rate == 5
        tb.halve()
        assert tb.rate == 5


class TestStreamDiscovery:
    @patch("d_worker.scanner._classify_market")
    def test_three_page_crawl(self, mock_classify):
        from d_worker.classify import VerdictRow
        mock_classify.return_value = VerdictRow(
            market_ticker="", series_ticker="KXTEST", close_ts=0,
            dclass="NONE", verdict="SKIP_NO_REGISTRY", side=None,
            evidence=None, fee_maker=None, fee_taker=None)
        pages = [
            {"markets": [_make_market(f"T{i}") for i in range(200)],
             "cursor": "page2"},
            {"markets": [_make_market(f"T{i+200}") for i in range(200)],
             "cursor": "page3"},
            {"markets": [_make_market(f"T{i+400}") for i in range(50)],
             "cursor": ""},
        ]
        client = MagicMock()
        client.request = MagicMock(side_effect=pages)
        gov = scanner.TokenBucket(1000)
        cycle_id = dstore.start_cycle(30)
        counters = {"seeds": 0, "skips": 0, "errs": 0}
        n = scanner._stream_discovery(client, gov, cycle_id, set(),
                                       counters, [], {}, set())
        assert n == 450
        assert client.request.call_count == 3

    def test_429_halves_and_stops(self):
        client = MagicMock()
        client.request = MagicMock(
            side_effect=RuntimeError("HTTP 429 Too Many Requests"))
        gov = scanner.TokenBucket(60)
        cycle_id = dstore.start_cycle(30)
        counters = {"seeds": 0, "skips": 0, "errs": 0}
        with patch("k_worker.notify"):
            n = scanner._stream_discovery(client, gov, cycle_id, set(),
                                           counters, [], {}, set())
        assert n == 0
        assert gov.rate == 30

    @patch("d_worker.scanner._classify_market")
    def test_empty_first_page(self, mock_classify):
        client = MagicMock()
        client.request = MagicMock(return_value={"markets": [], "cursor": ""})
        gov = scanner.TokenBucket(100)
        cycle_id = dstore.start_cycle(30)
        counters = {"seeds": 0, "skips": 0, "errs": 0}
        n = scanner._stream_discovery(client, gov, cycle_id, set(),
                                       counters, [], {}, set())
        assert n == 0

    @patch("d_worker.scanner._classify_market")
    def test_cursor_persists_and_resumes(self, mock_classify):
        from d_worker.classify import VerdictRow
        mock_classify.return_value = VerdictRow(
            market_ticker="", series_ticker="KXTEST", close_ts=0,
            dclass="NONE", verdict="SKIP_NO_REGISTRY", side=None,
            evidence=None, fee_maker=None, fee_taker=None)
        client = MagicMock()
        client.request = MagicMock(return_value={
            "markets": [_make_market("T1")], "cursor": "next_page"})
        gov = scanner.TokenBucket(100)
        cycle_id = dstore.start_cycle(30)
        counters = {"seeds": 0, "skips": 0, "errs": 0}

        old_pages = scanner.DISCOVERY_PAGES_PER_SWEEP
        scanner.DISCOVERY_PAGES_PER_SWEEP = 1
        try:
            scanner._stream_discovery(client, gov, cycle_id, set(),
                                       counters, [], {}, set())
        finally:
            scanner.DISCOVERY_PAGES_PER_SWEEP = old_pages

        assert dstore.get_state("discovery_cursor") == "next_page"

    @patch("d_worker.scanner._classify_market")
    def test_cursor_clears_at_end(self, mock_classify):
        from d_worker.classify import VerdictRow
        mock_classify.return_value = VerdictRow(
            market_ticker="", series_ticker="KXTEST", close_ts=0,
            dclass="NONE", verdict="SKIP_NO_REGISTRY", side=None,
            evidence=None, fee_maker=None, fee_taker=None)
        client = MagicMock()
        client.request = MagicMock(return_value={
            "markets": [_make_market("T1")], "cursor": ""})
        gov = scanner.TokenBucket(100)
        cycle_id = dstore.start_cycle(30)
        counters = {"seeds": 0, "skips": 0, "errs": 0}
        dstore.set_state("discovery_cursor", "old_cursor")
        scanner._stream_discovery(client, gov, cycle_id, set(),
                                   counters, [], {}, set())
        assert dstore.get_state("discovery_cursor") == ""


class TestPerMarketIsolation:
    @patch("k_worker.notify")
    @patch("d_worker.scanner._classify_market")
    @patch("d_worker.scanner._stream_discovery", return_value=0)
    @patch("d_worker.scanner._fetch_targeted")
    def test_one_error_does_not_crash_sweep(self, mock_targeted, mock_disc,
                                             mock_classify, mock_notify):
        markets = [_make_market("OK-1"), _make_market("BAD-1"),
                    _make_market("OK-2")]
        mock_targeted.return_value = markets

        from d_worker.classify import VerdictRow
        good_verdict = VerdictRow(
            market_ticker="", series_ticker="KXTEST", close_ts=0,
            dclass="D1_WX", verdict="SKIP_NOT_DECIDED", side=None,
            evidence=None, fee_maker=None, fee_taker=None)

        def classify_side_effect(client, market, gov, close_ts):
            if market["ticker"] == "BAD-1":
                raise ValueError("simulated error")
            return good_verdict

        mock_classify.side_effect = classify_side_effect
        scanner._governor = scanner.TokenBucket(100)

        with patch("d_worker.scanner.registry") as mock_reg:
            mock_reg.is_blacklisted.return_value = False
            client = MagicMock()
            cycle_id = scanner.sweep(client)

        cycle = dstore.latest_cycles(1)[0]
        assert cycle["errs"] == 1
        assert cycle["markets_seen"] == 3


class TestRunningValueKeying:
    def test_different_tz_different_key(self):
        """Stations in different timezones get different keys even at the same moment."""
        k1 = scanner._running_value_key("KNYC", "America/New_York")
        k2 = scanner._running_value_key("KLAX", "America/Los_Angeles")
        assert k1[0] != k2[0]
        assert isinstance(k1, tuple) and len(k1) == 2

    def test_same_station_same_key(self):
        k1 = scanner._running_value_key("KNYC", "America/New_York")
        k2 = scanner._running_value_key("KNYC", "America/New_York")
        assert k1 == k2

    def test_update_uses_local_date(self):
        """Running value update keys by station-local date, not NY."""
        scanner._running_values.clear()
        val = scanner._update_running_value("KLAX", 95.0, "America/Los_Angeles")
        assert val == 95.0
        key = scanner._running_value_key("KLAX", "America/Los_Angeles")
        assert scanner._running_values[key] == 95.0
        scanner._running_values.clear()


class TestObsCache:
    def test_cache_cleared_each_sweep(self):
        scanner._obs_cache["KNYC"] = "stale"
        scanner._obs_cache = {}
        assert "KNYC" not in scanner._obs_cache

    @patch("k_worker.notify")
    @patch("d_worker.scanner._classify_market")
    @patch("d_worker.scanner._stream_discovery", return_value=0)
    @patch("d_worker.scanner._fetch_targeted")
    def test_skip_aggregation(self, mock_targeted, mock_disc,
                               mock_classify, mock_notify):
        """Non-approved SKIPs aggregate; ERR rows stay individual."""
        markets = [_make_market(f"T{i}", series="KXUNAPPROVED") for i in range(5)]
        mock_targeted.return_value = markets

        from d_worker.classify import VerdictRow
        skip_verdict = VerdictRow(
            market_ticker="", series_ticker="KXUNAPPROVED", close_ts=0,
            dclass="NONE", verdict="SKIP_NO_REGISTRY", side=None,
            evidence=None, fee_maker=None, fee_taker=None)
        mock_classify.return_value = skip_verdict
        scanner._governor = scanner.TokenBucket(100)

        with patch("d_worker.scanner.registry") as mock_reg:
            mock_reg.is_blacklisted.return_value = False
            client = MagicMock()
            scanner.sweep(client)

        with dstore._lock:
            verdicts = dstore._conn.execute(
                "SELECT COUNT(*) FROM verdicts WHERE verdict='SKIP_NO_REGISTRY'"
            ).fetchone()[0]
            agg = dstore._conn.execute(
                "SELECT SUM(count) FROM verdict_skip_agg WHERE verdict='SKIP_NO_REGISTRY'"
            ).fetchone()[0]
        assert verdicts == 0
        assert agg == 5


class TestResolveCloseTs:
    def test_iso_string(self):
        ts = scanner._resolve_close_ts(
            {"close_time": "2026-07-15T00:00:00Z"})
        assert ts is not None and ts > 1_000_000_000

    def test_epoch_number(self):
        ts = scanner._resolve_close_ts(
            {"close_time": 1752624000.0})
        assert ts == 1752624000.0

    def test_no_time_returns_none(self):
        assert scanner._resolve_close_ts({}) is None


class TestTargetedSweep:
    def test_target_list_includes_bootstrap_grid(self):
        lst = scanner._target_series_list()
        assert "KXHIGHNY" in lst and "KXLOWCHI" in lst

    def test_dead_series_skipped_for_a_day(self):
        scanner._dead_series["KXHIGHXX"] = time.time()
        assert "KXHIGHXX" not in scanner._target_series_list()


class TestHandleSeedLive:
    @patch("d_worker.scanner.shadow")
    @patch("d_worker.scanner.budget")
    @patch("d_worker.scanner.gateway")
    @patch("d_worker.scanner.kalshi")
    def test_99_cent_never_goes_live(self, mock_kalshi, mock_gateway,
                                      mock_budget, mock_shadow):
        """99¢ 1-lot fails fee inequality — gateway.submit not called."""
        from d_worker.classify import VerdictRow
        from d_worker.budget import ReserveResult

        mock_gateway.is_live_enabled.return_value = True
        mock_budget.reserve.return_value = ReserveResult(True, "granted", 42)
        mock_shadow.record_seed.return_value = 1
        mock_kalshi.fetch_orderbook.return_value = None

        verdict = VerdictRow(
            market_ticker="KXTEST-B99", series_ticker="KXTEST",
            close_ts=time.time()+3600,
            dclass="D1_WX", verdict="SEED", side="yes",
            evidence={"taker_price": 99}, fee_maker=1.0, fee_taker=1.0)

        client = MagicMock()
        governor = MagicMock()
        scanner._handle_seed(client, verdict, {"ticker": "KXTEST-B99"}, governor)

        mock_gateway.submit.assert_not_called()

    @patch("d_worker.scanner.shadow")
    @patch("d_worker.scanner.budget")
    @patch("d_worker.scanner.gateway")
    @patch("d_worker.scanner.kalshi")
    def test_97_cent_goes_live_at_rung_2(self, mock_kalshi, mock_gateway,
                                          mock_budget, mock_shadow):
        """97¢ passes fee inequality → gateway.submit → seed marked live."""
        from d_worker.classify import VerdictRow
        from d_worker.budget import ReserveResult
        from d_worker.gateway import OrderResult

        vid = dstore.insert_verdict(0, "KXTEST-B97", "KXTEST", time.time()+3600,
                                     "D1_WX", "SEED", "yes", {}, 1.0, 1.0)
        seed_id = dstore.insert_seed(vid, "KXTEST-B97", "D1_WX", "yes", "{}",
                                      97, True, 96, "{}", time.time()+3600)

        mock_gateway.is_live_enabled.return_value = True
        mock_budget.reserve.return_value = ReserveResult(True, "granted", 42)
        mock_shadow.record_seed.return_value = seed_id
        mock_kalshi.fetch_orderbook.return_value = None
        mock_gateway.submit.return_value = OrderResult("PLACED", order_id="ord-123")

        verdict = VerdictRow(
            market_ticker="KXTEST-B97", series_ticker="KXTEST",
            close_ts=time.time()+3600,
            dclass="D1_WX", verdict="SEED", side="yes",
            evidence={"taker_price": 97}, fee_maker=1.0, fee_taker=1.0)

        client = MagicMock()
        governor = MagicMock()
        scanner._handle_seed(client, verdict, {"ticker": "KXTEST-B97"}, governor)

        mock_gateway.submit.assert_called_once()
        with dstore._lock:
            row = dstore._conn.execute(
                "SELECT is_live, live_order_id, live_fill_price, live_entry_fee "
                "FROM shadow_seeds WHERE id=?", (seed_id,)).fetchone()
        assert row[0] == 1
        assert row[1] == "ord-123"
        assert row[2] == 97
        assert row[3] == 1

    @patch("d_worker.scanner.shadow")
    @patch("d_worker.scanner.budget")
    @patch("d_worker.scanner.gateway")
    @patch("d_worker.scanner.kalshi")
    def test_no_live_at_rung_0(self, mock_kalshi, mock_gateway,
                                mock_budget, mock_shadow):
        """Rung 0 → gateway.submit not called even for good price."""
        from d_worker.classify import VerdictRow
        from d_worker.budget import ReserveResult

        mock_gateway.is_live_enabled.return_value = False
        mock_budget.reserve.return_value = ReserveResult(True, "granted", 42)
        mock_shadow.record_seed.return_value = 1
        mock_kalshi.fetch_orderbook.return_value = None

        verdict = VerdictRow(
            market_ticker="KXTEST-B97", series_ticker="KXTEST",
            close_ts=time.time()+3600,
            dclass="D1_WX", verdict="SEED", side="yes",
            evidence={"taker_price": 97}, fee_maker=1.0, fee_taker=1.0)

        client = MagicMock()
        governor = MagicMock()
        scanner._handle_seed(client, verdict, {"ticker": "KXTEST-B97"}, governor)

        mock_gateway.submit.assert_not_called()


class TestSlim:
    def test_slim_keeps_classification_fields(self):
        m = {"ticker": "T-1", "event_ticker": "T", "floor_strike": 84.5,
             "close_time": "2026-07-15T21:00:00Z", "junk": "x" * 10000}
        s = scanner._slim(m)
        assert "junk" not in s
        assert s["floor_strike"] == 84.5
        assert s["ticker"] == "T-1"

    def test_slim_handles_missing_fields(self):
        m = {"ticker": "T-1"}
        s = scanner._slim(m)
        assert s == {"ticker": "T-1"}
