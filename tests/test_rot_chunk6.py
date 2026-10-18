"""WO-ROT-1 Chunk 6 — spot per underlying, shadow-only without a feed, the
Mac host (power, sleep/wake, launcher). Network sources are faked: the Kalshi
index channel payloads, Coinbase and Pyth Hermes could not be reached from the
build environment — their shapes are UNPROVEN and the parsers are tolerant.
"""

import asyncio
import json
import os
import plistlib
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from relay_engine import config, failures, spot_multi as sm, venue
from relay_engine.book import OrderBook
from relay_engine.gateway import Order
from relay_engine.series_discovery import SeriesInfo
from relay_engine.shadow_runner import ShadowEngine

ROOT = Path(__file__).resolve().parents[1]
T = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    sm._cb_cache.clear()
    sm._pyth_ids.clear()
    sm._shapes_logged.clear()
    failures._warn_last.clear()
    monkeypatch.setattr(config, "DESK_MODE", "ROTATION")


class Resp:
    def __init__(self, data, ok=True):
        self.data, self.ok = data, ok

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError("HTTP 500")

    def json(self):
        return self.data


def fake_http(prices=None, pyth=None, fail=False):
    calls = []

    def get(url, params=None, headers=None, timeout=None):
        calls.append((url, params, headers))
        if fail:
            raise RuntimeError("down")
        if "coinbase" in url:
            asset = url.split("/prices/")[1].split("-")[0]
            return Resp({"data": {"amount": str(prices[asset])}})
        if url.endswith("/v2/price_feeds"):
            q = params["query"]
            sym = {"XAU": "Metal.XAU/USD", "XAG": "Metal.XAG/USD"}.get(q, q)
            return Resp([{"id": f"id_{q}", "attributes": {"symbol": sym}}])
        if url.endswith("/v2/updates/price/latest"):
            return Resp({"parsed": [{"id": i, "price": {"price": str(v), "expo": -2}}
                                    for i, v in (pyth or {}).items()]})
        raise AssertionError(url)
    get.calls = calls
    return get


# ── the settle-index channels (UNPROVEN payload) ─────────────────

def test_index_subscribe_is_one_cmd_per_channel():
    cmds = sm.index_subscribe_cmds()
    assert [c["params"]["channels"] for c in cmds] == [["cfbenchmarks_value"], ["pyth_value"]]


@pytest.mark.parametrize("frame,want", [
    ({"type": "cfbenchmarks_value", "msg": {"index": "BRTI", "value": "100123.5", "ts": 1800000000000}},
     [("BTC", 100123.5, 1800000000.0, "BRTI")]),
    ({"type": "cfbenchmarks_value_5hz", "msg": {"symbol": "ETHUSD_RTI", "price": 3500}},
     [("ETH", 3500.0, None, "ETHUSD_RTI")]),
    ({"type": "pyth_value", "msg": {"values": [{"feed": "Metal.XAU/USD", "value": 2400.1},
                                               {"feed": "Metal.XAG/USD", "value": 30.2}]}},
     [("GOLD", 2400.1, None, "Metal.XAU/USD"), ("SILVER", 30.2, None, "Metal.XAG/USD")]),
    ({"type": "orderbook_delta", "msg": {"price": 50}}, []),
])
def test_parse_index_message(frame, want):
    assert sm.parse_index_message(frame) == want


def test_unknown_index_shape_is_logged_once_not_guessed(caplog):
    f = {"type": "cfbenchmarks_value", "msg": {"weird": 1}}
    assert sm.parse_index_message(f) == []
    assert sm.parse_index_message(f) == []
    assert sum("unrecognized" in r.message for r in caplog.records) == 1


def test_index_ws_feeds_the_spot_book():
    frames = [json.dumps({"type": "cfbenchmarks_value", "msg": {"index": "BRTI", "value": 100000}}),
              json.dumps({"type": "error", "msg": {"code": 8, "msg": "Unknown channel name"}}),
              json.dumps({"type": "cfbenchmarks_value", "msg": {"index": "SOLUSD_RTI", "value": 150}})]
    sent = []
    stop = asyncio.Event()

    class WS:
        async def send(self, m):
            sent.append(json.loads(m))

        async def recv(self):
            if not frames:
                stop.set()
                return "{}"
            return frames.pop(0)

    class Conn:
        def __init__(self, url, additional_headers=None):
            self.headers = additional_headers

        async def __aenter__(self):
            return WS()

        async def __aexit__(self, *a):
            return False

    book = sm.SpotBook()
    asyncio.run(sm.run_index_ws(book, stop, Conn, lambda: {"K": "v"}, "wss://x"))
    assert len(sent) == 2
    assert book.latest("BTC") == 100000.0 and book.latest("SOL") == 150.0
    assert book.sources_seen["BTC"] == {"kalshi_index"}


# ── fallbacks: Coinbase (crypto), Pyth (metals/oil/FX) ───────────

def test_coinbase_generalized_cached_and_bounded():
    http = fake_http({"ETH": 3500})
    assert sm.coinbase_spot("ETH", http_get=http, now=T) == 3500.0
    assert sm.coinbase_spot("ETH", http_get=http, now=T + 3) == 3500.0
    assert len(http.calls) == 1                                   # 5 s cache
    down = fake_http(fail=True)
    assert sm.coinbase_spot("ETH", http_get=down, now=T + 10) == 3500.0   # bounded stale
    assert sm.coinbase_spot("ETH", http_get=down, now=T + 10_000) is None  # never unbounded


def test_three_crypto_underlyings_inside_the_kalshi_budget(monkeypatch):
    """Spot for BTC, ETH and SOL costs ZERO Kalshi REST reads (the index is a
    WS channel; the fallback is Coinbase): the read bucket is untouched."""
    before = venue.REST_READ_GOVERNOR.taken
    book = sm.SpotBook()
    sm.poll_once(book, {"BTC", "ETH", "SOL"},
                 http_get=fake_http({"BTC": 100000, "ETH": 3500, "SOL": 150}), now=T)
    assert {u: book.latest(u, T) for u in ("BTC", "ETH", "SOL")} == \
        {"BTC": 100000.0, "ETH": 3500.0, "SOL": 150.0}
    assert venue.REST_READ_GOVERNOR.taken == before


def test_fresh_settle_index_means_no_proxy_poll():
    book = sm.SpotBook()
    book.record("BTC", 100000, "kalshi_index", T)
    http = fake_http({"BTC": 99000})
    sm.poll_once(book, {"BTC"}, http_get=http, now=T + 1)
    assert http.calls == [] and book.latest("BTC", T + 1) == 100000


def test_pyth_without_a_key_is_shadow_only_and_live_with_one(monkeypatch):
    book = sm.SpotBook()
    monkeypatch.setattr(config, "PYTH_API_KEY", None)
    sm.poll_once(book, {"GOLD"}, http_get=fake_http(), now=T)
    assert not book.live("GOLD", T)
    assert "NO FEED -> SHADOW-ONLY" in book.status_lines({"GOLD"}, T)[0]
    monkeypatch.setattr(config, "PYTH_API_KEY", "k")
    http = fake_http(pyth={"id_XAU": 240010})
    sm.poll_once(book, {"GOLD"}, http_get=http, now=T)
    assert book.latest("GOLD", T) == pytest.approx(2400.10)
    assert book.live("GOLD", T)
    assert all(c[2] == {"Authorization": "Bearer k"} for c in http.calls)


def test_cross_source_mismatch_is_logged(caplog):
    book = sm.SpotBook()
    book.record("WTI", 70.0, "kalshi_index", T)
    book.record("WTI", 72.0, "pyth", T + 1)
    assert any("SPOT_MISMATCH" in r.message for r in caplog.records)


# ── ROT_SPOT_CONFIRM per underlying ──────────────────────────────

def test_btc_confirm_is_40_points():
    assert sm.SpotBook().confirm("BTC") == 40.0


def test_non_btc_confirm_needs_history_then_is_2x_median_minute_move():
    book = sm.SpotBook()
    assert book.confirm("ETH", T) is None                 # no history: no confirm
    # an hour of 10 s samples; the price steps +0.5 every 10 s -> 3.0 per minute
    for i in range(361):
        book.record("ETH", 3000 + 0.5 * i, "coinbase", T + 10 * i)
    now = T + 3600
    book._confirm.clear()
    assert book.confirm("ETH", now) == pytest.approx(6.0)  # 2 x 3.0
    book.record("ETH", 9999, "coinbase", now + 1)          # cached for the hour
    assert book.confirm("ETH", now + 60) == pytest.approx(6.0)


# ── shadow-only trips without a feed ─────────────────────────────

def _engine(tmp_path, name="c6.db"):
    e = ShadowEngine(db_path=str(tmp_path / name))
    e.boot()
    e.sent = []
    e.telegram.send = e.sent.append
    failures.configure(e.ledger, alert_fn=e.telegram.alert, run_mode="TEST", boot_id=1)
    return e


def _gold_market(e, m, now):
    e.market_meta[m] = {"close_ts": now + 3000, "rec_yes_bid": 46, "rec_yes_ask": 48}
    e.feed.book(m).apply_snapshot({46: 50}, {52: 50}, ts=now)


def test_no_feed_trip_is_shadow_only_for_life(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SERIES_INFO", {"KXGOLDH": SeriesInfo(
        "KXGOLDH", "1H", "t", "GOLD", ["Pyth"], "1c", "q", "1", 0, 1)})
    e = _engine(tmp_path)
    m = "KXGOLDH-26OCT1415-00"
    _gold_market(e, m, T)
    e.cycle([m], now=T)
    e.cycle([m], now=T + 1)
    t = e.rot.trips[m]
    assert t.shadow_only
    o = e.gateway.order_index[t.entry_oid]
    assert o.shadow_only and "SHADOW-ONLY" in o.why
    failures._ledger = None
    failures._alert_fn = None


def test_feed_live_trip_is_not_shadow_only(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SERIES_INFO", {"KXGOLDH": SeriesInfo(
        "KXGOLDH", "1H", "t", "GOLD", ["Pyth"], "1c", "q", "1", 0, 1)})
    e = _engine(tmp_path)
    import time as _t
    now = _t.time()
    e.spot_book.record("GOLD", 2400.0, "kalshi_index", now)
    m = "KXGOLDH-26OCT1415-00"
    _gold_market(e, m, now)
    e.cycle([m], now=now)
    e.cycle([m], now=now + 1)
    assert not e.rot.trips[m].shadow_only
    failures._ledger = None
    failures._alert_fn = None


def test_gateway_never_places_a_shadow_only_order_live(gateway, monkeypatch):
    monkeypatch.setattr(config, "RUN_MODE", "LIVE")
    monkeypatch.setattr(config, "I_UNDERSTAND_LIVE", config.I_UNDERSTAND_LIVE_PHRASE)
    monkeypatch.setattr(venue, "place_order_maker",
                        lambda *a, **k: pytest.fail("a shadow-only order reached the venue"))
    b = OrderBook(market="KXGOLDH-X-00")
    b.apply_snapshot({46: 50}, {52: 50}, ts=T)
    o = Order(lane="ROT", event="KXGOLDH-X", market="KXGOLDH-X-00", side="yes",
              action="buy", price_cents=46, count=1, size_tier="PROBE",
              purpose="ENTRY", band=(40, 54), why="w", shadow_only=True)
    r = gateway.submit(o, b)
    assert r.shadow and r.order_id.startswith("SHADOW-")


def test_boot_tape_says_which_underlyings_are_shadow_only(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SERIES_INFO", {
        "KXBTC15M": SeriesInfo("KXBTC15M", "15M", "t", "BTC", ["CF"], "1c", "q", "1", 0, 1),
        "KXGOLDH": SeriesInfo("KXGOLDH", "1H", "t", "GOLD", ["Pyth"], "1c", "q", "1", 0, 1)})
    e = _engine(tmp_path)
    e.spot_book.record("BTC", 100000.0, "coinbase")
    lines = "\n".join(e.spot_status_lines())
    assert "BTC: LIVE" in lines and "GOLD: NO FEED -> SHADOW-ONLY" in lines
    failures._ledger = None
    failures._alert_fn = None


# ── the Mac host: power, sleep/wake ──────────────────────────────

def test_power_source_parse():
    batt = lambda: "Now drawing from 'Battery Power'\n -InternalBattery-0 (id=1) 80%;"
    ac = lambda: "Now drawing from 'AC Power'\n"
    assert ShadowEngine.power_source(batt) == "Battery Power"
    assert ShadowEngine.power_source(ac) == "AC Power"
    assert ShadowEngine.power_source(lambda: "garbage") is None


def test_on_battery_pages_and_stops_entries_then_ac_resumes(tmp_path):
    e = _engine(tmp_path)
    e.check_power(lambda: "Now drawing from 'Battery Power'")
    assert "ON_BATTERY" in e.gateway.entries_halted_for("ROT")
    assert any("ON_BATTERY" in s for s in e.sent)
    e.check_power(lambda: "Now drawing from 'Battery Power'")
    assert sum("ON_BATTERY" in s for s in e.sent) == 1
    e.check_power(lambda: "Now drawing from 'AC Power'")
    assert "ON_BATTERY" not in e.gateway.entries_halted_for("ROT")
    failures._ledger = None
    failures._alert_fn = None


def test_shadow_allow_battery_keeps_entries_alive_on_battery(tmp_path, monkeypatch, caplog):
    # Drew 2026-10-17: test/collect on battery without the AC leash. In SHADOW
    # with SHADOW_ALLOW_BATTERY on, battery does NOT halt entries; it pages ONCE.
    monkeypatch.setattr(config, "RUN_MODE", "SHADOW")
    monkeypatch.setattr(config, "SHADOW_ALLOW_BATTERY", True)
    e = _engine(tmp_path, name="c6_batt_bypass.db")
    with caplog.at_level("WARNING"):
        e.check_power(lambda: "Now drawing from 'Battery Power'")
        # entries CONTINUE — the halt is never added
        assert "ON_BATTERY" not in e.gateway.entries_halted_for("ROT")
        assert "ON_BATTERY_BYPASS" in caplog.text
        # notice is once-per-boot, not every cycle
        e.check_power(lambda: "Now drawing from 'Battery Power'")
    assert "ON_BATTERY" not in e.gateway.entries_halted_for("ROT")
    assert caplog.text.count("ON_BATTERY_BYPASS") == 1
    assert e._battery_bypass_logged is True
    failures._ledger = None
    failures._alert_fn = None


def test_live_always_halts_on_battery_even_if_bypass_flag_set(tmp_path, monkeypatch):
    # The bypass is SHADOW-only. In LIVE the ON_BATTERY halt is enforced no
    # matter what SHADOW_ALLOW_BATTERY says — a dying laptop never carries risk.
    monkeypatch.setattr(config, "RUN_MODE", "LIVE")
    monkeypatch.setattr(config, "SHADOW_ALLOW_BATTERY", True)
    e = _engine(tmp_path, name="c6_live_batt.db")
    e.check_power(lambda: "Now drawing from 'Battery Power'")
    assert "ON_BATTERY" in e.gateway.entries_halted_for("ROT")
    assert not any("ON_BATTERY_BYPASS" in s for s in e.sent)
    failures._ledger = None
    failures._alert_fn = None


def test_boot_tape_prints_the_power_line(tmp_path, capsys):
    _engine(tmp_path)
    assert "POWER:" in capsys.readouterr().out
    failures._ledger = None
    failures._alert_fn = None


def test_wake_in_shadow_prints_gap_and_reconcile_before_any_entry(tmp_path, capsys):
    e = _engine(tmp_path)
    lines = e.on_wake(1800.0)
    out = capsys.readouterr().out
    assert "WAKE: loop gap 1800s" in out
    assert out.index("WAKE: loop gap") < out.index("RECONCILE: SHADOW") < out.index("entries resume")
    assert "WAKE_RECONCILE" not in e.ledger.entry_halts
    failures._ledger = None
    failures._alert_fn = None


def test_wake_in_live_reconciles_from_the_venue_first(tmp_path, monkeypatch, capsys):
    e = _engine(tmp_path)
    monkeypatch.setattr(config, "RUN_MODE", "LIVE")
    monkeypatch.setattr(config, "I_UNDERSTAND_LIVE", config.I_UNDERSTAND_LIVE_PHRASE)
    from relay_engine import lane_rot
    held = "KXBTC15M-26OCT141015-15"
    lane_rot.adopt_trip(e.rot, held, "yes", 10, 46, T, T + 600)
    e.gateway.positions[(held.rsplit("-", 1)[0], held, "ROT")] = 10
    monkeypatch.setattr(venue, "get_positions", lambda c: [
        {"ticker": held, "position": 10}, {"ticker": "KXETH15M-X-00", "position": 3}])
    seen_halt = []
    real = e.read_venue_positions

    def read(client):
        seen_halt.append("WAKE_RECONCILE" in e.ledger.entry_halts)
        return real(client)
    monkeypatch.setattr(e, "read_venue_positions", read)
    e.on_wake(900.0, client=object())
    out = capsys.readouterr().out
    assert seen_halt == [True]                        # entries halted while reconciling
    assert f"{held} yes x10" in out                   # its stops went unwatched: said so
    assert out.index("WAKE: loop gap") < out.index("RECONCILE (venue speaks first")
    assert "QUARANTINED KXETH15M-X-00 net 3" in out
    assert "WAKE_RECONCILE" not in e.ledger.entry_halts
    failures._ledger = None
    failures._alert_fn = None


def test_wake_with_unreadable_venue_keeps_entries_halted(tmp_path, monkeypatch):
    e = _engine(tmp_path)
    monkeypatch.setattr(config, "RUN_MODE", "LIVE")
    monkeypatch.setattr(config, "I_UNDERSTAND_LIVE", config.I_UNDERSTAND_LIVE_PHRASE)

    def boom(c):
        raise RuntimeError("HTTP 503")
    monkeypatch.setattr(venue, "get_positions", boom)
    e.on_wake(900.0, client=object())
    assert "WAKE_RECONCILE" in e.gateway.entries_halted_for("ROT")
    failures._ledger = None
    failures._alert_fn = None


def test_rest_loop_treats_a_gap_as_a_wake():
    src = (ROOT / "relay_engine" / "shadow_runner.py").read_text()
    loop = src[src.index("async def _rest_main"):src.index("async def run():")]
    assert "engine.on_wake" in loop
    assert loop.index("engine.on_wake") < loop.index("engine.cycle(")


# ── the Mac launcher ─────────────────────────────────────────────

def test_plist_keepalive_runatload_caffeinate():
    d = plistlib.load(open(ROOT / "mac" / "com.kal.rotationdesk.plist", "rb"))
    assert d["KeepAlive"] is True and d["RunAtLoad"] is True
    assert d["ProgramArguments"][:3] == ["/usr/bin/caffeinate", "-i", "-s"]
    assert d["ProgramArguments"][-2:] == ["-m", "relay_engine.mac_main"]


def test_install_script_parses_and_env_is_never_committed():
    assert subprocess.run(["bash", "-n", str(ROOT / "mac" / "install.sh")]).returncode == 0
    gi = (ROOT / ".gitignore").read_text().split()
    assert ".env" in gi and ".venv/" in gi
    assert "chmod 600" in (ROOT / "mac" / "install.sh").read_text()


def test_env_file_must_be_600(tmp_path, monkeypatch):
    from relay_engine import mac_main
    f = tmp_path / ".env"
    f.write_text("KAL_TEST_X=1\nexport KAL_TEST_Y='two'\n# c\n")
    os.chmod(f, 0o644)
    with pytest.raises(SystemExit):
        mac_main.load_env_file(str(f))
    os.chmod(f, 0o600)
    monkeypatch.delenv("KAL_TEST_X", raising=False)
    monkeypatch.delenv("KAL_TEST_Y", raising=False)
    assert mac_main.load_env_file(str(f)) == 2
    assert os.environ["KAL_TEST_X"] == "1" and os.environ["KAL_TEST_Y"] == "two"
    monkeypatch.delenv("KAL_TEST_X")
    monkeypatch.delenv("KAL_TEST_Y")


def test_mac_defaults_db_and_heartbeat_paths(tmp_path, monkeypatch):
    from relay_engine import mac_main
    monkeypatch.delenv("RELAY_DB_PATH", raising=False)
    monkeypatch.delenv("HEARTBEAT_PATH", raising=False)
    mac_main.prepare_environment(str(tmp_path / "Kal"))
    assert os.environ["RELAY_DB_PATH"] == str(tmp_path / "Kal" / "kal.db")
    assert os.environ["HEARTBEAT_PATH"] == str(tmp_path / "Kal" / "heartbeat.json")
    monkeypatch.delenv("RELAY_DB_PATH")
    monkeypatch.delenv("HEARTBEAT_PATH")
