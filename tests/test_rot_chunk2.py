"""WO-ROT-1 Chunk 2 — series discovery and the request budget.

The /markets pages here are SYNTHETIC, built to the field shape the tree
already reads (ticker, event_ticker, open_time/close_time ISO, *_dollars
touches) — no live pages could be recorded from this environment. Series
record fields (frequency, settlement_sources, fee_type, fee_multiplier) are
UNPROVEN shapes; discovery logs every value it reads so the first live roster
proves or corrects them.
"""

import itertools
import random
from datetime import datetime, timedelta, timezone

import pytest

from relay_engine import config, failures, series_discovery as sd, venue
from relay_engine.shadow_runner import ShadowEngine

T0 = datetime(2026, 10, 14, 14, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _mk(series, open_dt, minutes, strike="", tick_field=None):
    close = open_dt + timedelta(minutes=minutes)
    ev = f"{series}-{close.strftime('%y%b%d%H%M').upper()}"
    m = {"ticker": f"{ev}{('-' + strike) if strike else '-00'}",
         "event_ticker": ev, "status": "active",
         "open_time": _iso(open_dt), "close_time": _iso(close),
         "yes_bid_dollars": "0.4600", "yes_ask_dollars": "0.4800"}
    if tick_field:
        m.update(tick_field)
    return m


def _markets():
    out = []
    # 15-minute crypto: one window open (duration 900 s), plus the next listed
    for s in ("KXBTC15M", "KXETH15M", "KXXRP15M"):
        out.append(_mk(s, T0, 15, tick_field={"tick_size": 1}))
    # hourly crypto ladder: many strikes, one event, open 60 min
    out += [_mk("KXBTCD", T0, 60, strike=f"T{k}") for k in (110000, 111000, 112000)]
    # daily weather: open a day
    out += [_mk("KXHIGHNY", T0 - timedelta(hours=20), 24 * 60, strike=f"B{k}")
            for k in (60, 62)]
    # weekly: open a week
    out.append(_mk("KXGASW", T0 - timedelta(days=3), 7 * 24 * 60))
    # 15-minute index: listed, excluded (no settle source on file)
    out.append(_mk("KXINX15M", T0, 15))
    # 15-minute with no known underlying
    out.append(_mk("KXFOO15M", T0, 15))
    # 15-minute whose series record names no settlement source
    out.append(_mk("KXSOL15M", T0, 15))
    # hourly gold with the record carrying its own series_ticker field
    g = _mk("KXGOLDH", T0, 60)
    g["series_ticker"] = "KXGOLDH"
    out.append(g)
    return out


SERIES = {
    "KXBTC15M": {"ticker": "KXBTC15M", "frequency": "fifteen_min",
                 "settlement_sources": [{"name": "CF Benchmarks", "url": "https://cfbenchmarks.com"}],
                 "fee_type": "quadratic", "fee_multiplier": 1},
    "KXETH15M": {"ticker": "KXETH15M", "frequency": "custom",   # unrecognized -> custom = OTHER? no: see below
                 "settlement_sources": [{"name": "CF Benchmarks"}],
                 "fee_type": "quadratic", "fee_multiplier": 1},
    "KXXRP15M": {"ticker": "KXXRP15M",                       # no frequency: inferred
                 "settlement_sources": [{"name": "CF Benchmarks"}],
                 "fee_type": "quadratic", "fee_multiplier": 1},
    "KXBTCD": {"ticker": "KXBTCD", "frequency": "hourly",
               "settlement_sources": [{"name": "CF Benchmarks"}],
               "fee_type": "quadratic", "fee_multiplier": 1},
    "KXFOO15M": {"ticker": "KXFOO15M", "settlement_sources": [{"name": "X"}]},
    "KXSOL15M": {"ticker": "KXSOL15M", "frequency": "fifteen_min", "settlement_sources": []},
    "KXGOLDH": {"ticker": "KXGOLDH", "settlement_sources": [{"name": "Pyth"}],
                "fee_type": "quadratic", "fee_multiplier": 1},
}
# ETH carries an unrecognized frequency string: discovery must NOT trust it
# and must fall back to inference (it is a 15-minute series by its windows).
SERIES["KXETH15M"]["frequency"] = "every_quarter_hour_probably"


class FakeVenue:
    """Serves /markets in pages of `page` with a cursor, and /series/<t>."""

    def __init__(self, markets, series, page=4):
        self.markets, self.series, self.page = markets, series, page
        self.calls = []

    def request(self, method, path, params=None, json_body=None, **kw):
        self.calls.append((method, path, dict(params or {})))
        if path == "/markets":
            assert params["status"] == "open"
            start = int(params.get("cursor") or 0)
            chunk = self.markets[start:start + self.page]
            nxt = start + self.page
            return {"markets": chunk,
                    "cursor": str(nxt) if nxt < len(self.markets) else ""}
        if path.startswith("/series/"):
            t = path.rsplit("/", 1)[1]
            if t not in self.series:
                raise RuntimeError(f"HTTP 404 {path}")
            return {"series": self.series[t]}
        raise AssertionError(path)


def _no_fee_changes(client, series):
    return []


@pytest.fixture
def roster():
    return sd.discover(FakeVenue(_markets(), SERIES), fee_changes_fn=_no_fee_changes)


# ── discovery ────────────────────────────────────────────────────

def test_scan_pages_through_every_open_market():
    v = FakeVenue(_markets(), SERIES, page=4)
    mk, pages, truncated = sd.scan_open_markets(v)
    assert len(mk) == len(_markets())
    assert pages == -(-len(_markets()) // 4) and not truncated
    assert [c[2].get("cursor") for c in v.calls if c[1] == "/markets"][0] is None


def test_scan_truncation_is_reported_not_silent():
    v = FakeVenue(_markets(), SERIES, page=2)
    mk, pages, truncated = sd.scan_open_markets(v, max_pages=3)
    assert truncated and pages == 3 and len(mk) == 6


def test_finds_15_minute_and_hourly_series(roster):
    assert set(roster.admitted) == {"KXBTC15M", "KXETH15M", "KXXRP15M", "KXBTCD", "KXGOLDH"}
    a = roster.admitted
    assert a["KXBTC15M"].cadence == "15M" and a["KXBTC15M"].cadence_source == "frequency:fifteen_min"
    assert a["KXETH15M"].cadence == "15M" and a["KXETH15M"].cadence_source.startswith("inferred")
    assert a["KXXRP15M"].cadence == "15M" and a["KXXRP15M"].cadence_source.startswith("inferred")
    assert a["KXBTCD"].cadence == "1H"
    assert a["KXGOLDH"].cadence == "1H" and a["KXGOLDH"].underlying == "GOLD"


def test_rejects_daily_and_weekly(roster):
    assert roster.skipped["KXHIGHNY"].startswith(sd.SKIP_CADENCE)
    assert roster.skipped["KXGASW"].startswith(sd.SKIP_CADENCE)


def test_daily_series_cost_no_series_read():
    v = FakeVenue(_markets(), SERIES)
    sd.discover(v, fee_changes_fn=_no_fee_changes)
    read = {c[1] for c in v.calls}
    assert "/series/KXHIGHNY" not in read and "/series/KXGASW" not in read


def test_every_skip_has_its_reason(roster):
    assert roster.skipped["KXINX15M"] == sd.SKIP_EXCLUDED
    assert roster.skipped["KXFOO15M"] == sd.SKIP_NO_UNDERLYING
    assert roster.skipped["KXSOL15M"] == sd.SKIP_NO_SETTLE_SOURCE
    assert set(roster.found) == set(roster.admitted) | set(roster.skipped)


def test_admission_reads_settle_tick_fee_and_group(roster):
    b = roster.admitted["KXBTC15M"]
    assert b.underlying == "BTC" and b.settle_sources == ["CF Benchmarks"]
    assert b.tick == "1c" and b.fee_type == "quadratic" and b.fee_multiplier == "1"
    assert b.fee_changes == 0
    assert roster.admitted["KXBTCD"].tick == "unknown"   # nothing spoke: said so


def test_unreadable_series_record_skips():
    s = dict(SERIES)
    del s["KXBTCD"]
    r = sd.discover(FakeVenue(_markets(), s), fee_changes_fn=_no_fee_changes)
    assert r.skipped["KXBTCD"] == sd.SKIP_SERIES_UNREADABLE


def test_explicit_daily_frequency_is_rejected_even_if_windows_look_short():
    s = {k: dict(v) for k, v in SERIES.items()}
    s["KXBTCD"]["frequency"] = "daily"
    r = sd.discover(FakeVenue(_markets(), s), fee_changes_fn=_no_fee_changes)
    assert r.skipped["KXBTCD"].startswith(sd.SKIP_CADENCE)


def test_cadence_from_spacing_when_no_open_time():
    ms = [{"ticker": f"KXBTC15M-X{i}", "close_time": _iso(T0 + timedelta(minutes=15 * i))}
          for i in range(3)]
    assert sd.infer_cadence(ms) == ("15M", "inferred:spacing 900s")


def test_underlying_map():
    assert sd.underlying_of("KXBTC15M") == "BTC"
    assert sd.underlying_of("KXETHD") == "ETH"
    assert sd.underlying_of("KXSILVERH") == "SILVER"
    assert sd.underlying_of("KXNOPE") is None


# ── roster install ───────────────────────────────────────────────

def test_install_replaces_the_hardcoded_roster(roster, monkeypatch):
    monkeypatch.delenv("SERIES_LIST", raising=False)
    installed = sd.install(roster)
    assert config.SERIES == installed == list(roster.admitted)
    assert config.ROSTER_SOURCE == "discovered"
    assert config.SERIES_INFO["KXBTCD"].cadence == "1H"
    assert set(config.f_enabled_series()) == set(roster.admitted)


def test_series_list_env_still_overrides(roster, monkeypatch):
    monkeypatch.setenv("SERIES_LIST", "KXBTC15M,KXSOL15M")
    installed = sd.install(roster)
    assert installed == ["KXBTC15M"]      # SOL was skipped: never installed
    assert config.ROSTER_SOURCE == "env"
    lines = sd.roster_lines(roster, installed)
    assert any("KXETH15M" in ln and "[not in SERIES_LIST]" in ln for ln in lines)


# ── the engine: boot roster post, failure, book poll set ─────────

@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.delenv("SERIES_LIST", raising=False)
    monkeypatch.setattr(config, "DESK_MODE", "ROTATION")
    e = ShadowEngine(db_path=str(tmp_path / "c2.db"))
    e.boot()
    e.telegram_sent = []
    e.telegram.send = e.telegram_sent.append
    failures.configure(e.ledger, alert_fn=e.telegram.alert, run_mode="TEST", boot_id=1)
    monkeypatch.setattr(venue, "get_series_fee_changes", _no_fee_changes)
    yield e
    failures._ledger = None
    failures._alert_fn = None


def test_telegram_roster_prints_found_admitted_skipped(engine, capsys):
    engine.rediscover(FakeVenue(_markets(), SERIES))
    post = "\n".join(engine.telegram_sent)
    assert "ROSTER" in post and "admitted 5" in post and "skipped 5" in post
    for reason in (sd.SKIP_CADENCE, sd.SKIP_EXCLUDED, sd.SKIP_NO_UNDERLYING,
                   sd.SKIP_NO_SETTLE_SOURCE):
        assert reason in post
    assert "✅ KXBTC15M 15M BTC" in post
    assert "ROSTER" in capsys.readouterr().out
    # an unchanged hourly rediscovery does not re-post
    engine.rediscover(FakeVenue(_markets(), SERIES))
    assert sum("ROSTER" in m for m in engine.telegram_sent) == 1


def test_discovery_failure_never_falls_back_to_hardcoded(engine):
    class Down:
        def request(self, *a, **k):
            raise RuntimeError("HTTP 503 /markets")
    assert engine.rediscover(Down()) is None
    assert config.SERIES == []
    assert any("DISCOVERY_FAILED" in m for m in engine.telegram_sent)


def test_rotation_polls_only_held_resting_and_shortlisted_books(engine):
    for m in ("KXBTC15M-A-00", "KXETH15M-B-00", "KXXRP15M-C-00"):
        engine.market_meta[m] = {"close_ts": None}
    assert engine.book_poll_set() == []
    engine.gateway.positions[("KXBTC15M-A", "KXBTC15M-A-00", "ROT")] = 1
    engine.book_shortlist.add("KXXRP15M-C-00")
    assert engine.book_poll_set() == ["KXBTC15M-A-00", "KXXRP15M-C-00"]


# ── the request budget ───────────────────────────────────────────

class _Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _max_in_window(stamps, window=1.0):
    best, j = 0, 0
    for i, t in enumerate(stamps):
        while stamps[j] <= t - window:
            j += 1
        best = max(best, i - j + 1)
    return best


@pytest.mark.parametrize("which,limit", [("read", config.KALSHI_READS_PER_S_LIMIT),
                                         ("write", config.KALSHI_WRITES_PER_S_LIMIT)])
def test_governor_never_exceeds_the_limit_in_a_10_minute_soak(which, limit):
    """Hammered for 10 simulated minutes, with bursts and idle gaps, the bucket
    never admits more than the per-second limit in ANY 1-second window."""
    clk = _Clock()
    cap = (config.REST_READ_BUCKET_CAPACITY if which == "read"
           else config.REST_WRITE_BUCKET_CAPACITY)
    rate = (config.REST_READ_REFILL_PER_SECOND if which == "read"
            else config.REST_WRITE_REFILL_PER_SECOND)
    gov = venue._RestGovernor(cap, rate, clock=clk.now, sleep=clk.sleep)
    rng = random.Random(7)
    stamps = []
    idle = 0.0
    while clk.t < 600.0:
        if rng.random() < 0.02:
            gap = rng.uniform(0.5, 3.0)         # idle: the bucket refills
            clk.t += gap
            idle += gap
        gov.pace()
        stamps.append(clk.t)
    assert _max_in_window(stamps) <= limit
    # and while busy it runs at its rate, not throttled to nothing
    assert len(stamps) / (clk.t - idle) > rate * 0.95


def test_reads_and_writes_take_separate_buckets():
    assert venue.governor_for("GET") is venue.REST_READ_GOVERNOR
    for m in ("POST", "DELETE", "PUT", "post"):
        assert venue.governor_for(m) is venue.REST_WRITE_GOVERNOR


def test_budget_is_sized_under_kalshi_basic():
    assert (config.REST_READ_BUCKET_CAPACITY + config.REST_READ_REFILL_PER_SECOND
            <= config.KALSHI_READS_PER_S_LIMIT == 20)
    assert (config.REST_WRITE_BUCKET_CAPACITY + config.REST_WRITE_REFILL_PER_SECOND
            <= config.KALSHI_WRITES_PER_S_LIMIT == 10)


def test_real_clock_short_soak():
    """2 s on the real clock: same bound, real sleeps."""
    import time
    gov = venue._RestGovernor(config.REST_READ_BUCKET_CAPACITY,
                              config.REST_READ_REFILL_PER_SECOND)
    stamps = []
    end = time.monotonic() + 2.0
    while time.monotonic() < end:
        gov.pace()
        stamps.append(time.monotonic())
    assert _max_in_window(stamps) <= config.KALSHI_READS_PER_S_LIMIT
