import os
import time
import json
import base64
import uuid
import datetime as dt
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple, List

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend


# ----------------------------
# Config
# ----------------------------
def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    return int(v)


def env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    return float(v)


@dataclass
class BotConfig:
    # Kalshi
    api_base: str
    api_key_id: str
    private_key_pem_b64: str

    # Strategy
    series_ticker: str
    poll_seconds: int
    enable_trading: bool
    dry_run: bool

    # What you asked for:
    # "Both market" => buy YES and buy NO (1 each) per market
    trade_both_sides: bool
    contracts_per_side: int

    # "Take profit 1 dollar per trade" -> we interpret as cents in Kalshi price ladder
    # default: +1 cent
    take_profit_cents: int

    # Risk limits
    max_spend_dollars_per_market: float  # total spend cap per market ticker
    max_open_orders_per_market: int      # safety cap


def load_config() -> BotConfig:
    api_base = os.getenv("KALSHI_API_BASE", "https://api.elections.kalshi.com").strip()
    # Auto-fix common wrong host from earlier logs
    if api_base == "https://api.kalshi.com":
        # sometimes DNS failed on Render for you; keep elections default unless you override
        pass

    api_key_id = os.getenv("KALSHI_API_KEY_ID", "").strip()
    private_key_pem_b64 = os.getenv("KALSHI_PRIVATE_KEY_PEM_BASE64", "").strip()

    if not api_key_id:
        raise RuntimeError("Missing env var KALSHI_API_KEY_ID")
    if not private_key_pem_b64:
        raise RuntimeError(
            "Missing env var KALSHI_PRIVATE_KEY_PEM_BASE64. "
            "You must create an API key and download the .key file; then base64 it and set this env var."
        )

    return BotConfig(
        api_base=api_base.rstrip("/"),
        api_key_id=api_key_id,
        private_key_pem_b64=private_key_pem_b64,
        series_ticker=os.getenv("SERIES_PREFIX", "KXBTC15M").strip(),
        poll_seconds=env_int("POLL_SECONDS", 60),
        enable_trading=env_bool("ENABLE_TRADING", False),
        dry_run=env_bool("DRY_RUN", False),
        trade_both_sides=env_bool("TRADE_BOTH_SIDES", True),
        contracts_per_side=env_int("CONTRACTS_PER_SIDE", 1),
        take_profit_cents=env_int("TAKE_PROFIT_CENTS", 1),
        max_spend_dollars_per_market=env_float("MAX_SPEND_DOLLARS_PER_MARKET", 2.50),
        max_open_orders_per_market=env_int("MAX_OPEN_ORDERS_PER_MARKET", 6),
    )


# ----------------------------
# Kalshi Auth (RSA-PSS)
# Signature message = timestamp + METHOD + path_without_query
# ----------------------------
class KalshiClient:
    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.private_key = self._load_private_key(cfg.private_key_pem_b64)
        self.sess = requests.Session()
        self.sess.headers.update({"User-Agent": "kalshi-bot/1.0"})

    @staticmethod
    def _load_private_key(pem_b64: str):
        try:
            pem_bytes = base64.b64decode(pem_b64)
        except Exception as e:
            raise RuntimeError("KALSHI_PRIVATE_KEY_PEM_BASE64 is not valid base64") from e
        try:
            return serialization.load_pem_private_key(
                pem_bytes, password=None, backend=default_backend()
            )
        except Exception as e:
            raise RuntimeError("Failed to load private key. Is it the downloaded Kalshi .key PEM?") from e

    def _timestamp_ms(self) -> str:
        return str(int(dt.datetime.now().timestamp() * 1000))

    def _sign(self, timestamp_ms: str, method: str, path: str) -> str:
        path_wo_query = path.split("?")[0]
        message = f"{timestamp_ms}{method.upper()}{path_wo_query}".encode("utf-8")
        sig = self.private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode("utf-8")

    def _headers(self, method: str, path: str) -> Dict[str, str]:
        ts = self._timestamp_ms()
        sig = self._sign(ts, method, path)
        return {
            "KALSHI-ACCESS-KEY": self.cfg.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

    def get(self, path: str, auth: bool = False, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = self.cfg.api_base + path
        headers = self._headers("GET", path) if auth else {}
        r = self.sess.get(url, headers=headers, params=params, timeout=15)
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code} {r.text}")
        return r.json()

    def post(self, path: str, data: Dict[str, Any], auth: bool = True) -> Dict[str, Any]:
        url = self.cfg.api_base + path
        headers = self._headers("POST", path) if auth else {}
        headers["Content-Type"] = "application/json"
        r = self.sess.post(url, headers=headers, json=data, timeout=15)
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code} {r.text}")
        return r.json()

    # ---- endpoints we use ----
    def get_balance_cents(self) -> int:
        j = self.get("/trade-api/v2/portfolio/balance", auth=True)
        # docs show balance in cents
        return int(j["balance"])

    def list_markets(self, series_ticker: str, status: str = "open", limit: int = 200) -> List[Dict[str, Any]]:
        params = {"limit": limit, "status": status, "series_ticker": series_ticker}
        j = self.get("/trade-api/v2/markets", auth=False, params=params)
        return j.get("markets", [])

    def get_market(self, ticker: str) -> Dict[str, Any]:
        j = self.get(f"/trade-api/v2/markets/{ticker}", auth=False)
        return j.get("market", j)

    def list_orders(self, ticker: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        # For signing we must sign the path without query, but we can pass params separately.
        params: Dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        j = self.get("/trade-api/v2/portfolio/orders", auth=True, params=params)
        return j.get("orders", [])

    def create_order(
        self,
        ticker: str,
        action: str,   # "buy" or "sell"
        side: str,     # "yes" or "no"
        count: int,
        order_type: str,  # "limit"
        yes_price: Optional[int] = None,
        no_price: Optional[int] = None,
        client_order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        # Kalshi expects either yes_price or no_price depending on side
        payload: Dict[str, Any] = {
            "ticker": ticker,
            "action": action,
            "side": side,
            "count": count,
            "type": order_type,
            "client_order_id": client_order_id or str(uuid.uuid4()),
        }
        if side == "yes":
            if yes_price is None:
                raise ValueError("yes_price required when side=yes")
            payload["yes_price"] = int(yes_price)
        else:
            if no_price is None:
                raise ValueError("no_price required when side=no")
            payload["no_price"] = int(no_price)

        # Docs: POST /trade-api/v2/portfolio/orders  [oai_citation:2‡Kalshi API Documentation](https://docs.kalshi.com/getting_started/quick_start_create_order)
        return self.post("/trade-api/v2/portfolio/orders", payload, auth=True)


# ----------------------------
# Helpers / Strategy
# ----------------------------
def now_et() -> dt.datetime:
    # You were logging ET; we’ll keep it simple: local server time may be UTC on Render,
    # but market selection is based on open markets list anyway.
    return dt.datetime.utcnow()


def pick_next_open_market(markets: List[Dict[str, Any]]) -> Optional[str]:
    """
    Your log shows open markets returned: 1, so this works either way.
    If multiple, pick the one with earliest close_time / end_time if present.
    """
    if not markets:
        return None

    # Try common fields: close_time, end_time, expiration_time
    def key(m: Dict[str, Any]) -> Tuple:
        for f in ("close_time", "end_time", "expiration_time"):
            if f in m and m[f]:
                return (m[f], m.get("ticker", ""))
        return ("9999-12-31T00:00:00Z", m.get("ticker", ""))

    markets_sorted = sorted(markets, key=key)
    return markets_sorted[0].get("ticker")


def cents_to_dollars(c: int) -> float:
    return c / 100.0


def safe_price(p: Any) -> Optional[int]:
    try:
        if p is None:
            return None
        return int(p)
    except Exception:
        return None


def main():
    cfg = load_config()
    print("=== BOT STARTED ===")
    print(f"ENABLE_TRADING={cfg.enable_trading} DRY_RUN={cfg.dry_run}")
    print(f"POLL_SECONDS={cfg.poll_seconds}")
    print(f"SERIES_PREFIX={cfg.series_ticker}")
    print(f"API_BASE={cfg.api_base}")
    print(f"TRADE_BOTH_SIDES={cfg.trade_both_sides} CONTRACTS_PER_SIDE={cfg.contracts_per_side}")
    print(f"TAKE_PROFIT_CENTS={cfg.take_profit_cents} MAX_SPEND_DOLLARS_PER_MARKET={cfg.max_spend_dollars_per_market}")

    client = KalshiClient(cfg)

    last_traded_ticker: Optional[str] = None

    while True:
        t0 = time.time()
        try:
            et = now_et()
            print(f"\nHeartbeat (UTC) now={et.isoformat(timespec='seconds')} | resolving NEXT open market from series {cfg.series_ticker}")

            open_markets = client.list_markets(cfg.series_ticker, status="open", limit=200)
            print(f"Open markets returned: {len(open_markets)}")

            ticker = pick_next_open_market(open_markets)
            if not ticker:
                print("No open market found. Sleeping.")
                time.sleep(cfg.poll_seconds)
                continue

            print(f"Resolved next market ticker={ticker}")

            m = client.get_market(ticker)
            # Market object can vary; handle gracefully
            status = m.get("status", m.get("market_status", "unknown"))

            yes_bid = safe_price(m.get("yes_bid"))
            yes_ask = safe_price(m.get("yes_ask"))
            no_bid = safe_price(m.get("no_bid"))
            no_ask = safe_price(m.get("no_ask"))

            print("Market fetched OK (resolved).")
            print(f"market_ticker={ticker} status={status} yes_bid={yes_bid} yes_ask={yes_ask} no_bid={no_bid} no_ask={no_ask}")

            if not cfg.enable_trading:
                print("ENABLE_TRADING=False; not placing orders.")
            else:
                if last_traded_ticker == ticker:
                    print("Already traded this ticker; waiting for next market.")
                else:
                    # Basic sanity
                    if yes_ask is None or no_ask is None:
                        print("Missing asks; skipping.")
                    else:
                        balance_cents = client.get_balance_cents()
                        balance_dollars = cents_to_dollars(balance_cents)
                        print(f"Balance=${balance_dollars:.2f}")

                        # Spend cap (in cents)
                        max_spend_cents = int(cfg.max_spend_dollars_per_market * 100)

                        # Estimate cost:
                        est_yes_cost = yes_ask * cfg.contracts_per_side if cfg.trade_both_sides else 0
                        est_no_cost = no_ask * cfg.contracts_per_side if cfg.trade_both_sides else 0
                        est_total = est_yes_cost + est_no_cost

                        if est_total > max_spend_cents:
                            print(f"Estimated spend {est_total}c exceeds cap {max_spend_cents}c; skipping.")
                        elif est_total > balance_cents:
                            print("Not enough balance for this market; skipping.")
                        else:
                            # Safety: do not spam orders if already too many open orders on this ticker
                            try:
                                existing_orders = client.list_orders(ticker=ticker, limit=50)
                                open_like = [o for o in existing_orders if o.get("status") in ("open", "resting", "pending")]
                                if len(open_like) >= cfg.max_open_orders_per_market:
                                    print(f"Too many open-like orders for {ticker} ({len(open_like)}); skipping.")
                                    raise StopIteration
                            except StopIteration:
                                pass
                            except Exception as e:
                                # If orders endpoint fails, do not place trades blindly.
                                print(f"Could not verify existing orders; skipping trade for safety. err={e}")
                                time.sleep(cfg.poll_seconds)
                                continue

                            # Place entry orders:
                            # Buy YES at current ask, Buy NO at current ask (what you called "both market")
                            # Then place take-profit SELL at entry_price + TAKE_PROFIT_CENTS
                            def place_with_tp(side: str, entry_price: int):
                                if cfg.dry_run:
                                    print(f"[DRY_RUN] Would BUY {cfg.contracts_per_side} {side.upper()} @ {entry_price}c")
                                    print(f"[DRY_RUN] Would SELL {cfg.contracts_per_side} {side.upper()} @ {min(entry_price + cfg.take_profit_cents, 99)}c (TP)")
                                    return

                                # BUY
                                if side == "yes":
                                    buy_resp = client.create_order(
                                        ticker=ticker, action="buy", side="yes",
                                        count=cfg.contracts_per_side, order_type="limit",
                                        yes_price=entry_price
                                    )
                                else:
                                    buy_resp = client.create_order(
                                        ticker=ticker, action="buy", side="no",
                                        count=cfg.contracts_per_side, order_type="limit",
                                        no_price=entry_price
                                    )
                                print(f"BUY {side.upper()} placed: {json.dumps(buy_resp)[:300]}...")

                                # TAKE PROFIT SELL (limit)
                                tp_price = min(entry_price + cfg.take_profit_cents, 99)
                                if side == "yes":
                                    sell_resp = client.create_order(
                                        ticker=ticker, action="sell", side="yes",
                                        count=cfg.contracts_per_side, order_type="limit",
                                        yes_price=tp_price
                                    )
                                else:
                                    sell_resp = client.create_order(
                                        ticker=ticker, action="sell", side="no",
                                        count=cfg.contracts_per_side, order_type="limit",
                                        no_price=tp_price
                                    )
                                print(f"TP SELL {side.upper()} placed: {json.dumps(sell_resp)[:300]}...")

                            if cfg.trade_both_sides:
                                place_with_tp("yes", yes_ask)
                                place_with_tp("no", no_ask)
                            else:
                                # If you ever flip this off, default to trading the cheaper side
                                if yes_ask <= no_ask:
                                    place_with_tp("yes", yes_ask)
                                else:
                                    place_with_tp("no", no_ask)

                            last_traded_ticker = ticker
                            print("Trade cycle complete for this ticker.")

            elapsed = time.time() - t0
            print(f"Loop complete in {elapsed:.2f}s; sleeping {cfg.poll_seconds}s")
            time.sleep(cfg.poll_seconds)

        except Exception as e:
            print(f"LOOP ERROR: {repr(e)}")
            time.sleep(cfg.poll_seconds)


if __name__ == "__main__":
    main()
