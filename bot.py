import os
import json
import time
import base64
import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple, List

import requests
from dotenv import load_dotenv

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


# ----------------------------
# Logging (very verbose by design)
# ----------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("kalshi-bot")


# ----------------------------
# Helpers
# ----------------------------
def b64decode_str(s: str) -> bytes:
    # tolerate missing padding
    s = s.strip()
    pad = (-len(s)) % 4
    if pad:
        s += "=" * pad
    return base64.b64decode(s.encode("utf-8"))

def safe_json(obj: Any) -> str:
    try:
        return json.dumps(obj, indent=2, sort_keys=True)[:8000]
    except Exception:
        return str(obj)[:8000]

def is_dict(x: Any) -> bool:
    return isinstance(x, dict)

def is_list(x: Any) -> bool:
    return isinstance(x, list)


# ----------------------------
# Kalshi Client (RSA-PSS signing)
# ----------------------------
@dataclass
class KalshiConfig:
    api_base: str
    api_prefix: str
    key_id: str
    private_key_pem_b64: str
    subaccount: Optional[str] = None
    timeout_s: int = 20


class KalshiClient:
    def __init__(self, cfg: KalshiConfig):
        self.cfg = cfg
        self.session = requests.Session()
        self._private_key = self._load_private_key(cfg.private_key_pem_b64)

    def _load_private_key(self, pem_b64: str):
        raw = b64decode_str(pem_b64)

        # If user base64’d the PEM text, raw will start with b'-----BEGIN'
        # If user base64’d DER, it won’t. Handle both.
        if raw.lstrip().startswith(b"-----BEGIN"):
            pem_bytes = raw
        else:
            # Assume DER -> convert to PEM attempt is not feasible reliably.
            # But most users base64 the PEM text; if not, fail loudly.
            raise RuntimeError(
                "KALSHI_PRIVATE_KEY_B64 did not decode to a PEM that starts with '-----BEGIN'. "
                "Base64 encode the FULL PEM file contents including BEGIN/END lines."
            )

        try:
            key = serialization.load_pem_private_key(pem_bytes, password=None)
        except Exception as e:
            raise RuntimeError(f"Failed to load private key PEM: {e}")

        # We expect RSA based on your key format
        # (Kalshi supports RSA signing; we use RSA-PSS)
        return key

    def _sign(self, message: bytes) -> str:
        sig = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode("utf-8")

    def _headers(self, method: str, path_with_prefix: str, body: bytes) -> Dict[str, str]:
        # Timestamp (milliseconds) is typical; your prior logs suggest time-based signing.
        ts_ms = str(int(time.time() * 1000))

        # Canonical message:
        # method + path + timestamp + body_hash
        # NOTE: If Kalshi changes canonicalization, our debug logs will show 401/403 clearly.
        body_hash = hashlib.sha256(body).hexdigest()
        canonical = f"{method.upper()}\n{path_with_prefix}\n{ts_ms}\n{body_hash}".encode("utf-8")
        signature_b64 = self._sign(canonical)

        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "KALSHI-ACCESS-KEY": self.cfg.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts_ms,
            "KALSHI-ACCESS-SIGNATURE": signature_b64,
        }

    def request(self, method: str, path: str, payload: Optional[dict] = None) -> Tuple[int, Any]:
        # path should already include prefix like /trade-api/v2/...
        url = self.cfg.api_base.rstrip("/") + path
        body = b""
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

        headers = self._headers(method, path, body)

        # Debug: show request metadata (never print full signature)
        log.debug(
            "HTTP %s %s\nHeaders: key=%s ts=%s sig_len=%d body_len=%d",
            method.upper(),
            path,
            headers.get("KALSHI-ACCESS-KEY"),
            headers.get("KALSHI-ACCESS-TIMESTAMP"),
            len(headers.get("KALSHI-ACCESS-SIGNATURE", "")),
            len(body),
        )
        if payload is not None:
            log.debug("Payload: %s", safe_json(payload))

        try:
            resp = self.session.request(
                method=method.upper(),
                url=url,
                headers=headers,
                data=body if body else None,
                timeout=self.cfg.timeout_s,
            )
        except Exception as e:
            log.error("HTTP %s %s -> NETWORK ERROR: %s", method.upper(), path, e)
            return 0, {"_error": str(e)}

        text = resp.text or ""
        data: Any = None
        try:
            data = resp.json()
        except Exception:
            data = {"_raw": text[:8000]}

        if resp.status_code >= 400:
            log.error("HTTP %s %s -> %d %s", method.upper(), path, resp.status_code, text[:500])

        # Extra debug about response structure
        if is_dict(data):
            log.debug("Response keys: %s", list(data.keys()))
        elif is_list(data):
            log.debug("Response is list len=%d", len(data))
        else:
            log.debug("Response type=%s", type(data).__name__)

        return resp.status_code, data

    # ---- High-level endpoints ----
    def get_balance(self) -> Tuple[int, Any]:
        # Correct endpoint: /portfolio/balance  [oai_citation:2‡Kalshi API Documentation](https://docs.kalshi.com/api-reference/live-data/get-live-data?utm_source=chatgpt.com)
        return self.request("GET", f"{self.cfg.api_prefix}/portfolio/balance")

    def get_market(self, ticker: str) -> Tuple[int, Any]:
        return self.request("GET", f"{self.cfg.api_prefix}/markets/{ticker}")

    def get_markets(self, series_ticker: str, status: str = "open", limit: int = 200) -> Tuple[int, Any]:
        # Get Markets exists under market section in docs  [oai_citation:3‡Kalshi API Documentation](https://docs.kalshi.com/api-reference/live-data/get-live-data?utm_source=chatgpt.com)
        # Parameter names can vary; we log responses if mismatched.
        qs = f"?limit={limit}&status={status}&series_ticker={series_ticker}"
        return self.request("GET", f"{self.cfg.api_prefix}/markets{qs}")

    def get_orderbook(self, ticker: str) -> Tuple[int, Any]:
        return self.request("GET", f"{self.cfg.api_prefix}/markets/{ticker}/orderbook")

    def create_order(self, ticker: str, side: str, price: int, count: int, order_type: str = "limit") -> Tuple[int, Any]:
        payload = {
            "ticker": ticker,
            "side": side,          # "yes" / "no" often used; depends on market
            "type": order_type,    # "limit"
            "price": price,        # cents
            "count": count,
        }
        if self.cfg.subaccount:
            payload["subaccount"] = self.cfg.subaccount
        return self.request("POST", f"{self.cfg.api_prefix}/orders", payload=payload)


# ----------------------------
# Strategy / Market selection
# ----------------------------
def extract_ticker_from_url(url: str) -> Optional[str]:
    try:
        base = url.split("?")[0]
        tail = base.rstrip("/").split("/")[-1]
        return tail if tail else None
    except Exception:
        return None

def pick_next_open_market(markets_payload: Any) -> Optional[str]:
    # We don’t assume exact schema; we try common ones and log.
    if not is_dict(markets_payload):
        return None

    candidates = None
    for key in ["markets", "data", "results"]:
        if key in markets_payload and is_list(markets_payload[key]):
            candidates = markets_payload[key]
            break

    if not candidates:
        # some APIs return list directly; handled earlier
        return None

    # Prefer earliest close or soonest start if present
    def sort_key(m: dict):
        # Try fields commonly seen in exchange APIs:
        return (
            m.get("close_time", 10**18),
            m.get("end_time", 10**18),
            m.get("open_time", 10**18),
            m.get("start_time", 10**18),
        )

    try:
        candidates_sorted = sorted([m for m in candidates if is_dict(m)], key=sort_key)
    except Exception:
        candidates_sorted = [m for m in candidates if is_dict(m)]

    for m in candidates_sorted[:50]:
        t = m.get("ticker") or m.get("market_ticker")
        if t:
            return t
    return None


# ----------------------------
# Main loop
# ----------------------------
def main():
    load_dotenv()

    api_base = os.getenv("API_BASE", "https://api.elections.kalshi.com")
    api_prefix = os.getenv("API_PREFIX", "/trade-api/v2")

    key_id = os.getenv("KALSHI_KEY_ID", "").strip()
    private_key_b64 = os.getenv("KALSHI_PRIVATE_KEY_B64", "").strip()

    # Market selection inputs
    market_ticker = os.getenv("MARKET_TICKER", "").strip()
    market_url = os.getenv("MARKET_URL", "").strip()
    series_ticker = os