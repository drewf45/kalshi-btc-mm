"""Platform-wide market enumeration + priority queue.

Paginates GET /markets, honors DW_SCAN_REQ_PER_MIN via a token-bucket governor,
classifies each market with per-market error isolation.
"""

import os
import time
import logging
import threading
from typing import Optional, List, Dict

from k_worker import kalshi

from . import dstore, classify, registry, feeds, shadow, feemath, budget, gateway
from . import series_of
from .feeds import Observation

log = logging.getLogger("d_worker.scanner")

SCAN_REQ_PER_MIN = int(os.environ.get("DW_SCAN_REQ_PER_MIN", "30"))
SCAN_CYCLE_TARGET_SEC = int(os.environ.get("DW_SCAN_CYCLE_TARGET_SEC", "300"))
CLASSIFY_TIMEOUT_SEC = float(os.environ.get("DW_CLASSIFY_TIMEOUT_SEC", "1.0"))
FEED_STALENESS_SEC = int(os.environ.get("DW_FEED_STALENESS_SEC", "900"))
SIM_CAPITAL_USD = float(os.environ.get("DW_SIM_CAPITAL_USD", "10.00"))
MIN_NET_CLIP_CENTS = float(os.environ.get("DW_MIN_NET_CLIP_CENTS", "1.0"))
SHADOW_BATCH_SIZES = [int(x) for x in
                      os.environ.get("DW_SHADOW_BATCH_SIZES", "1,5,10").split(",")]
DISCOVERY_EVERY_N = int(os.environ.get("DW_DISCOVERY_EVERY_N", "6"))
DISCOVERY_PAGES_PER_SWEEP = int(os.environ.get("DW_DISCOVERY_PAGES", "100"))

_sweep_counter = 0
_dead_series: dict = {}

_SLIM_FIELDS = (
    "ticker", "event_ticker", "series_ticker", "title", "subtitle", "category",
    "rules_url", "close_time", "expected_expiration_time", "expiration_time",
    "status", "floor_strike", "cap_strike", "strike_type",
    "maker_fee_rate", "taker_fee_rate", "fee_rate_maker", "fee_rate_taker",
    "fee_schedule", "position_limit", "tick_size",
)


def _slim(m: dict) -> dict:
    return {k: m[k] for k in _SLIM_FIELDS if k in m}


class TokenBucket:
    """Simple token-bucket rate governor."""

    def __init__(self, rate_per_min: int):
        self.rate = rate_per_min
        self.tokens = float(rate_per_min)
        self.max_tokens = float(rate_per_min)
        self.last_refill = time.time()
        self._lock = threading.Lock()
        self.total_consumed = 0

    def consume(self, n: int = 1) -> None:
        with self._lock:
            now = time.time()
            elapsed = now - self.last_refill
            self.tokens = min(self.max_tokens,
                              self.tokens + elapsed * self.rate / 60.0)
            self.last_refill = now
            while self.tokens < n:
                wait = (n - self.tokens) * 60.0 / self.rate
                self._lock.release()
                time.sleep(min(wait + 0.05, 5.0))
                self._lock.acquire()
                now = time.time()
                elapsed = now - self.last_refill
                self.tokens = min(self.max_tokens,
                                  self.tokens + elapsed * self.rate / 60.0)
                self.last_refill = now
            self.tokens -= n
            self.total_consumed += n

    def halve(self) -> None:
        with self._lock:
            self.rate = max(5, self.rate // 2)
            self.max_tokens = float(self.rate)
            log.warning(f"[SCANNER] Rate halved to {self.rate}/min")


_governor: Optional[TokenBucket] = None


def _get_governor() -> TokenBucket:
    global _governor
    if _governor is None:
        _governor = TokenBucket(SCAN_REQ_PER_MIN)
    return _governor


_running_values: Dict[tuple, float] = {}
_obs_cache: Dict[str, Optional[Observation]] = {}


def _running_value_key(station: str, tz_name: str = "America/New_York") -> tuple:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    try:
        local_tz = ZoneInfo(tz_name)
    except Exception:
        local_tz = ZoneInfo("America/New_York")
    return (station, datetime.now(local_tz).strftime("%Y-%m-%d"))


def _update_running_value(station: str, value: float,
                          tz_name: str = "America/New_York") -> float:
    """Track daily running max for a station. Resets on station-local date change."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    try:
        local_tz = ZoneInfo(tz_name)
    except Exception:
        local_tz = ZoneInfo("America/New_York")
    local_date = datetime.now(local_tz).strftime("%Y-%m-%d")
    key = (station, local_date)
    current = _running_values.get(key)
    if current is None or value > current:
        _running_values[key] = value
    return _running_values[key]


def _stream_discovery(client, governor, cycle_id, approved_series,
                      counters, verdict_rows, skip_agg, seen_tickers) -> int:
    """Crawl up to DISCOVERY_PAGES_PER_SWEEP pages from the persisted cursor,
    classifying page-by-page. Returns markets processed. Memory: O(one page)."""
    cursor = dstore.get_state("discovery_cursor") or None
    pages = 0
    processed = 0
    while pages < DISCOVERY_PAGES_PER_SWEEP:
        governor.consume(1)
        params = {"limit": 200, "status": "open"}
        if cursor:
            params["cursor"] = cursor
        try:
            resp = client.request("GET", "/markets", params=params)
        except RuntimeError as e:
            if "429" in str(e):
                governor.halve()
                from k_worker import notify
                notify.send("🅳 ⚠ SCANNER: 429 on discovery — budget halved")
                break
            raise
        if not isinstance(resp, dict):
            break
        page_markets = [_slim(m) for m in resp.get("markets", [])
                        if m.get("ticker") not in seen_tickers]
        _process_batch(client, governor, page_markets, cycle_id,
                       approved_series, counters, verdict_rows, skip_agg)
        processed += len(page_markets)
        pages += 1
        cursor = resp.get("cursor")
        if not cursor or not resp.get("markets"):
            cursor = None
            break
    dstore.set_state("discovery_cursor", cursor or "")
    return processed


def _target_series_list() -> list:
    """Series to fetch by name every sweep: registry entries (drafted or approved)
    plus the prefix x city bootstrap grid, minus blacklist, minus dead (retry daily)."""
    targets = {r["series_ticker"] for r in dstore.list_registry()}
    for prefix in registry.DRAFT_PREFIXES:
        for city in registry.CITY_STATIONS:
            targets.add(f"{prefix}{city}")
    now = time.time()
    return sorted(s for s in targets
                  if not registry.is_blacklisted(s)
                  and now - _dead_series.get(s, 0) > 86400)


def _fetch_targeted(client: kalshi.KalshiClient,
                    governor: TokenBucket) -> List[dict]:
    """Phase T: one request per target series. Guaranteed weather coverage."""
    out = []
    for series in _target_series_list():
        governor.consume(1)
        try:
            resp = client.request("GET", "/markets",
                                  params={"series_ticker": series,
                                          "status": "open", "limit": 200})
        except RuntimeError as e:
            if "429" in str(e):
                governor.halve()
                from k_worker import notify
                notify.send("🅳 ⚠ SCANNER: 429 on targeted fetch — budget halved")
                break
            log.warning(f"[SCANNER] targeted fetch {series} failed: {e}")
            continue
        markets = (resp or {}).get("markets", []) if isinstance(resp, dict) else []
        if markets:
            _dead_series.pop(series, None)
            out.extend(markets)
        else:
            _dead_series[series] = time.time()
    return out


def _process_batch(client, governor, markets, cycle_id, approved_series,
                   counters, verdict_rows, skip_agg):
    """Classify a batch of markets, mutating counters/rows/agg in place."""
    for market in markets:
        ticker = market.get("ticker", "")
        series = series_of(market)
        close_ts = _resolve_close_ts(market) or 0

        if registry.is_blacklisted(series):
            key = (series, "SKIP_SERIES_BLACKLIST")
            skip_agg[key] = skip_agg.get(key, 0) + 1
            counters["skips"] += 1
            continue
        try:
            result = _classify_market(client, market, governor, close_ts)
            if result.verdict in ("SEED", "ERR") or series in approved_series:
                verdict_rows.append((
                    cycle_id, time.time(), ticker, series, close_ts,
                    result.dclass, result.verdict, result.side,
                    _json_or_none(result.evidence),
                    result.fee_maker, result.fee_taker))
            elif result.verdict.startswith("SKIP"):
                key = (series, result.verdict)
                skip_agg[key] = skip_agg.get(key, 0) + 1
            if result.verdict == "SEED":
                counters["seeds"] += 1
                _handle_seed(client, result, market, governor)
            elif result.verdict.startswith("SKIP"):
                counters["skips"] += 1
            elif result.verdict == "ERR":
                counters["errs"] += 1
        except Exception as e:
            log.warning(f"[SCANNER] Error classifying {ticker}: {e}")
            verdict_rows.append((
                cycle_id, time.time(), ticker, series, close_ts,
                "NONE", "ERR", None,
                _json_or_none({"error": str(e)}), None, None))
            counters["errs"] += 1


def _resolve_close_ts(market: dict) -> Optional[float]:
    """Extract close timestamp from a market object."""
    for field in ("close_time", "expected_expiration_time", "expiration_time"):
        v = market.get(field)
        if isinstance(v, str) and v:
            try:
                from datetime import datetime
                return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
        if isinstance(v, (int, float)) and v > 1_000_000_000:
            return float(v)
    return None


def sweep(client: kalshi.KalshiClient) -> int:
    """Run one full platform sweep. Returns cycle_id."""
    global _obs_cache
    _obs_cache = {}
    governor = _get_governor()
    cycle_id = dstore.start_cycle(governor.rate)
    started = time.time()
    seeds_count = 0
    skips_count = 0
    errs_count = 0
    req_start = governor.total_consumed

    from k_worker import notify

    global _sweep_counter
    _sweep_counter += 1

    verdict_rows = []
    skip_agg: Dict[tuple, int] = {}
    counters = {"seeds": 0, "skips": 0, "errs": 0}
    approved_series = {r["series_ticker"]
                       for r in dstore.list_registry(approved_only=True)}

    # PHASE T — targeted weather, fetched AND classified first
    try:
        targeted = [_slim(m) for m in _fetch_targeted(client, governor)]
    except Exception as e:
        log.error(f"[SCANNER] Targeted fetch failed: {e}")
        dstore.finish_cycle(cycle_id, 0, 0, 0, 1, 0, f"fetch_failed: {e}")
        return cycle_id
    targeted.sort(key=lambda m: _resolve_close_ts(m) or float("inf"))
    targeted_n = len(targeted)
    seen_tickers = {m.get("ticker") for m in targeted}
    _process_batch(client, governor, targeted, cycle_id, approved_series,
                   counters, verdict_rows, skip_agg)

    # PHASE D — streaming, resumable, bounded (every sweep)
    discovery_n = 0
    try:
        discovery_n = _stream_discovery(client, governor, cycle_id,
                                        approved_series, counters,
                                        verdict_rows, skip_agg, seen_tickers)
    except Exception as e:
        log.error(f"[SCANNER] Discovery failed: {e}")
        counters["errs"] += 1

    seeds_count = counters["seeds"]
    skips_count = counters["skips"]
    errs_count = counters["errs"]
    markets_seen = targeted_n + discovery_n

    dstore.insert_verdicts_batch(verdict_rows)
    if skip_agg:
        dstore.insert_skip_aggregates(cycle_id, skip_agg)

    elapsed = time.time() - started
    req_used = governor.total_consumed - req_start
    cursor_state = "resume" if (dstore.get_state("discovery_cursor") or "") else "done"
    notes = (f"T:{targeted_n} D:{discovery_n} cursor={cursor_state} "
             f"elapsed={elapsed:.0f}s")
    if elapsed > SCAN_CYCLE_TARGET_SEC * 2:
        notes += " SLOW"
        log.warning(f"[SCANNER] Sweep took {elapsed:.0f}s (target {SCAN_CYCLE_TARGET_SEC}s)")
        notify.send(f"🅳 ⚠ SCANNER: sweep took {elapsed:.0f}s "
                     f"(target {SCAN_CYCLE_TARGET_SEC}s)")

    dstore.finish_cycle(cycle_id, markets_seen, seeds_count, skips_count,
                        errs_count, req_used, notes)
    log.info(f"[SCANNER] Sweep done: {markets_seen} mkts, "
             f"{seeds_count} seeds, {skips_count} skips, {errs_count} errs, "
             f"{req_used} reqs in {elapsed:.0f}s")
    return cycle_id


def _classify_market(client: kalshi.KalshiClient, market: dict,
                     governor: TokenBucket, close_ts: float) -> classify.VerdictRow:
    """Classify a single market with feeds + book fetch."""
    ticker = market.get("ticker", "")
    series = series_of(market)

    registry.auto_draft(market)
    fee_change = registry.update_fee_from_market(market)
    if fee_change:
        from k_worker import notify
        notify.alert(f"🅳 FEE CHANGE: {series} — old: {fee_change}")

    reg_row = dstore.get_registry(series)
    fee_info = dstore.get_fee(series)

    obs = None
    obs2 = None
    station = (reg_row or {}).get("station_or_ref", "")
    if station and reg_row and reg_row.get("settle_source") in ("NWS", "NWS_STATION", "WEATHER"):
        if station in _obs_cache:
            obs = _obs_cache[station]
        else:
            obs = feeds.observe_nws(station)
            _obs_cache[station] = obs
        station_tz = (reg_row or {}).get("tz", "America/New_York")
        if obs:
            _update_running_value(station, obs.value, station_tz)

    stale = feeds.staleness(obs, FEED_STALENESS_SEC)

    book = None
    if reg_row and reg_row.get("approved_ts"):
        governor.consume(1)
        try:
            book = kalshi.fetch_orderbook(client, ticker)
        except Exception as e:
            log.warning(f"[SCANNER] Book fetch failed for {ticker}: {e}")

    station_tz = (reg_row or {}).get("tz", "America/New_York")
    running_key = _running_value_key(station, station_tz)
    state = {
        "close_ts": close_ts,
        "staleness_sec": stale,
        "max_staleness": FEED_STALENESS_SEC,
        "running_value": _running_values.get(running_key, obs.value if obs else 0),
        "min_net_clip_cents": MIN_NET_CLIP_CENTS,
        "batch_sizes": SHADOW_BATCH_SIZES,
        "sim_capital_remaining": SIM_CAPITAL_USD - dstore.sim_capital_used(),
        "already_seeded": dstore.is_already_seeded(ticker),
    }

    return classify.run(market, reg_row, obs, obs2, fee_info, book, state)


def _handle_seed(client: kalshi.KalshiClient, verdict: classify.VerdictRow,
                 market: dict, governor: TokenBucket) -> None:
    """Record a shadow seed, gated by the inside body's budget ledger."""
    ticker = verdict.market_ticker
    close_ts = verdict.close_ts or 0
    tts = max(0, close_ts - time.time())

    taker_price = (verdict.evidence or {}).get("taker_price", 99)
    res = budget.reserve(ticker, taker_price, verdict.dclass, tts, verdict.side)
    if not res.granted:
        log.info(f"[SCANNER] SEED denied by inside body: {ticker} — {res.reason}")
        return

    book = None
    governor.consume(1)
    try:
        book = kalshi.fetch_orderbook(client, ticker)
    except Exception:
        pass

    seed_id = shadow.record_seed(verdict, book, market)
    if seed_id is None:
        budget.release(res.reservation_id, "SEED_DUPLICATE")
        return

    if res.reservation_id:
        dstore.set_state(f"seed_reservation_{seed_id}", str(res.reservation_id))

    if gateway.is_live_enabled() and seed_id and res.reservation_id:
        taker_m = feemath.taker_mult(verdict.fee_taker or 1.0)
        ok, _ = feemath.entry_ok(taker_price, taker_m, 1, MIN_NET_CLIP_CENTS)
        if ok:
            order_result = gateway.submit(
                client, ticker, verdict.side, taker_price,
                res.reservation_id, governor)
            if order_result.status == "PLACED":
                fee_cents = feemath.order_fee_cents(taker_m, 1, taker_price)
                dstore.mark_seed_live(
                    seed_id, order_result.order_id, taker_price, fee_cents)


def _json_or_none(d: Optional[dict]) -> Optional[str]:
    if d is None:
        return None
    import json
    return json.dumps(d)
