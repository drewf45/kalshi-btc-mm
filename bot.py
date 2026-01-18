import os
import sys
import time
import base64
import json
import requests
from datetime import datetime, timedelta, timezone

# =========================
# FORCE UNBUFFERED LOGGING
# =========================
os.environ["PYTHONUNBUFFERED"] = "1"

def log(msg):
    ts = datetime.now(timezone.utc).isoformat()
    print(f"[{ts}] {msg}", flush=True)

log("BOOT: starting bot.py")
log(f"BOOT: python={sys.version}")

# =========================
# CONFIG
# =========================
BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

API_KEY_ID = os.getenv("KALSHI_API_KEY_ID")
PRIVATE_KEY_B64 = os.getenv("KALSHI_PRIVATE_KEY_PEM_B64")

ENABLE_TRADING = os.getenv("ENABLE_TRADING", "False").lower() == "true"
DRY_RUN = os.getenv("DRY_RUN", "True").lower() == "true"

BET_DOLLARS = float(os.getenv("BET_DOLLARS", "1"))
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
SERIES_PREFIX = os.getenv("SERIES_PREFIX", "KXBTC15M")

STARTUP_TEST_BET = os.getenv("STARTUP_TEST_BET", "False").lower() == "true"

# =========================
# VALIDATION
# =========================
if not API_KEY_ID:
    raise RuntimeError("Missing KALSHI_API_KEY_ID")

if not PRIVATE_KEY_B64:
    raise RuntimeError("Missing KALSHI_PRIVATE_KEY_PEM_B64")

PRIVATE_KEY_PEM = base64.b64decode(PRIVATE_KEY_B64).decode()

log(f"CONFIG: ENABLE_TRADING={ENABLE_TRADING} DRY_RUN={DRY_RUN}")
log(f"CONFIG: BET_DOLLARS=${BET_DOLLARS}")
log(f"CONFIG: SERIES_PREFIX={SERIES_PREFIX}")
log(f"CONFIG: POLL_SECONDS={POLL_SECONDS}")

# =========================
# AUTH HEADERS (SAFE)
# =========================
def auth_headers():
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Kalshi-API-Key-Id": API_KEY_ID,
        "Kalshi-API-Private-Key": PRIVATE_KEY_PEM.strip()
    }

# =========================
# MARKET HELPERS
# =========================
def current_15m_market():
    """
    Compute the *current* 15-minute BTC Kalshi ticker.
    NO SECONDS. Always future-aligned.
    Example: KXBTC15M-26JAN181230
    """
    now = datetime.utcnow().replace(second=0, microsecond=0)
    minute = (now.minute // 15) * 15
    slot = now.replace(minute=minute)

    # If already inside the window, trade the NEXT one
    if now >= slot:
        slot += timedelta(minutes=15)

    ticker = (
        f"{SERIES_PREFIX}-"
        f"{slot.strftime('%d%b%y').upper()}"
        f"{slot.strftime('%H%M')}"
    )

    return ticker

# =========================
# ORDERBOOK
# =========================
def get_orderbook(ticker):
    url = f"{BASE_URL}/markets/{ticker}/orderbook"
    r = requests.get(url, headers=auth_headers(), timeout=10)
    if r.status_code != 200:
        return None
    return r.json()

# =========================
# SIMPLE STRATEGY (SAFE)
# =========================
def choose_side(ob):
    """
    Extremely conservative:
    - Buy YES at <= $0.40
    - Buy NO at <= $0.40
    """
    yes = ob.get("yes", [])
    no = ob.get("no", [])

    if yes:
        best_yes = min(yes, key=lambda x: x["price"])
        if best_yes["price"] <= 40:
            return ("yes", best_yes["price"])

    if no:
        best_no = min(no, key=lambda x: x["price"])
        if best_no["price"] <= 40:
            return ("no", best_no["price"])

    return None

# =========================
# PLACE ORDER
# =========================
def place_order(ticker, side, price):
    contracts = int(BET_DOLLARS * 100 / price)

    payload = {
        "ticker": ticker,
        "side": side,
        "type": "limit",
        "price": price,
        "count": contracts
    }

    if DRY_RUN or not ENABLE_TRADING:
        log(f"DRY_RUN: would place {payload}")
        return

    log(f"LIVE ORDER: {payload}")
    r = requests.post(
        f"{BASE_URL}/orders",
        headers=auth_headers(),
        json=payload,
        timeout=10
    )

    log(f"ORDER RESPONSE: {r.status_code} {r.text}")

# =========================
# MAIN LOOP
# =========================
def main():
    log("BOOT: entered main()")

    if STARTUP_TEST_BET:
        log("STARTUP_TEST_BET enabled (will fire once when possible)")

    startup_test_done = False

    while True:
        try:
            ticker = current_15m_market()
            log(f"CHECKING MARKET: {ticker}")

            ob = get_orderbook(ticker)
            if not ob:
                log("No orderbook yet")
                time.sleep(POLL_SECONDS)
                continue

            decision = choose_side(ob)
            if not decision:
                log("No usable prices yet")
                time.sleep(POLL_SECONDS)
                continue

            side, price = decision
            log(f"DECISION: {side.upper()} @ {price} cents")

            if STARTUP_TEST_BET and not startup_test_done:
                log("RUNNING STARTUP TEST BET")
                place_order(ticker, side, price)
                startup_test_done = True

            elif ENABLE_TRADING:
                place_order(ticker, side, price)
            else:
                log("Trading disabled; skipping order")

        except Exception as e:
            log(f"ERROR: {e}")

        time.sleep(POLL_SECONDS)

# =========================
# ENTRY
# =========================
if __name__ == "__main__":
    main()