# f_worker/lib/spot.py
# BORROWED from bot.py: Coinbase spot fetcher + candle-based realized sigma. This is
# pricebrain's live feed. No crypto imports here, so it is safe for the testable core
# and for the offline crossing study (Deliverable 0) which reuses the sigma math.

import math
import time
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
    """Cached sigma with floor/ceil clamps (borrowed cadence from bot.py:get_sigma_cached)."""

    def __init__(self, floor: float = 6.0, ceil: float = 40.0, refresh_sec: float = 5.0,
                 default: float = 12.0):
        self.floor = floor
        self.ceil = ceil
        self.refresh_sec = refresh_sec
        self._val = default
        self._ts = 0.0

    def get(self, session: requests.Session, now: Optional[float] = None) -> float:
        now = time.time() if now is None else now
        if (now - self._ts) < self.refresh_sec:
            return float(self._val)
        rs = realized_sigma_usd_per_sqrt_sec(session)
        self._val = float(self._val) if (rs is None or rs <= 0) else float(max(self.floor, min(self.ceil, rs)))
        self._ts = now
        return float(self._val)
