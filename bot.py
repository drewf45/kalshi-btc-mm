import os
import time
import json
import base64
import hashlib
import logging
from datetime import datetime

import requests
from dotenv import load_dotenv

from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa, padding
from cryptography.exceptions import UnsupportedAlgorithm

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
load_dotenv()

API_BASE = os.getenv("API_BASE", "https://api.elections.kalshi.com").rstrip("/")
KALSHI_KEY_ID = os.getenv("KALSHI_KEY_ID", "").strip()
KALSHI_PRIVATE_KEY_B64 = os.getenv("KALSHI_PRIVATE_KEY_B64", "").strip()

ENABLE_TRADING = os.getenv("ENABLE_TRADING", "false").lower() == "true"
CONFIRM_LIVE_TRADING = os.getenv("CONFIRM_LIVE_TRADING", "false").lower() == "true"

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
SERIES_PREFIX = os.getenv("SERIES_PREFIX", "KXBTC15M").strip()
MARKET_TICKER = os.getenv("MARKET_TICKER", "").strip() or None
SUBACCOUNT = os.getenv("SUBACCOUNT", "").strip() or None

def shape_of(x):
    if isinstance(x, dict):
        return f"dict keys={list(x.keys())}"
    if isinstance(x, list):
        return f"list len={len(x)}"
    return type(x).__name__

def parse_iso_ts(ts: str) -> float | None:
    if not ts or not isinstance(ts, str):
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts).timestamp()
    except Exception:
        return None

class KalshiClient:
    def __init__(self, base: str, key_id: str, private_key_b64: str, subaccount=None):
        self.base = base.rstrip("/")
        self.key_id = key_id
        self.subaccount = subaccount
        if not self.key_id:
            raise RuntimeError("Missing env var: KALSHI_KEY_ID")
        if not private_key_b64:
            raise RuntimeError("Missing env var: KALSHI_PRIVATE_KEY_B64")
        self.private_key = self._load_private_key(private_key_b64)
        self.prefix = None

    def _load_private_key(self, b64: str):
        raw = base64.b64decode(b64)

        if raw.startswith(b"-----BEGIN"):
            try:
                key = serialization.load_pem_private_key(raw, password=None)
            except (ValueError, UnsupportedAlgorithm) as e:
                raise RuntimeError(f"Could not load PEM private key: {e}")

            if isinstance(key, rsa.RSAPrivateKey):
                logging.info("Loaded RSA private key (PEM).")
                return key
            if isinstance(key, ed25519.Ed25519PrivateKey):
                logging.info("Loaded Ed25519 private key (PEM).")
                return key
            raise RuntimeError(f"Unsupported PEM key type: {type(key)}")

        if len(raw) in (32, 64):
            try:
                key = ed25519.Ed25519PrivateKey.from_private_bytes(raw[:32])
                logging.info("Loaded Ed25519 private key (raw bytes).")
                return key
            except Exception as e:
                raise RuntimeError(f"Could not load Ed25519 private key bytes: {e}")

        raise RuntimeError(
            f"Private key format not recognized. decoded_len={len(raw)}. "
            "Expected PEM or Ed25519 bytes length 32/64."
        )

    def _sign(self, msg: bytes) -> str:
        if isinstance(self.private_key, rsa.RSAPrivateKey):
            sig = self.private_key.sign(msg, padding.PKCS1v15(), hashes.SHA256())
            return base64.b64encode(sig).decode()
        if isinstance(self.private_key, ed25519.Ed25519PrivateKey):
            sig = self.private_key.sign(msg)
            return base64.b64encode(sig).decode()
        raise RuntimeError(f"Unsupported key type: {type(self.private_key)}")

    def _headers(self, method: str, path: str, body: bytes | None):
        ts = str(int(time.time()))
        body_hash = hashlib.sha256(body or b"").hexdigest()
        signing_str = "\n".join([ts, method.upper(), path, body_hash]).encode()
        sig_b64 = self._sign(signing_str)

        headers = {
            "Content-Type": "application/json",
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sig_b64,
        }
        if self.subaccount:
            headers["KALSHI-SUBACCOUNT"] = self.subaccount
        return headers

    def request(self, method: str, path: str, payload=None, timeout=15):
        url = self.base + path
        body = None if payload is None else json.dumps(payload).encode()
        headers = self._headers(method, path, body)
        try:
            r = requests.request(method, url, headers=headers, data=body, timeout=timeout)
        except Exception as e:
            logging.error(f"HTTP {method} {path} exception: {e}")
            return 0, {"_raw": str(e)}

        txt = r.text
        try:
            data = r.json()
        except Exception:
            data = {"_raw": txt}

        if r.status_code >= 400:
            logging.error(f"HTTP {method} {path} -> {r.status_code} {txt[:400]}")
        return r.status_code, data

    def discover_prefix(self):
        candidates = ["/trade-api/v2", "/trade-api/v1", "/trade-api", ""]
        for p in candidates:
            code, _ = self.request("GET", f"{p}/markets?limit=1")
            if code == 200:
                self.prefix = p
                logging.info(f"Discovered API prefix: {p} (probe {p}/markets?limit=1 -> 200)")
                return p
        raise RuntimeError("Could not discover API prefix (all probes failed).")

    # ---- Change #3: probe for the correct "series markets list" endpoint ----
    def probe_series_market_listing(self, series_prefix: str):
        """
        Try likely endpoints that could list markets for a given series/event.
        We log which endpoint (if any) returns tickers starting with series_prefix-.
        """
        candidates = [
            f"{self.prefix}/markets?series_ticker={series_prefix}&limit=200",
            f"{self.prefix}/markets?event_ticker={series_prefix}&limit=200",
            f"{self.prefix}/series/{series_prefix}/markets?limit=200",
            f"{self.prefix}/events/{series_prefix}/markets?limit=200",
            f"{self.prefix}/markets?search={series_prefix}&limit=200",
        ]

        for path in candidates:
            code, data = self.request("GET", path)
            markets = []
            if isinstance(data, dict):
                markets = data.get("markets") or data.get("data") or []
            logging.info(f"[PROBE] GET {path} -> HTTP={code} shape={shape_of(data)}")

            found = []
            if isinstance(markets, list):
                for m in markets:
                    tkr = None
                    if isinstance(m, dict):
                        tkr = m.get("ticker") or m.get("market_ticker")
                    if isinstance(tkr, str) and tkr.startswith(series_prefix + "-"):
                        found.append(tkr)

            if found:
                logging.info(f"[PROBE] SUCCESS endpoint returned {len(found)} matching tickers. Example={found[0]}")
                return path, found

        logging.warning("[PROBE] No candidate series endpoints returned matching tickers.")
        return None, []

def main():
    logging.info("=== BOT STARTED ===")
    logging.info(f"ENABLE_TRADING={ENABLE_TRADING}")
    logging.info(f"CONFIRM_LIVE_TRADING={CONFIRM_LIVE_TRADING}")
    logging.info(f"POLL_SECONDS={POLL_SECONDS}")
    logging.info(f"SERIES_PREFIX={SERIES_PREFIX}")
    logging.info(f"MARKET_TICKER={MARKET_TICKER or 'None'}")
    logging.info(f"API_BASE={API_BASE}")
    logging.info(f"SUBACCOUNT={SUBACCOUNT}")

    kc = KalshiClient(API_BASE, KALSHI_KEY_ID, KALSHI_PRIVATE_KEY_B64, subaccount=SUBACCOUNT)
    kc.discover_prefix()

    # Change #3: probe correct listing endpoint
    path, found = kc.probe_series_market_listing(SERIES_PREFIX)
    if not found:
        logging.error("Could not find BTC15M markets via probe. Exiting.")
        return

    logging.info("Heartbeat: probe found BTC markets. Next step will be to select the next closing one.")
    while True:
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()