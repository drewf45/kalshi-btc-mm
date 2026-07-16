"""tests/test_flip_mode.py — WO-LANE-FLIP one-shot flag + stop counter.

NOTE: imports k_worker.flip_mode, which imports the cryptography-bound client — so this
runs in CI/deploy (where cryptography loads), not in the offline sandbox. Logic is
exercised with the engine's real modules monkeypatched to in-memory fakes."""

import time
import types
import unittest

try:
    from k_worker import flip_mode
    _IMPORTABLE = True
except BaseException:            # pyo3 PanicException (broken crypto binding) isn't Exception
    _IMPORTABLE = False


class _FakeClock:
    """Deterministic clock: sleeps advance time instead of blocking the test."""
    def __init__(self, start):
        self.t = float(start)

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += float(s)


@unittest.skipUnless(_IMPORTABLE, "flip_mode requires the cryptography-bound client (CI only)")
class TestFlipDiscipline(unittest.TestCase):
    def setUp(self):
        # in-memory state store
        self.state = {}
        self.sent, self.alerts, self.traded = [], [], set()
        flip_mode.store.get_state = lambda k: self.state.get(k)
        flip_mode.store.set_state = lambda k, v: self.state.__setitem__(k, v)
        flip_mode.notify.send = lambda t, **k: self.sent.append(t)
        flip_mode.notify.alert = lambda t: self.alerts.append(t)
        flip_mode.discipline.is_halted = lambda: bool(self.state.get("halted"))
        flip_mode.gateway.is_traded = lambda tk: tk in self.traded
        flip_mode.gateway.mark_traded = lambda tk, lane="F": self.traded.add(tk)

    def test_two_stops_pause(self):
        flip_mode._bump_stop_streak(True)                 # stop 1
        self.assertFalse(self.state.get("halted"))
        flip_mode._bump_stop_streak(True)                 # stop 2 -> pause
        self.assertTrue(self.state.get("halted"))
        self.assertTrue(any("FLIP PAUSE" in a for a in self.alerts))

    def test_win_resets_streak(self):
        flip_mode._bump_stop_streak(True)
        flip_mode._bump_stop_streak(False)                # a non-stop resets
        self.assertEqual(self.state.get(flip_mode._STOP_STREAK_KEY), "0")
        flip_mode._bump_stop_streak(True)
        self.assertIsNone(self.state.get("halted"))       # only 1 in the streak

    def test_one_shot_skips_traded_window(self):
        self.traded.add("KXBTC15M-X")
        out = flip_mode.run_flip_cycle(None, "KXBTC15M-X", int(time.time()) + 800, {})
        self.assertIsNone(out)                            # already traded -> no re-entry

    def test_missed_open_sats_and_marks(self):
        # booted deep into a window (well past the entry phase) -> SAT + one-shot mark
        close = int(time.time()) + 200                    # secs=200 < 900-120
        out = flip_mode.run_flip_cycle(None, "KXBTC15M-Y", close, {})
        self.assertEqual(out, "flip_sat_missed_open")
        self.assertIn("KXBTC15M-Y", self.traded)


@unittest.skipUnless(_IMPORTABLE, "flip_mode requires the cryptography-bound client (CI only)")
class TestFlipCycle(unittest.TestCase):
    """Full-window order path with the proven organs monkeypatched to in-memory fakes.
    Fills are injected by order_id via a patched _order_fill_price (skips get_fills/parse)."""

    CLOSE = 2_000_000_000        # fixed epoch; _tag formats it, nothing depends on 'now'

    def setUp(self):
        self.state, self.sent, self.alerts, self.traded = {}, [], [], set()
        self.orders, self.rows, self.cancels = [], [], []
        self.fills_map = {}                       # oid -> fill cents (what filled)
        self.book = types.SimpleNamespace(
            yes_bid=45, no_bid=48, yes_bid_fp="0.45", no_bid_fp="0.48")

        flip_mode.store.get_state = lambda k: self.state.get(k)
        flip_mode.store.set_state = lambda k, v: self.state.__setitem__(k, v)
        flip_mode.store.insert_row = lambda r: self.rows.append(r)
        flip_mode.store.SurfaceRow = lambda **kw: kw
        flip_mode.notify.send = lambda t, **k: self.sent.append(t)
        flip_mode.notify.alert = lambda t: self.alerts.append(t)
        flip_mode.discipline.is_halted = lambda: bool(self.state.get("halted"))
        flip_mode.gateway.is_traded = lambda tk: tk in self.traded
        flip_mode.gateway.mark_traded = lambda tk, lane="F": self.traded.add(tk)
        flip_mode.engine.heartbeat = lambda: None
        flip_mode.engine._observe_mode = False

        flip_mode.kalshi.fetch_orderbook = lambda c, tk: self.book
        flip_mode.kalshi.cancel_all_for_market = lambda c, tk: self.cancels.append(tk)
        flip_mode.kalshi.position_for_market = lambda c, tk: 0
        flip_mode.kalshi.get_positions = lambda c: []

        def _place(c, tk, side, price, count=1, expiration_ts=None, v2_price_str=None):
            oid = f"{side}-{price}"
            self.orders.append(dict(side=side, price=price, count=count,
                                    expiration_ts=expiration_ts, v2=v2_price_str, oid=oid))
            return oid, {}
        flip_mode.kalshi.place_order_maker = _place
        flip_mode._order_fill_price = lambda c, tk, oid, our_side: self.fills_map.get(oid)

        self._clock = _FakeClock(self.CLOSE - 800)        # secs=800: inside the arm window
        flip_mode.time = self._clock

    def tearDown(self):
        flip_mode.time = time                             # restore the real module

    def _exits(self):
        # the first two place_order_maker calls are the entry bids; the rest are exits/rejoins
        return self.orders[2:]

    def test_double_fill_nets_no_exits(self):
        # both legs fill -> the bundle nets flat, exchange banks the floor; no exits/sweep
        self.fills_map = {"yes-45": 45, "no-48": 48}
        out = flip_mode.run_flip_cycle(None, "KXBTC15M-D", self.CLOSE, {})
        self.assertEqual(out, "flip_done_netted")
        self.assertEqual(self._exits(), [])                       # zero exit orders posted
        self.assertTrue(any("netted +7" in s for s in self.sent))  # 100-(45+48)=7
        self.assertTrue(any(r.get("why_tag") == "FLIP_NETTED" for r in self.rows))
        self.assertEqual(self.state.get(flip_mode._STOP_STREAK_KEY), "0")  # streak reset

    def test_lone_fill_posts_one_exit_at_complement(self):
        # only YES fills @45 -> exactly one exit: buy NO @ (100-(45+X)) = 100-49 = 51
        self.fills_map = {"yes-45": 45}
        flip_mode.run_flip_cycle(None, "KXBTC15M-L", self.CLOSE, {})
        exit_orders = [o for o in self.orders if o["side"] == "no" and o["price"] == 51]
        self.assertEqual(len(exit_orders), 1)
        # and it is a flip of entry(45)+X(4) at the complement
        self.assertEqual(51, 100 - (45 + flip_mode.FLIP_X))

    def test_no_fill_cancels_and_sats(self):
        self.fills_map = {}                               # nothing fills
        out = flip_mode.run_flip_cycle(None, "KXBTC15M-N", self.CLOSE, {})
        self.assertEqual(out, "flip_sat_nofill")
        self.assertIn("KXBTC15M-N", self.cancels)         # unfilled entries canceled
        self.assertEqual(self._exits(), [])               # nothing flipped

    def test_rejoin_expiry_is_close_minus_2(self):
        # lone leg, exit never fills -> T-90 sweep re-posts with expiry = close-2 (F-3)
        self.fills_map = {"yes-45": 45}
        flip_mode.run_flip_cycle(None, "KXBTC15M-R", self.CLOSE, {})
        rejoins = [o for o in self._exits() if o["v2"] is not None]  # sweep rejoin carries fp
        self.assertTrue(rejoins)
        for o in rejoins:
            self.assertEqual(o["expiration_ts"], int(self.CLOSE - 2))


if __name__ == "__main__":
    unittest.main()
