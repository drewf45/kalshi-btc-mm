import os
import json
import time
import base64
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple, List
from urllib.parse import urlencode  # ✅ ADDED

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asy_padding


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
    return int(time.time() * 1000)


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
    pem_or_b64 = os.getenv("KALSHI_PRIVATE_KEY") or ""
    b64 = os.getenv("KALSHI_PRIVATE_KEY_B64") or ""

    candidate = pem_or_b64.strip() if pem_or_b64.strip() else b64.strip()
    if not candidate:
        raise RuntimeError("Missing env var KALSHI_PRIVATE_KEY or KALSHI_PRIVATE_KEY_B64")

    if "BEGIN" not in candidate:
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

    # ✅ CHANGED: use PSS and do NOT include body in the signature payload
    def _sign(self, method: str, path: str, ts: int, body: str) -> str:
        payload = f"{ts}{method.upper()}{path}".encode("utf-8")

        sig = self.private_key.sign(
            payload,
            asy_padding.PSS(
                mgf=asy_padding.MGF1(hashes.SHA256()),
                salt_length=asy_padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode("utf-8")

    def _headers(self, method: str, path: str, body: str) -> Dict[str, str]:
        ts = now_utc_ts()
        sig = self._sign(method, path, ts, body)

        # ---- DEBUG SIGNING (safe) ----
        try:
            payload_preview = f"{method.upper()} {path} ts={ts}ms body_len={len(body.encode('utf-8')) if body else 0}"
            payload_hash = hashes.Hash(hashes.SHA256())
            signing_payload = f"{ts}{method.upper()}{path}".encode("utf-8")
            payload_hash.update(signing_payload)
            digest = base64.b64encode(payload_hash.finalize()).decode("utf-8")
            log.info(f"[SIGNDBG] {payload_preview} signing_payload_sha256_b64={digest}")
        except Exception as _e:
            log.info(f"[SIGNDBG] failed to compute debug hash: {_e}")
        # -------------------------------

        h = {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "KALSHI-ACCESS-TIMESTAMP": str(ts),
        }
        if self.subaccount:
            h["KALSHI-SUBACCOUNT"] = self.subaccount
        return h

    def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Tuple[int, Any, str]:
        # build exact path including query string before signing
        path_q = path
        if params:
            items: List[Tuple[str, Any]] = []
            for k in sorted(params.keys()):
                v = params[k]
                if isinstance(v, (list, tuple)):
                    for vv in v:
                        items.append((k, vv))
                else:
                    items.append((k, v))
            qs = urlencode(items, doseq=True)
            path_q = f"{path}?{qs}"

        url = f"{self.api_base}{path_q}"

        body_str = ""
        data = None
        if json_body is not None:
            body_str = json.dumps(json_body, separators=(",", ":"))
            data = body_str

        # ✅ ONLY CHANGE: for GET /portfolio/orders, sign WITHOUT the query string
        sign_path = path_q
        if method.upper() == "GET" and path.endswith("/portfolio/orders") and params:
            sign_path = path  # sign only the base path

        headers = self._headers(method, sign_path, body_str)

        try:
            r = self.session.request(method=method, url=url, params=None, data=data, headers=headers, timeout=20)
        except Exception as e:
            log.error(f"HTTP {method} {path_q} failed: {e}")
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
        candidates = ["/trade-api/v2", "/trade-api/v1"]
        for pref in candidates:
            code, body, _ = self.request("GET", f"{pref}/markets", params={"limit": 1})
            if code == 200:
                self.api_prefix = pref
                log.info(f"Discovered API prefix: {pref} (probe {pref}/markets?limit=1 -> 200)")
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
        log.info(
            f"[SERIES] GET {path}?series_ticker={series_ticker}&limit={limit} -> HTTP={code} "
            f"shape={type(body).__name__} keys={list(body.keys()) if isinstance(body, dict) else None}"
        )
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
        log.info(
            f"[BOOK] GET {path} -> HTTP={code} shape={type(body).__name__} "
            f"keys={list(body.keys()) if isinstance(body, dict) else None}"
        )
        if code != 200:
            raise RuntimeError(f"Orderbook fetch failed HTTP={code} body={safe_json(body)}")
        return body

    def get_resting_orders(self, limit: int = 200) -> List[Dict[str, Any]]:
        if not self.api_prefix:
            self.discover_prefix()
        path = f"{self.api_prefix}/portfolio/orders"
        code, body, text = self.request("GET", path, params={"status": "resting", "limit": limit})
        log.info(
            f"[ORDERS] GET {path}?status=resting&limit={limit} -> HTTP={code} "
            f"shape={type(body).__name__} keys={list(body.keys()) if isinstance(body, dict) else None}"
        )
        if code != 200:
            raise RuntimeError(f"Resting orders fetch failed HTTP={code} body={safe_json(body)} raw={text[:200]}")
        orders = body.get("orders", []) if isinstance(body, dict) else []
        return orders

    def place_order(self, payload: Dict[str, Any]) -> Tuple[int, Any]:
        if not self.api_prefix:
            self.discover_prefix()
        path = f"{self.api_prefix}/portfolio/orders"
        code, body, _ = self.request("POST", path, json_body=payload)
        if code not in (200, 201):
            log.error(f"HTTP POST {path} -> {code} {safe_json(body)}")
        else:
            log.info(f"[ORDER] POST {path} -> {code} {safe_json(body)}")
        return code, body


def parse_best_ask(orderbook: Dict[str, Any]) -> Dict[str, Optional[Dict[str, int]]]:
    ob = orderbook.get("orderbook", {}) if isinstance(orderbook, dict) else {}
    yes = ob.get("yes") or []
    no = ob.get("no") or []
    yes_best = None
    no_best = None

    if isinstance(yes, list) and yes:
        p, q = min(yes, key=lambda x: x[0])
        yes_best = {"price_cents": int(p), "qty": int(q)}
    if isinstance(no, list) and no:
        p, q = min(no, key=lambda x: x[0])
        no_best = {"price_cents": int(p), "qty": int(q)}

    return {"yes_best_ask": yes_best, "no_best_ask": no_best}


def select_next_closing(markets: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    now = datetime.now(timezone.utc)

    def parse_z(ts: str) -> datetime:
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


def main():
    log.info("=== BOT STARTED ===")

    ENABLE_TRADING = env_bool("ENABLE_TRADING", False)
    CONFIRM_LIVE_TRADING = env_bool("CONFIRM_LIVE_TRADING", False)
    POLL_SECONDS = env_int("POLL_SECONDS", 60)
    SERIES_PREFIX = os.getenv("SERIES_PREFIX", "KXBTC15M").strip()
    MARKET_TICKER = os.getenv("MARKET_TICKER")
    API_BASE = os.getenv("KALSHI_API_BASE", "https://api.elections.kalshi.com").strip()
    SUBACCOUNT = os.getenv("KALSHI_SUBACCOUNT")

    FARM_SIDE = os.getenv("FARM_SIDE", "YES").strip().upper()
    BUY_PRICE_CENTS = env_int("BUY_PRICE_CENTS", 1)
    MAX_BUY_PRICE_CENTS = env_int("MAX_BUY_PRICE_CENTS", BUY_PRICE_CENTS)  # ✅ ADDED
    TARGET_PROFIT_CENTS = env_int("TARGET_PROFIT_CENTS", 1)

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
    log.info(
        f"FARM_SIDE={FARM_SIDE} BUY_PRICE_CENTS={BUY_PRICE_CENTS} "
        f"MAX_BUY_PRICE_CENTS={MAX_BUY_PRICE_CENTS} TARGET_PROFIT_CENTS={TARGET_PROFIT_CENTS}"
    )
    log.info(f"SIZING base={BASE_QTY} scale_after_wins=20 mult={SIZE_MULT} cap={SIZE_CAP}")

    if not os.getenv("KALSHI_KEY_ID"):
        raise RuntimeError("Missing env var KALSHI_KEY_ID")

    key_id = os.getenv("KALSHI_KEY_ID").strip()
    private_key = load_rsa_private_key_from_env()

    client = KalshiClient(api_base=API_BASE, key_id=key_id, private_key=private_key, subaccount=SUBACCOUNT)
    client.discover_prefix()

    if not MARKET_TICKER:
        markets = client.get_markets_by_series(SERIES_PREFIX, limit=200)
        chosen = select_next_closing(markets)
        if not chosen:
            log.warning(f"No markets found matching prefix {SERIES_PREFIX} with a future close_time.")
            raise RuntimeError("Could not resolve MARKET_TICKER. Exiting.")
        MARKET_TICKER = chosen.get("ticker") or chosen.get("market_ticker") or chosen.get("id")

        close_time = chosen.get("close_time")
        seconds_to_close = None
        if close_time:
            dt = datetime.strptime(close_time, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            seconds_to_close = int((dt - datetime.now(timezone.utc)).total_seconds())
        log.info(f"[SELECT] Next closing market: {MARKET_TICKER} close={close_time} seconds_to_close={seconds_to_close}")

    ob = client.get_orderbook(MARKET_TICKER)
    best = parse_best_ask(ob)
    log.info(f"[BEST] {safe_json(best)}")

    side = FARM_SIDE
    qty = BASE_QTY

    # ✅ MICRO CHANGE (SAFER): step toward the SAME-SIDE ask instead of parity.
    # This prevents "opposite ask = 1c" from forcing you up to MAX.
    if side == "YES":
        yes_best_ask = best.get("yes_best_ask")
        if not yes_best_ask:
            raise RuntimeError("No YES ask available.")
        ask = int(yes_best_ask["price_cents"])
    else:
        no_best_ask = best.get("no_best_ask")
        if not no_best_ask:
            raise RuntimeError("No NO ask available.")
        ask = int(no_best_ask["price_cents"])

    # "best bid + 1" behavior, but anchored to ask:
    # - if ask is 1c, stay at 1c
    # - otherwise bid 1c below ask (inside the spread)
    target_buy = ask if ask <= 1 else (ask - 1)

    # apply safety caps
    target_buy = max(1, min(99, target_buy))
    target_buy = min(MAX_BUY_PRICE_CENTS, target_buy)
    # ---------------------------------------------------------------

    log.info(f"[ORDER] BUY {side} {qty}@{target_buy}c on {MARKET_TICKER}")

    if not (ENABLE_TRADING and CONFIRM_LIVE_TRADING):
        log.warning("[ORDER] Trading disabled by env. Set ENABLE_TRADING=True and CONFIRM_LIVE_TRADING=True to actually place.")
        return

    try:
        existing = client.get_resting_orders(limit=200)
        side_key = "yes" if side == "YES" else "no"
        already = False
        for o in existing:
            if (o.get("ticker") == MARKET_TICKER and
                o.get("status") == "resting" and
                o.get("side") == side_key and
                int(o.get(f"{side_key}_price") or -1) == int(target_buy) and
                int(o.get("remaining_count") or 0) > 0):
                already = True
                break
        if already:
            log.warning("[GUARD] Existing resting order found (same ticker/side/price). Skipping new order.")
            return
    except Exception as e:
        log.warning(f"[GUARD] Could not check existing orders (will proceed): {e}")

    payload = {
        "ticker": MARKET_TICKER,
        "action": "buy",
        "type": "limit",
        "count": qty,
        "side": "yes" if side == "YES" else "no",
        "yes_price": target_buy if side == "YES" else None,
        "no_price": target_buy if side == "NO" else None,
    }
    payload = {k: v for k, v in payload.items() if v is not None}

    code, body = client.place_order(payload)
    log.info(f"[HEARTBEAT] alive order_post_http={code} resp={safe_json(body)}")

    while True:
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()