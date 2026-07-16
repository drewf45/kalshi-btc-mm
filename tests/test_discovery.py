# tests/test_discovery.py — WAKE AT THE BELL.
# §1: clock truth beats status truth — a just-closed market Kalshi still flags active is
# a corpse and must never be chosen. §2: idle naps target the next bell, not a lazy poll.
import time
import unittest
from datetime import datetime, timezone

from f_worker.config import Config
from f_worker.ledger import Ledger
from f_worker.fgateway import FGateway
from f_worker.manager import Manager
from f_worker.pricebrain import PriceBrain
from f_worker.lib import marketutil as mu
from f_worker.lib.marketutil import pick_active_market
from f_worker.desk import Desk
from tests.fakes import FakeClient, FakeNotifier

UTC = timezone.utc


class TestTickerCentury(unittest.TestCase):
    # BUG 1: the date block is YYMONDD then HHMM (close, UTC) — NOT DDMONYY. The old code
    # dated inferred markets a decade in the past, so the clock filter ate them as corpses.
    def test_infer_close_ts_real_ticker(self):
        # KXBTC15M-26JUL150030-30 closed 2026-07-15T00:30:00Z (from the live fill tape)
        expected = int(datetime(2026, 7, 15, 0, 30, tzinfo=UTC).timestamp())
        self.assertEqual(mu.infer_close_ts_from_ticker("KXBTC15M-26JUL150030-30", 15), expected)

    def test_infer_close_ts_today(self):
        expected = int(datetime(2026, 7, 16, 14, 30, tzinfo=UTC).timestamp())
        self.assertEqual(mu.infer_close_ts_from_ticker("KXBTC15M-26JUL161430-30", 15), expected)

    def test_inferred_market_is_not_a_corpse(self):
        # a freshly-opened window whose close_ts must be INFERRED must survive the clock
        # filter (the old century bug made it look ten years dead)
        now = time.time()
        future = datetime.fromtimestamp(now + 600, tz=UTC)
        tk = f"KXBTC15M-{future.strftime('%y%b%d%H%M').upper()}-30"
        m = {"ticker": tk, "event_ticker": "EV", "status": "active"}  # NO close_time field
        ev, mt, obj = pick_active_market([m])
        self.assertEqual(mt, tk)


def _mkt(ticker, open_off, close_off, status="active", now=None):
    now = now or time.time()
    return {"ticker": ticker, "event_ticker": "EV", "status": status,
            "open_time": int(now + open_off), "close_time": int(now + close_off)}


class TestCorpseFilter(unittest.TestCase):
    def test_corpse_is_never_chosen(self):
        now = time.time()
        corpse = _mkt("KXBTC15M-CORPSE", -900, -30, status="active", now=now)  # closed 30s ago
        fresh = _mkt("KXBTC15M-FRESH", 0, 900, status="active", now=now)       # just opened
        ev, mt, obj = pick_active_market([corpse, fresh])
        self.assertEqual(mt, "KXBTC15M-FRESH")

    def test_only_corpses_raises(self):
        now = time.time()
        c1 = _mkt("KXBTC15M-C1", -1800, -900, now=now)
        c2 = _mkt("KXBTC15M-C2", -900, -30, now=now)
        with self.assertRaises(RuntimeError):
            pick_active_market([c1, c2])

    def test_active_preferred_over_future_among_live(self):
        now = time.time()
        active = _mkt("KXBTC15M-ACTIVE", -100, 800, now=now)   # open now, closes sooner
        future = _mkt("KXBTC15M-FUTURE", 800, 1700, now=now)   # opens later
        ev, mt, obj = pick_active_market([future, active])
        self.assertEqual(mt, "KXBTC15M-ACTIVE")


class TestWakeAtBell(unittest.TestCase):
    def _desk(self):
        return Desk(None, None, None, None, None, Config(), None)

    def test_nap_targets_the_bell(self):
        d = self._desk()
        C = 1_000_000.0
        d._last_close_ts = C
        self.assertEqual(d._idle_nap(now=C - 100), 30.0)     # far out: capped at 30s
        self.assertAlmostEqual(d._idle_nap(now=C - 10), 10.5)  # closing in: shrinking nap
        self.assertEqual(d._idle_nap(now=C + 0.4), 0.25)     # past the bell: floor, re-discover
        self.assertEqual(d._idle_nap(now=C + 50), 0.25)      # well past: still floored

    def test_nap_without_close_is_one_second(self):
        d = self._desk()
        self.assertEqual(d._idle_nap(now=123.0), 1.0)


class TestSilentDeskIsLoud(unittest.TestCase):
    # BUG 2: discovery-idle must never be silent — it pages (throttled to 120s).
    def _desk(self, client):
        cfg = Config()
        led = Ledger(":memory:")
        notif = FakeNotifier()
        gw = FGateway(client, led, cfg, notif)
        mgr = Manager(gw, led, PriceBrain(cfg, gate_path=""), cfg, notif)
        desk = Desk(client, gw, mgr, PriceBrain(cfg, gate_path=""), led, cfg, notif)
        return desk, led, notif

    def test_empty_discovery_pages_and_throttles(self):
        client = FakeClient()             # discover_market returns None by default
        desk, led, notif = self._desk(client)
        self.assertIsNone(desk.loop_once())
        self.assertIsNone(desk.loop_once())   # second call within 120s
        pages = [s for s in notif.sent if "discovery_empty" in s]
        self.assertEqual(len(pages), 1)       # loud, but throttled to once per 120s
        n = led._conn.execute(
            "SELECT COUNT(*) c FROM window_events WHERE to_state='STAGE_ERR'").fetchone()["c"]
        self.assertEqual(n, 1)

    def test_entry_fill_announces_immediately(self):
        client = FakeClient()
        desk, led, notif = self._desk(client)
        win = _mkt_win(led)
        desk._announce_fill(win, "yes", 47)   # flip_x default 4 -> flip @51
        self.assertTrue(any("🌱" in s and "filled YES@47" in s and "@51" in s
                            for s in notif.sent))


def _mkt_win(led):
    from f_worker import window as W
    return W.Window(window_id="KXBTC15M-26JUL161430-30", market_ticker="KXBTC15M-26JUL161430-30",
                    event_ticker="E", open_ts=None, close_ts=1_784_212_200, rung=1, lots=1).bind_ledger(led)


if __name__ == "__main__":
    unittest.main()
