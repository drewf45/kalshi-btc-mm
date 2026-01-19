import os
import time
import json
import base64
import logging
import requests
import datetime as dt
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# ================= CONFIG =================

API_BASE = os.getenv("KALSHI_API_BASE")
API_KEY_ID = os.getenv("KALSHI_API_KEY_ID")
PRIVATE_KEY_B64 = os.getenv("KALSHI_PRIVATE_KEY_PEM_BASE64")

SERIES_PREFIX = os.getenv("SERIES_PREFIX", "KXBTC15M")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))

ENABLE_TRADING = os.getenv("ENABLE_TRADING", "FALSE").upper() == "TRUE"
DRY_RUN = os.getenv("DRY_RUN", "TRUE").upper() == "TRUE"

CONTRACTS_PER_SIDE = int(os.getenv("CONTRACTS_PER_SIDE", "1"))
TAKE_PROFIT_CENTS = int(os.getenv("TAKE_PROFIT_CENTS", "1"))

BET_PCT_PER_TRADE = float(os.getenv("BET_PCT_PER_TRADE", "1"))
MAX_DAILY_LOSS_PCT = float(os.getenv("MAX_DAILY_LOSS_PCT", "20"))

# ================= LOGGING =================

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ================= AUTH =================

PRIVATE_KEY = serialization.load_pem_private_key(
    base64.b64decode(PRIVATE_KEY_B64),
    password=None
)

def sign_request(timestamp, method, path, body=""):
    msg = f"{timestamp}{method}{path}{body}".encode()
    sig = PRIVATE_KEY.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256()
    )
    return base64.b64encode(sig).decode()

def headers(method, path, body=""):
    ts = str(int(time.time()))
    return {
        "Content-Type": "application/json",
        "KALSHI-ACCESS-KEY": API_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sign_request(ts, method, path, body)
    }

# ================= HELPERS =================

def now_utc():
    return dt.datetime.now(dt.timezone.utc)

def get_open_market():
    path = f"/trade-api/v2/markets?status=open&series_ticker={SERIES_PREFIX}&limit=1"
    r = requests.get(API_BASE + path)
    r.raise_for_status()
    markets = r.json()["markets"]
    if not markets:
        return None
    return markets[0]["ticker"]

def get_market(ticker):
    path = f"/trade-api/v2/markets/{ticker}"
    r = requests.get(API_BASE + path)
    r.raise_for_status()
    return r.json()["market"]

def place_order(ticker, side, price):
    path = "/trade-api/v2/orders"
    body = json.dumps({
        "ticker": ticker,
        "side": side,
        "action": "buy",
        "count": CONTRACTS_PER_SIDE,
        "price": price,
        "type": "limit"
    })

    if DRY_RUN:
        logging.info(f"[DRY_RUN] BUY {side} {CONTRACTS_PER_SIDE} @ {price}")
        return

    r = requests.post(API_BASE + path, headers=headers("POST", path, body), data=body)
    r.raise_for_status()
    logging.info(f"ORDER PLACED: {side} {CONTRACTS_PER_SIDE} @ {price}")

# ================= MAIN LOOP =================

logging.info("=== BOT STARTED ===")
logging.info(f"ENABLE_TRADING={ENABLE_TRADING} DRY_RUN={DRY_RUN}")

while True:
    try:
        market_ticker = get_open_market()
        if not market_ticker:
            logging.info("No open market found")
            time.sleep(POLL_SECONDS)
            continue

        market = get_market(market_ticker)

        yes_bid = market["yes_bid"]
        yes_ask = market["yes_ask"]
        no_bid = market["no_bid"]
        no_ask = market["no_ask"]

        logging.info(
            f"{market_ticker} YES {yes_bid}/{yes_ask} NO {no_bid}/{no_ask}"
        )

        if ENABLE_TRADING:
            place_order(market_ticker, "yes", max(1, yes_bid - TAKE_PROFIT_CENTS))
            place_order(market_ticker, "no", max(1, no_bid - TAKE_PROFIT_CENTS))

        time.sleep(POLL_SECONDS)

    except Exception as e:
        logging.error(f"LOOP ERROR: {e}")
        time.sleep(POLL_SECONDS)