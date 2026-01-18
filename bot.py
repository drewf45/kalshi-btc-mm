import os
import time
import json
import base64
import hashlib
import traceback
import requests
import smtplib

from datetime import datetime, timezone
from email.message import EmailMessage
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# =====================
# ENV
# =====================

API_KEY_ID = os.getenv("KALSHI_API_KEY_ID")
PEM_B64 = os.getenv("KALSHI_PRIVATE_KEY_PEM_B64")

ENABLE_TRADING = os.getenv("ENABLE_TRADING", "False").lower() == "true"
BET_DOLLARS = float(os.getenv("BET_DOLLARS", "1"))
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "20"))

SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
EMAIL_TO = os.getenv("EMAIL_TO")

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
SERIES_PREFIX = "KXBTC15M"
POLL_SECONDS = 60

# =====================
# LOAD PRIVATE KEY
# =====================

private_key = serialization.load_pem_private_key(
    base64.b64decode(PEM_B64),
    password=None,
)

# =====================
# EMAIL
# =====================

def send_email(subject, body):
    if not SMTP_HOST:
        return
    try:
        msg = EmailMessage()
        msg["From"] = SMTP_USERNAME
        msg["To"] = EMAIL_TO
        msg["Subject"] = subject
        msg.set_content(body)

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USERNAME, SMTP_PASSWORD)
            s.send_message(msg)
    except Exception as e:
        print("EMAIL ERROR:", e)

# =====================
# KALSHI SIGNING
# =====================

def sign_request(method, path, body=""):
    ts = str(int(time.time()))
    body_hash = hashlib.sha256(body.encode()).hexdigest()
    message = f"{ts}{method.upper()}{path}{body_hash}".encode()

    signature = private_key.sign(
        message,
        padding.PKCS1v15(),
        hashes.SHA256(),
    )

    return {
        "Kalshi-Access-Key": API_KEY_ID,
        "Kalshi-Access-Timestamp": ts,
        "Kalshi-Access-Signature": base64.b64encode(signature).decode(),
        "Content-Type": "application/json",
    }

# =====================
# TIME / MARKET
# =====================

def now_utc():
    return datetime.now(timezone.utc)

def current_bucket():
    t = now_utc()
    m = (t.minute // 15) * 15
    return t.replace(minute=m, second=0, microsecond=0)

def market_ticker():
    b = current_bucket()
    return f"{SERIES_PREFIX}-{b.strftime('%d%b%y').upper()}{b.strftime('%H%M')}"

# =====================
# API
# =====================

def get_orderbook(ticker):
    path = f"/markets/{ticker}/orderbook"
    headers = sign_request("GET", path)
    r = requests.get(BASE_URL + path, headers=headers, timeout=10)
    if r.status_code != 200:
        return None
    return r.json().get("orderbook")

def place_order(ticker, side, price):
    if not ENABLE_TRADING:
        print("DRY RUN — order skipped")
        return

    payload = json.dumps({
        "ticker": ticker,
        "side": side,
        "price": price,
        "quantity": BET_DOLLARS,
        "type": "limit",
    })

    path = "/orders"
    headers = sign_request("POST", path, payload)

    r = requests.post(BASE_URL + path, headers=headers, data=payload, timeout=10)
    if r.status_code != 200:
        raise RuntimeError(r.text)

# =====================
# STRATEGY
# =====================

def choose_trade(ob):
    yes = ob.get("yes", [])
    no = ob.get("no", [])
    if yes:
        return ("yes", yes[0]["price"])
    if no:
        return ("no", no[0]["price"])
    return None

# =====================
# MAIN
# =====================

def main():
    send_email("Kalshi bot started", "Bot is live.")
    last_trade = None

    while True:
        try:
            ticker = market_ticker()
            print("Checking", ticker)

            ob = get_orderbook(ticker)
            if not ob:
                time.sleep(POLL_SECONDS)
                continue

            trade = choose_trade(ob)
            if not trade or ticker == last_trade:
                time.sleep(POLL_SECONDS)
                continue

            side, price = trade
            print("PLACING", side, price)
            place_order(ticker, side, price)

            last_trade = ticker
            send_email("Trade placed", f"{ticker} {side} @ {price}")

        except Exception as e:
            traceback.print_exc()
            send_email("BOT ERROR", str(e))

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()