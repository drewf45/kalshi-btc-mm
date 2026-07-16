# tests/test_halt.py — W7: two consecutive stopped windows -> flip_halt. Sat-outs took
# no risk and neither stop nor clear the streak; a winning window resets it.
import unittest

from f_worker.ledger import Ledger, KIND_LONE, KIND_SALVAGE, KIND_SAT_OUT


def _stop(led, wid):
    led.record_pnl(wid, KIND_SALVAGE, -30, 2, -32, stopped=True, rung=1)


def _win(led, wid):
    led.record_pnl(wid, KIND_LONE, 6, 0, 6, stopped=False, rung=1)


def _sat(led, wid):
    led.record_pnl(wid, KIND_SAT_OUT, 0, 0, 0, stopped=False, rung=1)


class TestHalt(unittest.TestCase):
    def test_two_consecutive_stops(self):
        led = Ledger(":memory:")
        _stop(led, "w1"); _stop(led, "w2")
        self.assertEqual(led.consecutive_stops(), 2)

    def test_win_breaks_streak(self):
        led = Ledger(":memory:")
        _stop(led, "w1"); _win(led, "w2"); _stop(led, "w3")
        self.assertEqual(led.consecutive_stops(), 1)   # only the trailing stop counts

    def test_sat_out_does_not_break_streak(self):
        led = Ledger(":memory:")
        _stop(led, "w1"); _sat(led, "w2"); _stop(led, "w3")
        self.assertEqual(led.consecutive_stops(), 2)   # sat-out skipped, streak intact

    def test_halt_set_and_read(self):
        led = Ledger(":memory:")
        self.assertEqual(led.halt_state(), (False, "boot"))
        led.set_halt(True, "2 consecutive stops")
        halted, reason = led.halt_state()
        self.assertTrue(halted)
        self.assertIn("consecutive", reason)

    def test_halt_clear_is_an_append(self):
        led = Ledger(":memory:")
        led.set_halt(True, "stops")
        led.set_halt(False, "cleared by DW_CLEAR_HALT=1 boot")
        self.assertFalse(led.halt_state()[0])
        # append-only: both rows survive
        n = led._conn.execute("SELECT COUNT(*) c FROM halt").fetchone()["c"]
        self.assertGreaterEqual(n, 3)   # boot seed + set + clear


if __name__ == "__main__":
    unittest.main()
