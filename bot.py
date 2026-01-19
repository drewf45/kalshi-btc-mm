import os
import json
import time
import base64
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple, List

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


# -----------------------------
# Logging
# -----------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("kalshi-bot")


# -----------------------------
# Helpers
# -----------------------------
def now_utc_ts() -> int:
    return int(time.time())


def iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or not str(v).strip():
        return default
    return int(v)


def env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None or not str(v).strip():
        return default
    return float(v)


def safe_json(obj: Any, max_len: int = 900) -> str:
    try:
        s = json.dumps(obj, ensure_ascii=False)
    except Exception:
        s = str(obj)
    if len(s) > max_len:
        return s[:max_len] + "...(truncated)"
    return s


def load_rsa_private_key_from_env() -> Any:
    """
    Supports:
      - KALSHI_PRIVATE_KEY (raw PEM starting with -----BEGIN RSA PRIVATE KEY----- or -----BEGIN PRIVATE KEY-----)
      - KALSHI_PRIVATE_KEY_B64 (base64 of the PEM)
      - KALSHI_PRIVATE_KEY (sometimes user puts base64 here) -> we detect and decode if PEM header absent
    """
    pem_or_b64 = os.getenv("KALSHI_PRIVATE_KEY") or ""
    b64 = os.getenv("KALSHI_PRIVATE_KEY_B64") or ""

    candidate = pem_or_b64.strip() if pem_or_b64.strip() else b64.strip()
    if not candidate:
        raise RuntimeError("Missing env var KALSHI_PRIVATE_KEY or KALSHI_PRIVATE_KEY_B64")

    if "BEGIN" not in candidate:
        # assume base64
        try:
            candidate_bytes = base64.b64decode(candidate)
            candidate = candidate_bytes.decode("utf-8")
        except Exception as e:
            raise RuntimeError(f"Private key not PEM and base64 decode failed: {e}")

    try:
        key = serialization.load_pem_private_key(
            candidate.encode("utf-8"),
            password=None,
        )
        log.info("Loaded RSA private key (PEM).")
        return key
    except Exception as e:
        raise RuntimeError(f"Failed to load RSA private key: {e}")


# -----------------------------
# Kalshi Client (RSA signing)
# -----------------------------
class KalshiClient:
    def __init__(self, api_base: str, key_id: str, private_key: Any, subaccount: Optional[str] = None):
        self.api_base = api_base.rstrip("/")
        self.key_id = key_id
        self.private_key = private_key
        self.subaccount = (subaccount or "").strip() or None
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

        self.api_prefix = None  # discovered, e.g. "/trade-api/v2"

    def _sign(self, method: str, path: str, ts: int, body: str) -> str:
        """
        Signature format can vary by API.
        This matches the style we’ve been using in this project: sign "ts + method + path + body".
        """
        payload = f"{ts}{method.upper()}{path}{body}".encode("utf-8")
        sig = self.private_key.sign(
            payload,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode("utf-8")

    def _headers(self, method: str, path: str, body: str) -> Dict[str, str]:
        ts = now_utc_ts()
        sig = self._sign(method, path, ts, body)
        h = {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "KALSHI-ACCESS-TIMESTAMP": str(ts),
        }
        if self.subaccount:
            h["KALSHI-SUBACCOUNT"] = self.subaccount
        return h

    def request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None, json_body: Optional[Dict[str, Any]] = None) -> Tuple[int, Any, str]:
        url = f"{self.api_base}{path}"
        body_str = ""
        data = None
        if json_body is not None:
            body_str = json.dumps(json_body, separators=(",", ":"))
            data = body_str

        headers = self._headers(method, path, body_str)
        try:
            r = self.session.request(method=method, url=url, params=params, data=data, headers=headers, timeout=20)
        except Exception as e:
            log.error(f"HTTP {method} {path} failed: {e}")
            return 0, None, ""

        text = r.text or ""
        parsed: Any = None
        if text:
            try:
                parsed = r.json()
            except Exception:
                parsed = {"_raw": text}
        return r.status_code, parsed, text

    def discover_prefix(self) -> str:
        """
        Probe known prefixes and pick the one that returns 200 for /markets.
        """
        candidates = ["/trade-api/v2", "/trade-api/v1"]
        for pref in candidates:
            code, body, _ = self.request("GET", f"{pref}/markets", params={"limit": 1})
            if code == 200:
                self.api_prefix = pref
                log.info(f"Discovered API prefix: {pref} (probe {pref}/markets?limit=1 -> 200)")
                # show shape
                shape = type(body).__name__
                keys = list(body.keys()) if isinstance(body, dict) else None
                log.info(f"Markets probe HTTP=200 shape={shape} keys={keys}")
                return pref
        raise RuntimeError("Could not discover API prefix (markets probe not 200).")

    def get_markets_by_series(self, series_ticker: str, limit: int = 200) -> List[Dict[str, Any]]:
        if not self.api_prefix:
            self.discover_prefix()
        path = f"{self.api_prefix}/markets"
        code, body, text = self.request("GET", path, params={"series_ticker": series_ticker, "limit": limit})
        log.info(f"[SERIES] GET {path}?series_ticker={series_ticker}&limit={limit} -> HTTP={code} shape={type(body).__name__} keys={list(body.keys()) if isinstance(body, dict) else None}")
        if code != 200:
            raise RuntimeError(f"Series markets fetch failed HTTP={code} body={safe_json(body)} raw={text[:200]}")
        markets = body.get("markets", []) if isinstance(body, dict) else []
        log.info(f"[SERIES] Returned markets count={len(markets)}")
        return markets

    def get_orderbook(self, market_ticker: str) -> Dict[str, Any]:
        if not self.api_prefix:
            self.discover_prefix()
        path = f"{self.api_prefix}/markets/{market_ticker}/orderbook"
        code, body, _ = self.request("GET", path)
        log.info(f"[BOOK] GET {path} -> HTTP={code} shape={type(body).__name__} keys={list(body.keys()) if isinstance(body, dict) else None}")
        if code != 200:
            raise RuntimeError(f"Orderbook fetch failed HTTP={code} body={safe_json(body)}")
        return body

    def place_order(self, payload: Dict[str, Any]) -> Tuple[int, Any]:
        """
        NOTE: In your logs, POST /trade-api/v2/orders is returning 404.
        We are NOT fixing that in this step. This is just here so you can see the exact next failure.
        """
        if not self.api_prefix:
            self.discover_prefix()
        path = f"{self.api_prefix}/orders"
        code, body, raw = self.request("POST", path, json_body=payload)
        if code != 200:
            log.error(f"HTTP POST {path} -> {code} {safe_json(body)}")
        else:
            log.info(f"[ORDER] POST {path} -> 200 {safe_json(body)}")
        return code, body


# -----------------------------
# Strategy helpers
# -----------------------------
def parse_best_ask(orderbook: Dict[str, Any]) -> Dict[str, Optional[Dict[str, int]]]:
    """
    Kalshi orderbook response example:
      {"orderbook": {"yes": [[price_cents, qty], ...], "no": [[price_cents, qty], ...], ...}}
    Lowest price in 'yes' is yes_best_ask; lowest in 'no' is no_best_ask.
    """
    ob = orderbook.get("orderbook", {}) if isinstance(orderbook, dict) else {}
    yes = ob.get("yes") or []
    no = ob.get("no") or []
    yes_best = None
    no_best = None

    if isinstance(yes, list) and yes:
        # best ask = min price
        p, q = min(yes, key=lambda x: x[0])
        yes_best = {"price_cents": int(p), "qty": int(q)}
    if isinstance(no, list) and no:
        p, q = min(no, key=lambda x: x[0])
        no_best = {"price_cents": int(p), "qty": int(q)}

    return {"yes_best_ask": yes_best, "no_best_ask": no_best}


def select_next_closing(markets: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Pick the market whose close_time is the soonest in the future.
    close_time comes like "2026-01-19T16:15:00Z"
    """
    now = datetime.now(timezone.utc)

    def parse_z(ts: str) -> datetime:
        # "2026-01-19T16:15:00Z"
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

    future = []
    for m in markets:
        ct = m.get("close_time")
        if not ct:
            continue
        try:
            dt = parse_z(ct)
        except Exception:
            continue
        if dt > now:
            future.append((dt, m))

    if not future:
        return None

    future.sort(key=lambda x: x[0])
    return future[0][1]


# -----------------------------
# Main
# -----------------------------
def main():
    log.info("=== BOT STARTED ===")

    # Core env
    ENABLE_TRADING = env_bool("ENABLE_TRADING", False)
    CONFIRM_LIVE_TRADING = env_bool("CONFIRM_LIVE_TRADING", False)
    POLL_SECONDS = env_int("POLL_SECONDS", 60)
    SERIES_PREFIX = os.getenv("SERIES_PREFIX", "KXBTC15M").strip()
    MARKET_TICKER = os.getenv("MARKET_TICKER")  # optional override
    API_BASE = os.getenv("KALSHI_API_BASE", "https://api.elections.kalshi.com").strip()
    SUBACCOUNT = os.getenv("KALSHI_SUBACCOUNT")

    # Farm env (already in your logs)
    FARM_SIDE = os.getenv("FARM_SIDE", "YES").strip().upper()  # YES or NO
    BUY_PRICE_CENTS = env_int("BUY_PRICE_CENTS", 1)
    TARGET_PROFIT_CENTS = env_int("TARGET_PROFIT_CENTS", 1)

    # Sizing
    BASE_QTY = env_int("BASE_QTY", 1)
    SCALE_AFTER_WINS = env_int("SCALE_AFTER_WINS", 20)
    SIZE_MULT = env_float("SIZE_MULT", 1.25)
    SIZE_CAP = env_int("SIZE_CAP", 10)

    log.info(f"ENABLE_TRADING={ENABLE_TRADING}")
    log.info(f"CONFIRM_LIVE_TRADING={CONFIRM_LIVE_TRADING}")
    log.info(f"POLL_SECONDS={POLL_SECONDS}")
    log.info(f"SERIES_PREFIX={SERIES_PREFIX}")
    log.info(f"MARKET_TICKER={MARKET_TICKER}")
    log.info(f"API_BASE={API_BASE}")
    log.info(f"SUBACCOUNT={SUBACCOUNT if SUBACCOUNT else None}")
    log.info(f"FARM_SIDE={FARM_SIDE} BUY_PRICE_CENTS={BUY_PRICE_CENTS} TARGET_PROFIT_CENTS={TARGET_PROFIT_CENTS}")
    log.info(f"SIZING base={BASE_QTY} scale_after_wins={SCALE_AFTER_WINS} mult={SIZE_MULT} cap={SIZE_CAP}")

    # --------- ONLY CHANGE IN THIS STEP ----------
    # We DO NOT require KALSHI_API_KEY because RSA auth uses KEY_ID + PRIVATE_KEY.
    # Validate only what is actually needed:
    if not os.getenv("KALSHI_KEY_ID"):
        raise RuntimeError("Missing env var KALSHI_KEY_ID")
    # private key validated by loader below
    # --------------------------------------------

    key_id = os.getenv("KALSHI_KEY_ID").strip()
    private_key = load_rsa_private_key_from_env()

    client = KalshiClient(api_base=API_BASE, key_id=key_id, private_key=private_key, subaccount=SUBACCOUNT)

    # Prefix discovery
    client.discover_prefix()

    # Market selection
    if not MARKET_TICKER:
        markets = client.get_markets_by_series(SERIES_PREFIX, limit=200)
        chosen = select_next_closing(markets)
        if not chosen:
            log.warning(f"No markets found matching prefix {SERIES_PREFIX} with a future close_time.")
            raise RuntimeError("Could not resolve MARKET_TICKER. Exiting.")
        MARKET_TICKER = chosen.get("ticker") or chosen.get("market_ticker") or chosen.get("id")  # be defensive

        close_time = chosen.get("close_time")
        seconds_to_close = None
        if close_time:
            dt = datetime.strptime(close_time, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            seconds_to_close = int((dt - datetime.now(timezone.utc)).total_seconds())
        log.info(
            f"[SELECT] Next closing market: {MARKET_TICKER} close={close_time} seconds_to_close={seconds_to_close}"
        )

    # Fetch orderbook
    ob = client.get_orderbook(MARKET_TICKER)
    best = parse_best_ask(ob)
    log.info(f"[BEST] {safe_json(best)}")

    yes_best = best.get("yes_best_ask")
    no_best = best.get("no_best_ask")
    if yes_best and no_best:
        log.info(f"[SANITY] yes_ask={yes_best['price_cents']}c no_ask={no_best['price_cents']}c sum={yes_best['price_cents'] + no_best['price_cents']}c")

    # Decide buy
    side = FARM_SIDE  # "YES" or "NO"
    best_ask = (yes_best["price_cents"] if side == "YES" and yes_best else None) or (no_best["price_cents"] if side == "NO" and no_best else None)
    if best_ask is None:
        log.warning("[STRAT] No best ask found; cannot place order yet.")
        log.info("Heartbeat: orderbook fetch attempt complete. Next step will be to identify best bid/ask and compute tiny-order plan.")
        return

    target_buy = BUY_PRICE_CENTS  # you are forcing micro buy at 1c
    log.info(f"[STRAT] side={side} best_ask={best_ask}c target_buy={target_buy}c")

    qty = BASE_QTY
    log.info(f"[ORDER] BUY {side} {qty}@{target_buy}c on {MARKET_TICKER}")

    if not (ENABLE_TRADING and CONFIRM_LIVE_TRADING):
        log.warning("[ORDER] Trading disabled by env. Set ENABLE_TRADING=True and CONFIRM_LIVE_TRADING=True to actually place.")
        return

    # This is where your next bug shows up: POST path returns 404.
    # We are NOT fixing it in this step. We want the logs to confirm it consistently.
    payload = {
        "ticker": MARKET_TICKER,
        "side": "buy",
        "action": "buy",
        "type": "limit",
        "count": qty,
        "yes_price": target_buy if side == "YES" else None,
        "no_price": target_buy if side == "NO" else None,
    }
    # remove None keys (clean payload)
    payload = {k: v for k, v in payload.items() if v is not None}

    code, body = client.place_order(payload)
    log.info(f"[HEARTBEAT] alive order_post_http={code} resp={safe_json(body)}")


if __name__ == "__main__":
    main()