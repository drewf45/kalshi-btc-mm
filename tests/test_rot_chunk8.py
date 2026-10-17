"""WO-ROT-1 Chunk 8 — the break-even guard, the runaway cap, the go-live gates.
Done when: the guard pauses a band that falls under h*; the shadow bundle is
delivered to Telegram."""

import shutil
import time
import zipfile
from decimal import Decimal

import pytest

from relay_engine import config, failures, lane_rot as lr, rot_guard, rot_trips, venue
from relay_engine.book import OrderBook
from relay_engine.series_discovery import SeriesInfo
from relay_engine.shadow_runner import ShadowEngine

NOW = time.time()


@pytest.fixture
def eng(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DESK_MODE", "ROTATION")
    monkeypatch.setattr(config, "SERIES_INFO", {"KXBTC15M": SeriesInfo(
        "KXBTC15M", "15M", "t", "BTC", ["CF"], "1c", "q", "1", 0, 1)})
    # group_limit = 3x largest position asserts against the $100-book order budget
    # (500c / 40c = 12.5 -> 38). The live shadow baseline is now $50; pin $100 so
    # this keeps testing the group-limit arithmetic, not the new baseline.
    monkeypatch.setattr(config, "SHADOW_PAPER_BANKROLL_USD", 100.0)
    failures._warn_last.clear()
    e = ShadowEngine(db_path=str(tmp_path / "c8.db"))
    e.boot()
    e.sent = []
    e.telegram.send = e.sent.append
    failures.configure(e.ledger, alert_fn=e.telegram.alert, run_mode="TEST", boot_id=1)
    monkeypatch.setattr(e.gateway.governor, "take", lambda now=None: True)
    e.path = str(tmp_path / "c8.db")
    yield e
    failures._ledger = None
    failures._alert_fn = None


def trips(e, band, n, win_every, W=8.0, L=12.0, shadow=1, t0=None, cost=45):
    """n booked trips in a band; every `win_every`-th is a win of W, else a loss of L."""
    t0 = NOW - 3600 if t0 is None else t0
    for i in range(n):
        win = i % win_every == 0
        rot_trips.write(e.ledger, {
            "ts": t0 + i, "opened_ts": t0 + i - 100, "series": "KXBTC15M",
            "market": f"M-{band}-{shadow}-{t0}-{i}", "grp": "BTC", "side": "yes",
            "entry_cost": cost, "size": 1.0, "target": cost + 8, "stop": cost - 10,
            "hard_stop": cost - 20, "band": band, "fill_ts": t0 + i - 90,
            "exit_path": "TARGET" if win else "SOFT_STOP", "trigger_px": None,
            "fill_px": None, "gap": None, "spot_move": None, "reads_held": 3,
            "sold": 1.0, "settle_count": 0.0, "settle_result": None, "settled": 1,
            "net_cents": W if win else -L, "fees_cents": 0.0, "shadow": shadow,
            "rank": 1, "score": 0.1, "why": "test"})


# ── the break-even guard ─────────────────────────────────────────

def test_guard_pauses_a_band_that_falls_under_h_star(eng):
    trips(eng, "40-49", 50, win_every=2)          # h 0.5 < h* 12/(8+12) = 0.6
    assert eng.rot_guard_eval() == ["40-49"]
    assert "40-49" in eng.rot.paused_bands
    page = [s for s in eng.sent if "band 40-49 PAUSED" in s]
    assert len(page) == 1
    assert "h 0.500 < h* 0.600 over last 50 trips" in page[0] and "/rot_reopen 40-49" in page[0]
    # the desk stops entering the band
    m = "KXBTC15M-26OCT14Z-00"
    eng.rot.outside(NOW, {}, {m: {"close_ts": NOW + 600, "rec_yes_bid": 46,
                                  "rec_yes_ask": 48}}, 5)
    assert eng.rot.verdicts[m] == lr.SKIP_BAND_PAUSED
    # paged once, not every evaluation
    eng.rot_guard_eval()
    assert sum("band 40-49 PAUSED" in s for s in eng.sent) == 1


def test_guard_holds_a_band_at_or_above_break_even_and_waits_for_50(eng):
    trips(eng, "40-49", 49, win_every=2)          # under h*, but only 49 trips
    assert eng.rot_guard_eval() == []
    trips(eng, "50-54", 50, win_every=1)          # all wins
    assert eng.rot_guard_eval() == []
    assert eng.rot.paused_bands == set()


def test_guard_judges_the_runs_own_kind(eng):
    trips(eng, "40-49", 50, win_every=2, shadow=0)    # LIVE trips, in a SHADOW run
    assert eng.rot_guard_eval() == []


def test_pause_survives_a_restart(eng, tmp_path):
    trips(eng, "40-49", 50, win_every=2)
    eng.rot_guard_eval()
    again = ShadowEngine(db_path=eng.path)
    assert "40-49" in again.rot.paused_bands


def test_reopen_is_accounting_only_and_restarts_the_window(eng):
    trips(eng, "40-49", 50, win_every=2)
    eng.rot_guard_eval()
    orders_before = dict(eng.gateway.order_index)
    reply = eng.telegram.handle_command("/rot_reopen 40-49")
    assert "REOPENED" in reply and "no order placed" in reply
    assert eng.gateway.order_index == orders_before
    assert "40-49" not in eng.rot.paused_bands
    assert eng.rot_guard_eval() == []                 # old evidence is not re-judged
    assert "usage" in eng.telegram.handle_command("/rot_reopen")
    assert "not paused" in eng.telegram.handle_command("/rot_reopen 50-54")


def test_l_includes_taker_fees(eng):
    """Net is after fees: a fee-heavy loss widens L and lifts h*."""
    rot_trips.ensure(eng.ledger.db)
    trips(eng, "50-54", 50, win_every=2, W=8.0, L=7.0)   # h* 7/15 = .467 < .5: fine
    assert eng.rot_guard_eval() == []
    trips(eng, "50-54", 50, win_every=2, W=8.0, L=9.0, t0=NOW)  # fees push L to 9
    assert eng.rot_guard_eval() == ["50-54"]


# ── the runaway cap ──────────────────────────────────────────────

class GroupVenue:
    def __init__(self, gid="G-1"):
        self.gid, self.calls = gid, []

    def request(self, method, path, params=None, json_body=None, **k):
        self.calls.append((method, path, json_body))
        if path == "/portfolio/order_groups/create":
            return {"order_group_id": self.gid} if self.gid else {}
        if path.endswith("/reset"):
            return {}
        raise AssertionError(path)


def test_order_group_limit_is_3x_the_largest_position(eng):
    size, _ = eng.rot.size_for(40)                    # $100 book: 500c / 40c = 12.5
    assert rot_guard.group_limit(eng.rot) == int(-(-float(size) * 3 // 1)) == 38
    v = GroupVenue()
    gid = rot_guard.ensure_group(eng.ledger, eng.rot, v, eng.telegram.alert)
    assert gid == "G-1" and eng.rot.order_group_id == "G-1"
    assert v.calls[0] == ("POST", "/portfolio/order_groups/create", {"contracts_limit": 38})
    assert any("contracts limit 38" in s for s in eng.sent)


def test_no_group_means_no_entries(eng):
    rot_guard.ensure_group(eng.ledger, eng.rot, GroupVenue(gid=None), eng.telegram.alert)
    assert rot_guard.RUNAWAY_HALT in eng.gateway.entries_halted_for("ROT")


def test_live_entries_ride_the_group_and_a_trip_halts_until_drew_resets(eng, monkeypatch):
    monkeypatch.setattr(config, "RUN_MODE", "LIVE")
    monkeypatch.setattr(config, "I_UNDERSTAND_LIVE", config.I_UNDERSTAND_LIVE_PHRASE)
    monkeypatch.setattr(venue, "get_balance", lambda c: (100.0, 0.0))
    placed = []

    def place(*a, **k):
        placed.append(k)
        if len(placed) > 1:
            raise RuntimeError("HTTP 409 order group contracts limit exceeded")
        return ("LIVE-1", {})
    monkeypatch.setattr(venue, "place_order_maker", place)
    v = GroupVenue()
    eng.gateway.venue_client = v
    eng.rot.order_group_id = "G-1"
    eng.rot.live_fn = lambda u: True
    books, meta = {}, {}
    for i, m in enumerate(("KXBTC15M-26OCT14A-00", "KXBTC15M-26OCT14B-00")):
        b = OrderBook(market=m)
        b.apply_snapshot({46: 50}, {52: 50}, ts=NOW)
        books[m] = b
        meta[m] = {"close_ts": NOW + 600 + i, "rec_yes_bid": 46, "rec_yes_ask": 48}
    eng.rot.step(NOW, books, meta, 16)
    assert placed[0]["order_group_id"] == "G-1"
    assert rot_guard.RUNAWAY_HALT in eng.gateway.entries_halted_for("ROT")
    assert any("RUNAWAY CAP TRIPPED" in s for s in eng.sent)
    reply = eng.telegram.handle_command("/rot_group_reset")
    assert "reset" in reply and ("PUT", "/portfolio/order_groups/G-1/reset", {}) in v.calls
    assert rot_guard.RUNAWAY_HALT not in eng.gateway.entries_halted_for("ROT")


def test_shadow_only_entries_carry_no_group(eng):
    eng.rot.order_group_id = "G-1"
    eng.rot.live_fn = lambda u: False
    m = "KXBTC15M-26OCT14A-00"
    b = OrderBook(market=m)
    b.apply_snapshot({46: 50}, {52: 50}, ts=NOW)
    eng.rot.step(NOW, {m: b}, {m: {"close_ts": NOW + 600, "rec_yes_bid": 46,
                                   "rec_yes_ask": 48}}, 16)
    o = eng.gateway.order_index[eng.rot.trips[m].entry_oid]
    assert o.shadow_only and o.order_group_id is None


def test_venue_order_group_body():
    v = GroupVenue()
    assert venue.create_order_group(v, 38) == "G-1"
    with pytest.raises(ValueError):
        venue.create_order_group(v, 0)
    assert venue.reset_order_group(v, "G-1") is True

    class C:
        def request(self, method, path, json_body=None, **k):
            self.body = json_body
            return {"order_id": "X"}
    c = C()
    venue.place_order_maker(c, "KXBTC15M-X", "yes", 46, count=1, order_group_id="G-1")
    assert c.body["order_group_id"] == "G-1"


# ── go-live gates ────────────────────────────────────────────────

def test_shadow_verdict_gate(eng):
    from relay_engine.daily_bundle import _start_of_day
    y = _start_of_day(NOW, 1)
    trips(eng, "40-49", 20, win_every=1, t0=y + 100)            # all wins
    lines = "\n".join(rot_guard.verdict_lines(eng.ledger, y, shadow=1))
    assert "40-49: n 20" in lines and "ABOVE" in lines and "GATE" in lines and "MET" in lines
    y2 = _start_of_day(NOW, 2)
    trips(eng, "40-49", 20, win_every=2, t0=y2 + 100)          # h .5 < h* .6
    assert "NOT MET" in "\n".join(rot_guard.verdict_lines(eng.ledger, y2, shadow=1))


def test_first_live_day_runs_at_25pct_then_50(eng, monkeypatch):
    from relay_engine.daily_bundle import _start_of_day
    assert rot_guard.deploy_cap(eng.ledger) == 0.50                # shadow rehearses 50%
    monkeypatch.setattr(config, "RUN_MODE", "LIVE")
    monkeypatch.setattr(config, "I_UNDERSTAND_LIVE", config.I_UNDERSTAND_LIVE_PHRASE)
    assert rot_guard.deploy_cap(eng.ledger) == 0.25
    assert eng.rot.deploy_cap_fn() == 0.25
    y = _start_of_day(NOW, 1)
    trips(eng, "40-49", 5, win_every=1, shadow=0, t0=y + 100)      # too few trips
    assert not rot_guard.maybe_promote(eng.ledger, y, eng.telegram.alert)
    trips(eng, "40-49", 10, win_every=1, shadow=0, t0=y + 1000)
    assert rot_guard.maybe_promote(eng.ledger, y, eng.telegram.alert)
    assert rot_guard.deploy_cap(eng.ledger) == 0.50


def test_shadow_bundle_is_delivered_to_telegram(eng, tmp_path, monkeypatch):
    from relay_engine.daily_bundle import _start_of_day
    y = _start_of_day(NOW, 1)
    trips(eng, "40-49", 12, win_every=1, t0=y + 100)
    got = {}

    def send_document(path, caption=""):
        shutil.copy(path, tmp_path / "sent.xlsx")
        got["caption"] = caption
        return True
    monkeypatch.setattr(eng.telegram, "send_document", send_document)
    lines = eng.rot_daily(now=NOW)
    assert any("SHADOW VERDICT" in s for s in eng.sent)
    assert "GO-LIVE GATE" in "\n".join(lines)
    assert got["caption"].startswith("daily_")
    names = zipfile.ZipFile(tmp_path / "sent.xlsx").read("xl/workbook.xml").decode()
    assert 'name="ROT"' in names and 'name="rot_trips"' in names


def test_rot_status_is_read_only(eng):
    before = dict(eng.gateway.order_index)
    out = eng.telegram.handle_command("/rot_status")
    assert out.startswith("ROT: open") and "deploy cap now" in out
    assert eng.gateway.order_index == before
