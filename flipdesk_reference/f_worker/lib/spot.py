# f_worker/lib/spot.py
# BORROWED from bot.py: Coinbase spot fetcher + candle-based realized sigma. This is
# pricebrain's live feed. No crypto imports here, so it is safe for the testable core
# and for the offline crossing study (Deliverable 0) which reuses the sigma math.

import math
import time
from collections import deque
from typing import List, Optional

import requests

COINBASE_SPOT_URL = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
COINBASE_CANDLES_URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"


def fetch_btc_spot_usd(session: requests.Session, timeout: float = 5.0) -> Optional[float]:
    try:
        r = session.get(COINBASE_SPOT_URL, timeout=timeout)
        r.raise_for_status()
        amt = r.json().get("data", {}).get("amount")
        return float(amt) if amt is not None else None
    except Exception:
        return None


def fetch_coinbase_candles(session: requests.Session, granularity: int,
                           timeout: float = 5.0) -> Optional[List[List[float]]]:
    try:
        r = session.get(COINBASE_CANDLES_URL, params={"granularity": int(granularity)}, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) and data else None
    except Exception:
        return None


def realized_sigma_usd_per_sqrt_sec(session: requests.Session, granularity_sec: int = 60,
                                    lookback: int = 10) -> Optional[float]:
    """Per-sqrt-second realized volatility from recent 1m candle close deltas.
    Borrowed from bot.py:realized_sigma_usd_per_sqrt_sec."""
    candles = fetch_coinbase_candles(session, granularity_sec)
    if not candles or len(candles) < 3:
        return None
    take = sorted(candles[: max(3, int(lookback))], key=lambda x: float(x[0]))
    closes: List[float] = []
    for c in take:
        try:
            closes.append(float(c[4]))
        except Exception:
            continue
    if len(closes) < 3:
        return None
    diffs = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    if len(diffs) < 2:
        return None
    mean = sum(diffs) / len(diffs)
    var = sum((x - mean) ** 2 for x in diffs) / max(1, (len(diffs) - 1))
    sd_per_min = math.sqrt(max(0.0, var))
    return float(sd_per_min / math.sqrt(60.0))


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


class SigmaCache:
    """Cached realized sigma — HONEST edition (F2.2).

    NO fabricated default: a never-fetched or feed-dead sigma is None, so the BLIND
    gate becomes reachable instead of dead code hiding behind a fictional 12.0. Clamps
    are TAGGED: `last_raw` keeps the pre-clamp measurement and `last_clamped` flags when
    the floor/ceil moved it, so a floor-hugging σ=5.0 can never again be mistaken for a
    real reading."""

    def __init__(self, floor: float = 6.0, ceil: float = 40.0, refresh_sec: float = 5.0,
                 min_samples: int = 5, min_span_sec: float = 15.0):
        self.floor = floor
        self.ceil = ceil
        self.refresh_sec = refresh_sec
        self.min_samples = min_samples
        self.min_span_sec = min_span_sec
        self._val: Optional[float] = None
        self._ts: Optional[float] = None
        self.last_raw: Optional[float] = None
        self.last_clamped: bool = False
        self.last_source: str = "none"
        self._samples: "deque" = deque(maxlen=180)   # (ts, spot) — self-sampled (F5.5)

    def add_sample(self, price: Optional[float], now: Optional[float] = None) -> None:
        """Feed a live spot tick. The rolling sigma is computed from THESE, so the desk
        stops depending on the candles API — its own 1s-5s spot GETs carry it (F5.5)."""
        if price is None:
            return
        self._samples.append((time.time() if now is None else now, float(price)))

    def _sigma_from_samples(self) -> Optional[float]:
        s = list(self._samples)
        if len(s) < self.min_samples or (s[-1][0] - s[0][0]) < self.min_span_sec:
            return None
        acc = []
        for i in range(1, len(s)):
            dt = s[i][0] - s[i - 1][0]
            if dt <= 0:
                continue
            r = s[i][1] - s[i - 1][1]
            acc.append((r * r) / dt)               # per-sqrt-second variance contribution
        if not acc:
            return None
        return math.sqrt(sum(acc) / len(acc))

    def get(self, session: requests.Session, now: Optional[float] = None) -> Optional[float]:
        now = time.time() if now is None else now
        # 1) our own spot samples (candles-independent). 2) candle warmup (throttled).
        rs = self._sigma_from_samples()
        src = "spot_samples"
        if rs is None:
            if self._ts is None or (now - self._ts) >= self.refresh_sec:
                rs = realized_sigma_usd_per_sqrt_sec(session)
                self._ts = now
            else:
                rs = self.last_raw
            src = "candles"
        self.last_raw = rs
        self.last_source = src if rs is not None else "none"
        if rs is None or rs <= 0:
            # spot AND candles both blind — do NOT fabricate. None => BLIND.
            self._val = None
            self.last_clamped = False
        else:
            clamped = float(max(self.floor, min(self.ceil, rs)))
            self.last_clamped = clamped != rs
            self._val = clamped
        return self._val
