import os
import time
import json
import uuid
import base64
import random
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional, Tuple, List

import requests

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

try:
    # Python 3.9+
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None  # type: ignore


# ==========================================================
# Helpers
# ==========================================================
def _env(name: str, default: str = "") -> str:
    v = os.getenv(name, default)
    return v.strip() if isinstance(v, str) else default


def _env_bool(name: str, default: str = "false") -> bool:
    return _env(name, default).lower() in ("1", "true", "yes", "y", "on")


def _env_int(name: str, default: str) -> int:
    return int(_env(name, default))


def _env_float(name: str, default: str) -> float:
    return float(_env(name, default))


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(msg: str) -> None:
    print(f"[{utc_iso()}] {msg}", flush=True)


# ==========================================================
# CONFIG (Render env vars)
# ==========================================================
BASE_URL = _env("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2").rstrip("/")

# Key id
KALSHI_API_KEY_ID = _env("KALSHI_API_KEY_ID", "") or _env("KALSHI_API_KEYID", "")

# Private key (recommended b64 on mobile)
# - KALSHI_PRIVATE_KEY_PEM_B64 : base64(PEM bytes)
# - KALSHI_PRIVATE_KEY_PEM     : PEM text (multiline OR contains literal \n)
KALSHI_PRIVATE_KEY_PEM_B64 = _env("KALSHI_PRIVATE_KEY_PEM_B64", "")
KALSHI_PRIVATE_KEY_PEM = _env("KALSHI_PRIVATE_KEY_PEM", "")

# Trading cadence
POLL_SECONDS = _env_int("POLL_SECONDS", "60")
MARKET_REFRESH_SECONDS = _env_int("MARKET_REFRESH_SECONDS", "30")

# Market selection
# If set, bot trades ONLY this ticker; else it auto-computes current 15m window ticker.
MARKET_TICKER_OVERRIDE = _env("MARKET_TICKER", "")
SERIES_PREFIX = _env("SERIES_PREFIX", "KXBTC15M")

# Choose which 15m market to target (end of the 15-min window)
USE_NEXT_QUARTER_HOUR_END = _env_bool("USE_NEXT_QUARTER_HOUR_END", "true")

# Risk / sizing
BET_DOLLARS = _env_float("BET_DOLLARS", "1.0")              # $1 test wagers
MIN_CONTRACTS = _env_int("MIN_CONTRACTS", "1")              # at least 1 contract
MAX_DAILY_SPEND = _env_float("MAX_DAILY_SPEND", "25.0")     # guardrail
MAX_TRADES_PER_DAY = _env_int("MAX_TRADES_PER_DAY", "50")   # guardrail

# Price filters (avoid ultra-thin / extreme prices)
MIN_PRICE_CENTS = _env_int("MIN_PRICE_CENTS", "5")          # don’t buy < 5c
MAX_PRICE_CENTS = _env_int("MAX_PRICE_CENTS", "95")         # don’t buy > 95c

# Market open trigger behavior
WAIT_FOR_OPEN = _env_bool("WAIT_FOR_OPEN", "true")
MIN_OPEN_DELAY_SECONDS = _env_int("MIN_OPEN_DELAY_SECONDS", "3")
MAX_OPEN_DELAY_SECONDS = _env_int("MAX_OPEN_DELAY_SECONDS", "15")

# Dry run
DRY_RUN = _env_bool("DRY_RUN", "false")

# One-time startup test bet
STARTUP_TEST_BET = _env_bool("STARTUP_TEST_BET", "false")
STARTUP_TEST_MARKET = _env("STARTUP_TEST_MARKET", "")  # optional; if empty uses computed ticker

# Stop after N successful wagers (useful for testing)
STOP_AFTER_WAGERS = _env_int("STOP_AFTER_WAGERS", "0")

# Timezone (we want proper date/time so we don’t trade “yesterday”)
NY_TZ = "America/New_York"


# ==========================================================
# Time / ticker generation (NO seconds; matches Kalshi)
# Format example: KXBTC15M-26JAN172000
# ==========================================================
def now_et() -> datetime:
    if ZoneInfo is not None:
        return datetime.now(ZoneInfo(NY_TZ))
    # fallback (not DST-aware): treat ET as UTC-5
    return (datetime.now(timezone.utc) + timedelta(hours=-5)).replace(tzinfo=None)


def next_quarter_hour_end(dt: datetime) -> datetime:
    # dt is ET-aware (zoneinfo) or naive fallback
    minute = dt.minute
    next_q = ((minute // 15) + 1) * 15
    if next_q == 60:
        return dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return dt.replace(minute=next_q, second=0, microsecond=0)


def current_quarter_hour_end(dt: datetime) -> datetime:
    minute = dt.minute
    this_q = (minute // 15) * 15
    return dt.replace(minute=this_q, second=0, microsecond=0)


def format_btc15m_ticker(dt_et: datetime) -> str:
    yy = dt_et.strftime("%y")
    mon = dt_et.strftime("%b").upper()
    dd = dt_et.strftime("%d")
    hhmm = dt_et.strftime("%H%M")  # <- NO seconds
    return f"{SERIES_PREFIX}-{yy}{mon}{dd}{hhmm}"


def compute_target_market_ticker() -> str:
    if MARKET_TICKER_OVERRIDE:
        return MARKET_TICKER_OVERRIDE

    dt = now_et()
    dt_target = next_quarter_hour_end(dt) if USE_NEXT_QUARTER_HOUR_END else current_quarter_hour_end(dt)
    return format_btc15m_ticker(dt_target)


# ==========================================================
# Private key loading (robust for Render)
# ==========================================================
def load_private_key_from_env():
    if KALSHI_PRIVATE_KEY_PEM_B64:
        try:
            pem_bytes = base64.b64decode(KALSHI_PRIVATE_KEY_PEM_B64.encode("utf-8"))
            return serialization.load_pem_private_key(pem_bytes, password=None)
        except Exception as e:
            raise RuntimeError(f"Invalid KALSHI_PRIVATE_KEY_PEM_B64 (base64/PEM parse failed): {e}")

    if KALSHI_PRIVATE_KEY_PEM:
        try:
            pem_bytes = KALSHI_PRIVATE_KEY_PEM.replace("\\n", "\n").encode("utf-8")
            return serialization.load_pem_private_key(pem_bytes, password=None)
        except Exception as e:
            raise RuntimeError(f"Invalid KALSHI_PRIVATE_KEY_PEM (PEM parse failed): {e}")

    raise RuntimeError("Missing private key. Set KALSHI_PRIVATE_KEY_PEM_B64 (recommended) or KALSHI_PRIVATE_KEY_PEM")


# ==========================================================
# Kalshi signing (RSA-PSS)
# ==========================================================
def make_signature(private_key, timestamp_ms: str, method: str, path_with_query: str, body: str) -> str:
    payload = (timestamp_ms + method.upper() + path_with_query + body).encode("utf-8")
    sig = private_key.sign(
        payload,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("utf-8")


@dataclass
class KalshiClient:
    base_url: str
    key_id: str

    def __post_init__(self):
        if not self.key_id:
            raise RuntimeError("Missing KALSHI_API_KEY_ID env var.")

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "kalshi-btc-mm/1.0"})
        self.private_key = load_private_key_from_env()

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        timeout: int = 20,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        body = ""
        if json_body is not None:
            body = json.dumps(json_body, separators=(",", ":"), ensure_ascii=False)

        path_with_query = path
        if params:
            from urllib.parse import urlencode
            qs = urlencode(params)
            path_with_query = f"{path}?{qs}"

        ts = str(int(time.time() * 1000))
        sig = make_signature(self.private_key, ts, method, path_with_query, body)

        headers = {
            "Content-Type": "application/json",
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

        resp = self.session.request(
            method=method,
            url=url,
            params=params,
            data=body if body else None,
            headers=headers,
            timeout=timeout,
        )

        # Better errors
        if resp.status_code == 429:
            raise RuntimeError("429_RATE_LIMIT")

        if resp.status_code == 404:
            # Market not found (very common if ticker is wrong / stale)
            return {"_http_404": True, "_text": resp.text}

        resp.raise_for_status()
        return resp.json()

    def get_orderbook(self, market_ticker: str, depth: int = 1) -> Dict[str, Any]:
        # GET /markets/{ticker}/orderbook
        return self._request("