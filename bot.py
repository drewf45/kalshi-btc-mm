import os
import time
import json
import uuid
import base64
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# =========================
# ENV HELPERS
# =========================
def env(name, default=None):
    v = os.getenv(name)
    return v.strip() if isinstance(v, str) else default

# =========================
# CONFIG
# =========================
BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

API_KEY_ID = env("KALSHI_API_KEY_ID")
PRIVATE_KEY_B64 = env("KALSHI_PRIVATE_KEY_PEM_B64")

BET_DOLLARS = float(env("BET_DOLLARS", "1"))
DRY_RUN = env("DRY_RUN", "true").lower() == "true"

POLL_SECONDS = 60
SERIES_PREFIX = "KXBTC15M"

EMAIL_ENABLED = env("EMAIL_ENABLED", "false").lower() == "true"
EMAIL_TO = env("EMAIL_TO")
SMTP_USER = env("SMTP_USER")
SMTP_PASSWORD = env("SMTP_PASSWORD")

# =========================
# EMAIL
# =========================
def send_email(subject: str, body: str):
    if not EMAIL_ENABLED:
        return
    try:
        msg = EmailMessage()
        msg["From"] = SMTP_USER
        msg["To"] = EMAIL_TO
        msg["Subject"] = subject
        msg.set_content(body)

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.send_message(msg)
    except Exception as e:
        print("EMAIL FAILED:", e, flush=True)

# =========================
# LOAD PRIVATE KEY (SAFE)
# =========================
def load_private_key():
    raw = base64.b64decode(PRIVATE_KEY_B64)
    return serialization.load_pem_private_key(raw, password=None)

PRIVATE_KEY = load_private_key()

# =========================
# SIGNING
# =========================
def sign(ts: str, method: str, path: str, body: str):
    payload = (ts + method + path + body).encode()
    sig = PRIVATE_KEY.sign(
        payload,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode()

# =========================
# KALSHI REQUEST
# =========================
def kalshi_request(method, path, body=None):
    ts = str(int(time.time() * 1000))
    body_json = json.dumps(body) if body else ""
    sig = sign(ts, method, path, body_json)

    headers = {
        "KALSHI-ACCESS-KEY": API_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "Content-Type": "application/json",
    }

    r = requests.request(
        method,
        BASE_URL + path,
        headers=headers,
        data=body_json if body else None,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()

# =========================
# MARKET HELPERS
# =========================
def current_15m_ticker():
    now = datetime.utcnow().replace(second=0, microsecond=0)
    minute = (now.minute // 15) * 15
    now = now.replace(minute=minute)
    return f"{SERIES_PREFIX}-{now.strftime('%d%b%y%H%M').upper()}"

def get_best_ask(orderbook: dict):
    fp = orderbook.get("orderbook_fp", {})
    yes = fp.get("yes_dollars_asks", [])
    no = fp.get("no_dollars_asks", [])
    if yes:
        return "yes", int(float(yes[0][0]) * 100)
    if no:
        return "no", int(float(no[0][0]) * 100)
    return None, None

# =========================
# MAIN LOOP
# =========================
def main():
    print("=== BOT STARTED ===", flush=True)
    send_email("Kalshi Bot Started", "Bot is live and monitoring BTC markets.")

    last_traded = None

    while True:
        try:
            ticker = current_15m_ticker()
            print(f"CHECKING MARKET: {ticker}", flush=True)

            if ticker == last_traded:
                time.sleep(POLL_SECONDS)
                continue

            ob = kalshi_request("GET", f"/markets/{ticker}/orderbook?depth=1")
            side, price = get_best_ask(ob)

            if not side:
                print("No liquidity yet", flush=True)
                time.sleep(POLL_SECONDS)
                continue

            contracts = max(1, int((BET_DOLLARS * 100) // price))
            print(f"BETTING {side.upper()} {price}c x{contracts}", flush=True)

            if not DRY_RUN:
                order = {
                    "ticker": ticker,
                    "side": side,
                    "action": "buy",
                    "type": "limit",
                    "count": contracts,
                    "time_in_force": "fill_or_kill",
                    f"{side}_price": price,
                    "client_order_id": str(uuid.uuid4()),
                }
                kalshi_request("POST", "/portfolio/orders", order)
                send_email("Kalshi Order Placed",
                           f"{ticker} {side.upper()} {price}c x{contracts}")

            last_traded = ticker
            time.sleep(POLL_SECONDS)

        except Exception as e:
            print("ERROR:", e, flush=True)
            send_email("Kalshi Bot Error", str(e))
            time.sleep(10)

if __name__ == "__main__":
    main()