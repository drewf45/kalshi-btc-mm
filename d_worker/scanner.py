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

from . import dstore, classify, registry, feeds, shadow, feemath
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


def _fetch_all_markets(client: kalshi.KalshiClient,
                       governor: TokenBucket) -> List[dict]:
    """Paginate GET /markets to get all open/active markets."""
    all_markets = []
    cursor = None
    page = 0
    while True:
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
                notify.send("🅳 ⚠ SCANNER: 429 rate limit — budget halved")
                break
            raise
        if not isinstance(resp, dict):
            break
        markets = resp.get("markets", [])
        all_markets.extend(markets)
        cursor = resp.get("cursor")
        page += 1
        if not cursor or not markets:
            break
        if len(all_markets) > 10000:
            break
    return all_markets


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
    governor = _get_governor()
    cycle_id = dstore.start_cycle(governor.rate)
    started = time.time()
    seeds_count = 0
    skips_count = 0
    errs_count = 0
    req_start = governor.total_consumed

    from k_worker import notify

    try:
        markets = _fetch_all_markets(client, governor)
    except Exception as e:
        log.error(f"[SCANNER] Market fetch failed: {e}")
        dstore.finish_cycle(cycle_id, 0, 0, 0, 1, 0, f"fetch_failed: {e}")
        return cycle_id

    markets.sort(key=lambda m: _resolve_close_ts(m) or float("inf"))

    verdict_rows = []
    for market in markets:
        ticker = market.get("ticker", "")
        series = market.get("series_ticker", "")
        close_ts = _resolve_close_ts(market) or 0

        if registry.is_blacklisted(series):
            verdict_rows.append((
                cycle_id, time.time(), ticker, series, close_ts,
                "NONE", "SKIP_SERIES_BLACKLIST", None, None, None, None))
            skips_count += 1
            continue

        try:
            result = _classify_market(client, market, governor, close_ts)
            verdict_rows.append((
                cycle_id, time.time(), ticker, series, close_ts,
                result.dclass, result.verdict, result.side,
                _json_or_none(result.evidence),
                result.fee_maker, result.fee_taker))

            if result.verdict == "SEED":
                seeds_count += 1
                _handle_seed(client, result, market, governor)
            elif result.verdict.startswith("SKIP"):
                skips_count += 1
            elif result.verdict == "ERR":
                errs_count += 1
        except Exception as e:
            log.warning(f"[SCANNER] Error classifying {ticker}: {e}")
            verdict_rows.append((
                cycle_id, time.time(), ticker, series, close_ts,
                "NONE", "ERR", None,
                _json_or_none({"error": str(e)}), None, None))
            errs_count += 1

    dstore.insert_verdicts_batch(verdict_rows)

    elapsed = time.time() - started
    req_used = governor.total_consumed - req_start
    notes = f"elapsed={elapsed:.0f}s"
    if elapsed > SCAN_CYCLE_TARGET_SEC * 2:
        notes += " SLOW"
        log.warning(f"[SCANNER] Sweep took {elapsed:.0f}s (target {SCAN_CYCLE_TARGET_SEC}s)")
        notify.send(f"🅳 ⚠ SCANNER: sweep took {elapsed:.0f}s "
                     f"(target {SCAN_CYCLE_TARGET_SEC}s)")

    dstore.finish_cycle(cycle_id, len(markets), seeds_count, skips_count,
                        errs_count, req_used, notes)
    log.info(f"[SCANNER] Sweep done: {len(markets)} mkts, "
             f"{seeds_count} seeds, {skips_count} skips, {errs_count} errs, "
             f"{req_used} reqs in {elapsed:.0f}s")
    return cycle_id


def _classify_market(client: kalshi.KalshiClient, market: dict,
                     governor: TokenBucket, close_ts: float) -> classify.VerdictRow:
    """Classify a single market with feeds + book fetch."""
    ticker = market.get("ticker", "")
    series = market.get("series_ticker", "")

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
        obs = feeds.observe_nws(station)
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
    """Record a shadow seed from a SEED verdict."""
    halt = dstore.get_state("halt_promotion")
    if halt == "1":
        log.warning(f"[SCANNER] SEED suppressed (halt_promotion): {verdict.market_ticker}")
        return

    ticker = verdict.market_ticker
    book = None
    governor.consume(1)
    try:
        book = kalshi.fetch_orderbook(client, ticker)
    except Exception:
        pass

    shadow.record_seed(verdict, book, market)


def _json_or_none(d: Optional[dict]) -> Optional[str]:
    if d is None:
        return None
    import json
    return json.dumps(d)
