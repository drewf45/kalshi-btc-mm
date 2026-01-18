import os
import time
import json
import uuid
import base64
import re
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional, Tuple

import requests

# --- Requires cryptography ---
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


# ==========================================================
# ENV HELPERS + PRIVATE KEY LOADING
# ==========================================================
def _env(name: str, default: str = "") -> str:
    v = os.getenv(name, default)
    return v.strip() if isinstance(v, str) else default


def load_private_key_from_env():
    """
    Preferred on Render/mobile: KALSHI_PRIVATE_KEY_PEM_B64 = base64(PEM bytes)
    Fallback:               KALSHI_PRIVATE_KEY_PEM     = PEM text (multiline or with \\n)
    """
    b64 = _env("KALSHI_PRIVATE_KEY_PEM_B64", "")
    if b64:
        try:
            pem_bytes = base64.b64decode(b64.encode("utf-8"))
            return serialization.load_pem_private_key(pem_bytes, password=None)
        except Exception as e:
            raise RuntimeError(f"Invalid KALSHI_PRIVATE_KEY_PEM_B64 (base64/PEM parse failed): {e}")

    pem_text = _env("KALSHI_PRIVATE_KEY_PEM", "")
    if pem_text:
        try:
            pem_bytes = pem_text.replace("\\n", "\n").encode("utf-8")
            return serialization.load_pem_private_key(pem_bytes, password=None)
        except Exception as e:
            raise RuntimeError(f"Invalid KALSHI_PRIVATE_KEY_PEM (PEM parse failed): {e}")

    raise RuntimeError(
        "Missing private key. Set KALSHI_PRIVATE_KEY_PEM_B64 (recommended) or KALSHI_PRIVATE_KEY_PEM"
    )


# =========================
# CONFIG (Render env vars)
# =========================
BASE_URL = _env("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2").rstrip("/")

# Kalshi API Key ID (the "key id", NOT your username)
KALSHI_API_KEY_ID = _env("KALSHI_API_KEY_ID", "") or _env("KALSHI_API_KEYID", "")

# Strategy / risk
POLL_SECONDS = int(_env("POLL_SECONDS", "60"))
MARKET_REFRESH_SECONDS = int(_env("MARKET_REFRESH_SECONDS", "60"))
MAX_DAILY_LOSS = float(_env("MAX_DAILY_LOSS", "20"))

# Wagering
BET_DOLLARS = float(_env("BET_DOLLARS", "1"))  # $1 test wagers
MIN_CONTRACTS = int(_env("MIN_CONTRACTS", "1"))

# If provided, bot will ONLY trade this exact market ticker (example: KXBTC15M-26JAN172000).
MARKET_TICKER_OVERRIDE = _env("MARKET_TICKER", "")

# Otherwise bot will generate tickers using this prefix
SERIES_PREFIX = _env("SERIES_PREFIX", "KXBTC15M")

USE_NEXT_QUARTER_HOUR_END = _env("USE_NEXT_QUARTER_HOUR_END", "true").lower() in ("1", "true", "yes", "y")
DRY_RUN = _env("DRY_RUN", "false").lower() in ("1", "true", "yes", "y")
STOP_AFTER_WAGERS = int(_env("STOP_AFTER_WAGERS", "0"))

# ET offset (set -4 during DST if you want; otherwise -5 is fine)
ET_UTC_OFFSET_HOURS = int(_env("ET_UTC_OFFSET_HOURS", "-5"))


# =========================
# ET helpers (no pytz)
# =========================
def now_et_naive() -> datetime:
    return (datetime.now(timezone.utc) + timedelta(hours=ET_UTC_OFFSET_HOURS)).replace(tzinfo=None)


def format_kalshi_btc15m_ticker(dt_et: datetime) -> str:
    # KXBTC15M-26JAN172000  (YYMMMDDHHMM)
    yy = dt_et.strftime("%y")
    mon = dt_et.strftime("%b").upper()
    dd = dt_et.strftime("%d")
    hhmm = dt_et.strftime("%H%M")
    return f"{SERIES_PREFIX}-{yy}{mon}{dd}{hhmm}"


def next_quarter_hour_end(dt_et: datetime) -> datetime:
    minute = dt_et.minute
    next_q = ((minute // 15) + 1) * 15
    if next_q == 60:
        return dt_et.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return dt_et.replace(minute=next_q, second=0, microsecond=0)


def compute_target_market_ticker() -> str:
    dt_et = now_et_naive()
    if USE_NEXT_QUARTER_HOUR_END:
        dt_et = next_quarter_hour_end(dt_et)
    return format_kalshi_btc15m_ticker(dt_et)


def normalize_market_ticker(ticker: str) -> str:
    """
    Fixes common bad overrides like:
      KXBTC15M-26JAN171930  -> seconds included / not quarter boundary
    Returns a clean quarter-boundary ticker:
      KXBTC15M-26JAN171945 or KXBTC15M-26JAN172000 etc.
    """
    if not ticker:
        return ""

    t = ticker.strip().upper()

    # Must start with prefix-
    if not t.startswith(f"{SERIES_PREFIX}-"):
        return ""

    suffix = t.split("-", 1)[1]  # e.g. 26JAN171930 or 26JAN172000

    # Accept YYMMMDDHHMM or YYMMMDDHHMMSS
    m = re.fullmatch(r"(\d{2}[A-Z]{3}\d{2}\d{4})(\d{2})?", suffix)
    if not m:
        return ""

    base = m.group(1)  # YYMMMDDHHMM (seconds dropped if present)

    try:
        yy = int(base[0:2])
        mon_str = base[2:5]
        dd = int(base[5:7])
        hh = int(base[7:9])
        mm = int(base[9:11])

        month_map = {
            "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
            "JUL": 7, "AUG": 8, "SEP": 9, "OCT