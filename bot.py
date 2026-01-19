import os
import time
import json
import base64
import hashlib
import logging
from datetime import datetime, timezone

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
    """Return epoch seconds from an ISO timestamp or None."""
    if not ts or not isinstance(ts, str):
        return None
    try:
        # Handles "2026-01-19T14:59:17Z" etc
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

    # ---- Change #2: resolve ticker automatically ----
    def resolve_market_ticker(self, series_prefix: str) -> str | None:
        """
        Find an active market ticker for a series prefix.
        Strategy:
        - fetch first N markets that match prefix
        - choose the one with the soonest close_time in the future
        """
        # NOTE: we are not assuming the filter param name; we’ll just page and filter client-side.
        # Keep it simple and safe.
        cursor = None
        candidates = []

        for _ in range(5):  # up to 5 pages
            path = f"{self.prefix}/markets?limit=200"
            if cursor:
                path += f"&cursor={cursor}"

            code, data = self.request("GET", path)
            if code != 200 or not isinstance(data, dict) or "markets" not in data:
                logging.error(f"Could not list markets to resolve ticker. HTTP={code} shape={shape_of(data)}")
                return None

            markets = data.get("markets", [])
            for m in markets:
                tkr = m.get("ticker") or m.get("market_ticker")
                if not tkr or not isinstance(tkr, str):
                    continue
                if not tkr.startswith(series_prefix + "-"):
                    continue

                status = (m.get("status") or m.get("market_status") or "").lower()
                close_ts = m.get("close_time") or m.get("close_ts") or m.get("closeTime")
                close_epoch = parse_iso_ts(close_ts) if isinstance(close_ts, str) else None

                # If no close time parseable, still keep (low priority)
                candidates.append((close_epoch, status, tkr, m))

            cursor = data.get("cursor")
            if not cursor:
                break

        if not candidates:
            logging.warning(f"No markets found matching prefix {series_prefix}.")
            return None

        now_epoch = time.time()

        # Prefer: close time in the future and status indicates open/trading
        def score(item):
            close_epoch, status, tkr, _ = item
            # open-ish status gets preference
            open_bonus = 0
            if "open" in status or "trading" in status or status == "":
                open_bonus = 1
            # future close gets preference
            future_bonus = 0
            if close_epoch and close_epoch > now_epoch:
                future_bonus = 1
            # sort: future first, open first, soonest close time
            close_sort = close_epoch if close_epoch is not None else (now_epoch + 10**12)
            return (-future_bonus, -open_bonus, close_sort)

        candidates.sort(key=score)
        best = candidates[0]
        close_epoch, status, tkr, m = best

        logging.info(f"Resolved market ticker: {tkr}")
        logging.info(f"Resolved market status={status or 'UNKNOWN'} close_time={m.get('close_time') or m.get('close_ts')}")
        return tkr

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

    # If ticker not set, auto-resolve it
    ticker = MARKET_TICKER or kc.resolve_market_ticker(SERIES_PREFIX)
    if not ticker:
        logging.error("Could not resolve MARKET_TICKER. Exiting.")
        return

    # Debug: fetch the resolved market object (this is the next failure point we want to verify)
    code, data = kc.request("GET", f"{kc.prefix}/markets/{ticker}")
    logging.info(f"Market fetch HTTP={code} shape={shape_of(data)}")
    if code != 200:
        logging.error("Market fetch failed. Exiting (we will fix this in the next change).")
        return

    logging.info("Heartbeat: ticker resolved and market fetch OK (no strategy yet).")
    while True:
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()