"""tests/test_flip_wo5.py — WO-LANE-FLIP-5: enter-row order_id, series allowlist, treasury wipe.

The dataclass/predicate/treasury classes are crypto-free (sandbox-runnable). The boot-scan
and _record_enter round-trip import the client-bound modules, so they run in CI/deploy (or
against a stubbed client)."""

import os
import sqlite3
import tempfile
import unittest
from dataclasses import asdict

from k_worker import store, treasury, discipline

_RealSurfaceRow = store.SurfaceRow    # capture before any test can monkeypatch the module attr

try:
    from k_worker import __main__ as kw_main, flip_mode
    _IMPORTABLE = True
except BaseException:            # broken crypto binding isn't Exception (pyo3 panic)
    _IMPORTABLE = False


class _RestoreMixin(unittest.TestCase):
    """Patch shared module attributes and restore them in tearDown (no cross-test leakage)."""
    def setUp(self):
        self._saved = []

    def P(self, obj, attr, val):
        self._saved.append((obj, attr, getattr(obj, attr)))
        setattr(obj, attr, val)

    def tearDown(self):
        for obj, attr, orig in reversed(self._saved):
            setattr(obj, attr, orig)


# ── §1 ENTER-row order_id field (crypto-free) ────────────────────────
class TestSurfaceRowOrderId(unittest.TestCase):
    def test_order_id_present_and_absent(self):
        r1 = _RealSurfaceRow(market_ticker="KXBTC15M-X", decision_ts=1.0, action="ENTER",
                             order_id="oid-123")
        self.assertEqual(asdict(r1)["order_id"], "oid-123")
        r2 = _RealSurfaceRow(market_ticker="KXBTC15M-X", decision_ts=1.0, action="ENTER")
        self.assertIsNone(asdict(r2)["order_id"])


# ── §2 series allowlist predicate (crypto-free) ──────────────────────
class TestSeriesAllowlist(unittest.TestCase):
    def test_btc_allowed_wnba_not(self):
        allow = ["KXBTC15M"]
        self.assertTrue(store.series_allowed("KXBTC15M-25JUL16-B", allow))
        self.assertFalse(store.series_allowed("KXWNBAGAME-25JUL16NYDAL-DAL", allow))
        self.assertFalse(store.series_allowed(None, allow))
        self.assertFalse(store.series_allowed("", allow))

    def test_case_insensitive_and_multi(self):
        self.assertTrue(store.series_allowed("kxbtc15m-x", ["KXBTC15M"]))
        self.assertTrue(store.series_allowed("KXETH-x", ["KXBTC15M", "KXETH"]))


# ── §3 treasury wipe (crypto-free; store patched to in-memory) ───────
class TestTreasuryWipe(_RestoreMixin):
    def setUp(self):
        super().setUp()
        self.state, self.sent = {}, []
        self.P(treasury.store, "get_state", lambda k: self.state.get(k))
        self.P(treasury.store, "set_state", lambda k, v: self.state.__setitem__(k, v))
        self.P(treasury.notify, "send", lambda t, **k: self.sent.append(t))

    def test_wipe_zeros_owed_and_sets_book(self):
        self.state["treasury_accrued_tax"] = "2.00"
        self.state["treasury_accrued_fee"] = "0.90"
        self.assertTrue(treasury.wipe_owed_once(10.00))
        t = treasury.get_totals()
        self.assertEqual(t["accrued_tax"], 0.0)
        self.assertEqual(t["accrued_fee"], 0.0)
        self.assertEqual(t["paid_tax"], 0.0)
        self.assertEqual(t["paid_fee"], 0.0)
        self.assertEqual(t["engine_book"], 10.00)
        self.assertEqual(treasury.accrued_total(), 0.0)          # owed $0.00
        self.assertTrue(any("wiped per ruling" in s for s in self.sent))

    def test_wipe_runs_once(self):
        self.state["treasury_accrued_tax"] = "2.90"
        self.assertTrue(treasury.wipe_owed_once(10.0))
        self.state["treasury_accrued_tax"] = "5.00"              # pretend re-accrued
        self.assertFalse(treasury.wipe_owed_once(10.0))          # flag set → no-op
        self.assertEqual(treasury.get_totals()["accrued_tax"], 5.0)


# ── integration: boot scan + _record_enter (client-bound → CI/stub) ──
# ── WO-MORNING §1 migration parity (crypto-free; real DB fixture) ────
_LEGACY_FLIP_WINDOWS = """
    CREATE TABLE flip_windows (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, close_ts INTEGER,
        tag TEXT, ticker TEXT, entry_yes INTEGER, entry_no INTEGER, bundle_cost INTEGER,
        exit_yes INTEGER, exit_no INTEGER, capture_a_cents INTEGER, capture_b_cents INTEGER,
        realized_cents INTEGER, mtm_open_cents INTEGER, spread_yes INTEGER, spread_no INTEGER,
        sigma_at_gate REAL, ttff_s REAL, ttflat_s REAL, outcome_tag TEXT, broker_flat INTEGER)
"""


class TestFlipWindowMigration(unittest.TestCase):
    def test_legacy_schema_migrates_then_insert_succeeds(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "legacy.db")
        c = sqlite3.connect(path)                 # a DB predating WO-4/WO-6 columns
        c.execute(_LEGACY_FLIP_WINDOWS)
        c.commit()
        c.close()
        saved_path, saved_conn = store.DB_PATH, store._conn
        try:
            store.DB_PATH = path
            store.init_db()                        # runs the guarded ALTER on the legacy table
            rid = store.insert_flip_window(
                ts=1.0, tag="20:00", ticker="KXBTC15M-X", entry_yes=45, entry_no=48,
                join_yes=45, join_no=48, post_dt_yes=1.0, post_dt_no=2.0,
                booksum_yes=93, booksum_no=93, bundle_cost=93, realized_cents=7,
                outcome_tag="NETTED_2R", broker_flat=1)
            self.assertTrue(rid)                   # INSERT with every column succeeds
            row = store.flip_windows_between(0, 9)[0]
            self.assertEqual(row["booksum_yes"], 93)
            self.assertEqual(row["post_dt_no"], 2.0)
        finally:
            store.DB_PATH, store._conn = saved_path, saved_conn


# ── WO-MORNING §2 the kill learns magnitude (crypto-free) ────────────
class TestTailLossMagnitude(_RestoreMixin):
    def setUp(self):
        super().setUp()
        self.state, self.alerts = {}, []
        self.P(discipline.store, "get_state", lambda k: self.state.get(k))
        self.P(discipline.store, "set_state", lambda k, v: self.state.__setitem__(k, v))
        self.P(discipline.notify, "alert", lambda t: self.alerts.append(t))

    def _halted(self):
        return bool(self.state.get("halted"))

    def test_small_declines_do_not_kill(self):
        for i in range(3):                         # three routine −2¢ LONE_DECLINED flattens
            discipline.record_loss(2, f"W{i}", "LONE_DECLINED")
        self.assertFalse(self._halted())

    def test_sub_threshold_losses_do_not_kill(self):
        for i in range(3):                         # three −5¢ (< 10¢) windows
            discipline.record_loss(5, f"W{i}", "LONE_RIDE")
        self.assertFalse(self._halted())

    def test_three_real_losses_kill(self):
        for i in range(3):                         # three −15¢ genuine losing windows
            discipline.record_loss(15, f"W{i}", "LONE_RIDE")
        self.assertTrue(self._halted())
        self.assertTrue(any("TAIL-LOSS KILL" in a for a in self.alerts))

    def test_two_legs_one_window_counts_once(self):
        discipline.record_loss(15, "W1", "LONE_RIDE")   # both legs of the SAME window
        discipline.record_loss(48, "W1", "LONE_RIDE")
        import json
        self.assertEqual(len(json.loads(self.state["loss_ts"])), 1)
        self.assertFalse(self._halted())


@unittest.skipUnless(_IMPORTABLE, "imports the cryptography-bound client (CI only)")
class TestBootScanAndEnterRow(_RestoreMixin):
    def test_mixed_positions_allowlist(self):
        state, orphaned, sent, alerts = {}, [], [], []
        self.P(kw_main.store, "get_state", lambda k: state.get(k))
        self.P(kw_main.store, "set_state", lambda k, v: state.__setitem__(k, v))
        self.P(kw_main.store, "has_row_for_ticker", lambda tk: False)
        self.P(kw_main.store, "insert_orphan_row", lambda tk, p: orphaned.append(tk))
        self.P(kw_main.notify, "send", lambda t, **k: sent.append(t))
        self.P(kw_main.notify, "alert", lambda t: alerts.append(t))
        self.P(kw_main.kalshi, "get_positions", lambda c: [
            {"ticker": "KXWNBAGAME-25JUL16NYDAL-DAL", "position": 5},
            {"ticker": "KXBTC15M-25JUL16-B", "position": 1},
        ])
        self.P(kw_main, "KW_SERIES_ALLOWLIST", ["KXBTC15M"])
        kw_main.adopt_or_ignore_positions(None)
        self.assertEqual(orphaned, ["KXBTC15M-25JUL16-B"])       # BTC orphan adopted
        self.assertTrue(any("PERSONAL" in s and "WNBA" in s for s in sent))  # WNBA logged
        self.assertFalse(any("WNBA" in a for a in alerts))       # not paged as an orphan

    def test_personal_logged_once(self):
        state, sent = {}, []
        self.P(kw_main.store, "get_state", lambda k: state.get(k))
        self.P(kw_main.store, "set_state", lambda k, v: state.__setitem__(k, v))
        self.P(kw_main.store, "has_row_for_ticker", lambda tk: False)
        self.P(kw_main.store, "insert_orphan_row", lambda tk, p: None)
        self.P(kw_main.notify, "send", lambda t, **k: sent.append(t))
        self.P(kw_main.notify, "alert", lambda t: None)
        self.P(kw_main.kalshi, "get_positions",
               lambda c: [{"ticker": "KXWNBAGAME-X-DAL", "position": 5}])
        self.P(kw_main, "KW_SERIES_ALLOWLIST", ["KXBTC15M"])
        kw_main.adopt_or_ignore_positions(None)
        kw_main.adopt_or_ignore_positions(None)                  # second boot scan
        self.assertEqual(sum("PERSONAL" in s for s in sent), 1)  # logged once, not repeated

    def test_record_enter_order_id_roundtrip(self):
        rows = []
        self.P(flip_mode.store, "insert_row", lambda r: rows.append(r))
        self.P(flip_mode.store, "SurfaceRow", _RealSurfaceRow)   # the REAL dataclass
        self.P(flip_mode.notify, "send", lambda t, **k: None)
        flip_mode._record_enter("KXBTC15M-X", 2_000_000_000, "no", 48, order_id="oid-9")
        flip_mode._record_enter("KXBTC15M-X", 2_000_000_000, "yes", 45)   # order_id absent
        self.assertEqual(rows[0].order_id, "oid-9")
        self.assertIsNone(rows[1].order_id)
        self.assertEqual(rows[1].fill_cost_cents, 45)


@unittest.skipUnless(_IMPORTABLE, "imports the cryptography-bound client (CI only)")
class TestBootClearHalt(_RestoreMixin):
    """WO-RESUME §2: KW_CLEAR_HALT=1 clears halt once at boot; a marker guards the crash-loop."""
    def _fakes(self):
        state, sent = {}, []
        self.P(kw_main.store, "get_state", lambda k: state.get(k))
        self.P(kw_main.store, "set_state", lambda k, v: state.__setitem__(k, v))
        self.P(kw_main.notify, "send", lambda t, **k: sent.append(t))
        return state, sent

    def test_clears_once_then_guards_crashloop(self):
        state, sent = self._fakes()
        state["halted"] = "3 losses in 60min"
        state["loss_ts"] = "[1,2,3]"
        state["flip_stop_streak"] = "1"
        self.P(kw_main, "KW_CLEAR_HALT", True)
        kw_main.maybe_clear_halt()
        self.assertEqual(state["halted"], "")                       # halt cleared
        self.assertEqual(state["loss_ts"], "[]")                    # loss history cleared
        self.assertEqual(state["flip_stop_streak"], "0")            # flip streak cleared
        self.assertEqual(state[kw_main._CLEAR_HALT_MARKER], "1")
        self.assertTrue(any("halt cleared by env" in s for s in sent))
        # crash-loop: env still set, marker present -> must NOT re-clear
        state["halted"] = "re-halted"
        kw_main.maybe_clear_halt()
        self.assertEqual(state["halted"], "re-halted")

    def test_env_removed_rearms_marker(self):
        state, _sent = self._fakes()
        state[kw_main._CLEAR_HALT_MARKER] = "1"
        self.P(kw_main, "KW_CLEAR_HALT", False)
        kw_main.maybe_clear_halt()
        self.assertEqual(state[kw_main._CLEAR_HALT_MARKER], "0")     # re-armed for next env-set


if __name__ == "__main__":
    unittest.main()
