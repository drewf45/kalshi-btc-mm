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

# --- Safety / Patience gates ---
ENTER_OK_SECONDS = float(getenv_first(["ENTER_OK_SECONDS"], "3.0"))
EXIT_BAD_SECONDS = float(getenv_first(["EXIT_BAD_SECONDS"], "6.0"))
NOT_STABLE_EXIT_SECONDS = float(getenv_first(["NOT_STABLE_EXIT_SECONDS"], "1.5"))

# Order params
ORDER_QTY = int(getenv_first(["ORDER_QTY"], "1"))

# Minimum time between reprices (prevents churn)
MIN_REQUOTE_SECONDS = float(getenv_first(["MIN_REQUOTE_SECONDS"], "3.0"))

# Only reprice if we are "meaningfully" off target
REPRICE_IF_OFF_BY_CENTS = int(getenv_first(["REPRICE_IF_OFF_BY_CENTS"], "2"))

# Anti-churn / volatility controls
MAX_CHASE_CENTS = int(getenv_first(["MAX_CHASE_CENTS"], "4"))
UNSAFE_GRACE_SECONDS = float(getenv_first(["UNSAFE_GRACE_SECONDS"], "0.6"))

# When BOTH targets are None, hold existing orders instead of canceling immediately
HOLD_ON_SKIP = parse_bool(getenv_first(["HOLD_ON_SKIP"], "true"), default=True)

# Only cancel after BOTH targets have been None continuously for this long (generic no-target)
CANCEL_IF_NO_TARGET_SECONDS = float(getenv_first(["CANCEL_IF_NO_TARGET_SECONDS"], "10.0"))

# Micro #1: ask cache TTL to ride out NO-side blips
ASK_CACHE_TTL_SECONDS = float(getenv_first(["ASK_CACHE_TTL_SECONDS"], "5.0"))

# Micro #2: fallback to /markets/{ticker} when ask missing (throttled)
MARKET_FALLBACK_MIN_SECONDS = float(getenv_first(["MARKET_FALLBACK_MIN_SECONDS"], "5.0"))

# Micro #3 toggles
ENABLE_ONE_SIDED_TIGHT = parse_bool(getenv_first(["ENABLE_ONE_SIDED_TIGHT"], "true"), default=True)
ENABLE_JOIN_TIGHT_SPREAD = parse_bool(getenv_first(["ENABLE_JOIN_TIGHT_SPREAD"], "true"), default=True)

# Micro #4: per-side hysteresis
SIDE_HOLD_SECONDS = float(getenv_first(["SIDE_HOLD_SECONDS"], "5.0"))

# Live flags (optional)
POST_ONLY = parse_bool(getenv_first(["POST_ONLY"], "true"), default=True)

# -----------------------------
# Logging
# -----------------------------
LOG_LEVEL = getenv_first(["LOG_LEVEL"], "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kalshi-bot")

# -----------------------------
# Credentials + diagnostics
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
    KALSHI_PRIVATE_KEY_RAW = getenv_by_prefix(
        ["KALSHI_PRIVATE_KEY_PEM", "KALSHI_PRIVATE_KEY_B64"]
    ).strip()

_detected_kalshi_keys = env_keys_with_prefix("KALSHI_")
log.info(
    "[ENV] Detected KALSHI_* keys: %s",
    _detected_kalshi_keys if _detected_kalshi_keys else "<none>",
)

if not KALSHI_KEY_ID or not KALSHI_PRIVATE_KEY_RAW:
    raise RuntimeError(
        "Missing Kalshi credentials at runtime.\n"
        f"Detected KALSHI_* keys: {_detected_kalshi_keys}\n"
        "Expected one of:\n"
        "  - KALSHI_KEY_ID or KALSHI_API_KEY_ID\n"
        "  - KALSHI_PRIVATE_KEY_B64 or KALSHI_PRIVATE_KEY_PEM\n"
    )

# -----------------------------
# Signing helpers
# -----------------------------
# Monotonic timestamp for signing (never goes backwards / never repeats)
_LAST_TS_MS: int = 0


def now_ms() -> int:
    global _LAST_TS_MS
    t = int(time.time() * 1000)
    if t <= _LAST_TS_MS:
        t = _LAST_TS_MS + 1
    _LAST_TS_MS = t
    return t


def load_private_key_from_env(raw: str) -> Any:
    s = raw.strip()
    if "BEGIN" in s and "PRIVATE KEY" in s:
        return serialization.load_pem_private_key(s.encode("utf-8"), password=None)

    try:
        key_bytes = base64.b64decode(s)
        return serialization.load_pem_private_key(key_bytes, password=None)
    except Exception as e:
        raise RuntimeError(
            "Could not parse private key. Provide PEM text in KALSHI_PRIVATE_KEY_PEM "
            "or base64 PEM in KALSHI_PRIVATE_KEY_B64."
        ) from e


PRIVATE_KEY = load_private_key_from_env(KALSHI_PRIVATE_KEY_RAW)


def sign_message(message: str) -> str:
    # ✅ ONLY CHANGE: use RSA-PSS padding (Kalshi expects PSS, not PKCS1v15)
    sig = PRIVATE_KEY.sign(
        message.encode("utf-8"),
        asy_padding.PSS(
            mgf=asy_padding.MGF1(hashes.SHA256()),
            salt_length=asy_padding.PSS.MAX_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("utf-8")


def build_signature_headers(method: str, path_with_query: str, body: str) -> Dict[str, str]:
    ts = str(now_ms())
    payload = ts + method.upper() + path_with_query + body
    return {
        "KALSHI-ACCESS-KEY": KALSHI_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": sign_message(payload),
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "Content-Type": "application/json",
    }

# -----------------------------
# HTTP helper (with 429 backoff)
# -----------------------------
def request_json(
    method: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    url = API_BASE + API_PREFIX + path
    params = params or {}

    if json_body is None:
        body_bytes = b""
    else:
        body_str = json.dumps(json_body, separators=(",", ":"), sort_keys=True)
        body_bytes = body_str.encode("utf-8")

    backoff = BACKOFF_START
    session = requests.Session()

    while True:
        req = requests.Request(
            method.upper(),
            url,
            params=params,
            data=body_bytes,
        )
        prepped = req.prepare()

        signed_path = prepped.path_url
        body_for_sig = "" if not body_bytes else body_bytes.decode("utf-8")

        headers = build_signature_headers(method, signed_path, body_for_sig)
        prepped.headers.update(headers)

        resp = session.send(prepped, timeout=20)

        if resp.status_code == 429:
            log.warning("[429] %s backing off %.1fs", path, backoff)
            time.sleep(backoff)
            backoff = min(BACKOFF_MAX, backoff * 2)
            continue

        if resp.status_code >= 400:
            text = resp.text or ""
            try:
                j = resp.json()
                raise RuntimeError(f"HTTP {resp.status_code} {path}: {j}")
            except Exception:
                raise RuntimeError(
                    f"HTTP {resp.status_code} {path}: "
                    f"{{'_non_json': True, '_text_head': {text[:200]!r}}}"
                )

        if resp.status_code == 204:
            return {}

        try:
            return resp.json()
        except Exception:
            raise RuntimeError(f"Bad JSON response for {path}: {(resp.text or '')[:200]!r}")

# -----------------------------
# LIVE ORDER ROUTES
# -----------------------------
def place_order_live(market_ticker: str, action: str, yes_price_cents: int, count: int) -> str:
    body: Dict[str, Any] = {
        "ticker": market_ticker,
        "action": action,          # "buy" or "sell"
        "type": "limit",
        "side": "yes",
        "count": int(count),
        "yes_price": int(yes_price_cents),
    }
    if POST_ONLY:
        body["post_only"] = True

    resp = request_json("POST", "/portfolio/orders", json_body=body)

    order = resp.get("order") if isinstance(resp, dict) else None
    if isinstance(order, dict):
        oid = order.get("order_id") or order.get("id")
        if oid:
            return str(oid)

    oid = resp.get("order_id") or resp.get("id")
    if oid:
        return str(oid)

    raise RuntimeError(f"Order placed but could not find order_id in response: {resp}")


def cancel_order_live(order_id: str) -> None:
    """
    IMPORTANT FIX:
    Exchange may return 404 if the order is already filled/canceled/expired.
    We treat 404 not_found as success so the bot doesn't spam LOOPERR forever.
    """
    try:
        request_json("DELETE", f"/portfolio/orders/{order_id}")
    except Exception as e:
        msg = str(e)
        if "HTTP 404" in msg and "not_found" in msg:
            log.info("[OM] CANCEL already-gone order_id=%s (ignoring 404 not_found)", order_id)
            return
        raise

# -----------------------------
# Rolling via /markets ONLY
# -----------------------------
_last_roll_ts = 0.0
_active_event_ticker: Optional[str] = None
_active_market_ticker: Optional[str] = None


def _parse_dt_to_ts(s: Any) -> float:
    if not isinstance(s, str):
        return 0.0
    try:
        s2 = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s2).timestamp()
    except Exception:
        return 0.0


def pick_open_market(markets: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    now = time.time()
    open_markets = []
    for m in markets:
        if not isinstance(m, dict):
            continue
        status = str(m.get("status", "")).lower()
        if status in ("open", "active", "trading"):
            open_markets.append(m)

    candidates = open_markets if open_markets else [m for m in markets if isinstance(m, dict)]
    if not candidates:
        return None

    def score(m: Dict[str, Any]) -> Tuple[int, float]:
        status = str(m.get("status", "")).lower()
        is_open = status in ("open", "active", "trading")
        close_ts = 0.0
        for k in ("close_time", "end_time", "expiration_time", "settlement_time"):
            if k in m:
                close_ts = max(close_ts, _parse_dt_to_ts(m.get(k)))
        if close_ts > 0:
            dtc = close_ts - now
            if dtc >= 0:
                return (2 if is_open else 1, -dtc)
        return (1 if is_open else 0, -1e18)

    return sorted(candidates, key=score, reverse=True)[0]


def resolve_event_and_market_via_markets(series_ticker: str) -> Tuple[Optional[str], Optional[str]]:
    param_sets = [
        {"series_ticker": series_ticker, "limit": 200},
        {"series": series_ticker, "limit": 200},
        {"series_ticker": series_ticker, "status": "open", "limit": 200},
    ]

    last_err = None
    for params in param_sets:
        try:
            data = request_json("GET