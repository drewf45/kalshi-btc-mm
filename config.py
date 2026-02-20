# config.py — Shared constants, data-driven sizing, price caps
# Used by all 4 bots (BTC, ETH, SOL, XRP)
#
# DATA-DRIVEN V6 (Feb 20, 2026)
# Full historical analysis: 2,221 settled 15M markets across all data
#
# CORE FINDING: All edge lives in NO contracts at low prices.
# YES 81-99¢ entries are net NEGATIVE on every single asset.
# BTC has no edge above NO 20¢. Killed all losing buckets.
#
# PROVEN WINNERS ONLY:
#   XRP  NO 1-10¢:  $4.07/trade, 80% WR  ← best bucket in system
#   XRP  NO 11-20¢: $3.24/trade, 69% WR
#   ETH  NO 1-10¢:  $3.02/trade, 86% WR
#   BTC  NO 1-10¢:  $2.93/trade, 78% WR
#   ETH  NO 11-20¢: $2.22/trade, 73% WR
#   BTC  NO 11-20¢: $1.80/trade, 60% WR
#   SOL  NO 11-20¢: $1.34/trade, 68% WR
#   SOL  NO 1-10¢:  $1.09/trade, 56% WR
#   ETH  NO 21-30¢: $1.01/trade, 58% WR
#   XRP  NO 31-50¢: $0.95/trade, 67% WR

# ======================== IRON RULES ========================
# RULE 1: One direction per market — once positioned, DONE
# RULE 2: Data-driven contract sizing per asset per price bucket
# RULE 3: Asset-specific price caps with dead zone skips
# RULE 4: Never buy both YES and NO on same market
# RULE 5: Single entry per market — TRADED_TICKERS + API check
# =============================================================

# -------------- BANKROLL ALLOCATION ----------------------------
TOTAL_BANKROLL = 100.00
BOT_ALLOCATION = 25.00        # Per bot ($100 / 4 bots)
MIN_BOT_BALANCE = 5.00        # Pause if balance drops below this
MAX_RISK_PER_TRADE_PCT = 0.25 # 25% of bot allocation = $6.25 max risk

# -------------- PRICE CAPS (DATA-DRIVEN PER ASSET) -------------
# Derived from 19 days of settlement data. Only price ranges with
# positive EV and 55%+ win rate are permitted.
MAX_ENTRY_PRICE_CENTS = {
    'BTC': 20,   # Data: only NO 1-10¢ and NO 11-20¢ are net positive
    'ETH': 30,   # Data: NO 1-10¢, NO 11-20¢, NO 21-30¢ all net positive
    'SOL': 20,   # Data: only NO 1-10¢ and NO 11-20¢ are net positive
    'XRP': 50,   # Data: edge extends to NO 31-50¢ ($0.95/trade, 67% WR)
}

# XRP-specific: skip the 21-30¢ dead zone (insufficient sample size, 5 trades)
# XRP NO 31-50¢ IS now a valid bucket (21 trades, 67% WR)
XRP_SKIP_RANGE = (21, 30)

# ETH high-price window: DISABLED — YES 81-99¢ is net -$154 across all data
ETH_HIGH_PRICE_ALLOWED = False
ETH_HIGH_PRICE_MIN = 81          # kept for reference, flag is False

# Hard cost ceiling per market (belt-and-suspenders)
MAX_COST_PER_MARKET = 6.25    # 25% of $25 bot allocation

# -------------- CONTRACT SIZING TABLE --------------------------
# Kelly criterion applied to 19 days of actual win rates per bucket.
# (asset, price_bucket) → exact contract count
# Capped at 25% of bot allocation ($6.25 max risk) per trade.
CONTRACT_SIZING = {
    # BTC: only NO 1-20¢ has edge
    # NO 1-10¢:  78% WR, $2.93/trade net
    # NO 11-20¢: 60% WR, $1.80/trade net
    'BTC': {
        (1, 10):  30,   # 78% WR — 30ct @ avg 5¢  = $1.50 risk
        (11, 20): 15,   # 60% WR — lower confidence, reduce from 25 to 15
    },

    # ETH: NO 1-10¢, 11-20¢, 21-30¢ all net positive
    # NO 1-10¢:  86% WR, $3.02/trade net
    # NO 11-20¢: 73% WR, $2.22/trade net
    # NO 21-30¢: 58% WR, $1.01/trade net
    'ETH': {
        (1, 10):  30,   # 86% WR — 30ct @ avg 5¢  = $1.50 risk
        (11, 20): 20,   # 73% WR — 20ct @ avg 15¢ = $3.00 risk
        (21, 30): 8,    # 58% WR — 8ct  @ avg 25¢ = $2.00 risk
    },

    # SOL: NO 1-10¢ and NO 11-20¢ only
    # NO 1-10¢:  56% WR, $1.09/trade net
    # NO 11-20¢: 68% WR, $1.34/trade net
    'SOL': {
        (1, 10):  20,   # 56% WR — reduce sizing, thinner edge
        (11, 20): 20,   # 68% WR — 20ct @ avg 15¢ = $3.00 risk
    },

    # XRP: edge extends further than other assets
    # NO 1-10¢:  80% WR, $4.07/trade net  ← BEST BUCKET IN ENTIRE SYSTEM
    # NO 11-20¢: 69% WR, $3.24/trade net
    # NO 31-50¢: 67% WR, $0.95/trade net
    'XRP': {
        (1, 10):  30,   # 80% WR — 30ct @ avg 5¢  = $1.50 risk
        (11, 20): 20,   # 69% WR — 20ct @ avg 15¢ = $3.00 risk
        (31, 50): 8,    # 67% WR — 8ct  @ avg 40¢ = $3.20 risk
    },
}

# -------------- MINIMUM CONFIDENCE (loose gate) ----------------
MIN_CONFIDENCE = 0.60

# -------------- HISTORICAL ACCURACY BY PRICE BUCKET -------------
# From full analysis of 2,221 settled markets across all data
HISTORICAL_ACCURACY = {
    'BTC': {
        'NO_1_10':   (0.780, 288, 0.90),   # live data: 78% WR, 288 trades
        'NO_11_20':  (0.600, 200, 0.72),   # live data: 60% WR, 200 trades
    },
    'ETH': {
        'NO_1_10':   (0.857, 98,  0.90),   # live data: 86% WR, 98 trades
        'NO_11_20':  (0.730, 133, 0.72),   # live data: 73% WR, 133 trades
        'NO_21_30':  (0.576, 33,  0.55),   # live data: 58% WR, 33 trades
    },
    'SOL': {
        'NO_1_10':   (0.556, 153, 0.85),   # live data: 56% WR, 153 trades
        'NO_11_20':  (0.684, 76,  0.72),   # live data: 68% WR, 76 trades
    },
    'XRP': {
        'NO_1_10':   (0.798, 84,  0.90),   # live data: 80% WR, 84 trades
        'NO_11_20':  (0.688, 48,  0.72),   # live data: 69% WR, 48 trades
        'NO_31_50':  (0.667, 21,  0.55),   # live data: 67% WR, 21 trades
    },
}

# -------------- SETTLEMENT BIAS --------------------------------
SETTLEMENT_BIAS = {
    'BTC': {'yes': 0.532, 'no': 0.468},
    'ETH': {'yes': 0.514, 'no': 0.486},
    'SOL': {'yes': 0.469, 'no': 0.531},
    'XRP': {'yes': 0.727, 'no': 0.273},
}

# -------------- ASSET CONFIGURATION ----------------------------
ASSET_CONFIG = {
    'BTC': {
        'series_ticker': 'KXBTC15M',
        'spot_url': 'https://api.coinbase.com/v2/prices/BTC-USD/spot',
        'candles_url': 'https://api.exchange.coinbase.com/products/BTC-USD/candles',
        'default_sigma': 12.0,
        'sigma_floor': 6.0,
        'sigma_ceil': 40.0,
        'boundary_buffer_usd': 50.0,
    },
    'ETH': {
        'series_ticker': 'KXETH15M',
        'spot_url': 'https://api.coinbase.com/v2/prices/ETH-USD/spot',
        'candles_url': 'https://api.exchange.coinbase.com/products/ETH-USD/candles',
        'default_sigma': 0.35,
        'sigma_floor': 0.1,
        'sigma_ceil': 2.0,
        'boundary_buffer_usd': 5.0,
    },
    'SOL': {
        'series_ticker': 'KXSOL15M',
        'spot_url': 'https://api.coinbase.com/v2/prices/SOL-USD/spot',
        'candles_url': 'https://api.exchange.coinbase.com/products/SOL-USD/candles',
        'default_sigma': 0.07,
        'sigma_floor': 0.02,
        'sigma_ceil': 0.5,
        'boundary_buffer_usd': 2.0,
    },
    'XRP': {
        'series_ticker': 'KXXRP15M',
        'spot_url': 'https://api.coinbase.com/v2/prices/XRP-USD/spot',
        'candles_url': 'https://api.exchange.coinbase.com/products/XRP-USD/candles',
        'default_sigma': 0.0006,
        'sigma_floor': 0.0002,
        'sigma_ceil': 0.005,
        'boundary_buffer_usd': 0.02,
    },
}

# -------------- TIMING ----------------------------------------
OBSERVE_START_SECONDS = 720
BUY_START_SECONDS = 600
ENTRY_LAST_SECONDS = 5
POLL_SECONDS = 1.0
META_REFRESH_SECONDS = 10.0

# -------------- SESSION LIMITS ---------------------------------
ENABLE_SESSION_LIMITS = True
DAILY_MAX_LOSS_PERCENT = 0.75
SESSION_CONSECUTIVE_LOSSES_LIMIT = 4
SESSION_COOLDOWN_MINUTES = 15
BALANCE_CHECK_DELAY_SECONDS = 300

# -------------- SHARED CONSTANTS --------------------------------
FEE_CENTS_PER_CONTRACT = 0
NUM_CONCURRENT_BOTS = 4
