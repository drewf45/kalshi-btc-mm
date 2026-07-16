# tests/test_state_machine.py — the One-Shot Law as code (§3), incl. NO path out of DONE.
import unittest

from f_worker import window as W
from f_worker.ledger import Ledger


def _win(ledger=None):
    w = W.Window(window_id="KXBTC15M-25JUL16T1415", market_ticker="KXBTC15M-25JUL16T1415",
                 event_ticker="E", open_ts=None, close_ts=1_700_000_000, rung=1, lots=1)
    return w.bind_ledger(ledger) if ledger else w


class TestStateMachine(unittest.TestCase):
    def test_happy_path_bundle(self):
        led = Ledger(":memory:")
        w = _win(led)
        self.assertEqual(w.state, W.IDLE)
        w.transition(W.SEEKING)
        w.transition(W.HOLDING)
        w.transition(W.DONE)
        self.assertEqual(w.state, W.DONE)
        self.assertEqual(led.current_state(w.window_id), W.DONE)

    def test_sat_out_from_idle(self):
        w = _win()
        w.transition(W.SAT_OUT)
        self.assertTrue(w.is_terminal)

    def test_illegal_skip(self):
        w = _win()
        with self.assertRaises(W.IllegalTransition):
            w.transition(W.HOLDING)   # IDLE -> HOLDING is not an edge

    def test_no_path_out_of_DONE(self):
        w = _win()
        w.transition(W.SEEKING)
        w.transition(W.HOLDING)
        w.transition(W.DONE)
        for target in (W.IDLE, W.SEEKING, W.HOLDING, W.EXITING, W.SAT_OUT, W.DONE):
            with self.assertRaises(W.IllegalTransition):
                w.transition(target)

    def test_no_path_out_of_SAT_OUT(self):
        w = _win()
        w.transition(W.SAT_OUT)
        for target in (W.IDLE, W.SEEKING, W.HOLDING, W.EXITING, W.DONE):
            with self.assertRaises(W.IllegalTransition):
                w.transition(target)

    def test_holding_can_exit_then_done(self):
        w = _win()
        w.transition(W.SEEKING)
        w.transition(W.HOLDING)
        w.transition(W.EXITING)     # T-90 flat wall
        w.transition(W.DONE)
        self.assertEqual(w.state, W.DONE)

    def test_transitions_are_logged(self):
        led = Ledger(":memory:")
        w = _win(led)
        w.transition(W.SEEKING, {"gate": "one shot"})
        w.transition(W.SAT_OUT)  # not legal from SEEKING? it IS: SEEKING->SAT_OUT
        self.assertEqual(led.current_state(w.window_id), W.SAT_OUT)


if __name__ == "__main__":
    unittest.main()
