import os
import time
import json
import base64
import smtplib
import traceback
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import requests

# =======================
# CONFIG
# =======================

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

BET_DOLLARS = float(os.getenv("BET_DOLLARS", "1"))
ENABLE_TRADING = os.getenv("ENABLE_TRADING", "False").lower() == "true"
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "20"))

POLL_SECONDS = 60
MIN_PRICE = 5
MAX_PRICE = 95

SERIES_PREFIX = "KXBTC15M"

# =======================
# EMAIL
# =======================

SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
EMAIL_TO = os.getenv("EMAIL_TO")

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
        print("EMAIL FAILED:", e)

# =======================
# AUTH
# =======================

API_KEY_ID = os.getenv("KALSHI_API_KEY_ID")
PEM_B64 = os.getenv("KALSHI_PRIVATE_KEY_PEM_B64")

if not API_KEY_ID or not PEM_B64:
    raise RuntimeError("Missing Kalshi API credentials")

PRIVATE_KEY = base64.b64decode(PEM_B64).decode()

HEADERS = {
    "Content-Type": "application/json",
    "Kalshi-Access-Key": API_KEY_ID,
    "Kalshi-Access-Signature": PRIVATE_KEY,
}

# =======================
# UTIL
# =======================

def now_utc():
    return datetime.now(timezone.utc)

def current_15m_bucket():
    t = now_utc()
    minute = (t.minute // 15) * 15
    bucket = t.replace(minute=minute, second=0, microsecond=0)
    return bucket

def market_ticker_for_now():
    bucket = current_15m_bucket()
    return f"{SERIES_PREFIX}-{bucket.strftime('%d%b%y').upper()}{bucket.strftime('%H%M')}"

# =======================
# API
# =======================

def get_orderbook(ticker):
    r = requests.get(
        f"{BASE_URL}/markets/{ticker}/orderbook",
        headers=HEADERS,
        timeout=10,
    )
    if r.status_code != 200:
        return None
    return r.json().get("orderbook")

def place_order(ticker, side, price):
    if not ENABLE_TRADING:
        print("DRY RUN — order skipped")
        return True

    payload = {
        "ticker": ticker,
        "side": side,
        "price": price,
        "quantity": BET_DOLLARS,
        "type": "limit",
    }

    r = requests.post(
        f"{BASE_URL}/orders",
        headers=HEADERS,
        data=json.dumps(payload),
        timeout=10,
    )

    if r.status_code != 200:
        raise RuntimeError(r.text)

    return True

# =======================
# STRATEGY
# =======================

def choose_trade(orderbook):
    yes = orderbook.get("yes", [])
    no = orderbook.get("no", [])

    if not yes or not no:
        return None

    best_yes = min(yes, key=lambda x: x["price"])
    best_no = min(no, key=lambda x: x["price"])

    if MIN_PRICE <= best_yes["price"] <= MAX_PRICE:
        return ("yes", best_yes["price"])

    if MIN_PRICE <= best_no["price"] <= MAX_PRICE:
        return ("no", best_no["price"])

    return None

# =======================
# MAIN LOOP
# =======================

def main():
    send_email("Kalshi bot started", "Bot is live and monitoring markets.")

    print("=== BOT STARTED ===")
    print("Trading enabled:", ENABLE_TRADING)

    daily_loss = 0
    last_trade_ticker = None

    while True:
        try:
            ticker = market_ticker_for_now()
            print(f"[{now_utc().isoformat()}] Checking {ticker}")

            ob = get_orderbook(ticker)

            if not ob:
                print("No orderbook yet")
                time.sleep(POLL_SECONDS)
                continue

            decision = choose_trade(ob)

            if not decision:
                print("No usable prices")
                time.sleep(POLL_SECONDS)
                continue

            side, price = decision

            if ticker == last_trade_ticker:
                print("Already traded this market")
                time.sleep(POLL_SECONDS)
                continue

            print(f"PLACING {side.upper()} @ {price}")
            place_order(ticker, side, price)

            last_trade_ticker = ticker

            send_email(
                "Kalshi trade placed",
                f"{ticker}\n{side.upper()} @ {price}",
            )

        except Exception as e:
            print("ERROR:", e)
            traceback.print_exc()
            send_email("Kalshi bot error", str(e))

        time.sleep(POLL_SECONDS)

# =======================

if __name__ == "__main__":
    main()