#!/usr/bin/env python3
"""
Polymarket 5-min crypto Up/Down bot — V10 scoring
Same strategy as Kalshi bots: trend confirmation + vol adjustment + BTC master signal.

Market format: "Bitcoin/Ethereum/Solana/XRP Up or Down - [Date] [StartTime]-[EndTime] ET"
YES = price goes UP vs window open. NO = price goes DOWN (or flat).
USDC-settled on Polygon. Auth via Magic wallet private key.

Usage: ASSET=BTC python3 polymarket_bot.py
"""

import os, re, sys, time, logging, requests, json, signal, threading
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Optional
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs

# ── AUTO-REDEEM (on-chain via web3) ──────────────────────────
try:
    from web3 import Web3 as _Web3
    _POLYGON_RPCS   = [
        "https://polygon.drpc.org",
        "https://polygon-rpc.com",
        "https://rpc-mainnet.matic.network",
        "https://rpc.ankr.com/polygon",
    ]
    _POLYGON_RPC    = _POLYGON_RPCS[0]
    _CTF_ADDRESS    = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
    _USDC_ADDRESS   = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"  # native USDC on Polygon (not bridged USDC.e)
    _CTF_ABI        = [
        {"inputs":[{"name":"collateralToken","type":"address"},{"name":"parentCollectionId","type":"bytes32"},{"name":"conditionId","type":"bytes32"},{"name":"indexSets","type":"uint256[]"}],"name":"redeemPositions","outputs":[],"stateMutability":"nonpayable","type":"function"},
        {"inputs":[{"name":"account","type":"address"},{"name":"id","type":"uint256"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
        {"inputs":[{"name":"parentCollectionId","type":"bytes32"},{"name":"conditionId","type":"bytes32"},{"name":"indexSet","type":"uint256"}],"name":"getPositionId","outputs":[{"name":"","type":"uint256"}],"stateMutability":"pure","type":"function"},
    ]
    _WEB3_AVAILABLE = True
except ImportError:
    _WEB3_AVAILABLE = False

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
CONFIRM_THRESHOLD  = 0.90    # ask >= 90¢ to consider "certain" (matches Kalshi: bid >= 90¢)
WATCH_WINDOW       = 180     # start watching 180s before close
CONFIRM_CHECKS     = 4       # ticks required at start of watch window
CONFIRM_STEP       = 30      # reduce by 1 every 30s → min 1 at T=90s
SAFETY_NET_SECS    = 30      # fallback: buy best side at T=10s if no position yet
POLL_SECS          = 1.0     # orderbook poll interval (same as Kalshi)
DRY_RUN            = os.getenv('PM_DRY_RUN', 'false').lower() == 'true'
NUM_BOTS           = 4       # bots sharing the same CLOB wallet — divide balance equally
MAX_RISK_PCT       = 0.20    # 20% of per-bot allocation per trade
MAX_USDC           = 6.0     # hard cap per trade
MIN_ORDER_SHARES   = 5       # Polymarket minimum order size
MIN_ORDER_USDC     = 1.10    # hard floor: skip trade if cost < this (PM min is $1.00)
ASSUMED_BALANCE    = 25.0    # fallback if CLOB returns 0 — set to ~expected wallet balance

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
_sim_pnl:    float = 0.0
_sim_trades: int   = 0
_redeem_lock = __import__("threading").Lock()  # prevents nonce collisions when all 4 bots redeem simultaneously

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
    bot_balance = balance / NUM_BOTS   # each bot owns its equal share of the pool
    tier_pct    = TIER_PCT[tier]
    raw_usdc    = tier_pct * bot_balance
    hard_cap    = MAX_RISK_PCT * bot_balance
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

        def best_price_from_book(book):
            """Best available price: lowest ask, or highest bid, or None."""
            if not book:
                return None
            # Prefer lowest ask (what we'd pay to buy)
            if book.asks:
                return float(sorted(book.asks, key=lambda x: float(x.price))[0].price)
            # Fall back to highest bid (what market values it at)
            if book.bids:
                return float(sorted(book.bids, key=lambda x: -float(x.price))[0].price)
            return None

        yes_book  = client.get_order_book(yes_id)
        yes_price = best_price_from_book(yes_book)

        no_price = None
        if no_id:
            no_book  = client.get_order_book(no_id)
            no_price = best_price_from_book(no_book)

        # Derive missing side: YES + NO ≈ 1.0
        if yes_price is not None and no_price is None:
            no_price = round(1.0 - yes_price, 4)
        elif no_price is not None and yes_price is None:
            yes_price = round(1.0 - no_price, 4)

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
        if DRY_RUN:
            log.warning(f"[DRY-RUN] Would place {side_str.upper()}@{price:.2f} x{size:.2f} — skipping real order")
            return True
        resp = client.create_and_post_order(order_args)
        log.warning(f"[ORDER] {side_str.upper()}@{price:.2f} x{size:.2f} → {resp}")
        return True
    except Exception as e:
        log.error(f"[ORDER-FAIL] {e}")
        return False

# ── BALANCE ───────────────────────────────────────────────────

def get_usdc_balance(client: ClobClient) -> float:
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        bal = client.get_balance_allowance(params=params)
        result = float(bal.get("balance", 0)) / 1e6
        if result > 0:
            return result
        # CLOB returned 0 — use assumed balance as fallback
        log.warning(f"[BALANCE] CLOB returned 0 — using ASSUMED_BALANCE=${ASSUMED_BALANCE}")
        return ASSUMED_BALANCE
    except Exception as e:
        log.warning(f"[BALANCE] Error fetching balance: {e} — using ASSUMED_BALANCE=${ASSUMED_BALANCE}")
        return ASSUMED_BALANCE

# ── MAIN LOOP ─────────────────────────────────────────────────

TRADED_WINDOWS: set = set()

def redeem_market_positions(condition_id: str, private_key: str, index_sets: list = None) -> bool:
    """Redeem positions for a settled Polymarket market on-chain.
    
    Retries every 60s for up to 10 minutes — oracle settlement can take 2-5 min post-close.
    index_sets defaults to [1, 2] (both YES and NO) which is safe — zero-balance sets are no-ops.
    """
    if not _WEB3_AVAILABLE:
        log.warning("[REDEEM] web3 not available — skipping")
        return False
    if not condition_id:
        log.warning("[REDEEM] No condition_id — cannot redeem. Check gamma API response fields.")
        return False
    if index_sets is None:
        index_sets = [1, 2]

    with _redeem_lock:  # serialize redeems — prevents nonce collisions across bots
        # Try each RPC in order
        w3 = None
        for rpc in _POLYGON_RPCS:
            try:
                _w3 = _Web3(_Web3.HTTPProvider(rpc, request_kwargs={"timeout": 15}))
                _ = _w3.eth.block_number  # real connectivity test
                w3 = _w3
                log.warning(f"[REDEEM] Connected to {rpc}")
                break
            except Exception as e:
                log.warning(f"[REDEEM] RPC {rpc} failed: {e}")
                continue
        if w3 is None:
            log.warning("[REDEEM] All Polygon RPCs unreachable — skipping")
            return False

        try:
            acct  = w3.eth.account.from_key(private_key)
            matic = w3.from_wei(w3.eth.get_balance(acct.address), "ether")
            log.warning(f"[REDEEM] Caller={acct.address} MATIC={matic:.6f} conditionId={condition_id[:18]}…")
            if matic < 0.001:
                log.warning(f"[REDEEM] ⚠️  Insufficient MATIC ({matic:.6f}) — send ~$1 of MATIC to {acct.address}")
                return False

            ctf    = w3.eth.contract(address=_Web3.to_checksum_address(_CTF_ADDRESS), abi=_CTF_ABI)
            cid    = bytes.fromhex(condition_id.replace("0x", ""))
            parent = bytes(32)

            # ── Pre-check: skip if position already redeemed (balance = 0) ────────
            try:
                pos_id_yes = ctf.functions.getPositionId(parent, cid, 1).call()
                pos_id_no  = ctf.functions.getPositionId(parent, cid, 2).call()
                bal_yes = ctf.functions.balanceOf(acct.address, pos_id_yes).call()
                bal_no  = ctf.functions.balanceOf(acct.address, pos_id_no).call()
                if bal_yes == 0 and bal_no == 0:
                    log.warning(f"[REDEEM] Position balance=0 — already redeemed (manually or by oracle). Skipping.")
                    return True
                log.warning(f"[REDEEM] Position balance YES={bal_yes} NO={bal_no} — proceeding with redeem")
            except Exception as e:
                log.warning(f"[REDEEM] Could not check position balance: {e} — proceeding anyway")

            # ── Retry loop: oracle can take 2-5 min after market close ────────────
            max_attempts = 10
            for attempt in range(1, max_attempts + 1):
                try:
                    gas_price = int(w3.eth.gas_price * 1.5)
                    nonce     = w3.eth.get_transaction_count(acct.address, "pending")

                    # Simulate first to get revert reason before wasting gas
                    try:
                        ctf.functions.redeemPositions(
                            _Web3.to_checksum_address(_USDC_ADDRESS), parent, cid, index_sets
                        ).call({"from": acct.address})
                    except Exception as sim_err:
                        err_str = str(sim_err)
                        log.warning(f"[REDEEM] Attempt {attempt}/{max_attempts} — sim revert: {err_str[:120]}")
                        if attempt < max_attempts:
                            log.warning(f"[REDEEM] Oracle not ready yet — retrying in 60s…")
                            time.sleep(60)
                            continue
                        else:
                            log.warning(f"[REDEEM] ❌ All {max_attempts} attempts failed. Redeem manually: polymarket.com/portfolio")
                            return False

                    # Simulation passed — send the real TX
                    txn    = ctf.functions.redeemPositions(
                        _Web3.to_checksum_address(_USDC_ADDRESS), parent, cid, index_sets
                    ).build_transaction({
                        "from":     acct.address,
                        "nonce":    nonce,
                        "gas":      250000,
                        "gasPrice": gas_price,
                    })
                    signed   = acct.sign_transaction(txn)
                    tx_hash  = w3.eth.send_raw_transaction(signed.raw_transaction)
                    receipt  = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
                    status   = "✅ OK" if receipt.status == 1 else "❌ FAIL"
                    log.warning(f"[REDEEM] {status} cid={condition_id[:16]}… tx={tx_hash.hex()[:16]}… attempt={attempt}")
                    if receipt.status == 1:
                        return True
                    # TX mined but reverted on-chain — position likely already redeemed, don't waste more gas
                    log.warning(f"[REDEEM] TX reverted on-chain — position already redeemed or payout=0. Not retrying.")
                    return True
                except Exception as e:
                    log.warning(f"[REDEEM] Attempt {attempt}/{max_attempts} error: {e}")
                    if attempt < max_attempts:
                        time.sleep(60)

            log.warning(f"[REDEEM] ❌ Exhausted retries for {condition_id[:18]}… — check polymarket.com/portfolio")
            return False
        except Exception as e:
            log.warning(f"[REDEEM] Fatal error: {e}")
            return False


def main():
    log.warning(f"🚀 Polymarket {ASSET} bot starting (V5 — matches Kalshi btc_bot.py)")

    client  = ClobClient(HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID, signature_type=0)
    creds   = client.create_or_derive_api_creds()
    client.set_api_creds(creds)
    log.warning(f"✅ Connected — API key {creds.api_key[:12]}...")

    # V11: set session start balance
    global _session_start_balance
    _session_start_balance = get_usdc_balance(client)
    log.warning(f"[BOT] Session start balance: ${_session_start_balance:.2f}")
    log.warning(f"[BOT] Watch: last {WATCH_WINDOW}s | Threshold: {CONFIRM_THRESHOLD*100:.0f}¢ | Confirms: {CONFIRM_CHECKS}→1")

    while True:
        now_utc = datetime.now(timezone.utc)

        # Find next market
        candidate = find_next_market(now_utc)
        if not candidate:
            log.info(f"[SCAN] No open {ASSET} windows (next scan in 30s)")
            time.sleep(30)
            continue

        m      = candidate["market"]
        secs   = candidate["secs"]
        q      = m["question"]
        end_dt     = candidate["end_dt"]
        window_key = f"{ASSET}_{end_dt.strftime('%Y%m%d_%H%M')}"

        # Already traded this window?
        if window_key in TRADED_WINDOWS:
            log.info(f"[SKIP] Already traded {q[:50]}")
            time.sleep(10)
            continue

        # Too early — wait
        if secs > WATCH_WINDOW:
            log.info(f"[WAIT] {q[:50]} closes in {secs:.0f}s")
            time.sleep(max(0, min(secs - WATCH_WINDOW - 5, 30)))
            continue

        # Parse token IDs and condition ID
        try:
            token_ids = json.loads(m.get("clobTokenIds", "[]"))
        except:
            token_ids = []
        condition_id = m.get("condition_id") or m.get("conditionId") or ""
        if len(token_ids) < 2:
            log.warning(f"[SKIP] No token IDs for {q[:50]}")
            TRADED_WINDOWS.add(window_key)
            time.sleep(10)
            continue

        # ── INNER POLL LOOP — stays on this market, no re-fetch ────
        certainty_side    = None
        certainty_counter = 0

        while True:
            # Use original end_dt — no re-fetch, no key mismatch
            secs = (end_dt - datetime.now(timezone.utc)).total_seconds()
            if secs <= 0:
                log.info(f"[WINDOW-DONE] {q[:40]}")
                TRADED_WINDOWS.add(window_key)
                break

            yes_price, no_price = get_best_prices(client, token_ids)
            log.info(f"[TICK] {q[:50]} t={secs:.0f}s yes={yes_price} no={no_price}")

            if yes_price is None and no_price is None:
                time.sleep(POLL_SECS)
                continue

            side, price, token_id = None, None, None

            # Safety net at T≤30s
            if secs <= SAFETY_NET_SECS:
                best_side, best_price, best_token = None, 0, None
                if yes_price is not None and yes_price > best_price:
                    best_side, best_price, best_token = "yes", yes_price, token_ids[0]
                if no_price is not None and no_price > best_price:
                    best_side, best_price, best_token = "no", no_price, token_ids[1]
                if best_side and best_price >= 0.90 and (1.0 - best_price) >= 0.03:
                    side, price, token_id = best_side, best_price, best_token
                    log.warning(f"[SAFETY-NET] T={secs:.0f}s forcing {side}@{price:.2f}")
                else:
                    TRADED_WINDOWS.add(window_key)
                    break
            else:
                yes_certain = yes_price is not None and yes_price >= CONFIRM_THRESHOLD
                no_certain  = no_price  is not None and no_price  >= CONFIRM_THRESHOLD
                if yes_certain:
                    if certainty_side == "yes": certainty_counter += 1
                    else: certainty_side, certainty_counter = "yes", 1
                elif no_certain:
                    if certainty_side == "no": certainty_counter += 1
                    else: certainty_side, certainty_counter = "no", 1
                else:
                    certainty_side, certainty_counter = None, 0
                    log.info(f"[WATCH] t={secs:.0f}s yes={yes_price} no={no_price} need {required_confirms(secs)} at ≥{CONFIRM_THRESHOLD:.0%}")
                    time.sleep(POLL_SECS)
                    continue
                if certainty_counter < required_confirms(secs):
                    log.info(f"[WATCH] {certainty_side.upper()}@{(yes_price if certainty_side=='yes' else no_price):.2f} {certainty_counter}/{required_confirms(secs)} t={secs:.0f}s")
                    time.sleep(POLL_SECS)
                    continue
                side = certainty_side
                price = yes_price if side == "yes" else no_price
                token_id = token_ids[0] if side == "yes" else token_ids[1]

            # Size and place
            balance = get_usdc_balance(client)
            bot_balance = balance / NUM_BOTS   # this bot's share of the shared pool
            usdc_risk = bot_balance * MAX_RISK_PCT
            size = round(usdc_risk / price, 2)
            size = max(0.01, min(size, MAX_USDC / price))
            usdc_cost = size * price

            if usdc_cost < MIN_ORDER_USDC:
                log.warning(f"[SIZE-SKIP] 🚫 ${usdc_cost:.2f} < min ${MIN_ORDER_USDC} — bal=${balance:.2f}/bot=${balance/NUM_BOTS:.2f} — need >${MIN_ORDER_USDC*NUM_BOTS/MAX_RISK_PCT:.0f} total to trade")
                TRADED_WINDOWS.add(window_key)
                continue

            log.warning(f"[ENTER] {ASSET} {side.upper()}@{price:.2f} x{size:.2f} (${usdc_cost:.2f}) bal=${balance:.2f} t={secs:.0f}s")
            success = place_order(client, token_id, price, size, side)
            TRADED_WINDOWS.add(window_key)

            if success:
                log.warning(f"[FILLED] {q[:45]}")
                if DRY_RUN:
                    global _sim_pnl, _sim_trades
                    _sim_trades += 1
                    settled_yes = None
                    for _ in range(35):
                        time.sleep(1)
                        try:
                            sy, sn = get_best_prices(client, token_ids)
                            if sy is not None and sy >= 0.98: settled_yes = True; break
                            if sy is not None and sy <= 0.02: settled_yes = False; break
                            if sn is not None and sn >= 0.98: settled_yes = False; break
                            if sn is not None and sn <= 0.02: settled_yes = True; break
                        except: pass
                    if settled_yes is not None:
                        won = (settled_yes and side == "yes") or (not settled_yes and side == "no")
                        payout = size * (1.0 - price) if won else 0.0
                        trade_pnl = payout - (0 if won else usdc_cost)
                        _sim_pnl += trade_pnl
                        result = "WIN" if won else "LOSS"
                        log.warning(f"[SIM-{result}] {side.upper()}@{price:.2f} x{size:.2f} pnl=${trade_pnl:+.2f} running=${_sim_pnl:+.2f}")
                    else:
                        log.warning(f"[SIM-UNKNOWN] Could not determine outcome")
                else:
                    # Live mode: background thread waits for settlement then redeems
                    def _settle_and_redeem(c, tids, cid, s, p, sz, secs_left, pk):
                        try:
                            wait = max(0, secs_left) + 10
                            log.warning(f"[SETTLE-WAIT] waiting {wait:.0f}s for market to close")
                            time.sleep(wait)
                            settled_yes = None
                            for _ in range(90):  # poll up to 90s post-close
                                time.sleep(1)
                                try:
                                    sy, sn = get_best_prices(c, tids)
                                    if sy is not None and sy >= 0.98: settled_yes = True; break
                                    if sy is not None and sy <= 0.02: settled_yes = False; break
                                    if sn is not None and sn >= 0.98: settled_yes = False; break
                                    if sn is not None and sn <= 0.02: settled_yes = True; break
                                except: pass
                            if settled_yes is not None:
                                won = (settled_yes and s == "yes") or (not settled_yes and s == "no")
                                result = "WIN" if won else "LOSS"
                                log.warning(f"[{result}] {s.upper()}@{p:.2f} x{sz:.2f}")
                                if cid and pk:
                                    # Wait for oracle to post on-chain before redeeming (typically 60-120s after close)
                                    log.warning("[REDEEM-WAIT] pausing 90s for oracle settlement...")
                                    time.sleep(90)
                                    redeem_market_positions(cid, pk)
                            else:
                                log.warning(f"[SETTLE-UNKNOWN] Could not determine outcome — redeem manually on polymarket.com")
                        except Exception as e:
                            log.error(f"[SETTLE-ERR] {e}")
                    threading.Thread(
                        target=_settle_and_redeem,
                        args=(client, token_ids, condition_id, side, price, size, secs, PRIVATE_KEY),
                        daemon=True
                    ).start()
            else:
                log.warning(f"[FAIL] Order failed — {q[:45]}")
            break  # exit inner poll loop


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        main()
    except KeyboardInterrupt:
        log.warning("Bot stopped.")
