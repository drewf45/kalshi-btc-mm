"""tests/test_flip_vision.py — WO-VISION: fill-clock scratch, ratchet, OFI gate, R6, tap-off.

Crypto-free classes (R6 pack math, treasury tap-off) run in the sandbox. The signal units
and the full ratchet cycle import the client-bound flip_mode, so they run in CI / under a
stubbed client."""

import types
import unittest

from k_worker import flip_pack, treasury

try:
    from k_worker import flip_mode
    _IMPORTABLE = True
except BaseException:
    _IMPORTABLE = False


class _Restore(unittest.TestCase):
    def setUp(self):
        self._saved = []

    def P(self, obj, attr, val):
        self._saved.append((obj, attr, getattr(obj, attr)))
        setattr(obj, attr, val)

    def tearDown(self):
        for obj, attr, orig in reversed(self._saved):
            setattr(obj, attr, orig)


class _Clock:
    def __init__(self, t):
        self.t = float(t)

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += float(s)


# ── §5 R6 pack (crypto-free) ─────────────────────────────────────────
class TestR6Pack(unittest.TestCase):
    def _trips(self, *outs):
        return [{"outcome": o, "realized_cents": c} for o, c in outs]

    def test_trip_stats_basic(self):
        s = flip_pack.trip_stats(self._trips(("take", 4), ("take", 4), ("scratched", -3)))
        self.assertEqual(s["n"], 3)
        self.assertAlmostEqual(s["wr"], 2 / 3, places=3)
        self.assertAlmostEqual(s["avg_take"], 4.0, places=3)
        self.assertAlmostEqual(s["avg_scratch"], -3.0, places=3)
        self.assertAlmostEqual(s["mean"], (4 + 4 - 3) / 3, places=3)

    def test_none_when_empty(self):
        self.assertIsNone(flip_pack.trip_stats([]))

    def test_r6_line_and_scalable_gate(self):
        line = flip_pack.format_r6(flip_pack.trip_stats(self._trips(("take", 4))))
        self.assertIn("R6:", line)
        self.assertIn("trips 1", line)
        self.assertIn("Wilson-LB", line)
        # not scalable below N=40 even if positive
        self.assertNotIn("SCALABLE", line)

    def test_scalable_requires_n_and_positive_lb(self):
        trips = self._trips(*[("take", 4)] * 45)      # 45 wins, tight positive LB
        s = flip_pack.trip_stats(trips)
        self.assertTrue(s["scalable"])
        self.assertIn("SCALABLE", flip_pack.format_r6(s))

    def test_pack_includes_r6_line(self):
        w = dict(tag="20:00", outcome_tag="RATCHET", bundle_cost=None,
                 realized_cents=4, broker_flat=1)
        text, _u, _f = flip_pack.build_pack(
            account_usd=10.0, midnight_usd=10.0, hour_windows=[w], day_windows=[w],
            settled_pnl_usd=0.0, fees_usd=0.0,
            day_trips=self._trips(("take", 4), ("scratched", -3)))
        self.assertIn("R6: trips 2", text)


# ── §4 treasury tap-off (crypto-free; store patched in-memory) ───────
class TestTreasuryTapOff(_Restore):
    def setUp(self):
        super().setUp()
        self.state, self.sent = {}, []
        self.P(treasury.store, "get_state", lambda k: self.state.get(k))
        self.P(treasury.store, "set_state", lambda k, v: self.state.__setitem__(k, v))
        self.P(treasury.notify, "send", lambda t, **k: self.sent.append(t))

    def test_waterfall_all_to_book_when_off(self):
        self.P(treasury, "TREASURY_ACCRUE", 0)
        self.state["treasury_engine_book"] = "10.00"
        out = treasury.waterfall(0.50)                # a +$0.50 win
        self.assertEqual(out["tax"], 0.0)
        self.assertEqual(out["fee"], 0.0)
        t = treasury.get_totals()
        self.assertAlmostEqual(t["engine_book"], 10.50, places=4)
        self.assertEqual(treasury.accrued_total(), 0.0)    # owed stays $0.00

    def test_enforce_folds_rebuilt_owed(self):
        self.P(treasury, "TREASURY_ACCRUE", 0)
        self.state["treasury_engine_book"] = "7.50"
        self.state["treasury_accrued_tax"] = "1.60"
        self.state["treasury_accrued_fee"] = "0.50"        # owed $2.10 rebuilt overnight
        self.assertTrue(treasury.enforce_accrual_off())
        t = treasury.get_totals()
        self.assertEqual(t["accrued_tax"], 0.0)
        self.assertEqual(t["accrued_fee"], 0.0)
        self.assertAlmostEqual(t["engine_book"], 9.60, places=4)   # 7.50 + 2.10 folded back
        self.assertTrue(any("accrual OFF" in s for s in self.sent))

    def test_enforce_noop_when_flat(self):
        self.P(treasury, "TREASURY_ACCRUE", 0)
        self.assertFalse(treasury.enforce_accrual_off())   # nothing owed → no-op


# ── §1/§2 signal units (client-bound → CI / stub) ───────────────────
@unittest.skipUnless(_IMPORTABLE, "flip_mode requires the cryptography-bound client")
class TestSignals(_Restore):
    def test_tick_direction(self):
        self.P(flip_mode, "FLIP_OFI_TICKS", 4)
        self.assertEqual(flip_mode._tick_direction([1, 2, 3, 4, 5], 4), "yes")
        self.assertEqual(flip_mode._tick_direction([5, 4, 3, 2, 1], 4), "no")
        self.assertIsNone(flip_mode._tick_direction([1, 2, 1, 2, 3], 4))   # mixed
        self.assertIsNone(flip_mode._tick_direction([1, 2, 3], 4))         # insufficient

    def test_ofi_gate_fails_closed(self):
        self.P(flip_mode, "FLIP_OFI_TICKS", 4)
        rising = [1, 2, 3, 4, 5]
        self.assertEqual(flip_mode._ofi_side(rising, "yes"), "yes")   # ticks up + book yes → yes
        self.assertIsNone(flip_mode._ofi_side(rising, "no"))          # disagreement → wait
        self.assertIsNone(flip_mode._ofi_side(rising, None))          # no lean → wait
        self.assertIsNone(flip_mode._ofi_side([1, 2, 1, 2, 3], "yes"))  # no signal → wait

    def test_book_lean(self):
        self.assertEqual(flip_mode._book_lean(
            types.SimpleNamespace(yes_bid_qty=10, no_bid_qty=3)), "yes")
        self.assertEqual(flip_mode._book_lean(
            types.SimpleNamespace(yes_bid_qty=2, no_bid_qty=9)), "no")
        self.assertIsNone(flip_mode._book_lean(types.SimpleNamespace(yes_bid_qty=None, no_bid_qty=None)))

    def test_spot_adverse_single_strike(self):
        self.assertTrue(flip_mode._spot_adverse("yes", 99.0, 100.0, None))   # YES, spot below strike
        self.assertFalse(flip_mode._spot_adverse("yes", 101.0, 100.0, None))
        self.assertTrue(flip_mode._spot_adverse("no", 101.0, 100.0, None))   # NO, spot above strike
        self.assertFalse(flip_mode._spot_adverse("no", 101.0, 100.0, 105.0))  # two-sided → conservative

    def test_scratch_reasons(self):
        self.P(flip_mode, "FLIP_SCRATCH_S", 3)
        self.P(flip_mode, "FLIP_MARKOUT_STOP", 2)
        self.assertIn("mark", flip_mode._scratch_reason("yes", 49, 46, None, None, 0))   # (a)
        self.assertIn("strike", flip_mode._scratch_reason("yes", 49, 49, None, None, 2))  # (b)
        self.assertIn("markout", flip_mode._scratch_reason("yes", 49, 49, -3, -1, 0))     # (c) worsening
        self.assertIsNone(flip_mode._scratch_reason("yes", 49, 49, -3, -3, 0))            # (c) not worsening
        self.assertIsNone(flip_mode._scratch_reason("yes", 49, 49, None, None, 0))        # nothing


def _bk(yb, nb, yq=5, nq=5):
    return types.SimpleNamespace(
        yes_bid=yb, no_bid=nb, yes_bid_qty=yq, no_bid_qty=nq,
        yes_ask=(yb + 6) if yb else None, no_ask=(nb + 4) if nb else None,
        yes_bid_fp=(f"0.{yb:02d}" if yb else None), no_bid_fp=(f"0.{nb:02d}" if nb else None))


@unittest.skipUnless(_IMPORTABLE, "flip_mode requires the cryptography-bound client")
class TestRatchetCycle(_Restore):
    CLOSE = 2_000_000_000

    def setUp(self):
        super().setUp()
        self.state, self.sent, self.alerts, self.traded = {}, [], [], set()
        self.orders, self.canceled, self.trips, self.windows = [], [], [], []
        self.fills_map, self.net = {}, 0
        self.book_fn = lambda i: _bk(45, 55)      # default: only YES posts
        self._bc = 0

        self.P(flip_mode, "FLIP_RATCHET", 1)
        self.P(flip_mode, "FLIP_MAX_TRIPS", 1)    # single trip unless a test widens it
        self.P(flip_mode.store, "get_state", lambda k: self.state.get(k))
        self.P(flip_mode.store, "set_state", lambda k, v: self.state.__setitem__(k, v))
        self.P(flip_mode.store, "insert_row", lambda r: None)
        self.P(flip_mode.store, "SurfaceRow", lambda **k: k)
        self.P(flip_mode.store, "insert_markout", lambda *a, **k: None)
        self.P(flip_mode.store, "insert_trip",
               lambda ts, tk, tag, side, entry, outcome, realized:
               self.trips.append(dict(side=side, entry=entry, outcome=outcome, realized_cents=realized)))
        self.P(flip_mode.store, "insert_flip_window", lambda **k: self.windows.append(k))
        self.P(flip_mode.store, "insert_orphan_row", lambda tk, pd: None)
        self.P(flip_mode.notify, "send", lambda t, **k: self.sent.append(t))
        self.P(flip_mode.notify, "alert", lambda t: self.alerts.append(t))
        self.P(flip_mode.discipline, "is_halted", lambda: bool(self.state.get("halted")))
        self.P(flip_mode.gateway, "is_traded", lambda tk: tk in self.traded)
        self.P(flip_mode.gateway, "mark_traded", lambda tk, lane="F": self.traded.add(tk))
        self.P(flip_mode.engine, "heartbeat", lambda: None)
        self.P(flip_mode.engine, "_observe_mode", False)

        def _fetch(c, tk):
            b = self.book_fn(self._bc)
            self._bc += 1
            return b
        self.P(flip_mode.kalshi, "fetch_orderbook", _fetch)
        self.P(flip_mode.kalshi, "cancel_all_for_market", lambda c, tk: None)
        self.P(flip_mode.kalshi, "cancel_order", lambda c, oid: self.canceled.append(oid))
        self.P(flip_mode.kalshi, "position_for_market", lambda c, tk: self.net)
        self.P(flip_mode.kalshi, "get_positions", lambda c: [])
        self.P(flip_mode.kalshi, "extract_boundaries", lambda m: (None, None))
        self.P(flip_mode.kalshi, "get_btc_spot", lambda: None)

        def _place(c, tk, side, price, count=1, expiration_ts=None, v2_price_str=None):
            oid = f"{side}-{price}"
            self.orders.append(oid)
            return oid, {}
        self.P(flip_mode.kalshi, "place_order_maker", _place)
        self.P(flip_mode, "_order_fill_price", lambda c, tk, oid, sd: self.fills_map.get(oid))
        self.clock = _Clock(self.CLOSE - 800)
        self.P(flip_mode, "time", self.clock)

    def _run(self, tk="KXBTC15M-T"):
        return flip_mode.run_flip_cycle(None, tk, self.CLOSE, {})

    def test_scratch_cancels_opposite_first(self):
        self.book_fn = lambda i: _bk(45, 48) if i == 0 else _bk(40, 52)   # YES mark drops to 40
        self.fills_map = {"yes-45": 45, "no-52": 52}    # YES entry fills; scratch flatten NO@52
        self._run()
        self.assertTrue(any("✂️" in s for s in self.sent))
        self.assertIn("no-48", self.canceled)           # the opposite entry
        self.assertIn("no-51", self.canceled)           # the take (exit_args(yes,49)→NO@51)
        self.assertLess(self.canceled.index("no-48"), self.canceled.index("no-51"))
        self.assertEqual(self.trips[-1]["outcome"], "scratched")

    def test_take_fills(self):
        self.fills_map = {"yes-45": 45, "no-51": 51}     # entry + take fill
        self._run()
        self.assertTrue(any("take filled" in s for s in self.sent))
        self.assertEqual(self.trips[-1]["outcome"], "take")
        self.assertEqual(self.trips[-1]["realized_cents"], flip_mode.FLIP_X)

    def test_both_fill_nets_rung_a(self):
        self.book_fn = lambda i: _bk(45, 48)             # both sides post
        self.fills_map = {"yes-45": 45, "no-48": 48}     # both entry legs fill
        self._run()
        self.assertTrue(any("netted rung A" in s for s in self.sent))
        self.assertEqual(self.trips[-1]["outcome"], "netted")

    def test_r1_wall_blocks_when_holding(self):
        self.net = 1                                     # already holding → not flat
        out = self._run()
        self.assertEqual(out, "flip_done")
        self.assertEqual(self.trips, [])                 # no trip opened
        self.assertTrue(any("R1 WALL" in a for a in self.alerts))

    def test_curfew_blocks_all_entries(self):
        self.P(flip_mode, "FLIP_CURFEW", 850)            # T-850 > secs-to-close(800) → past curfew
        out = self._run()
        self.assertEqual(out, "flip_done")
        self.assertEqual(self.trips, [])

    def test_three_scratches_sit_out(self):
        self.P(flip_mode, "FLIP_MAX_TRIPS", 6)
        self.P(flip_mode, "FLIP_SCRATCH_SITOUT", 3)
        # constant book (YES fills @45, book leans YES); spot rises but stays BELOW the strike
        # → scratch fires via (b) spot-through-strike each trip, and rising spot + YES lean
        # re-blesses YES at the OFI gate, so the desk re-enters until the sit-out threshold.
        self.book_fn = lambda i: _bk(45, 55, 10, 2)
        self.fills_map = {"yes-45": 45, "no-55": 55}     # entry YES, scratch flatten NO@55
        self.P(flip_mode.kalshi, "extract_boundaries", lambda m: (100.0, None))   # strike 100
        seq = iter([90 + j * 0.1 for j in range(4000)])  # rising but < 100 (adverse to YES)
        self.P(flip_mode.kalshi, "get_btc_spot", lambda: next(seq, 99.0))
        self._run()
        scratched = [t for t in self.trips if t["outcome"] == "scratched"]
        self.assertEqual(len(scratched), 3)              # stops at the sit-out threshold
        self.assertTrue(any("sitting the window out" in s for s in self.sent))


@unittest.skipUnless(_IMPORTABLE, "notify handler imports discipline/kalshi (CI only)")
class TestPhoneResume(_Restore):
    def setUp(self):
        super().setUp()
        from k_worker import notify, discipline
        self.notify, self.discipline = notify, discipline
        self.state, self.replies = {}, []
        self.P(discipline.store, "get_state", lambda k: self.state.get(k))
        self.P(discipline.store, "set_state", lambda k, v: self.state.__setitem__(k, v))
        self.P(discipline.notify, "send", lambda t, **k: None)
        self.P(notify, "reply", lambda t: self.replies.append(t))
        self.P(notify, "_log_cmd", lambda *a, **k: None)

    def _fire(self, text):
        from k_worker import kalshi, treasury, store
        self.notify._handle_inbound(None, text, kalshi, treasury, store)

    def test_resume_yes_clears_halt(self):
        self.state["halted"] = "3 losses in 60min"
        self._fire("/resume_yes")
        self.assertFalse(self.discipline.is_halted())
        self.assertTrue(any("resumed" in r for r in self.replies))

    def test_resume_no_acknowledges_and_stays_parked(self):
        self.state["halted"] = "flip: 2 consecutive stopped windows"
        self._fire("/resume_no")
        self.assertTrue(self.discipline.is_halted())     # still parked
        self.assertTrue(any("staying parked" in r for r in self.replies))

    def test_restart_no_longer_refused(self):
        self.assertNotIn("/restart", self.notify._REFUSED_CMDS)


if __name__ == "__main__":
    unittest.main()
