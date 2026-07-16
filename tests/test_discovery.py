# tests/test_discovery.py — WAKE AT THE BELL.
# §1: clock truth beats status truth — a just-closed market Kalshi still flags active is
# a corpse and must never be chosen. §2: idle naps target the next bell, not a lazy poll.
import time
import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from f_worker.config import Config
from f_worker.ledger import Ledger
from f_worker.fgateway import FGateway
from f_worker.manager import Manager
from f_worker.pricebrain import PriceBrain
from f_worker.lib import marketutil as mu
from f_worker.lib.marketutil import pick_active_market
from f_worker.desk import Desk, TokenBucket
from tests.fakes import FakeClient, FakeNotifier

UTC = timezone.utc
NY = ZoneInfo("America/New_York")


class TestTickerCentury(unittest.TestCase):
    # YYMONDD then HHMM, HHMM = close in EASTERN (the 00:30 ticker settled at 04:30Z).
    def test_infer_close_ts_real_ticker(self):
        # KXBTC15M-26JUL150030-30 closed 00:30 ET (= 04:30Z) on 2026-07-15
        expected = int(datetime(2026, 7, 15, 0, 30, tzinfo=NY).timestamp())
        self.assertEqual(mu.infer_close_ts_from_ticker("KXBTC15M-26JUL150030-30", 15), expected)

    def test_infer_close_ts_today(self):
        expected = int(datetime(2026, 7, 16, 14, 30, tzinfo=NY).timestamp())
        self.assertEqual(mu.infer_close_ts_from_ticker("KXBTC15M-26JUL161430-30", 15), expected)

    def test_inferred_market_is_not_a_corpse(self):
        # a freshly-opened window whose close_ts must be INFERRED survives the clock filter
        now = time.time()
        tk, close = mu.current_window_ticker("KXBTC15M", now)
        m = {"ticker": tk, "event_ticker": "EV", "status": "active"}  # NO close_time field
        ev, mt, obj = pick_active_market([m])
        self.assertEqual(mt, tk)


class TestTickerArithmetic(unittest.TestCase):
    # THE COMPUTED BELL: construct the ticker from the ET clock instead of asking the list.
    def test_construction_matches_tape(self):
        # 00:20 ET -> in-progress window closes 00:30 ET -> the tape ticker exactly
        now = datetime(2026, 7, 15, 0, 20, tzinfo=NY).timestamp()
        tk, close = mu.current_window_ticker("KXBTC15M", now)
        self.assertEqual(tk, "KXBTC15M-26JUL150030-30")
        self.assertEqual(close, int(datetime(2026, 7, 15, 0, 30, tzinfo=NY).timestamp()))

    def test_next_is_one_quarter_later(self):
        now = datetime(2026, 7, 16, 15, 7, tzinfo=NY).timestamp()
        cur_tk, cur_close = mu.current_window_ticker("KXBTC15M", now)
        nxt_tk, nxt_close = mu.next_window_ticker("KXBTC15M", now)
        self.assertEqual(cur_tk, "KXBTC15M-26JUL161515-15")
        self.assertEqual(nxt_tk, "KXBTC15M-26JUL161530-30")
        self.assertEqual(nxt_close - cur_close, 900)

    def test_roundtrip_infer(self):
        # constructing then inferring a ticker returns the same close_ts (ET consistent)
        now = datetime(2026, 7, 16, 9, 3, tzinfo=NY).timestamp()
        tk, close = mu.next_window_ticker("KXBTC15M", now)
        self.assertEqual(mu.infer_close_ts_from_ticker(tk, 15), close)


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

    def test_unopened_newborn_is_pickable(self):
        # F3.1: at a bell the just-closed corpse is still "open" and the newborn is
        # "unopened" — the clock, not the status field, must arbitrate. `markets=1`
        # becomes `markets=2` and the newborn wins.
        now = time.time()
        corpse = _mkt("KXBTC15M-CORPSE", -900, -30, status="open", now=now)
        newborn = _mkt("KXBTC15M-NEWBORN", 0, 900, status="unopened", now=now)
        ev, mt, obj = pick_active_market([corpse, newborn])
        self.assertEqual(mt, "KXBTC15M-NEWBORN")


class TestWakeAtBell(unittest.TestCase):
    def _desk(self):
        return Desk(None, None, None, None, Ledger(":memory:"), Config(), None)

    def test_nap_races_toward_the_bell(self):
        d = self._desk()
        C = 1_000_000.0
        d._last_close_ts = C
        self.assertEqual(d._idle_nap(now=C - 100), 30.0)      # far out: capped at 30s
        self.assertAlmostEqual(d._idle_nap(now=C - 10), 10.5)   # closing in: shrinking nap
        self.assertAlmostEqual(d._idle_nap(now=C - 0.4), 0.9)   # ~half-second polls into the bell

    def test_post_bell_backs_off_not_hammers(self):
        # F3.2: past the bell + empty = STARVATION -> back off, never 4 Hz
        d = self._desk()
        C = 1000.0
        d._last_close_ts = C
        naps = [d._idle_nap(now=C + 1) for _ in range(6)]
        self.assertEqual(naps, [0.5, 1.0, 2.0, 4.0, 5.0, 5.0])   # cap 5s

    def test_at_most_8_calls_in_first_10s(self):
        d = self._desk()
        C = 1000.0
        d._last_close_ts = C
        t, calls = 0.0, 0
        while t < 10.0:
            calls += 1
            t += d._idle_nap(now=C + 1 + t)
        self.assertLessEqual(calls, 8)

    def test_no_known_bell_is_starvation(self):
        d = self._desk()
        self.assertEqual(d._idle_nap(now=123.0), 0.5)   # first backoff step


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

    def test_stuck_direct_discovery_pages_and_throttles(self):
        # every ticker 404s (markets={}) while a window is in progress -> ticker suspect +
        # loud list fallback, NEVER quietly bored; throttled so it doesn't spam.
        client = FakeClient(markets={})
        desk, led, notif = self._desk(client)
        now = (int(time.time()) // 900) * 900 + 300   # ~5 min into a window (mid-window)
        self.assertIsNone(desk._discover_direct(now))
        self.assertIsNone(desk._discover_direct(now))   # within throttle
        self.assertTrue(any("ticker_suspect" in s or "discovery_empty" in s for s in notif.sent))
        self.assertLessEqual(len([s for s in notif.sent if "ticker_suspect" in s]), 1)

    def test_knocking_before_birth_is_quiet(self):
        # in the first seconds of a window the NEXT window legitimately 404s — knock, no page
        client = FakeClient(markets={})
        desk, led, notif = self._desk(client)
        # place `now` at a window open (0s in) so current exists-check is inside grace and
        # only the next-window knock happens
        now = (int(time.time()) // 900) * 900 + 900   # exactly a boundary (new window opens)
        # current window (just opened) 404s but we're within the 30s grace -> no suspect page
        desk._discover_direct(now + 1)
        self.assertEqual([s for s in notif.sent if "ticker_suspect" in s], [])

    def test_born_market_is_handled_with_latency(self):
        # the NEXT ticker returns 200 -> born: discovered, birth latency recorded + printed
        cfg = Config()
        led = Ledger(":memory:")
        notif = FakeNotifier()
        now = (int(time.time()) // 900) * 900 + 300   # mid current window
        cur_tk, _ = mu.current_window_ticker(cfg.series_ticker, now)
        nxt_tk, nxt_close = mu.next_window_ticker(cfg.series_ticker, now)
        client = FakeClient(markets={nxt_tk: {"ticker": nxt_tk, "event_ticker": "EV"}})
        gw = FGateway(client, led, cfg, notif)
        mgr = Manager(gw, led, PriceBrain(cfg, gate_path=""), cfg, notif)
        desk = Desk(client, gw, mgr, PriceBrain(cfg, gate_path=""), led, cfg, notif)
        desk._last_window_id = cur_tk        # current already handled -> knock the next
        disc = desk._discover_direct(now)
        self.assertIsNotNone(disc)
        self.assertEqual(disc[1], nxt_tk)
        rows = led._conn.execute(
            "SELECT evidence FROM window_events WHERE to_state='BORN'").fetchall()
        self.assertTrue(rows)
        self.assertIn("listing_latency_s", rows[0]["evidence"])

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


class TestDiscoveryBudget(unittest.TestCase):
    # F3.3: the discovery spin lives under a token budget — bursts ok, storms capped.
    def test_bursts_then_throttles(self):
        tb = TokenBucket(capacity=3, refill_per_min=60, now=0.0)   # 1 token/sec
        self.assertTrue(tb.allow(now=0.0))
        self.assertTrue(tb.allow(now=0.0))
        self.assertTrue(tb.allow(now=0.0))
        self.assertFalse(tb.allow(now=0.0))          # bucket drained
        self.assertTrue(tb.allow(now=1.0))           # +1 token after a second
        self.assertFalse(tb.allow(now=1.0))


class TestRatedAndAggregated(unittest.TestCase):
    def test_throttled_alert_carries_its_rate(self):
        # F3.4: a throttled ⚠ says how many it swallowed — storms can't hide in politeness
        from f_worker.config import Config
        from f_worker.ledger import Ledger
        notif = FakeNotifier()
        d = Desk(FakeClient(), None, None, None, Ledger(":memory:"), Config(), notif)
        d._stage_err(None, "discovery_empty", Exception("x"), throttle=100.0, now=0.0)   # fires
        d._stage_err(None, "discovery_empty", Exception("x"), throttle=100.0, now=1.0)   # swallowed
        d._stage_err(None, "discovery_empty", Exception("x"), throttle=100.0, now=2.0)   # swallowed
        d._stage_err(None, "discovery_empty", Exception("x"), throttle=100.0, now=200.0) # fires w/ rate
        self.assertTrue(any("×3 in 200s" in s for s in notif.sent))

    def test_throttled_print_collapses_repeats(self):
        import io
        import contextlib
        mu._print_state.clear()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            for _ in range(5):
                mu.throttled_print("[discover] same line", min_interval=1000.0)
        self.assertEqual(buf.getvalue().count("[discover] same line"), 1)   # 4 collapsed


if __name__ == "__main__":
    unittest.main()
