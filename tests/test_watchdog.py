"""Watchdog tests: evidence re-verification, settle-lag alerts, watch rows."""

import os
import json
import pytest
import time
from unittest.mock import patch, MagicMock
from d_worker import dstore, watchdog
from d_worker.feeds import Observation


@pytest.fixture(autouse=True)
def _setup_db(tmp_path):
    db_path = str(tmp_path / "test_kal_d.db")
    os.environ["DW_DB_PATH"] = db_path
    dstore.DB_PATH = db_path
    dstore.init_db()
    yield
    dstore._conn.close()
    dstore._conn = None


def _insert_open_seed(ticker="KXTEST-26JUL15-B97", dclass="D1_WX", side="yes",
                      close_ts=None, evidence=None):
    past = time.time() - 300
    if close_ts is None:
        close_ts = time.time() + 3600
    ev = evidence or {"strike": 97, "direction": "above", "rounding": 0.5}
    vid = dstore.insert_verdict(
        0, ticker, "KXTEST", close_ts,
        dclass, "SEED", side, ev, 1.0, 1.0)
    sid = dstore.insert_seed(
        vid, ticker, dclass, side, "{}",
        97, True, 96, "{}", close_ts)
    return sid, vid


class TestWatchCycle:
    @patch("d_worker.watchdog.notify")
    @patch("d_worker.watchdog.feeds")
    @patch("d_worker.watchdog.kalshi")
    def test_held_when_obs_above_threshold(self, mock_kalshi, mock_feeds, mock_notify):
        sid, vid = _insert_open_seed()
        dstore.draft_registry("KXTEST", "NWS", "KNYC", "America/New_York",
                              "F", "0.5")
        dstore.approve_registry("KXTEST", "test")

        obs = Observation(value=98.0, ts=time.time(), source="NWS", raw={})
        mock_feeds.observe_nws.return_value = obs
        mock_feeds.staleness.return_value = 10

        client = MagicMock()
        stats = watchdog.watch_cycle(client)
        assert stats["checked"] == 1
        assert stats["held"] == 1
        assert stats["broken"] == 0

        ws = dstore.watch_stats_since(time.time() - 60)
        assert ws["held"] == 1

    @patch("d_worker.watchdog.notify")
    @patch("d_worker.watchdog.feeds")
    @patch("d_worker.watchdog.kalshi")
    def test_broken_when_obs_below_threshold(self, mock_kalshi, mock_feeds, mock_notify):
        sid, vid = _insert_open_seed()
        dstore.draft_registry("KXTEST", "NWS", "KNYC", "America/New_York",
                              "F", "0.5")
        dstore.approve_registry("KXTEST", "test")

        obs = Observation(value=95.0, ts=time.time(), source="NWS", raw={})
        mock_feeds.observe_nws.return_value = obs
        mock_feeds.staleness.return_value = 10

        mock_kalshi.fetch_orderbook.return_value = MagicMock(yes_bid=45, no_bid=55)

        client = MagicMock()
        stats = watchdog.watch_cycle(client)
        assert stats["broken"] == 1
        assert mock_notify.send.call_count == 1
        assert "EVIDENCE BROKEN" in mock_notify.send.call_args[0][0]


class TestSettleLag:
    @patch("d_worker.watchdog.notify")
    @patch("d_worker.watchdog.feeds")
    @patch("d_worker.watchdog.kalshi")
    def test_settle_lag_alert(self, mock_kalshi, mock_feeds, mock_notify):
        past_close = time.time() - 1200
        sid, vid = _insert_open_seed(close_ts=past_close)
        dstore.draft_registry("KXTEST", "NWS", "KNYC", "America/New_York",
                              "F", "0.5")
        dstore.approve_registry("KXTEST", "test")

        obs = Observation(value=98.0, ts=time.time(), source="NWS", raw={})
        mock_feeds.observe_nws.return_value = obs
        mock_feeds.staleness.return_value = 10

        client = MagicMock()
        stats = watchdog.watch_cycle(client)
        assert stats["settle_lag"] == 1
        assert any("SETTLE LAG" in str(c) for c in mock_notify.send.call_args_list)


class TestWatchRows:
    def test_insert_and_query(self):
        row_id = dstore.insert_watch_row(1, "EVIDENCE_HELD", '{"test": true}')
        assert row_id > 0
        stats = dstore.watch_stats_since(time.time() - 60)
        assert stats["total"] == 1
        assert stats["held"] == 1

    @patch("d_worker.watchdog.notify")
    @patch("d_worker.watchdog.feeds")
    @patch("d_worker.watchdog.kalshi")
    def test_governor_consumed_on_broken(self, mock_kalshi, mock_feeds, mock_notify):
        sid, vid = _insert_open_seed()
        dstore.draft_registry("KXTEST", "NWS", "KNYC", "America/New_York",
                              "F", "0.5")
        dstore.approve_registry("KXTEST", "test")

        obs = Observation(value=95.0, ts=time.time(), source="NWS", raw={})
        mock_feeds.observe_nws.return_value = obs
        mock_feeds.staleness.return_value = 10
        mock_kalshi.fetch_orderbook.return_value = MagicMock(yes_bid=45)

        client = MagicMock()
        gov = MagicMock()
        watchdog.watch_cycle(client, governor=gov)
        gov.consume.assert_called_once_with(1)
