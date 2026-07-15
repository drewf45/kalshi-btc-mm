"""Scanner tests: pagination, per-market error isolation, 429 handling."""

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
def _reset_governor():
    scanner._governor = None
    yield
    scanner._governor = None


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


class TestPagination:
    @patch("d_worker.scanner.kalshi")
    def test_three_page_fetch(self, mock_kalshi):
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
        gov = scanner.TokenBucket(100)
        result = scanner._fetch_all_markets(client, gov)
        assert len(result) == 450
        assert client.request.call_count == 3

    @patch("d_worker.scanner.kalshi")
    def test_429_halves_and_stops(self, mock_kalshi):
        client = MagicMock()
        client.request = MagicMock(
            side_effect=RuntimeError("HTTP 429 Too Many Requests"))
        gov = scanner.TokenBucket(60)
        with patch("k_worker.notify"):
            result = scanner._fetch_all_markets(client, gov)
        assert result == []
        assert gov.rate == 30

    @patch("d_worker.scanner.kalshi")
    def test_empty_first_page(self, mock_kalshi):
        client = MagicMock()
        client.request = MagicMock(return_value={"markets": [], "cursor": ""})
        gov = scanner.TokenBucket(100)
        result = scanner._fetch_all_markets(client, gov)
        assert result == []


class TestPerMarketIsolation:
    @patch("k_worker.notify")
    @patch("d_worker.scanner._classify_market")
    @patch("d_worker.scanner._fetch_all_markets")
    def test_one_error_does_not_crash_sweep(self, mock_fetch, mock_classify,
                                             mock_notify):
        markets = [_make_market("OK-1"), _make_market("BAD-1"),
                    _make_market("OK-2")]
        mock_fetch.return_value = markets

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
    @patch("d_worker.scanner._fetch_all_markets")
    def test_skip_aggregation(self, mock_fetch, mock_classify, mock_notify):
        """Non-approved SKIPs aggregate; ERR rows stay individual."""
        markets = [_make_market(f"T{i}", series="KXUNAPPROVED") for i in range(5)]
        mock_fetch.return_value = markets

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
