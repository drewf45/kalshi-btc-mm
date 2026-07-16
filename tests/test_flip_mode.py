"""tests/test_flip_mode.py — WO-LANE-FLIP one-shot flag + stop counter.

NOTE: imports k_worker.flip_mode, which imports the cryptography-bound client — so this
runs in CI/deploy (where cryptography loads), not in the offline sandbox. Logic is
exercised with the engine's real modules monkeypatched to in-memory fakes."""

import time
import unittest

try:
    from k_worker import flip_mode
    _IMPORTABLE = True
except BaseException:            # pyo3 PanicException (broken crypto binding) isn't Exception
    _IMPORTABLE = False


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


if __name__ == "__main__":
    unittest.main()
