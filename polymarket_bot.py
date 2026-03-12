#!/usr/bin/env python3
"""
Polymarket 5-min crypto Up/Down bot — V10 scoring
Same strategy as Kalshi bots: trend confirmation + vol adjustment + BTC master signal.

Market format: "Bitcoin/Ethereum/Solana/XRP Up or Down - [Date] [StartTime]-[EndTime] ET"
YES = price goes UP vs window open. NO = price goes DOWN (or flat).
USDC-settled on Polygon. Auth via Magic wallet private key.

Usage: ASSET=BTC python3 polymarket_bot.py
"""

import os, re, sys, time, logging, requests, json, signal
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Optional
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs

# ── PROXY (residential EU required to bypass Polymarket geoblock) ─
# Set HTTPS_PROXY env var on Render: http://user:pass@host:port
_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
if _proxy:
    os.environ["HTTPS_PROXY"] = _proxy
    os.environ["HTTP_PROXY"]  = _proxy
    logging.getLogger("polymarket").warning(f"[PROXY] Routing through proxy")

# ── CONFIG ────────────────────────────────────────────────────
ASSET           = os.environ.get("ASSET", "BTC").upper()
HOST            = "https://clob.polymarket.com"
CHAIN_ID        = 137
GAMMA_URL       = "https://gamma-api.polymarket.com/markets"
CANDLES_URL     = {
    "BTC": "https://api.exchange.coinbase.com/products/BTC-USD/candles",
    "ETH": "https://api.exchange.coinbase.com/products/ETH-USD/candles",
    "SOL": "https://api.exchange.coinbase.com/products/SOL-USD/candles",
    "XRP": "https://api.exchange.coinbase.com/products/XRP-USD/candles",
}
BTC_CANDLES_URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"

# ── SECRETS ───────────────────────────────────────────────────
# Prefer environment variables (Render), fall back to local secrets.json (dev)
PRIVATE_KEY = os.environ.get("POLYMARKET_PRIVATE_KEY")
FUNDER      = os.environ.get("POLYMARKET_FUNDER_ADDRESS")

if not PRIVATE_KEY or not FUNDER:
    try:
        _raw = open("/Users/mr.fagaly/.openclaw/secrets.json").read()
        try:
            _sec = json.loads(_raw)
            PRIVATE_KEY = PRIVATE_KEY or _sec.get("POLYMARKET_PRIVATE_KEY")
            FUNDER      = FUNDER      or _sec.get("POLYMARKET_FUNDER_ADDRESS")
        except Exception:
            PRIVATE_KEY = PRIVATE_KEY or re.search(r'"POLYMARKET_PRIVATE_KEY"\s*:\s*"([^"]+)"', _raw).group(1)
            FUNDER      = FUNDER      or re.search(r'"POLYMARKET_FUNDER_ADDRESS"\s*:\s*"([^"]+)"', _raw).group(1)
    except Exception:
        raise RuntimeError("POLYMARKET_PRIVATE_KEY and POLYMARKET_FUNDER_ADDRESS must be set as env vars")

# ── THRESHOLDS — matches Kalshi V5 btc_bot.py exactly ────────
CONFIRM_THRESHOLD  = 0.85    # ask >= 90¢ to consider "certain" (Kalshi: bid >= 90¢)
WATCH_WINDOW       = 180     # start watching 180s before close
CONFIRM_CHECKS     = 4       # ticks required at start of watch window
CONFIRM_STEP       = 30      # reduce by 1 every 30s → min 1 at T=90s
SAFETY_NET_SECS    = 10      # fallback: buy best side at T=10s if no position yet
POLL_SECS          = 1.0     # orderbook poll interval (same as Kalshi)
MAX_RISK_PCT       = 0.20    # 20% of balance per trade
MAX_USDC           = 10.0    # hard cap per trade
MIN_ORDER_SHARES   = 5       # Polymarket minimum order size
ASSUMED_BALANCE    = 24.0    # fallback if CLOB returns 0

def required_confirms(secs_to_close: float) -> int:
    elapsed = max(0, WATCH_WINDOW - secs_to_close)
    reduction = int(elapsed / CONFIRM_STEP)
    return max(1, CONFIRM_CHECKS - reduction)

# ── TREND STATE ───────────────────────────────────────────────
TREND_REFRESH_SEC   = 300
TREND_WINDOW_CANDLES = 12
_trend_state  = {"direction": "neutral", "updated": 0.0, "avg_range": 0.0}
_btc_trend    = {"direction": "neutral", "updated": 0.0}
_last_trend   = 0.0
_btc_1h_pct   = 0.0   # V11: BTC 1h % change (numeric)
_session_start_balance: float = 0.0  # V11: drawdown tracking

# ── LOGGING ───────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("pm_bot")

# ── TREND HELPERS ─────────────────────────────────────────────

def _fetch_candles(url: str) -> list:
    try:
        r = requests.get(url, params={"granularity": 300}, timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"[CANDLES] {e}")
        return []

def fetch_trend(http: requests.Session) -> str:
    candles = _fetch_candles(CANDLES_URL[ASSET])
    if len(candles) < TREND_WINDOW_CANDLES:
        return "neutral"
    recent = float(candles[0][4])
    old    = float(candles[TREND_WINDOW_CANDLES - 1][4])
    pct    = (recent - old) / old * 100
    direction = "bearish" if pct < -0.5 else "bullish" if pct > 0.5 else "neutral"
    ranges = [abs(float(c[2]) - float(c[3])) for c in candles[:6]]
    avg_range = sum(ranges) / len(ranges)
    _trend_state["direction"] = direction
    _trend_state["updated"]   = time.time()
    _trend_state["avg_range"] = avg_range
    log.warning(f"[TREND] {ASSET} {direction.upper()} 1h={pct:+.2f}% avg_range={avg_range:.4f}")
    return direction

def fetch_btc_trend(http: requests.Session) -> str:
    global _btc_1h_pct
    if ASSET == "BTC":
        _btc_trend["direction"] = _trend_state["direction"]
        # Grab pct from trend_state if available
        candles = _fetch_candles(CANDLES_URL[ASSET])
        if len(candles) >= TREND_WINDOW_CANDLES:
            recent = float(candles[0][4])
            old    = float(candles[TREND_WINDOW_CANDLES - 1][4])
            _btc_1h_pct = (recent - old) / old * 100
        return _btc_trend["direction"]
    candles = _fetch_candles(BTC_CANDLES_URL)
    if len(candles) < TREND_WINDOW_CANDLES:
        return "neutral"
    recent = float(candles[0][4])
    old    = float(candles[TREND_WINDOW_CANDLES - 1][4])
    pct    = (recent - old) / old * 100
    _btc_1h_pct = pct
    direction = "bearish" if pct < -0.5 else "bullish" if pct > 0.5 else "neutral"
    _btc_trend["direction"] = direction
    _btc_trend["updated"]   = time.time()
    log.warning(f"[BTC-TREND] {direction.upper()} 1h={pct:+.2f}%")
    return direction

def get_trend() -> str: return _trend_state.get("direction", "neutral")
def get_btc_trend() -> str: return _btc_trend.get("direction", "neutral")

# ── V10 SCORING ───────────────────────────────────────────────

def signal_score(own: str, btc: str, side: str) -> float:
    confirming = "bullish" if side == "yes" else "bearish"
    opposing   = "bearish" if side == "yes" else "bullish"
    if own == confirming and btc == confirming: return 1.0
    if own == confirming and btc == "neutral":  return 0.75
    if btc == confirming and own == "neutral":  return 0.70
    if own == "neutral"  and btc == "neutral":  return 0.55
    if own == opposing   or btc == opposing:    return 0.30
    return 0.55

def price_score(price: float, side: str) -> float:
    p = price
    if side == "no":
        if p >= 0.97: return 1.00
        if p >= 0.95: return 0.90
        if p >= 0.90: return 0.55
        if p >= 0.85: return 0.70
        return 0.40
    else:
        if p >= 0.97: return 1.00
        if p >= 0.95: return 0.90
        if p >= 0.90: return 0.55
        if p >= 0.85: return 0.65
        return 0.35

def vol_multiplier(avg_range: float) -> float:
    bands = {
        "BTC": [(50, 1.0), (150, 0.85), (300, 0.70), (float("inf"), 0.50)],
        "ETH": [(2,  1.0), (8,   0.85), (20,  0.70), (float("inf"), 0.50)],
        "SOL": [(0.1,1.0), (0.5, 0.85), (1.5, 0.70), (float("inf"), 0.50)],
        "XRP": [(0.005,1.0),(0.02,0.85),(0.05,0.70), (float("inf"), 0.50)],
    }
    for threshold, mult in bands.get(ASSET, bands["BTC"]):
        if avg_range < threshold:
            return mult
    return 0.50

def time_score(secs_to_close: float) -> float:
    if secs_to_close <= 0: return 1.0
    if secs_to_close >= 600: return 0.5
    return 1.0 - (secs_to_close / 600) * 0.5

def v10_score(side: str, price: float, secs: float) -> float:
    own = get_trend()
    btc = get_btc_trend()
    avg_range = _trend_state.get("avg_range", 0)
    sig  = signal_score(own, btc, side)
    ps   = price_score(price, side)
    vm   = vol_multiplier(avg_range)
    ts   = time_score(secs)
    score = sig * ps * vm * ts
    log.warning(
        f"[V10] {ASSET} {side}@{price:.2f} signal={sig:.2f} price={ps:.2f} "
        f"vol={vm:.2f} time={ts:.2f} → score={score:.3f}"
    )
    return score

def score_to_tier(score: float) -> Optional[str]:
    if score >= SCORE_HIGH: return "HIGH"
    if score >= SCORE_MED:  return "MEDIUM"
    if score >= MIN_SCORE:  return "LOW"
    return None

def compute_size(score: float, price: float, balance: float) -> float:
    tier = score_to_tier(score)
    if not tier or price <= 0 or balance <= 0:
        return 0.0
    tier_pct    = TIER_PCT[tier]
    raw_usdc    = tier_pct * balance
    hard_cap    = MAX_RISK_PCT * balance
    # Also cap by fixed tier amounts (safe conservative floor while wallet is being verified)
    fixed_cap   = FIXED_TIER_USDC.get(tier, 1.0)
    capped_usdc = min(raw_usdc, hard_cap, MAX_USDC, fixed_cap)
    size        = capped_usdc / price
    log.info(f"[SIZE] tier={tier} ${capped_usdc:.2f} @ {price:.2f} = {size:.2f} shares")
    return round(size, 2)

# ── MARKET DISCOVERY ──────────────────────────────────────────

ASSET_SLUG = {
    "BTC": "btc",
    "ETH": "eth",
    "SOL": "sol",
    "XRP": "xrp",
}
GAMMA_EVENT_URL = "https://gamma-api.polymarket.com/events/slug"

def _next_15min_timestamps(now: datetime) -> list:
    """Return unix timestamps for next 2 upcoming 15-min window start times."""
    m = now.minute
    current_boundary = (m // 15) * 15
    starts = []
    for i in range(3):
        boundary = current_boundary + i * 15
        h_offset = boundary // 60
        min_offset = boundary % 60
        dt = now.replace(minute=min_offset, second=0, microsecond=0) + timedelta(hours=h_offset)
        starts.append(int(dt.timestamp()))
    return starts

def find_next_market(now: datetime) -> Optional[dict]:
    """Find next open 15-min Up/Down market via slug-based lookup."""
    slug_prefix = ASSET_SLUG.get(ASSET, ASSET.lower())
    timestamps = _next_15min_timestamps(now)

    for ts in timestamps:
        slug = f"{slug_prefix}-updown-15m-{ts}"
        try:
            r = requests.get(f"{GAMMA_EVENT_URL}/{slug}", timeout=8)
            if r.status_code != 200:
                continue
            event = r.json()
        except Exception as e:
            log.warning(f"[MARKET-FIND] {e}")
            continue

        markets = event.get("markets", [])
        if not markets:
            continue

        # Use the single market in this event
        m = markets[0]
        end = m.get("endDate", "") or ""
        if not m.get("clobTokenIds"):
            continue
        try:
            end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        except:
            continue

        secs = (end_dt - now).total_seconds()
        if secs <= 0 or secs > 1200:
            log.info(f"[SCAN] {ASSET} window found but not in entry range (secs={secs:.0f})")
            continue

        liq = float(m.get("liquidity", 0) or 0)
        log.info(f"[MARKET] {event.get('title','')[:65]} ends_in={secs:.0f}s liq=${liq:,.0f}")
        return {"market": m, "secs": secs, "liq": liq, "end_dt": end_dt}

    return None

# ── ORDERBOOK READ ────────────────────────────────────────────

def get_best_prices(client: ClobClient, token_ids: list) -> tuple:
    """Returns (yes_price, no_price) as floats 0-1. None if no data."""
    try:
        if not token_ids:
            return None, None
        yes_id = token_ids[0]
        no_id  = token_ids[1] if len(token_ids) > 1 else None

        # YES price = best ask for YES token
        yes_book = client.get_order_book(yes_id)
        yes_price = None
        if yes_book and yes_book.asks:
            yes_price = float(sorted(yes_book.asks, key=lambda x: float(x.price))[0].price)

        # NO price = best ask for NO token
        no_price = None
        if no_id:
            no_book = client.get_order_book(no_id)
            if no_book and no_book.asks:
                no_price = float(sorted(no_book.asks, key=lambda x: float(x.price))[0].price)

        return yes_price, no_price
    except Exception as e:
        log.warning(f"[ORDERBOOK] {e}")
        return None, None

# ── ORDER PLACEMENT ───────────────────────────────────────────

def place_order(client: ClobClient, token_id: str, price: float, size: float, side_str: str) -> bool:
    """Place a market order on Polymarket. Returns True on success."""
    try:
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side="BUY",
        )
        resp = client.create_and_post_order(order_args)
        log.warning(f"[ORDER] {side_str.upper()}@{price:.2f} x{size:.2f} → {resp}")
        return True
    except Exception as e:
        log.error(f"[ORDER-FAIL] {e}")
        return False

# ── BALANCE ───────────────────────────────────────────────────

def get_usdc_balance(client: ClobClient) -> float:
    try:
        bal = client.get_balance_allowance(asset_type=0)  # 0=USDC
        return float(bal.get("balance", 0)) / 1e6  # Polymarket USDC has 6 decimals
    except Exception as e:
        log.warning(f"[BALANCE] {e}")
        return 0.0

# ── MAIN LOOP ─────────────────────────────────────────────────

TRADED_WINDOWS: set = set()

def main():
    log.warning(f"🚀 Polymarket {ASSET} bot starting (V5 — matches Kalshi btc_bot.py)")

    client  = ClobClient(HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID, signature_type=1, funder=FUNDER)
    creds   = client.create_or_derive_api_creds()
    client.set_api_creds(creds)
    log.warning(f"✅ Connected — API key {creds.api_key[:12]}...")

    # V11: set session start balance
    global _session_start_balance
    try:
        _session_start_balance = get_usdc_balance(client)
        if _session_start_balance < 0.50:
            _session_start_balance = ASSUMED_BALANCE
    except:
        _session_start_balance = ASSUMED_BALANCE
    log.warning(f"[V11] Session start balance: ${_session_start_balance:.2f}")
    log.warning(f"[BOT] Watch: last {WATCH_WINDOW}s | Threshold: {CONFIRM_THRESHOLD*100:.0f}¢ | Confirms: {CONFIRM_CHECKS}→1")

    while True:
        now_utc = datetime.now(timezone.utc)

        # Find next market
        candidate = find_next_market(now_utc)
        if not candidate:
            log.info(f"[SCAN] No open {ASSET} windows (next scan in 15s)")
            time.sleep(15)
            continue

        m      = candidate["market"]
        secs   = candidate["secs"]
        q      = m["question"]

        # Already traded this window?
        window_key = m.get("conditionId", q)
        if window_key in TRADED_WINDOWS:
            log.info(f"[SKIP] Already traded {q[:50]}")
            time.sleep(10)
            continue

        # Too early — wait
        if secs > WATCH_WINDOW:
            log.info(f"[WAIT] {q[:50]} closes in {secs:.0f}s")
            time.sleep(max(0, min(secs - WATCH_WINDOW - 5, 30)))
            continue

        # Parse token IDs
        try:
            token_ids = json.loads(m.get("clobTokenIds", "[]"))
        except:
            token_ids = []
        if len(token_ids) < 2:
            log.warning(f"[SKIP] No token IDs for {q[:50]}")
            TRADED_WINDOWS.add(window_key)
            time.sleep(10)
            continue

        # Poll orderbook every 1s (same as Kalshi)
        yes_price, no_price = get_best_prices(client, token_ids)
        log.info(f"[TICK] {q[:50]} t={secs:.0f}s yes={yes_price} no={no_price}")

        if yes_price is None and no_price is None:
            log.warning("[TICK] No prices — retry")
            time.sleep(POLL_SECS)
            continue

        # Pick high-confidence side (ask >= 90¢), same as Kalshi bid >= 90¢
        side, price, token_id = None, None, None
        if yes_price is not None and yes_price >= CONFIRM_THRESHOLD:
            side, price, token_id = "yes", yes_price, token_ids[0]
        if no_price is not None and no_price >= CONFIRM_THRESHOLD:
            if side is None or no_price > price:
                side, price, token_id = "no", no_price, token_ids[1]

        # Safety net at T=10s — force best side like Kalshi
        if side is None and secs <= SAFETY_NET_SECS:
            if yes_price is not None and no_price is not None:
                if yes_price >= no_price:
                    side, price, token_id = "yes", yes_price, token_ids[0]
                else:
                    side, price, token_id = "no", no_price, token_ids[1]
                log.warning(f"[SAFETY-NET] T={secs:.0f}s forcing {side}@{price}")

        if side is None:
            log.info(f"[WATCH] t={secs:.0f}s yes={yes_price} no={no_price} need {required_confirms(secs)} confirms at ≥{CONFIRM_THRESHOLD}")
            time.sleep(POLL_SECS)
            continue

        # V11 scoring gate
        from scoring_v11 import v11_score, compute_shares_polymarket, score_to_tier, MIN_SCORE
        v11 = v11_score(ASSET, side, int(price * 100), secs, _btc_1h_pct)
        tier = score_to_tier(v11)
        if v11 < MIN_SCORE:
            log.warning(f"[V11-SKIP] {q[:45]} {side.upper()}@{price:.2f} score={v11:.3f} — skip")
            TRADED_WINDOWS.add(window_key)
            time.sleep(5)
            continue

        balance = get_usdc_balance(client)
        if balance < 0.50:
            log.warning(f"[LOW-BAL] CLOB ${balance:.2f} — using ${ASSUMED_BALANCE:.2f}")
            balance = ASSUMED_BALANCE

        size = compute_shares_polymarket(v11, price, balance, _session_start_balance)
        if size == 0:
            log.warning(f"[V11-SKIP] size=0 — skip")
            TRADED_WINDOWS.add(window_key)
            continue
        # hard cap
        size = min(size, MAX_USDC / price)
        usdc_cost = size * price

        log.warning(f"[ENTER] {ASSET} {side.upper()}@{price:.2f} x{size:.2f} (${usdc_cost:.2f}) V11={v11:.3f} tier={tier} bal=${balance:.2f} t={secs:.0f}s")

        success = place_order(client, token_id, price, size, side)
        TRADED_WINDOWS.add(window_key)

        if success:
            log.warning(f"[FILLED] {q[:45]}")
        else:
            log.warning(f"[FAIL] Order failed — {q[:45]}")

        time.sleep(20)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        main()
    except KeyboardInterrupt:
        log.warning("Bot stopped.")
