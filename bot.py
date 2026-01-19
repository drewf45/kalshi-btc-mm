import os
import time
import json
import base64
import uuid
import hmac
import hashlib
import logging
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa, padding
from cryptography.exceptions import UnsupportedAlgorithm

# ----------------------------
# Logging
# ----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

# ----------------------------
# Env / Config
# ----------------------------
load_dotenv()

API_BASE = os.getenv("API_BASE", "https://api.elections.kalshi.com").rstrip("/")
KALSHI_KEY_ID = os.getenv("KALSHI_KEY_ID", "").strip()
KALSHI_PRIVATE_KEY_B64 = os.getenv("KALSHI_PRIVATE_KEY_B64", "").strip()

ENABLE_TRADING = os.getenv("ENABLE_TRADING", "false").lower() == "true"
CONFIRM_LIVE_TRADING = os.getenv("CONFIRM_LIVE_TRADING", "false").lower() == "true"

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))

# You previously used SERIES_PREFIX; later you moved to MARKET_TICKER.
# Keep both for compatibility — but DO NOT change env vars right now.
SERIES_PREFIX = os.getenv("SERIES_PREFIX", "KXBTC15M").strip()
MARKET_TICKER = os.getenv("MARKET_TICKER", "").strip()  # optional

SUBACCOUNT = os.getenv("SUBACCOUNT", "").strip() or None

# ----------------------------
# Helpers
# ----------------------------
def now_ms() -> int:
    return int(time.time() * 1000)

def safe_json(obj) -> str:
    try:
        return json.dumps(obj, sort_keys=True)
    except Exception:
        return str(obj)

def shape_of(x):
    if isinstance(x, dict):
        return f"dict keys={list(x.keys())}"
    if isinstance(x, list):
        return f"list len={len(x)}"
    return type(x).__name__

# ----------------------------
# Kalshi Client (RSA or Ed25519)
# ----------------------------
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
        self.prefix = None  # discovered later

    def _load_private_key(self, b64: str):
        """
        Accepts:
        - base64 of PEM text (RSA/Ed25519 PEM)
        - base64 of raw Ed25519 private bytes (32 or 64)
        """
        raw = base64.b64decode(b64)

        # If it looks like PEM text
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

        # Otherwise try raw Ed25519 bytes
        # Ed25519 private key can be 32 bytes seed; some exports 64 bytes.
        if len(raw) in (32, 64):
            try:
                if len(raw) == 32:
                    key = ed25519.Ed25519PrivateKey.from_private_bytes(raw)
                else:
                    key = ed25519.Ed25519PrivateKey.from_private_bytes(raw[:32])
                logging.info("Loaded Ed25519 private key (raw bytes).")
                return key
            except Exception as e:
                raise RuntimeError(f"Could not load Ed25519 private key bytes: {e}")

        raise RuntimeError(
            f"Private key format not recognized. decoded_len={len(raw)}. "
            "Expected PEM (starts with -----BEGIN) or Ed25519 bytes length 32/64."
        )

    def _sign(self, msg: bytes) -> str:
        """
        Returns signature as base64 string.
        - RSA: PKCS1v15 + SHA256
        - Ed25519: pure Ed25519 signature
        """
        if isinstance(self.private_key, rsa.RSAPrivateKey):
            sig = self.private_key.sign(
                msg,
                padding.PKCS1v15(),
                hashes.SHA256()
            )
            return base64.b64encode(sig).decode()

        if isinstance(self.private_key, ed25519.Ed25519PrivateKey):
            sig = self.private_key.sign(msg)
            return base64.b64encode(sig).decode()

        raise RuntimeError(f"Unsupported key object type: {type(self.private_key)}")

    def _headers(self, method: str, path: str, body: bytes | None):
        ts = str(int(time.time()))
        body_hash = hashlib.sha256(body or b"").hexdigest()
        # A consistent signing string. (We’re not changing your endpoints yet.)
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
            r = requests.request(
                method=method,
                url=url,
                headers=headers,
                data=body,
                timeout=timeout,
            )
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
        # Previously you discovered /trade-api/v2. Keep that behavior.
        candidates = ["/trade-api/v2", "/trade-api/v1", "/trade-api", ""]
        for p in candidates:
            code, data = self.request("GET", f"{p}/markets?limit=1")
            if code == 200:
                self.prefix = p
                logging.info(f"Discovered API prefix: {p} (probe {p}/markets?limit=1 -> 200)")
                return p
        raise RuntimeError("Could not discover API prefix (all probes failed).")

# ----------------------------
# Main loop (minimal, no strategy changes yet)
# ----------------------------
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

    # --- Debug ping: list markets (small)
    code, data = kc.request("GET", f"{kc.prefix}/markets?limit=3")
    logging.info(f"Markets probe HTTP={code} shape={shape_of(data)}")
    if isinstance(data, dict):
        logging.info(f"Markets probe keys={list(data.keys())}")

    # Keep running so Render doesn’t restart loop
    while True:
        logging.info("Heartbeat: bot alive (no strategy yet).")
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()