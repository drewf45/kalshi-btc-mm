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

        # signing debug (safe)
        try:
            payload_preview = f"{method.upper()} {path} ts={ts}ms body_len={len(body.encode('utf-8')) if body else 0}"
            payload_hash = hashes.Hash(hashes.SHA256())
            signing_payload = f"{ts}{method.upper()}{path}".encode("utf-8")
            payload_hash.update(signing_payload)
            digest = base64.b64encode(payload_hash.finalize()).decode("utf-8")
            log.info(f"[SIGNDBG] {payload_preview} signing_payload_sha256_b64={digest}")
        except Exception as _e:
            log.info(f"[SIGNDBG] failed to compute debug hash: {_e}")

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
        return body.get("orders", []) if isinstance(body, dict) else []

    def cancel_order(self, order_id: str) -> Tuple[int, Any]:
        if not self.api_prefix:
            self.discover_prefix()

        path = f"{self.api_prefix}/portfolio/orders/{order_id}"
        code, body, _ = self.request("DELETE", path)
        if code in (200, 204):
            log.info(f"[CANCEL] DELETE {path} -> {code} {safe_json(body)}")
            return code, body

        path2 = f"{self.api_prefix}/portfolio/orders/{order_id}/cancel"
        code2, body2, _ = self.request("POST", path2, json_body={})
        if code2 in (200, 201, 204):
            log.info(f"[CANCEL] POST {path2} -> {code2} {safe_json(body2)}")
        else:
            log.warning(f"[CANCEL] Failed cancel attempts http={code}/{code2} body1={safe_json(body)} body2={safe_json(body2)}")
        return code2, body2

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


def parse_best_bid_ask(orderbook: Dict[str, Any]) -> Dict[str, Optional[Dict[str, int]]]:
    ob = orderbook.get("orderbook", {}) if isinstance(orderbook, dict) else {}
    yes = ob.get("yes") or []
    no = ob.get("no") or []

    # NOTE: Many Kalshi orderbooks are lists of [price, qty] for EACH SIDE.
    # In your earlier logs you already see bid/ask being derived correctly elsewhere.
    # Here we assume:
    # - for each side list, lowest price is best ASK, highest price is best BID
    def best_bid(side_list: Any) -> Optional[Dict[str, int]]:
        if isinstance(side_list, list) and side_list:
            p, q = max(side_list, key=lambda x: x[0])
            return {"price_cents": int(p), "qty": int(q)}
        return None

    def best_ask(side_list: Any) -> Optional[Dict[str, int]]:
        if isinstance(side_list, list) and side_list:
            p, q = min(side_list, key=lambda x: x[0])
            return {"price_cents": int(p), "qty": int(q)}
        return None

    return {
        "yes_best_bid": best_bid(yes),
        "yes_best_ask": best_ask(yes),
        "no_best_bid": best_bid(no),
        "no_best_ask": best_ask(no),
    }


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

    LOOP_SECONDS = env_int("LOOP_SECONDS", 1)  # used for quote loop
    SERIES_PREFIX = os.getenv("SERIES_PREFIX", "KXBTC15M").strip()
    MARKET_TICKER = os.getenv("MARKET_TICKER")

    API_BASE = os.getenv("KALSHI_API_BASE", "https://api.elections.kalshi.com").strip()
    SUBACCOUNT = os.getenv("KALSHI_SUBACCOUNT")

    FARM_SIDE = os.getenv("FARM_SIDE", "YES").strip().upper()
    MAX_BUY_PRICE_CENTS = env_int("MAX_BUY_PRICE_CENTS", 5)

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
        raise RuntimeError("This bot is YES-only right now. Set FARM_SIDE=YES.")

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

    log.info(f"[MM] Starting quote-maintenance loop for BUY (YES).")

    active_order_id: Optional[str] = None
    active_price: Optional[int] = None

    while True:
        time.sleep(LOOP_SECONDS)

        try:
            ob = client.get_orderbook(MARKET_TICKER)
            best = parse_best_bid_ask(ob)
            log.info(f"[BEST] {safe_json(best)}")

            yes_ask = best.get("yes_best_ask")
            yes_bid = best.get("yes_best_bid")

            if not yes_ask or not yes_bid:
                log.warning("[MM] Missing YES bid/ask; skipping this loop.")
                continue

            ask = int(yes_ask["price_cents"])
            bid = int(yes_bid["price_cents"])

            # -----------------------------------------------------------
            # ✅ NEXT MICRO CHANGE: Participation guard
            # If market ask is above our risk cap, do NOT quote at MAX.
            # Instead: cancel our resting order (if any) and wait.
            # -----------------------------------------------------------
            if ask > int(MAX_BUY_PRICE_CENTS):
                if active_order_id:
                    log.info(f"[MM] Ask {ask}c > MAX {MAX_BUY_PRICE_CENTS}c. Canceling active order_id={active_order_id} price={active_price}c")
                    try:
                        client.cancel_order(active_order_id)
                    except Exception as e:
                        log.warning(f"[MM] Cancel failed (will keep looping): {e}")
                    active_order_id = None
                    active_price = None

                log.info(f"[MM] Out of range: YES ask={ask}c > MAX_BUY_PRICE_CENTS={MAX_BUY_PRICE_CENTS}. Standing down.")
                continue
            # -----------------------------------------------------------

            # If we're in-range, quote near the top (simple baseline):
            # buy at min(ask-1, MAX), but never below 1.
            desired = ask if ask <= 1 else (ask - 1)
            desired = max(1, min(int(MAX_BUY_PRICE_CENTS), desired))

            log.info(f"[MM] Desired BUY YES {BASE_QTY}@{desired}c (bid={bid} ask={ask} MAX={MAX_BUY_PRICE_CENTS})")

            if not (ENABLE_TRADING and CONFIRM_LIVE_TRADING):
                log.warning("[MM] Trading disabled by env. Set ENABLE_TRADING=True and CONFIRM_LIVE_TRADING=True to actually place.")
                continue

            # If we already have an active order at the desired price, do nothing.
            if active_order_id and active_price == desired:
                continue

            # Cancel old order if we have one (price changed)
            if active_order_id and active_price != desired:
                log.info(f"[MM] Price changed. Cancel+replace {active_price}c -> {desired}c (order_id={active_order_id})")
                try:
                    client.cancel_order(active_order_id)
                except Exception as e:
                    log.warning(f"[MM] Cancel failed (will still try replace): {e}")
                active_order_id = None
                active_price = None

            payload = {
                "ticker": MARKET_TICKER,
                "action": "buy",
                "type": "limit",
                "count": int(BASE_QTY),
                "side": "yes",
                "yes_price": int(desired),
            }

            code, body = client.place_order(payload)
            log.info(f"[MM] BUY placed http={code} resp={safe_json(body)}")

            new_id = None
            try:
                if isinstance(body, dict) and isinstance(body.get("order"), dict):
                    new_id = body["order"].get("order_id")
            except Exception:
                new_id = None

            if new_id:
                active_order_id = new_id
                active_price = int(desired)

        except Exception as e:
            log.warning(f"[MM] Loop error: {e}")


if __name__ == "__main__":
    main()