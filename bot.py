import os
import json
import time
import base64
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple, List
from urllib.parse import urlencode

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


def safe_json(obj: Any, max_len: int = 1200) -> str:
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
        self.api_prefix = None

    def _sign(self, method: str, path: str, ts: int) -> str:
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

    def _headers(self, method: str, path: str) -> Dict[str, str]:
        ts = now_utc_ts()
        sig = self._sign(method, path, ts)

        # signing debug
        try:
            signing_payload = f"{ts}{method.upper()}{path}".encode("utf-8")
            h = hashes.Hash(hashes.SHA256())
            h.update(signing_payload)
            digest = base64.b64encode(h.finalize()).decode("utf-8")
            log.info(f"[SIGNDBG] {method.upper()} {path} ts={ts}ms signing_payload_sha256_b64={digest}")
        except Exception as _e:
            log.info(f"[SIGNDBG] failed: {_e}")

        headers = {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "KALSHI-ACCESS-TIMESTAMP": str(ts),
        }
        if self.subaccount:
            headers["KALSHI-SUBACCOUNT"] = self.subaccount
        return headers

    def request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None, json_body: Optional[Dict[str, Any]] = None):
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

        data = None
        if json_body is not None:
            data = json.dumps(json_body, separators=(",", ":"))

        headers = self._headers(method, path_q if method.upper() == "GET" else path)

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
        for pref in ("/trade-api/v2", "/trade-api/v1"):
            code, body, _ = self.request("GET", f"{pref}/markets", params={"limit": 1})
            if code == 200:
                self.api_prefix = pref
                log.info(f"Discovered API prefix: {pref}")
                log.info(f"Markets probe keys={list(body.keys()) if isinstance(body, dict) else None}")
                return pref
        raise RuntimeError("Could not discover API prefix.")

    def get_markets_by_series(self, series_ticker: str, limit: int = 200) -> List[Dict[str, Any]]:
        if not self.api_prefix:
            self.discover_prefix()
        path = f"{self.api_prefix}/markets"
        code, body, text = self.request("GET", path, params={"series_ticker": series_ticker, "limit": limit})
        log.info(f"[SERIES] HTTP={code} keys={list(body.keys()) if isinstance(body, dict) else None}")
        if code != 200:
            raise RuntimeError(f"Series markets fetch failed HTTP={code} body={safe_json(body)} raw={text[:200]}")
        return body.get("markets", []) if isinstance(body, dict) else []

    def get_orderbook(self, market_ticker: str) -> Dict[str, Any]:
        if not self.api_prefix:
            self.discover_prefix()
        path = f"{self.api_prefix}/markets/{market_ticker}/orderbook"
        code, body, _ = self.request("GET", path)
        log.info(f"[BOOK] GET {path} -> HTTP={code} keys={list(body.keys()) if isinstance(body, dict) else None}")
        if code != 200:
            raise RuntimeError(f"Orderbook fetch failed HTTP={code} body={safe_json(body)}")
        return body

    def place_order(self, payload: Dict[str, Any]):
        if not self.api_prefix:
            self.discover_prefix()
        path = f"{self.api_prefix}/portfolio/orders"
        code, body, _ = self.request("POST", path, json_body=payload)
        return code, body


# -----------------------------
# ✅ FIXED PARSER (handles real shapes)
# -----------------------------
def _best_from_list(side_list: Any, choose: str) -> Optional[Dict[str, int]]:
    if not isinstance(side_list, list) or not side_list:
        return None
    # entries usually [price, qty]
    try:
        if choose == "max":
            p, q = max(side_list, key=lambda x: x[0])
        else:
            p, q = min(side_list, key=lambda x: x[0])
        return {"price_cents": int(p), "qty": int(q)}
    except Exception:
        return None


def parse_best_bid_ask(orderbook: Dict[str, Any]) -> Tuple[Dict[str, Optional[Dict[str, int]]], bool]:
    """
    Returns (best_dict, ok)
    ok=False means "we could not reliably find bids+asks", so bot should stand down.
    """
    ob = orderbook.get("orderbook", {}) if isinstance(orderbook, dict) else {}

    # If yes/no are dicts with bids/asks, use that (reliable).
    yes = ob.get("yes")
    no = ob.get("no")

    def parse_side(side_obj: Any) -> Tuple[Optional[Dict[str, int]], Optional[Dict[str, int]], bool]:
        # returns (bid, ask, ok)
        if isinstance(side_obj, dict):
            bids = side_obj.get("bids")
            asks = side_obj.get("asks")
            bid = _best_from_list(bids, "max")
            ask = _best_from_list(asks, "min")
            ok = bid is not None and ask is not None
            return bid, ask, ok

        # If it's only a list, we DO NOT know if it's bids or asks.
        # Stand down rather than guessing.
        if isinstance(side_obj, list):
            return None, None, False

        return None, None, False

    yes_bid, yes_ask, yes_ok = parse_side(yes)
    no_bid, no_ask, no_ok = parse_side(no)

    best = {
        "yes_best_bid": yes_bid,
        "yes_best_ask": yes_ask,
        "no_best_bid": no_bid,
        "no_best_ask": no_ask,
    }
    ok = yes_ok and no_ok

    # Sanity check (bid must be <= ask)
    if ok:
        if best["yes_best_bid"]["price_cents"] > best["yes_best_ask"]["price_cents"]:
            ok = False
        if best["no_best_bid"]["price_cents"] > best["no_best_ask"]["price_cents"]:
            ok = False

    return best, ok


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

    LOOP_SECONDS = env_int("LOOP_SECONDS", 1)
    SERIES_PREFIX = os.getenv("SERIES_PREFIX", "KXBTC15M").strip()
    MARKET_TICKER = os.getenv("MARKET_TICKER")

    API_BASE = os.getenv("KALSHI_API_BASE", "https://api.elections.kalshi.com").strip()
    SUBACCOUNT = os.getenv("KALSHI_SUBACCOUNT")

    FARM_SIDE = os.getenv("FARM_SIDE", "YES").strip().upper()
    MAX_BUY_PRICE_CENTS = env_int("MAX_BUY_PRICE_CENTS", 99)
    BASE_QTY = env_int("BASE_QTY", 1)

    log.info(f"ENABLE_TRADING={ENABLE_TRADING}")
    log.info(f"CONFIRM_LIVE_TRADING={CONFIRM_LIVE_TRADING}")
    log.info(f"LOOP_SECONDS={LOOP_SECONDS}")
    log.info(f"SERIES_PREFIX={SERIES_PREFIX}")
    log.info(f"MARKET_TICKER={MARKET_TICKER}")
    log.info(f"API_BASE={API_BASE}")
    log.info(f"SUBACCOUNT={SUBACCOUNT if SUBACCOUNT else None}")
    log.info(f"FARM_SIDE={FARM_SIDE} MAX_BUY_PRICE_CENTS={MAX_BUY_PRICE_CENTS}")
    log.info(f"QTY base={BASE_QTY}")

    if FARM_SIDE != "YES":
        raise RuntimeError("YES-only right now. Set FARM_SIDE=YES.")

    key_id = (os.getenv("KALSHI_KEY_ID") or "").strip()
    if not key_id:
        raise RuntimeError("Missing env var KALSHI_KEY_ID")

    private_key = load_rsa_private_key_from_env()

    client = KalshiClient(api_base=API_BASE, key_id=key_id, private_key=private_key, subaccount=SUBACCOUNT)
    client.discover_prefix()

    if not MARKET_TICKER:
        markets = client.get_markets_by_series(SERIES_PREFIX, limit=200)
        chosen = select_next_closing(markets)
        if not chosen:
            raise RuntimeError("Could not resolve MARKET_TICKER (no future close_time).")
        MARKET_TICKER = chosen.get("ticker") or chosen.get("market_ticker") or chosen.get("id")
        close_time = chosen.get("close_time")
        log.info(f"[SELECT] Next closing market: {MARKET_TICKER} close={close_time}")

    log.info("[MM] Starting quote loop (YES BUY).")

    while True:
        time.sleep(LOOP_SECONDS)

        ob = client.get_orderbook(MARKET_TICKER)

        # NEW: best + ok
        best, ok = parse_best_bid_ask(ob)
        log.info(f"[BEST] {safe_json(best)} ok={ok}")

        if not ok:
            # key: do not trade on garbage parsing
            # but also print the raw shape so we can fix quickly
            raw_ob = ob.get("orderbook", {})
            yes_shape = type(raw_ob.get("yes")).__name__
            no_shape = type(raw_ob.get("no")).__name__
            log.warning(f"[MM] Orderbook shape not parseable reliably. yes_shape={yes_shape} no_shape={no_shape}. Standing down.")
            continue

        yes_bid = best["yes_best_bid"]["price_cents"]
        yes_ask = best["yes_best_ask"]["price_cents"]

        if yes_ask > MAX_BUY_PRICE_CENTS:
            log.info(f"[MM] Out of range: ask={yes_ask}c > MAX={MAX_BUY_PRICE_CENTS}c. Standing down.")
            continue

        # basic quote rule: buy 1 tick inside ask
        desired = yes_ask - 1 if yes_ask > 1 else 1
        desired = max(1, min(MAX_BUY_PRICE_CENTS, desired))

        log.info(f"[MM] Desired BUY YES {BASE_QTY}@{desired}c (bid={yes_bid} ask={yes_ask} MAX={MAX_BUY_PRICE_CENTS})")

        if not (ENABLE_TRADING and CONFIRM_LIVE_TRADING):
            log.warning("[MM] Trading disabled. Set ENABLE_TRADING=True and CONFIRM_LIVE_TRADING=True.")
            continue

        payload = {
            "ticker": MARKET_TICKER,
            "action": "buy",
            "type": "limit",
            "count": int(BASE_QTY),
            "side": "yes",
            "yes_price": int(desired),
        }
        code, body = client.place_order(payload)
        log.info(f"[ORDER] place http={code} resp={safe_json(body)}")


if __name__ == "__main__":
    main()