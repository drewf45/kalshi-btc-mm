# tests/test_loud_tape.py — Drew's law: EVERYTHING fails or tags why, loudly. Nothing
# reaches the tape without its reason + evidence.
import unittest

from f_worker import window as W
from f_worker.config import Config
from f_worker.ledger import Ledger
from f_worker.fgateway import FGateway
from f_worker.manager import Manager, compute_window_pnl, window_story
from f_worker.pricebrain import PriceBrain
from f_worker.desk import Desk
from tests.fakes import FakeClient, FakeNotifier

MKT = "KXBTC15M-25JUL16T1415"
BOOK = {"orderbook": {"yes": {"bids": [[48, 5]], "asks": [[50, 5]]},
                      "no": {"bids": [[49, 5]], "asks": [[51, 5]]}}}


def _win(**kw):
    d = dict(window_id=MKT, market_ticker=MKT, event_ticker="E", open_ts=None,
             close_ts=1_752_678_900, rung=1, lots=1)
    d.update(kw)
    return W.Window(**d)


class TestBlindGate(unittest.TestCase):
    def test_dead_feed_fails_closed_not_crash(self):
        pb = PriceBrain(Config(), gate_path="")
        pb.sigma = lambda now=None: None          # feed is down
        g = pb.gate_window(BOOK)                    # must NOT raise round(None)
        self.assertFalse(g.ok)
        self.assertTrue(g.reason.startswith("BLIND"))
        self.assertEqual(g.regime, "blind")
        self.assertIsNone(g.evidence["sigma"])


class TestSatOutVariants(unittest.TestCase):
    def test_gate_refusal_line(self):
        win = _win()
        win.sat_reason = "vol too calm to cross a strike"
        win.sat_evidence = {"sigma": 4.1, "regime": "low"}
        line = window_story(win, compute_window_pnl(win, Config()))
        self.assertIn("SAT_OUT (gate: vol too calm to cross a strike", line)
        self.assertIn("σ=4.1 low", line)

    def test_posted_no_fill_line(self):
        win = _win()
        win.sat_reason = "posted, no fill in entry phase"
        win.sat_evidence = {"posted_yes": 49, "posted_no": 48,
                            "touch": {"yes_bid": 51, "no_bid": 50}, "entry_window_sec": 60}
        line = window_story(win, compute_window_pnl(win, Config()))
        self.assertIn("posted 49/48", line)
        self.assertIn("0 fills in 60s", line)
        self.assertIn("touch 51/50", line)

    def test_blind_line(self):
        win = _win()
        win.sat_reason = "BLIND: spot/sigma feed unavailable"
        win.sat_evidence = {"sigma": None}
        line = window_story(win, compute_window_pnl(win, Config()))
        self.assertIn("SAT_OUT (gate: BLIND", line)

    def test_two_variants_are_distinguishable(self):
        # the whole point: from the phone alone, gate-tuning vs thesis are different lines
        gate = _win(); gate.sat_reason = "x"; gate.sat_evidence = {"sigma": 3.0, "regime": "low"}
        post = _win(); post.sat_reason = "posted, no fill in entry phase"
        post.sat_evidence = {"posted_yes": 49, "posted_no": 48, "touch": {}, "entry_window_sec": 60}
        self.assertNotEqual(window_story(gate, compute_window_pnl(gate, Config())),
                            window_story(post, compute_window_pnl(post, Config())))


class TestStageErrorsAreLoud(unittest.TestCase):
    def _desk(self, client):
        cfg = Config()
        led = Ledger(":memory:")
        notif = FakeNotifier()
        gw = FGateway(client, led, cfg, notif)
        mgr = Manager(gw, led, PriceBrain(cfg, gate_path=""), cfg, notif)
        desk = Desk(client, gw, mgr, PriceBrain(cfg, gate_path=""), led, cfg, notif)
        return desk, led, notif

    def test_discovery_error_tags_and_pages(self):
        client = FakeClient()

        def boom(ticker):
            raise RuntimeError("discovery boom")
        client.get_market = boom            # direct-ticker fetch errors (non-404)

        desk, led, notif = self._desk(client)
        handled = desk.loop_once()
        self.assertIsNone(handled)
        # tagged STAGE_ERR row on the tape
        rows = led._conn.execute(
            "SELECT evidence FROM window_events WHERE to_state='STAGE_ERR'").fetchall()
        self.assertTrue(rows)
        self.assertIn("discovery", rows[0]["evidence"])
        # and a ⚠ phone line
        self.assertTrue(any("discovery error" in s for s in notif.sent))

    def test_fills_poll_error_is_tagged_once(self):
        client = FakeClient()

        def boom(mt=None, limit=200):
            raise RuntimeError("fills boom")
        client.get_fills = boom

        desk, led, notif = self._desk(client)
        win = _win().bind_ledger(led)
        desk._cur_win = win
        desk._poll_fills(win, {"yes": "A", "no": "B"}, "buy")
        desk._poll_fills(win, {"yes": "A", "no": "B"}, "buy")   # second call same window
        n = led._conn.execute(
            "SELECT COUNT(*) c FROM window_events WHERE to_state='STAGE_ERR'").fetchone()["c"]
        self.assertEqual(n, 1)   # rate-limited to once per window
        self.assertTrue(any("fills_poll error" in s for s in notif.sent))


if __name__ == "__main__":
    unittest.main()
