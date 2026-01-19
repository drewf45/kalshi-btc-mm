# bot.py
import os
import time
import json
import base64
import logging
import datetime as dt
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple, List

import requests
from dotenv import load_dotenv

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


# ----------------------------
# Logging
# ----------------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("kalshi-bot")


def safe_preview(obj: Any, max_chars: int = 600) -> str:
    try:
        s = json.dumps(obj, ensure_ascii=False)
    except Exception:
        s = str(obj)
    if len(s) > max_chars:
        return s[:max_chars] + "...(truncated)"
    return s


def utc_now_ts() -> str:
    # Unix timestamp as string (seconds)
    return str(int(time.time()))


# ----------------------------
# Config
# ----------------------------
@dataclass
class BotConfig:
    enable_trading: bool
    confirm_live_trading: bool
    poll_seconds: int
    api_base: str
    series_prefix: str
    market_ticker: Optional[str]
    subaccount: Optional[str]

    farm_side: str
    buy_price_cents: int
    target_profit_cents: int

    # sizing
    base_size: int
    scale_after_wins: int
    mult: float
    cap: int


def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def load_config() -> BotConfig:
    return BotConfig(
        enable_trading=env_bool("ENABLE_TRADING", False),
        confirm_live_trading=env_bool("CONFIRM_LIVE_TRADING", False),
        poll_seconds=int(os.getenv("POLL_SECONDS", "60")),
        api_base=os.getenv("API_BASE", "https://api.elections.kalshi.com").rstrip("/"),
        series_prefix=os.getenv("SERIES_PREFIX", "KXBTC15M"),
        market_ticker=os.getenv("MARKET_TICKER") or None,
        subaccount=os.getenv("SUBACCOUNT") or None,

        farm_side=(os.getenv("FARM_SIDE", "YES").strip().upper()),
        buy_price_cents=int(os.getenv("BUY_PRICE_CENTS", "1")),
        target_profit_cents=int(os.getenv("TARGET_PROFIT_CENTS", "1")),

        base_size=int(os.getenv("SIZE_BASE", "1")),
        scale_after_wins=int(os.getenv("SIZE_SCALE_AFTER_WINS", "20")),
        mult=float(os.getenv("SIZE_MULT", "1.25")),
        cap=int(os.getenv("SIZE_CAP", "10")),
    )


# ----------------------------
# Kalshi Client (RSA auth)
# ----------------------------
class KalshiClient:
    def __init__(self, api_base: str, api_key: str, private_key_pem_or_b64: str, subaccount: Optional[str] = None):
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.subaccount = subaccount

        # autodiscovered
        self.api_prefix = None  # like "/trade-api/v2"
        self.orders_path = None  # FULL path candidate that works

        self._priv = self._load_private_key(private_key_pem_or_b64)

    def _load_private_key(self, s: str):
        """
        Supports:
          - raw PEM starting with -----BEGIN
          - base64 string that decodes into PEM
        """
        raw = s.strip()

        # If it's base64, decode; otherwise treat as PEM
        pem_bytes: Optional[bytes] = None
        if raw.startswith("-----BEGIN"):
            pem_bytes = raw.encode("utf-8")
        else:
            # try base64 decode
            try:
                decoded = base64.b64decode(raw)
                # if decoded looks like PEM
                if b"-----BEGIN" in decoded:
                    pem_bytes = decoded
                else:
                    # might be already PEM without header (unlikely for RSA)
                    pem_bytes = decoded
            except Exception as e:
                raise RuntimeError(f"Could not parse private key. Provide PEM or base64(PEM). Error: {e}")

        try:
            key = serialization.load_pem_private_key(pem_bytes, password=None)
            logger.info("Loaded RSA private key (PEM).")
            return key
        except Exception as e:
            raise RuntimeError(f"Failed to load RSA private key. Error: {e}")

    def _sign(self, message: bytes) -> str:
        sig = self._priv.sign(
            message,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode("utf-8")

    def _headers(self, method: str, path: str, body: str) -> Dict[str, str]:
        """
        Kalshi-style header signing (RSA).
        This matches the pattern that already got you 200s for markets/orderbook.
        """
        ts = utc_now_ts()
        # Sign method + path + timestamp + body
        payload = (method.upper() + path + ts + body).encode("utf-8")

        h = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "KALSHI-ACCESS-KEY": self.api_key,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": self._sign(payload),
        }
        if self.subaccount:
            h["KALSHI-SUBACCOUNT"] = self.subaccount
        return h

    def request(
        self,
        method: str,
        path: str,
        body_obj: Optional[dict] = None,
        require_prefix: bool = True,
        timeout: int = 15,
    ) -> Tuple[int, Any]:
        """
        If require_prefix=True, path is relative to self.api_prefix, and we will call:
          {api_base}{api_prefix}{path}
        If require_prefix=False, path is treated as absolute (e.g. "/trade-api/v1/orders")
          {api_base}{path}
        """
        if body_obj is None:
            body = ""
        else:
            body = json.dumps(body_obj, separators=(",", ":"), ensure_ascii=False)

        if require_prefix:
            if not self.api_prefix:
                raise RuntimeError("api_prefix not discovered yet")
            full_path = self.api_prefix + path
        else:
            full_path = path

        url = self.api_base + full_path
        headers = self._headers(method, full_path, body)

        try:
            resp = requests.request(
                method=method.upper(),
                url=url,
                headers=headers,
                data=(body if body_obj is not None else None),
                timeout=timeout,
            )
            code = resp.status_code
            try:
                data = resp.json()
            except Exception:
                data = {"_raw": resp.text}
            if code >= 400:
                logger.error(f"HTTP {method} {full_path} -> {code} {safe_preview(data)}")
            else:
                logger.info(f"[OK] {method} {full_path} -> {code} shape={type(data).__name__} keys={list(data.keys()) if isinstance(data, dict) else 'n/a'}")
            return code, data
        except Exception as e:
            logger.exception(f"HTTP {method} {full_path} failed: {e}")
            return 0, {"error": str(e)}

    # ----------------------------
    # Discovery
    # ----------------------------
    def discover_prefix(self) -> None:
        """
        Find which API prefix works for markets.
        """
        candidates = ["/trade-api/v2", "/trade-api/v1"]

        for p in candidates:
            self.api_prefix = p
            code, data = self.request("GET", "/markets?limit=1", require_prefix=True)
            if code == 200:
                logger.info(f"Discovered API prefix: {p} (probe {p}/markets?limit=1 -> 200)")
                return

        raise RuntimeError("Could not discover API prefix. markets probes failed.")

    # ----------------------------
    # ✅ THIS IS THE ONLY NEW LOGIC: discover_orders_path()
    # ----------------------------
    def discover_orders_path(self) -> None:
        """
        Find the correct endpoint path for creating orders.
        We'll probe common candidates and pick the first that does NOT return 404.
        """
        if not self.api_prefix:
            raise RuntimeError("api_prefix not discovered yet")

        # FULL paths (some include prefix; some absolute fallbacks)
        candidates = [
            f"{self.api_prefix}/orders",
            f"{self.api_prefix}/portfolio/orders",
            f"{self.api_prefix}/exchange/orders",
            f"{self.api_prefix}/trading/orders",
            f"{self.api_prefix}/orders/place",
            "/trade-api/v2/orders",
            "/trade-api/v1/orders",
        ]

        # Minimal invalid body to avoid accidental order placement.
        # If endpoint exists, it should respond 400/401/405 rather than 404.
        probe_body = {"_probe": True}

        for full in candidates:
            if full.startswith(self.api_prefix):
                rel = full.replace(self.api_prefix, "")
                code, data = self.request("POST", rel, body_obj=probe_body, require_prefix=True)
            else:
                code, data = self.request("POST", full, body_obj=probe_body, require_prefix=False)

            if code == 404:
                logger.info(f"[ORDERS_PROBE] {full} -> 404 (not found)")
                continue

            logger.info(f"[ORDERS_PROBE] FOUND orders path: {full} -> HTTP={code} body={safe_preview(data, 400)}")
            self.orders_path = full
            return

        raise RuntimeError("Could not discover a working orders path (all candidates 404).")

    # ----------------------------
    # Market helpers
    # ----------------------------
    def list_markets_for_series(self, series_ticker: str, limit: int = 200) -> List[dict]:
        code, data = self.request("GET", f"/markets?series_ticker={series_ticker}&limit={limit}", require_prefix=True)
        logger.info(f"[SERIES] GET {self.api_prefix}/markets?series_ticker={series_ticker}&limit={limit} -> HTTP={code} shape=dict keys={list(data.keys()) if isinstance(data, dict) else 'n/a'}")
        if code != 200:
            return []
        return data.get("markets", []) or []

    def get_orderbook(self, market_ticker: str) -> Tuple[int, Any]:
        return self.request("GET", f"/markets/{market_ticker}/orderbook", require_prefix=True)

    def create_order(self, order: dict) -> Tuple[int, Any]:
        """
        Use discovered orders_path.
        """
        if not self.orders_path:
            raise RuntimeError("orders_path not discovered yet")

        if self.orders_path.startswith(self.api_prefix):
            rel = self.orders_path.replace(self.api_prefix, "")
            return self.request("POST", rel, body_obj=order, require_prefix=True)
        else:
            return self.request("POST", self.orders_path, body_obj=order, require_prefix=False)


# ----------------------------
# Strategy helpers
# ----------------------------
def parse_iso_z(s: str) -> Optional[dt.datetime]:
    # "2026-01-19T16:15:00Z"
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return dt.datetime.fromisoformat(s)
    except Exception:
        return None


def pick_next_closing_market(markets: List[dict]) -> Optional[dict]:
    now = dt.datetime.now(dt.timezone.utc)
    best = None
    best_close = None

    for m in markets:
        # prefer close_time; fallback expected_expiration_time/open_time
        close_s = m.get("close_time") or m.get("expected_expiration_time") or m.get("expiration_time")
        close_dt = parse_iso_z(close_s) if close_s else None
        if not close_dt:
            continue
        if close_dt <= now:
            continue
        if best is None or close_dt < best_close:
            best = m
            best_close = close_dt

    return best


def best_ask_from_orderbook(ob: dict, side: str) -> Optional[dict]:
    """
    Kalshi orderbook payload includes:
      orderbook: { yes: [[price_cents, qty], ...], no: [[price_cents, qty], ...] }
    where lists are asks at various prices.
    We take the lowest price available as "best ask".
    """
    try:
        book = ob["orderbook"]
        levels = book["yes"] if side.upper() == "YES" else book["no"]
        if not levels:
            return None
        # levels are [price_cents, qty]
        levels_sorted = sorted(levels, key=lambda x: int(x[0]))
        p, q = int(levels_sorted[0][0]), int(levels_sorted[0][1])
        return {"price_cents": p, "qty": q}
    except Exception:
        return None


def build_buy_order(market_ticker: str, side: str, price_cents: int, count: int) -> dict:
    """
    NOTE: exact Kalshi order schema can differ by account/version.
    This matches the typical "orders" shape for trade-api.
    """
    # Side YES/NO, action BUY, type LIMIT
    return {
        "market_ticker": market_ticker,
        "action": "buy",
        "side": side.lower(),   # "yes" / "no"
        "type": "limit",
        "count": int(count),
        "price": int(price_cents),  # cents
        "client_order_id": f"farm-{int(time.time())}-{market_ticker}",
    }


# ----------------------------
# Main
# ----------------------------
def main():
    load_dotenv()

    cfg = load_config()

    logger.info("=== BOT STARTED ===")
    logger.info(f"ENABLE_TRADING={cfg.enable_trading}")
    logger.info(f"CONFIRM_LIVE_TRADING={cfg.confirm_live_trading}")
    logger.info(f"POLL_SECONDS={cfg.poll_seconds}")
    logger.info(f"SERIES_PREFIX={cfg.series_prefix}")
    logger.info(f"MARKET_TICKER={cfg.market_ticker}")
    logger.info(f"API_BASE={cfg.api_base}")
    logger.info(f"SUBACCOUNT={cfg.subaccount}")
    logger.info(f"FARM_SIDE={cfg.farm_side} BUY_PRICE_CENTS={cfg.buy_price_cents} TARGET_PROFIT_CENTS={cfg.target_profit_cents}")
    logger.info(f"SIZING base={cfg.base_size} scale_after_wins={cfg.scale_after_wins} mult={cfg.mult} cap={cfg.cap}")

    api_key = os.getenv("KALSHI_API_KEY") or os.getenv("KALSHI_ACCESS_KEY") or ""
    priv_b64 = os.getenv("KALSHI_PRIVATE_KEY_B64") or ""

    if not api_key:
        raise RuntimeError("Missing env var KALSHI_API_KEY")
    if not priv_b64:
        raise RuntimeError("Missing env var KALSHI_PRIVATE_KEY_B64 (base64 of PEM is OK)")

    kc = KalshiClient(cfg.api_base, api_key, priv_b64, subaccount=cfg.subaccount)

    # 1) discover working API prefix (markets)
    kc.discover_prefix()

    # ✅ 2) NEW: discover working ORDERS endpoint
    kc.discover_orders_path()

    # 3) show we can fetch series and select next market
    markets = kc.list_markets_for_series(cfg.series_prefix, limit=200)
    logger.info(f"[SERIES] Returned markets count={len(markets)}")

    nxt = pick_next_closing_market(markets)
    if not nxt:
        logger.error("No future markets found for series. Exiting.")
        return

    ticker = nxt.get("ticker") or nxt.get("market_ticker") or nxt.get("event_ticker")
    close_s = nxt.get("close_time") or nxt.get("expected_expiration_time")
    close_dt = parse_iso_z(close_s) if close_s else None
    now = dt.datetime.now(dt.timezone.utc)
    seconds_to_close = int((close_dt - now).total_seconds()) if close_dt else -1

    logger.info(
        f"[SELECT] Next closing market: {ticker} "
        f"close={close_s} seconds_to_close={seconds_to_close}"
    )

    # 4) fetch orderbook
    code, ob = kc.get_orderbook(ticker)
    logger.info(f"[BOOK] Used endpoint: {kc.api_prefix}/markets/{ticker}/orderbook HTTP={code}")
    if code != 200:
        logger.error("Orderbook fetch failed. Exiting.")
        return

    logger.info(f"[BOOK] Payload preview: {safe_preview(ob, 700)}")

    yes_best = best_ask_from_orderbook(ob, "YES")
    no_best = best_ask_from_orderbook(ob, "NO")

    logger.info(f"[BEST] {safe_preview({'yes_best_ask': yes_best, 'no_best_ask': no_best}, 400)}")

    if not yes_best or not no_best:
        logger.error("Could not parse best asks. Exiting.")
        return

    logger.info(f"[SANITY] yes_ask={yes_best['price_cents']}c no_ask={no_best['price_cents']}c sum={yes_best['price_cents'] + no_best['price_cents']}c")

    # 5) Decide whether to place a tiny buy (your current test behavior)
    side = cfg.farm_side.upper()
    best_ask = yes_best["price_cents"] if side == "YES" else no_best["price_cents"]
    target_buy = min(best_ask, cfg.buy_price_cents)

    logger.info(f"[STRAT] side={side} best_ask={best_ask}c target_buy={target_buy}c")

    # If you want the bot to ONLY buy when it can get at/below target, keep this guard.
    if best_ask > target_buy:
        logger.info(f"[STRAT] Best ask {best_ask}c is above target {target_buy}c. No trade.")
        return

    order = build_buy_order(ticker, side, target_buy, cfg.base_size)
    logger.info(f"[ORDER] BUY {side} {cfg.base_size}@{target_buy}c on {ticker}")

    if not (cfg.enable_trading and cfg.confirm_live_trading):
        logger.warning("[ORDER] Trading disabled by flags. Set ENABLE_TRADING=true and CONFIRM_LIVE_TRADING=true to place live orders.")
        return

    # 6) Place order (THIS is where you were getting 404 before)
    code, data = kc.create_order(order)
    if code in (200, 201):
        logger.info(f"[ORDER] success HTTP={code} body={safe_preview(data, 800)}")
    else:
        logger.error(f"[ORDER] buy failed HTTP={code} body={safe_preview(data, 800)}")

    logger.info("[HEARTBEAT] done")


if __name__ == "__main__":
    main()