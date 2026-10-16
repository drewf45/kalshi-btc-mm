"""WO-ROT-1 Chunk 2 — series discovery: the roster is found, not hardcoded.

At boot and hourly: page through every open market (`/markets?status=open`,
cursor-paged), group by series, keep the series whose markets run on a
15-minute or hourly cadence, and ADMIT each one only when its settlement
source, tick size, fee terms and underlying are read and logged. A series that
cannot be admitted is SKIPPED WITH ITS REASON — every one, on the Telegram
roster post.

Cadence. A series' own `frequency` field is preferred when it carries a value
this module recognizes; otherwise the cadence is inferred from the markets
themselves: each market's open-to-close duration, and the spacing between
distinct close times when more than one window is listed. UNPROVEN: the exact
`frequency` strings Kalshi uses for 15-minute series (the recognized set below
is a superset guess, logged per series so the first live roster proves it).

Field shapes (series `settlement_sources`, `fee_type`, `fee_multiplier`;
market `tick_size` / `price_level_structure`) are read tolerantly and every
value is logged as read — UNPROVEN until the first live roster prints them.

The same listing carries each market's touch (yes/no bid/ask in *_dollars), so
the outside loop (Chunk 3A) prices candidates from this scan and pulls full
orderbooks only for held markets and the shortlist.
"""

import logging
import os
import statistics
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Tuple

from . import config

log = logging.getLogger("relay.discovery")

# Series frequency strings this module maps to a cadence (UNPROVEN superset).
_FREQ_15M = {"fifteen_min", "fifteen_minute", "fifteen_minutes", "15min", "15m",
             "15_min", "quarter_hourly", "quarter_hour"}
_FREQ_1H = {"hourly", "hour", "1h", "one_hour", "60min"}
_FREQ_OTHER = {"daily", "weekly", "monthly", "annual", "yearly", "custom",
               "one_off", "quarterly"}

# Underlying (the group-cap key) from the series ticker. Longest key first so
# "KXETHD" never reads as "KXET…". A series with no known underlying is skipped:
# the group cap cannot bound what it cannot name.
_UNDERLYING_KEYS = sorted([
    ("BTC", "BTC"), ("ETH", "ETH"), ("SOL", "SOL"), ("XRP", "XRP"),
    ("DOGE", "DOGE"), ("BNB", "BNB"), ("HYPE", "HYPE"), ("SHIBA", "SHIB"),
    ("GOLD", "GOLD"), ("XAU", "GOLD"), ("SILVER", "SILVER"), ("XAG", "SILVER"),
    ("WTI", "WTI"), ("OIL", "WTI"), ("BRENT", "BRENT"), ("NATGAS", "NATGAS"),
    ("INX", "SPX"), ("NDQ", "NDX"), ("NASDAQ", "NDX"), ("DJI", "DJI"),
    ("EURUSD", "EURUSD"), ("USDJPY", "USDJPY"), ("GBPUSD", "GBPUSD"),
], key=lambda kv: -len(kv[0]))

SKIP_CADENCE = "SKIP_CADENCE"
SKIP_NO_SETTLE_SOURCE = "SKIP_NO_SETTLE_SOURCE"
SKIP_NO_UNDERLYING = "SKIP_NO_UNDERLYING"
SKIP_EXCLUDED = "SKIP_EXCLUDED_NO_SETTLE_SOURCE"
SKIP_SERIES_UNREADABLE = "SKIP_SERIES_UNREADABLE"


@dataclass
class SeriesInfo:
    series: str
    cadence: str                       # "15M" | "1H"
    cadence_source: str                # "frequency:<raw>" | "inferred:<secs>s"
    underlying: str
    settle_sources: List[str]
    tick: str                          # as read ("1c", "0.1c", "tapered", "unknown")
    fee_type: str
    fee_multiplier: Optional[str]
    fee_changes: Optional[int]         # count of pending fee changes; None = unreadable
    open_markets: int


@dataclass
class Roster:
    found: Dict[str, int] = field(default_factory=dict)          # series -> open markets
    admitted: Dict[str, SeriesInfo] = field(default_factory=dict)
    skipped: Dict[str, str] = field(default_factory=dict)        # series -> reason (+detail)
    markets: Dict[str, List[dict]] = field(default_factory=dict)  # series -> market records
    pages: int = 0
    truncated: bool = False
    ts: float = 0.0


# ── the scan ─────────────────────────────────────────────────────

def scan_open_markets(client, page_limit: int = None, max_pages: int = None
                      ) -> Tuple[List[dict], int, bool]:
    """Every open market, cursor-paged. Returns (markets, pages, truncated).
    One read per page under the read bucket. A scan cut off by max_pages is
    returned with truncated=True — the caller pages it, never silent."""
    page_limit = config.ROT_DISCOVERY_PAGE_LIMIT if page_limit is None else page_limit
    max_pages = config.ROT_DISCOVERY_MAX_PAGES if max_pages is None else max_pages
    out: List[dict] = []
    cursor = None
    pages = 0
    while True:
        params = {"status": "open", "limit": page_limit}
        if cursor:
            params["cursor"] = cursor
        resp = client.request("GET", "/markets", params=params)
        pages += 1
        if not isinstance(resp, dict):
            raise RuntimeError(f"/markets page {pages}: unexpected response {resp!r}"[:200])
        out.extend(m for m in resp.get("markets") or [] if isinstance(m, dict))
        cursor = resp.get("cursor")
        if not cursor:
            return out, pages, False
        if pages >= max_pages:
            return out, pages, True


def scan_series_markets(client, series_list: List[str], page_limit: int = None
                        ) -> Tuple[List[dict], int, bool]:
    """WO-ROT-ROSTER-1: targeted scan. When SERIES_LIST names the roster, query
    `/markets?series_ticker=X` per series instead of the blind all-open scan.

    The blind scan (scan_open_markets) pages EVERY open market — and Kalshi's
    open book is flooded with tens of thousands of sports/combinatorial markets
    (KXMVECROSSCATEGORY alone ~28.8k), so the 100-page budget is exhausted on
    noise BEFORE the crypto/metals series are ever reached: found 0 admitted 0,
    the desk goes blind. Querying by series_ticker returns only the markets we
    asked for, instantly, with no truncation. Returns (markets, reads, truncated)
    where truncated is always False (each series is paged to completion)."""
    page_limit = config.ROT_DISCOVERY_PAGE_LIMIT if page_limit is None else page_limit
    out: List[dict] = []
    reads = 0
    for series in series_list:
        cursor = None
        while True:
            params = {"series_ticker": series, "status": "open", "limit": page_limit}
            if cursor:
                params["cursor"] = cursor
            resp = client.request("GET", "/markets", params=params)
            reads += 1
            if not isinstance(resp, dict):
                raise RuntimeError(f"/markets[{series}] read {reads}: unexpected {resp!r}"[:200])
            out.extend(m for m in resp.get("markets") or [] if isinstance(m, dict))
            cursor = resp.get("cursor")
            if not cursor:
                break
    return out, reads, False


def series_of_market(m: dict) -> str:
    """The market's series: its own series_ticker when the record carries one,
    else the event ticker's prefix (Kalshi event tickers lead with the series,
    e.g. KXBTC15M-26OCT141015), else the market ticker's prefix."""
    s = m.get("series_ticker")
    if s:
        return str(s)
    for k in ("event_ticker", "ticker"):
        v = m.get(k)
        if v:
            return config.series_of(str(v))
    return ""


def group_by_series(markets: List[dict]) -> Dict[str, List[dict]]:
    out: Dict[str, List[dict]] = {}
    for m in markets:
        s = series_of_market(m)
        if s:
            out.setdefault(s, []).append(m)
    return out


# ── cadence ──────────────────────────────────────────────────────

def _ts(m: dict, keys) -> Optional[int]:
    from .venue import _parse_iso
    for k in keys:
        v = m.get(k)
        if isinstance(v, (int, float)):
            v = int(v)
            return v // 1000 if v > 10_000_000_000 else v
        if isinstance(v, str) and v:
            t = _parse_iso(v)
            if t:
                return t
    return None


def _match_cadence(secs: float) -> Optional[str]:
    for name, c in config.ROT_CADENCES.items():
        if abs(secs - c) <= config.ROT_CADENCE_TOLERANCE_S:
            return name
    return None


def infer_cadence(markets: List[dict]) -> Tuple[Optional[str], str]:
    """(cadence | None, how). Durations (open -> close) first, then the spacing
    between distinct close times. None when neither reads as 15M or 1H."""
    durations = []
    closes = set()
    for m in markets:
        c = _ts(m, ("close_time", "close_ts", "expiration_time"))
        o = _ts(m, ("open_time", "open_ts"))
        if c is not None:
            closes.add(c)
            if o is not None and c > o:
                durations.append(c - o)
    if durations:
        med = statistics.median(durations)
        cad = _match_cadence(med)
        if cad is not None:
            return cad, f"inferred:duration {med:.0f}s"
    if len(closes) >= 2:
        cs = sorted(closes)
        gap = statistics.median(b - a for a, b in zip(cs, cs[1:]))
        cad = _match_cadence(gap)
        if cad is not None:
            return cad, f"inferred:spacing {gap:.0f}s"
        return None, f"spacing {gap:.0f}s"
    if durations:
        return None, f"duration {statistics.median(durations):.0f}s"
    return None, "no open/close times"


def cadence_from_frequency(freq) -> Optional[str]:
    """'15M' | '1H' | 'OTHER' | None (unrecognized or absent)."""
    if not freq:
        return None
    f = str(freq).strip().lower().replace("-", "_").replace(" ", "_")
    if f in _FREQ_15M:
        return "15M"
    if f in _FREQ_1H:
        return "1H"
    if f in _FREQ_OTHER:
        return "OTHER"
    return None


# ── admission ────────────────────────────────────────────────────

def underlying_of(series: str) -> Optional[str]:
    s = series.upper()
    if s.startswith("KX"):
        s = s[2:]
    for key, und in _UNDERLYING_KEYS:
        if s.startswith(key):
            return und
    return None


def settle_sources_of(series_rec: dict) -> List[str]:
    """Names (or URLs) of the series' settlement sources; [] when none read."""
    out = []
    for k in ("settlement_sources", "settlement_source"):
        v = series_rec.get(k)
        if isinstance(v, dict):
            v = [v]
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
        elif isinstance(v, list):
            for src in v:
                if isinstance(src, dict):
                    name = src.get("name") or src.get("url")
                    if name:
                        out.append(str(name))
                elif isinstance(src, str) and src.strip():
                    out.append(src.strip())
    return out


def tick_of(markets: List[dict]) -> str:
    """Tick as read from the market records — 'unknown' when no field speaks."""
    for m in markets:
        pls = m.get("price_level_structure")
        if pls:
            return str(pls)
        t = m.get("tick_size")
        if t is not None:
            try:
                return f"{Decimal(str(t))}c"
            except InvalidOperation:
                return str(t)
    return "unknown"


def fetch_series(client, series: str) -> Optional[dict]:
    try:
        resp = client.request("GET", f"/series/{series}")
    except Exception as e:
        log.warning("[DISCOVERY] /series/%s unreadable: %s", series, e)
        return None
    if isinstance(resp, dict):
        rec = resp.get("series", resp)
        return rec if isinstance(rec, dict) else None
    return None


def admit(client, series: str, markets: List[dict], series_rec: Optional[dict],
          fee_changes_fn=None) -> Tuple[Optional[SeriesInfo], str]:
    """(SeriesInfo, "") or (None, reason). Every value logged as read."""
    if series in config.ROT_EXCLUDED_SERIES:
        return None, SKIP_EXCLUDED
    if series_rec is None:
        return None, SKIP_SERIES_UNREADABLE
    freq_raw = series_rec.get("frequency")
    cad = cadence_from_frequency(freq_raw)
    if cad == "OTHER":
        return None, f"{SKIP_CADENCE} (frequency {freq_raw})"
    if cad is not None:
        how = f"frequency:{freq_raw}"
    else:
        cad, how = infer_cadence(markets)
        if cad is None:
            return None, f"{SKIP_CADENCE} ({how}; frequency {freq_raw!r})"
    und = underlying_of(series)
    if und is None:
        return None, SKIP_NO_UNDERLYING
    sources = settle_sources_of(series_rec)
    if not sources:
        return None, SKIP_NO_SETTLE_SOURCE
    fee_changes = None
    if fee_changes_fn is not None:
        ch = fee_changes_fn(client, series)
        fee_changes = None if ch is None else len(ch)
    mult = series_rec.get("fee_multiplier")
    info = SeriesInfo(
        series=series, cadence=cad, cadence_source=how, underlying=und,
        settle_sources=sources, tick=tick_of(markets),
        fee_type=str(series_rec.get("fee_type") or "unknown"),
        fee_multiplier=None if mult is None else str(mult),
        fee_changes=fee_changes, open_markets=len(markets))
    log.info("[DISCOVERY] ADMIT %s cadence=%s (%s) underlying=%s settle=%s "
             "tick=%s fee_type=%s fee_mult=%s fee_changes=%s open=%d",
             series, cad, how, und, sources, info.tick, info.fee_type,
             info.fee_multiplier, fee_changes, len(markets))
    return info, ""


def discover(client, now: Optional[float] = None, fee_changes_fn=None) -> Roster:
    """One discovery pass. Series records are fetched only for series whose
    markets could plausibly be 15M/1H (a cheap pre-filter on the scan) — the
    full scan of daily/weekly series costs no extra reads."""
    if fee_changes_fn is None:
        from .venue import get_series_fee_changes as fee_changes_fn
    roster = Roster(ts=time.time() if now is None else now)
    # WO-ROT-ROSTER-1: a named SERIES_LIST takes the targeted path — query only
    # those series, bypassing the noise-flooded blind scan that was returning
    # admitted 0. Without SERIES_LIST, the blind discovery scan is unchanged.
    env = os.getenv("SERIES_LIST")
    series_list = [s.strip() for s in env.split(",") if s.strip()] if env else []
    if series_list:
        markets, roster.pages, roster.truncated = scan_series_markets(client, series_list)
    else:
        markets, roster.pages, roster.truncated = scan_open_markets(client)
    groups = group_by_series(markets)
    for series in sorted(groups):
        mk = groups[series]
        roster.found[series] = len(mk)
        roster.markets[series] = mk
        if series in config.ROT_EXCLUDED_SERIES:
            roster.skipped[series] = SKIP_EXCLUDED
            continue
        pre, how = infer_cadence(mk)
        # a series whose listed windows clearly run longer than an hour + the
        # tolerance is daily/weekly: skip without spending a /series read
        if pre is None and how.startswith(("duration", "spacing")):
            secs = float(how.split()[1].rstrip("s"))
            if secs > config.ROT_CADENCES["1H"] + config.ROT_CADENCE_TOLERANCE_S:
                roster.skipped[series] = f"{SKIP_CADENCE} ({how})"
                continue
        info, why = admit(client, series, mk, fetch_series(client, series),
                          fee_changes_fn=fee_changes_fn)
        if info is None:
            roster.skipped[series] = why
        else:
            roster.admitted[series] = info
    if roster.truncated:
        from . import failures
        failures.fail("SCAN_TRUNCATED",
                      f"/markets scan stopped at {roster.pages} pages "
                      f"({len(markets)} markets) — roster may be incomplete",
                      pages=roster.pages)
    return roster


# ── roster install + reporting ───────────────────────────────────

def install(roster: Roster) -> List[str]:
    """Make the discovered roster the desk's roster. SERIES_LIST in the
    environment still overrides (the emergency lever); then only the env list's
    series that were admitted are installed, and the rest are named."""
    import os
    admitted = list(roster.admitted)
    env = os.getenv("SERIES_LIST")
    if env:
        wanted = [s.strip() for s in env.split(",") if s.strip()]
        admitted = [s for s in wanted if s in roster.admitted]
        source = "env"
    else:
        source = "discovered"
    config.SERIES[:] = admitted
    for s in admitted:
        config.SERIES_MODE[s] = "LIVE"   # per-series mode; the global kill still rules
    config.ROSTER_SOURCE = source
    config.SERIES_INFO.clear()
    config.SERIES_INFO.update({s: roster.admitted[s] for s in admitted})
    return admitted


def roster_lines(roster: Roster, installed: Optional[List[str]] = None) -> List[str]:
    """The Telegram / boot roster: found, admitted, skipped and why."""
    lines = [f"🗂 ROSTER (WO-ROT-1 C2): found {len(roster.found)} series "
             f"({sum(roster.found.values())} open markets, {roster.pages} page(s)"
             + (", TRUNCATED" if roster.truncated else "") + ") · "
             f"admitted {len(roster.admitted)} · skipped {len(roster.skipped)}"]
    for s, i in roster.admitted.items():
        mark = "" if installed is None or s in installed else " [not in SERIES_LIST]"
        lines.append(f"  ✅ {s} {i.cadence} {i.underlying} · settle "
                     f"{', '.join(i.settle_sources)} · tick {i.tick} · fee "
                     f"{i.fee_type} x{i.fee_multiplier} · open {i.open_markets}{mark}")
    by_reason: Dict[str, List[str]] = {}
    for s, why in roster.skipped.items():
        by_reason.setdefault(why.split(" ", 1)[0], []).append(s)
    for reason, ss in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        shown = ", ".join(ss[:12]) + (f" +{len(ss) - 12} more" if len(ss) > 12 else "")
        lines.append(f"  ⏭ {reason} ×{len(ss)}: {shown}")
    return lines
