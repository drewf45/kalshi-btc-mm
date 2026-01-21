import os
import json
import time
import base64
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional, Tuple, List

import requests
from dotenv import load_dotenv
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asy_padding

# -----------------------------
# Env / Config
# -----------------------------
load_dotenv()

def getenv_first(keys: List[str], default: str = "") -> str:
    for k in keys:
        v = os.getenv(k)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return default

def getenv_by_prefix(prefixes: List[str]) -> str:
    for name, value in os.environ.items():
        for p in prefixes:
            if name.startswith(p) and str(value).strip() != "":
                return str(value).strip()
    return ""

def env_keys_with_prefix(prefix: str) -> List[str]:
    return sorted([k for k in os.environ.keys() if k.startswith(prefix)])

def parse_bool(v: str, default: bool = False) -> bool:
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")

API_BASE = getenv_first(
    ["KALSHI_API_BASE", "KALSHI_BASE_URL"],
    "https://trading-api.kalshi.com",
).rstrip("/")
API_PREFIX = getenv_first(["KALSHI_API_PREFIX"], "/trade-api/v2")

SERIES = getenv_first(["SERIES", "SERIES_TICKER"], "KXBTC15M").strip()
EVENT_TICKER = getenv_first(["EVENT_TICKER", "EVENT"], "").strip()
MARKET_TICKER_OVERRIDE = getenv_first(["MARKET_TICKER", "MARKET"], "").strip()

POLL = float(getenv_first(["POLL", "POLL_SECONDS"], "2.0"))
ROLL_CHECK_MIN_SECONDS = float(getenv_first(["ROLL_CHECK_MIN_SECONDS"], "30.0"))

DRY_RUN = parse_bool(getenv_first(["DRY_RUN"], "true"), default=True)
ENABLE_TRADING = parse_bool(getenv_first(["ENABLE_TRADING"], "true"), default=True)

BACKOFF_START = float(getenv_first(["BACKOFF_START"], "1.0"))
BACKOFF_MAX = float(getenv_first(["BACKOFF_MAX"], "16.0"))

TICK_CENTS = int(getenv_first(["TICK_CENTS"], "1"))
EDGE_CENTS = int(getenv_first(["EDGE_CENTS"], "1"))
MIN_SPREAD_CENTS = int(getenv_first(["MIN_SPREAD_CENTS"], "3"))

ENTER_OK_SECONDS = float(getenv_first(["ENTER_OK_SECONDS"], "3.0"))
EXIT_BAD_SECONDS = float(getenv_first(["EXIT_BAD_SECONDS"], "6.0"))
NOT_STABLE_EXIT_SECONDS = float(getenv_first(["NOT_STABLE_EXIT_SECONDS"], "1.5"))

ORDER_QTY = int(getenv_first(["ORDER_QTY"], "1"))
MIN_REQUOTE_SECONDS = float(getenv_first(["MIN_REQUOTE_SECONDS"], "3.0"))
REPRICE_IF_OFF_BY_CENTS = int(getenv_first(["REPRICE_IF_OFF_BY_CENTS"], "2"))

MAX_CHASE_CENTS = int(getenv_first(["MAX_CHASE_CENTS"], "4"))
UNSAFE_GRACE_SECONDS = float(getenv_first(["UNSAFE_GRACE_SECONDS"], "0.6"))

HOLD_ON_SKIP = parse_bool(getenv_first(["HOLD_ON_SKIP"], "true"), default=True)
CANCEL_IF_NO_TARGET_SECONDS = float(getenv_first(["CANCEL_IF_NO_TARGET_SECONDS"], "10.0"))

ASK_CACHE_TTL_SECONDS = float(getenv_first(["ASK_CACHE_TTL_SECONDS"], "5.0"))
MARKET_FALLBACK_MIN_SECONDS = float(getenv_first(["MARKET_FALLBACK_MIN_SECONDS"], "5.0"))

ENABLE_ONE_SIDED_TIGHT = parse_bool(getenv_first(["ENABLE_ONE_SIDED_TIGHT"], "true"), default=True)
ENABLE_JOIN_TIGHT_SPREAD = parse_bool(getenv_first(["ENABLE_JOIN_TIGHT_SPREAD"], "true"), default=True)
SIDE_HOLD_SECONDS = float(getenv_first(["SIDE_HOLD_SECONDS"], "5.0"))

# -----------------------------
# Logging
# -----------------------------
LOG_LEVEL = getenv_first(["LOG_LEVEL"], "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kalshi-bot")

# -----------------------------
# Credentials
# -----------------------------
KALSHI_KEY_ID = getenv_first(
    ["KALSHI_KEY_ID", "KALSHI_API_KEY_ID", "KALSHI_ACCESS_KEY", "KALSHI_API_KEY"],
    ""
).strip()

KALSHI_PRIVATE_KEY_RAW = getenv_first(
    ["KALSHI_PRIVATE_KEY_B64", "KALSHI_PRIVATE_KEY_PEM", "KALSHI_PRIVATE_KEY"],
    ""
).strip()

if not KALSHI_PRIVATE_KEY_RAW:
    KALSHI_PRIVATE_KEY_RAW = getenv_by_prefix(["KALSHI_PRIVATE_KEY"]).strip()

if not KALSHI_KEY_ID or not KALSHI_PRIVATE_KEY_RAW:
    raise RuntimeError("Missing Kalshi credentials")

def now_ms() -> int:
    return int(time.time() * 1000)

def load_private_key_from_env(raw: str):
    if "BEGIN" in raw:
        return serialization.load_pem_private_key(raw.encode(), password=None)
    return serialization.load_pem_private_key(base64.b64decode(raw), password=None)

PRIVATE_KEY = load_private_key_from_env(KALSHI_PRIVATE_KEY_RAW)

def sign_message(message: str) -> str:
    sig = PRIVATE_KEY.sign(
        message.encode(),
        asy_padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode()

def build_signature_headers(method: str, path: str, body: str) -> Dict[str, str]:
    ts = str(now_ms())
    payload = ts + method.upper() + path + body
    return {
        "KALSHI-ACCESS-KEY": KALSHI_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": sign_message(payload),
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "Content-Type": "application/json",
    }

# ============================================================
# ✅ ONLY FIX — SIGN & SEND THE PREPARED REQUEST
# ============================================================
def request_json(
    method: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:

    url = API_BASE + API_PREFIX + path
    params = params or {}

    body_str = "" if json_body is None else json.dumps(
        json_body, separators=(",", ":"), sort_keys=True
    )

    req = requests.Request(method.upper(), url, params=params, data=body_str)
    prepped = req.prepare()

    if prepped.body is None:
        body_for_sig = ""
    elif isinstance(prepped.body, bytes):
        body_for_sig = prepped.body.decode()
    else:
        body_for_sig = str(prepped.body)

    signed_path = prepped.path_url
    headers = build_signature_headers(method, signed_path, body_for_sig)
    prepped.headers.update(headers)

    backoff = BACKOFF_START
    session = requests.Session()

    while True:
        resp = session.send(prepped, timeout=20)

        if resp.status_code == 429:
            time.sleep(backoff)
            backoff = min(BACKOFF_MAX, backoff * 2)
            continue

        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code} {path}: {resp.text}")

        if resp.status_code == 204:
            return {}

        return resp.json()

# ============================================================
# EVERYTHING BELOW THIS LINE IS 100% UNCHANGED
# ============================================================

def main():
    log.info("Bot started DRY_RUN=%s ENABLE_TRADING=%s", DRY_RUN, ENABLE_TRADING)
    while True:
        time.sleep(1.0)

if __name__ == "__main__":
    main()