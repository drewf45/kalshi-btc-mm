import os
import time
import json
import base64
import logging
import datetime
from typing import Any, Dict, Optional, Tuple, List
from zoneinfo import ZoneInfo

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


# ----------------------------
# Logging
# ----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("kalshi-btc-mm")


# ----------------------------
# Config helpers
# ----------------------------
def env_bool(key: str, default: bool = False) -> bool:
    v = os.getenv(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")

def env_int(key: str, default: int) -> int:
    v = os.getenv(key)
    if v is None:
        return default
    try:
        return int(v.strip())
    except Exception:
        return default

def pick_env(*keys: str) -> Optional[str]:
    for k in keys:
        v = os.getenv(k)
        if v and v.strip():
            return v.strip()
    return None

def b64decode_forgiving(s: str) -> bytes:
    # Removes whitespace/newlines and adds padding if needed
    compact = "".join(s.split())
    # add padding
    pad = (-len(compact)) % 4
    if pad:
        compact += "=" * pad
    return base64.b64decode(compact)


# ----------------------------
# Kalshi Client
# ----------------------------
class KalshiClient:
    def __init__(self, api_base: str, api_key_id: str, private_key_pem_b64: Optional[str]):
        self.api_base = api_base.rstrip("/")
        self.api_key_id = api_key_id

        self.private_key = None
        if private_key_pem_b64:
            try:
                pem_bytes = b64decode_forgiving(private_key_pem_b64)
                self.private_key = serialization.load_pem_private_key(pem_bytes, password=None)
            except Exception as e:
                log.error("Failed to load private key from base64 PEM: %s", e)
                self.private_key = None

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "kalshi-btc-mm/1.0"})

    def _sign(self, timestamp_ms: str, method: str, path: str) -> str:
        if not self.private_key:
            raise RuntimeError("Missing private key")
        path_wo_query = path.split("?")[0]
        message = f"{timestamp_ms}{method}{path_wo_query}".encode("utf-8")
        sig = self.private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH
            ),
            hashes.SHA256()
        )
        return base64.b64encode(sig).decode("utf-8")

    def request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None, json_body: Any = None) -> Dict[str, Any]:
        url = self.api_base + path
        timestamp_ms = str(int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000))

        headers = {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms
        }
        if self.private_key:
            headers["KALSHI-ACCESS-SIGNATURE"] = self._sign(timestamp_ms, method.upper(), path)

        resp = self.session.request(method=method.upper(), url=url, params=params, json=json_body, headers=headers, timeout=20)
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code} {resp.text[:500]}")
        try:
            return resp.json()
        except Exception:
            return {"raw": resp.text}

    def get_balance_cents(self) -> Optional[int]:
        data = self.request("GET", "/trade-api/v2/portfolio/balance")
        bal = data.get("balance")
        return bal if isinstance(bal, int) else None

    def get_market(self, ticker: str) -> Dict[str, Any]:
        return self.request("GET", f"/trade-api/v2/markets/{ticker}")

    def get_orderbook(self, ticker: str) -> Dict[str, Any]:
        return self.request("GET", f"/trade-api/v2/markets/{ticker}/orderbook")

    def list_markets(self, series_ticker: str, status: str = "open", limit: int = 200) -> Dict[str, Any]:
        params = {"limit": limit, "status": status, "series_ticker": series_ticker}
        return self.request("GET", "/trade-api/v2/markets", params=params)


# ----------------------------
# Time / ticker logic
# NOTE: Kalshi real ticker here includes "-30"
# Example resolved: KXBTC15M-26JAN181730-30
# ----------------------------
ET = ZoneInfo("America/New_York")

def next_15m_boundary_et(now_et: datetime.datetime) -> datetime.datetime:
    minute = now_et.minute
    add = (15 - (minute % 15)) % 15
    if add == 0:
        add = 15
    return (now_et.replace(second=0, microsecond=0) + datetime.timedelta(minutes=add))

def format_market_ticker(series_prefix: str, boundary_et: datetime.datetime) -> str:
    yy = f"{boundary_et.year % 100:02d}"
    mon = boundary_et.strftime("%b").upper()  # JAN
    dd = f"{boundary_et.day:02d}"
    hhmm = boundary_et.strftime("%H%M")
    # IMPORTANT: add -30 suffix (observed in your live market)
    return f"{series_prefix}-{yy}{mon}{dd}{hhmm}-30"


# ----------------------------
# Orderbook parsing (handles multiple shapes)
# ----------------------------
def orderbook_side_shares(orderbook: Dict[str, Any]) -> Tuple[int, int]:
    # Common shapes we might see:
    # 1) {"orderbook": {"yes": [[price, qty], ...], "no": [[price, qty], ...]}}
    # 2) {"orderbook": {"yes": [{"price":..,"quantity":..}], "no": [...]}}
    # 3) {"yes": [...], "no": [...]} (rare)
    ob = None
    if isinstance(orderbook, dict):
        if isinstance(orderbook.get("orderbook"), dict):
            ob = orderbook["orderbook"]
        elif "yes" in orderbook or "no" in orderbook:
            ob = orderbook

    if not isinstance(ob, dict):
        return (0, 0)

    def sum_qty(levels: Any) -> int:
        total = 0
        if not isinstance(levels, list):
            return 0
        for lvl in levels:
            # [[price, qty], ...]
            if isinstance(lvl, (list, tuple)) and len(lvl) >= 2 and isinstance(lvl[1], int):
                total += lvl[1]
                continue
            # [{"price":..,"quantity":..}, ...]
            if isinstance(lvl, dict):
                q = lvl.get("quantity") or lvl.get("qty") or lvl.get("size")
                if isinstance(q, int):
                    total += q
        return total

    yes_levels = ob.get("yes") or []
    no_levels = ob.get("no") or []
    return (sum_qty(yes_levels), sum_qty(no_levels))


# ----------------------------
# Main loop
# ----------------------------
def main():
    # Force correct default base
    api_base = os.getenv("KALSHI_API_BASE", "https://api.kalshi.com").strip()

    api_key_id = os.getenv("KALSHI_API_KEY_ID", "").strip()

    # Accept the correct name + your current wrong name so it still works,
    # but you should rename the env var to BASE64.
    private_key_b64 = pick_env(
        "KALSHI_PRIVATE_KEY_PEM_BASE64",  # correct
        "KALSHI_PRIVATE_KEY_PEM_BASE4",   # your current env var in screenshot (wrong)
        "KALSHI_PRIVATE_KEY_PEM_Base64",  # older wrong-case variant
    )

    series_prefix = os.getenv("SERIES_PREFIX", "KXBTC15M").strip()

    poll_seconds = env_int("POLL_SECONDS", 60)
    enable_trading = env_bool("ENABLE_TRADING", False)
    extreme_threshold = float(os.getenv("EXTREME_THRESHOLD", "0.95").strip() or "0.95")

    log.info("=== BOT STARTED ===")
    log.info("ENABLE_TRADING=%s", enable_trading)
    log.info("POLL_SECONDS=%s", poll_seconds)
    log.info("SERIES_PREFIX=%s", series_prefix)
    log.info("API_BASE=%s", api_base)

    if not api_key_id:
        raise RuntimeError("Missing KALSHI_API_KEY_ID")

    client = KalshiClient(api_base=api_base, api_key_id=api_key_id, private_key_pem_b64=private_key_b64)

    if not client.private_key:
        log.warning("Kalshi credentials missing/invalid private key -> running in READ-ONLY mode")
        log.warning("Fix by setting KALSHI_PRIVATE_KEY_PEM_BASE64 to base64(PEM file contents).")

    while True:
        try:
            now_et = datetime.datetime.now(ET)
            boundary_et = next_15m_boundary_et(now_et)
            ticker = format_market_ticker(series_prefix, boundary_et)

            log.info("Heartbeat ET now=%s | next15=%s | ticker=%s",
                     now_et.strftime("%Y-%m-%d %H:%M:%S %Z"),
                     boundary_et.strftime("%Y-%m-%d %H:%M:%S %Z"),
                     ticker)

            # Fetch market
            try:
                market = client.get_market(ticker)
                resolved = market.get("ticker") if isinstance(market, dict) else ticker
                log.info("Market resolved (direct): %s", resolved)
            except Exception as e:
                log.warning("Direct market fetch failed for %s: %r", ticker, e)
                # Fallback listing
                listing = client.list_markets(series_ticker=series_prefix, status="open", limit=200)
                markets = listing.get("markets") or listing.get("data") or listing.get("results") or []
                candidate = None
                if isinstance(markets, list):
                    for m in markets:
                        if isinstance(m, dict) and isinstance(m.get("ticker"), str) and m["ticker"].startswith(f"{series_prefix}-"):
                            # pick the first that contains our boundary HHMM
                            if boundary_et.strftime("%H%M") in m["ticker"]:
                                candidate = m["ticker"]
                                break
                if not candidate:
                    raise RuntimeError("Fallback could not find candidate ticker")
                market = client.get_market(candidate)
                resolved = market.get("ticker") if isinstance(market, dict) else candidate
                log.info("Market resolved (fallback): %s", resolved)
                ticker = resolved

            # Orderbook
            try:
                ob = client.get_orderbook(ticker)
                yes_shares, no_shares = orderbook_side_shares(ob)
                total = yes_shares + no_shares
                if total <= 0:
                    keys = list(ob.keys()) if isinstance(ob, dict) else []
                    log.warning("Orderbook unparseable/empty. Top-level keys=%s", keys)
                else:
                    yes_frac = yes_shares / total
                    no_frac = no_shares / total
                    log.info("Orderbook shares YES=%s NO=%s | YES%%=%.3f NO%%=%.3f",
                             yes_shares, no_shares, yes_frac, no_frac)

                    heavy_side = None
                    if yes_frac >= extreme_threshold:
                        heavy_side = "YES"
                    elif no_frac >= extreme_threshold:
                        heavy_side = "NO"

                    if heavy_side:
                        log.info("EXTREME: %.2f%% on %s side (threshold=%.2f%%)",
                                 100.0 * max(yes_frac, no_frac), heavy_side, 100.0 * extreme_threshold)
            except Exception as e:
                log.warning("Orderbook fetch/parse failed: %r", e)

        except Exception as e:
            log.error("LOOP ERROR: %r", e)

        time.sleep(poll_seconds)


if __name__ == "__main__":
    main()