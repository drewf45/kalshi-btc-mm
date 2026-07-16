# tests/test_discovery.py — WAKE AT THE BELL.
# §1: clock truth beats status truth — a just-closed market Kalshi still flags active is
# a corpse and must never be chosen. §2: idle naps target the next bell, not a lazy poll.
import time
import unittest

from f_worker.config import Config
from f_worker.lib.marketutil import pick_active_market
from f_worker.desk import Desk


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


if __name__ == "__main__":
    unittest.main()
