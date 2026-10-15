"""WO-ROT-1 Chunk 6 — spot per underlying, and the soft-stop confirm thresholds.

Sources, in order of trust (the first that answers wins for that underlying):

  1. KALSHI SETTLE INDEXES (LENS). Kalshi's authenticated WebSocket lists
     `cfbenchmarks_value` (and `_5hz`) and `pyth_value` channels — the very
     indexes the markets settle on. Payload shape and coverage are UNPROVEN:
     the parser below is tolerant (it looks for a symbol-ish and a value-ish
     field), logs every shape it has not seen once, and the boot tape / hourly
     line say which underlyings actually appeared. Prove on Kalshi demo first.
  2. COINBASE public spot for crypto (venue.get_btc_spot generalized: same 5 s
     cache, fail to None, bounded stale serve).
  3. PYTH HERMES for metals / oil / FX, Bearer key required since 2026-08-26
     (PYTH_API_KEY). Feeds Metal.XAU/USD, Metal.XAG/USD, front-month WTI —
     resolved to feed ids through Hermes' own /v2/price_feeds search (UNPROVEN
     until the first keyed read). Kalshi's named WTI feed reportedly stopped
     publishing 2026-03-30: every cross-source mismatch is logged.

NO INDEX FEED, NO LIVE. An underlying with no working source runs SHADOW-only
(its series' trips are rehearsed, never placed) and the boot tape says so.

ROT_SPOT_CONFIRM. BTC: 40 points (the Record). Every other underlying: 2x the
median 1-minute move over the last hour, recomputed hourly and logged; None
(soft stop cannot confirm; the hard stop still protects) until there is at
least ROT_SPOT_CONFIRM_MIN_HISTORY_S of samples. UNPROVEN until the stop log
scores them.
"""

import logging
import statistics
import threading
import time
from collections import deque
from typing import Callable, Dict, List, Optional, Tuple

from . import config

log = logging.getLogger("relay.spot")

CRYPTO = {"BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE", "SHIB"}
PYTH_SYMBOLS = {"GOLD": "Metal.XAU/USD", "SILVER": "Metal.XAG/USD",
                "WTI": "Commodities.WTI"}   # WTI front-month symbol UNPROVEN
# index-channel symbol fragments -> underlying (longest first when matching)
_INDEX_HINTS = sorted([
    ("BRTI", "BTC"), ("XBT", "BTC"), ("BTC", "BTC"), ("ETH", "ETH"), ("SOL", "SOL"),
    ("XRP", "XRP"), ("DOGE", "DOGE"), ("BNB", "BNB"), ("HYPE", "HYPE"),
    ("XAU", "GOLD"), ("GOLD", "GOLD"), ("XAG", "SILVER"), ("SILVER", "SILVER"),
    ("WTI", "WTI"), ("EURUSD", "EURUSD"), ("USDJPY", "USDJPY"),
], key=lambda kv: -len(kv[0]))

SOURCE_RANK = {"kalshi_index": 0, "coinbase": 1, "pyth": 1}


class SpotBook:
    """Per-underlying samples (ts, price, source), thread-safe."""

    def __init__(self, history_s: float = 3900.0):
        self.history_s = history_s
        self._s: Dict[str, deque] = {}
        self._lock = threading.Lock()
        self._confirm: Dict[str, Tuple[float, Optional[float]]] = {}  # und -> (computed_ts, value)
        self.sources_seen: Dict[str, set] = {}
        self.mismatch_logged: set = set()

    def record(self, underlying: str, price: Optional[float], source: str,
               ts: Optional[float] = None) -> None:
        if price is None:
            return
        try:
            price = float(price)
        except (TypeError, ValueError):
            return
        if not price > 0:
            return
        ts = time.time() if ts is None else ts
        with self._lock:
            dq = self._s.setdefault(underlying, deque())
            dq.append((ts, price, source))
            while dq and dq[0][0] < ts - self.history_s:
                dq.popleft()
            self.sources_seen.setdefault(underlying, set()).add(source)
        self._check_mismatch(underlying, price, source, ts)

    def _check_mismatch(self, und: str, price: float, source: str, ts: float) -> None:
        """Log (once per pair per hour) when two sources disagree by more than
        ROT_SPOT_MISMATCH_PCT within 10 s of each other."""
        with self._lock:
            others = [(t, p, s) for t, p, s in self._s.get(und, ())
                      if s != source and abs(t - ts) <= 10]
        for _t, p, s in others[-1:]:
            if abs(p - price) / max(p, price) > config.ROT_SPOT_MISMATCH_PCT:
                key = (und, tuple(sorted((s, source))), int(ts // 3600))
                if key not in self.mismatch_logged:
                    self.mismatch_logged.add(key)
                    log.warning("[SPOT_MISMATCH] %s %s=%.6g vs %s=%.6g", und, source,
                                price, s, p)

    def latest(self, underlying: str, now: Optional[float] = None,
               max_age_s: Optional[float] = None) -> Optional[float]:
        """The freshest price within max_age, preferring the settle index."""
        now = time.time() if now is None else now
        max_age = config.ROT_SPOT_MAX_AGE_S if max_age_s is None else max_age_s
        with self._lock:
            dq = self._s.get(underlying)
            if not dq:
                return None
            fresh = [(t, p, s) for t, p, s in dq if now - t <= max_age]
        if not fresh:
            return None
        best = min(fresh, key=lambda x: (SOURCE_RANK.get(x[2], 9), -x[0]))
        return best[1]

    def live(self, underlying: str, now: Optional[float] = None) -> bool:
        return self.latest(underlying, now, config.ROT_SPOT_LIVE_MAX_AGE_S) is not None

    def minute_moves(self, underlying: str, now: Optional[float] = None) -> List[float]:
        """|p(t) - p(t - 60 s)| over the last hour, from the samples."""
        now = time.time() if now is None else now
        with self._lock:
            pts = [(t, p) for t, p, _s in self._s.get(underlying, ()) if t >= now - 3600]
        moves = []
        j = 0
        for i, (t, p) in enumerate(pts):
            while j < i and pts[j][0] < t - 60:
                j += 1
            if j < i and t - pts[j][0] >= 55:      # a sample ~60 s earlier
                moves.append(abs(p - pts[j][1]))
        return moves

    def history_span(self, underlying: str) -> float:
        with self._lock:
            dq = self._s.get(underlying)
            return (dq[-1][0] - dq[0][0]) if dq and len(dq) > 1 else 0.0

    def confirm(self, underlying: str, now: Optional[float] = None) -> Optional[float]:
        """ROT_SPOT_CONFIRM for this underlying (points / price units)."""
        if underlying == "BTC":
            return float(config.ROT_SPOT_CONFIRM_BTC_PTS)
        now = time.time() if now is None else now
        cached = self._confirm.get(underlying)
        if cached is not None and now - cached[0] < config.ROT_SPOT_CONFIRM_RECOMPUTE_S:
            return cached[1]
        val = None
        if self.history_span(underlying) >= config.ROT_SPOT_CONFIRM_MIN_HISTORY_S:
            moves = self.minute_moves(underlying, now)
            if moves:
                val = 2.0 * statistics.median(moves)
                if val <= 0:
                    val = None
        self._confirm[underlying] = (now, val)
        log.info("[SPOT_CONFIRM] %s = %s (2x median 1-min move, %d moves, UNPROVEN)",
                 underlying, "n/a" if val is None else f"{val:.6g}",
                 len(self.minute_moves(underlying, now)))
        return val

    def status_lines(self, underlyings, now: Optional[float] = None) -> List[str]:
        out = []
        for u in sorted(underlyings):
            p = self.latest(u, now)
            srcs = ",".join(sorted(self.sources_seen.get(u, ()))) or "none"
            c = self.confirm(u, now)
            out.append(f"  {u}: {'LIVE' if self.live(u, now) else 'NO FEED -> SHADOW-ONLY'}"
                       f" · last {p if p is not None else 'n/a'} · sources {srcs}"
                       f" · confirm {'n/a' if c is None else f'{c:.6g}'}")
        return out


# ── source 1: Kalshi settle-index channels (UNPROVEN payload) ────

INDEX_CHANNELS = ("cfbenchmarks_value", "pyth_value")
_shapes_logged: set = set()


def index_subscribe_cmds(start_id: int = 1000) -> List[dict]:
    """One subscribe command per channel (a stranger channel kills only itself).
    No market_tickers: the index channels are not per-market (UNPROVEN)."""
    return [{"id": start_id + i, "cmd": "subscribe", "params": {"channels": [ch]}}
            for i, ch in enumerate(INDEX_CHANNELS)]


def underlying_from_symbol(sym: str) -> Optional[str]:
    s = str(sym).upper()
    for frag, und in _INDEX_HINTS:
        if frag in s:
            return und
    return None


def parse_index_message(msg: dict) -> List[Tuple[str, float, Optional[float], str]]:
    """[(underlying, price, ts, symbol)] from one WS frame, or [] when the
    frame is not an index value. Tolerant: the symbol is the first of
    index/symbol/ticker/name/feed/id; the value the first of
    value/price/index_value/v. A frame type containing 'cfbenchmarks' or
    'pyth' with no readable pair logs its shape ONCE (never silent)."""
    if not isinstance(msg, dict):
        return []
    typ = str(msg.get("type") or msg.get("channel") or "")
    if "cfbenchmarks" not in typ and "pyth" not in typ:
        return []
    body = msg.get("msg") if isinstance(msg.get("msg"), dict) else msg
    items = body.get("values") if isinstance(body.get("values"), list) else [body]
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        sym = next((it[k] for k in ("index", "symbol", "ticker", "name", "feed",
                                    "index_ticker", "price_feed_id", "id") if it.get(k)), None)
        val = next((it[k] for k in ("value", "price", "index_value", "v")
                    if it.get(k) is not None), None)
        ts = next((it[k] for k in ("ts", "timestamp", "time") if it.get(k) is not None), None)
        und = underlying_from_symbol(sym) if sym is not None else None
        try:
            price = float(val) if val is not None else None
        except (TypeError, ValueError):
            price = None
        if und is None or price is None:
            shape = (typ, tuple(sorted(it.keys())))
            if shape not in _shapes_logged:
                _shapes_logged.add(shape)
                log.warning("[INDEX_WS] unrecognized %s frame shape: %s", typ, it)
            continue
        if isinstance(ts, (int, float)) and ts > 10_000_000_000:
            ts = ts / 1000.0
        out.append((und, price, ts if isinstance(ts, (int, float)) else None, str(sym)))
    return out


async def run_index_ws(spot: SpotBook, stop, connect: Callable, headers_fn: Callable,
                       url: str) -> None:
    """Subscribe to the index channels and feed the spot book until stop.
    A rejected channel (error frame) is logged and the rest keep flowing."""
    import json
    async with connect(url, additional_headers=headers_fn()) as ws:
        for cmd in index_subscribe_cmds():
            await ws.send(json.dumps(cmd))
        while not stop.is_set():
            raw = await ws.recv()
            try:
                msg = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if msg.get("type") == "error":
                log.warning("[INDEX_WS] channel error: %s", msg)
                continue
            for und, price, ts, _sym in parse_index_message(msg):
                spot.record(und, price, "kalshi_index", ts)


# ── source 2: Coinbase (crypto) ──────────────────────────────────

_cb_cache: Dict[str, Tuple[float, float]] = {}   # asset -> (ts, price)


def coinbase_spot(asset: str, http_get=None, now: Optional[float] = None) -> Optional[float]:
    """venue.get_btc_spot generalized: 5 s cache, bounded stale serve
    (SPOT_MAX_STALE_SEC), else None. Never an unbounded stale price."""
    from .venue import SPOT_MAX_STALE_SEC
    now = time.time() if now is None else now
    c = _cb_cache.get(asset)
    if c is not None and now - c[0] < 5:
        return c[1]
    if http_get is None:
        import requests
        http_get = requests.get
    try:
        resp = http_get(f"https://api.coinbase.com/v2/prices/{asset}-USD/spot", timeout=5)
        resp.raise_for_status()
        price = float(resp.json()["data"]["amount"])
        _cb_cache[asset] = (now, price)
        return price
    except Exception as e:
        if c is not None and now - c[0] < SPOT_MAX_STALE_SEC:
            log.warning("[SPOT] Coinbase %s failed: %s — cache age %.0fs", asset, e, now - c[0])
            return c[1]
        log.warning("[SPOT] Coinbase %s failed: %s -> None", asset, e)
        return None


# ── source 3: Pyth Hermes (metals / oil / FX; key required) ──────

HERMES = "https://pyth.dourolabs.app/hermes"
_pyth_ids: Dict[str, Optional[str]] = {}


def pyth_feed_id(symbol: str, key: str, http_get) -> Optional[str]:
    """Resolve a Pyth symbol (e.g. Metal.XAU/USD) to its feed id via Hermes'
    /v2/price_feeds search; cached. UNPROVEN until the first keyed read."""
    if symbol in _pyth_ids:
        return _pyth_ids[symbol]
    try:
        q = symbol.split(".", 1)[-1].split("/")[0]
        r = http_get(f"{HERMES}/v2/price_feeds", params={"query": q},
                     headers={"Authorization": f"Bearer {key}"}, timeout=5)
        r.raise_for_status()
        fid = None
        for f in r.json():
            attrs = f.get("attributes", {})
            if attrs.get("symbol") == symbol:
                fid = f.get("id")
                break
        _pyth_ids[symbol] = fid
        if fid is None:
            log.warning("[PYTH] no feed id for %s", symbol)
        return fid
    except Exception as e:
        log.warning("[PYTH] feed search %s failed: %s", symbol, e)
        return None


def pyth_prices(underlyings, key: Optional[str], http_get=None) -> Dict[str, float]:
    """{underlying: price} from one Hermes latest-price read. No key -> {}
    (those underlyings run shadow-only unless Kalshi's pyth_value carries them)."""
    if not key:
        return {}
    if http_get is None:
        import requests
        http_get = requests.get
    ids = {}
    for u in underlyings:
        sym = PYTH_SYMBOLS.get(u)
        if sym:
            fid = pyth_feed_id(sym, key, http_get)
            if fid:
                ids[fid] = u
    if not ids:
        return {}
    try:
        r = http_get(f"{HERMES}/v2/updates/price/latest",
                     params=[("ids[]", i) for i in ids],
                     headers={"Authorization": f"Bearer {key}"}, timeout=5)
        r.raise_for_status()
        out = {}
        for p in r.json().get("parsed", []):
            fid = p.get("id")
            pr = p.get("price", {})
            if fid in ids and "price" in pr and "expo" in pr:
                out[ids[fid]] = float(pr["price"]) * (10 ** int(pr["expo"]))
            elif fid and ("0x" + fid) in ids and "price" in pr:
                out[ids["0x" + fid]] = float(pr["price"]) * (10 ** int(pr["expo"]))
        return out
    except Exception as e:
        log.warning("[PYTH] latest read failed: %s", e)
        return {}


def poll_once(spot: SpotBook, underlyings, http_get=None, now=None) -> None:
    """One fallback poll: Coinbase for crypto the index has not covered
    freshly, Pyth for the rest (when keyed)."""
    now = time.time() if now is None else now
    pyth_wanted = []
    for u in underlyings:
        if spot.latest(u, now, max_age_s=config.ROT_SPOT_POLL_S * 2) is not None and \
                "kalshi_index" in spot.sources_seen.get(u, ()):
            continue    # the settle index is speaking: no proxy needed
        if u in CRYPTO:
            spot.record(u, coinbase_spot(u, http_get=http_get, now=now), "coinbase", now)
        elif u in PYTH_SYMBOLS:
            pyth_wanted.append(u)
    if pyth_wanted:
        for u, p in pyth_prices(pyth_wanted, config.PYTH_API_KEY, http_get=http_get).items():
            spot.record(u, p, "pyth", now)
