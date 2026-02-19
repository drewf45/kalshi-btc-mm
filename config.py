# config.py — Shared constants, data-driven sizing, price caps
# Used by all 4 bots (BTC, ETH, SOL, XRP)
#
# DATA-DRIVEN V4 (Feb 19, 2026)
# Full historical analysis: 2,066 settled 15M markets, Feb 1-19 2026
# Kelly-criterion contract sizing per asset per price bucket

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
    'BTC': 40,   # Edge stops at 40¢. 41-99¢ is net negative.
    'ETH': 25,   # Primary edge is 1-25¢. 26-80¢ is dead zone.
    'SOL': 20,   # Hard cap. 21¢+ is marginal or losing.
    'XRP': 99,   # XRP has edge at almost all prices except 31-50¢.
}

# XRP-specific: skip the 31-50¢ dead zone (10% WR, -$4.40 net)
XRP_SKIP_RANGE = (31, 50)

# ETH high-price window: allow 81-99¢ entries (83% WR, +$708 net)
ETH_HIGH_PRICE_ALLOWED = True
ETH_HIGH_PRICE_MIN = 81

# Hard cost ceiling per market (belt-and-suspenders)
MAX_COST_PER_MARKET = 6.25    # 25% of $25 bot allocation

# -------------- CONTRACT SIZING TABLE --------------------------
# Kelly criterion applied to 19 days of actual win rates per bucket.
# (asset, price_bucket) → exact contract count
# Capped at 25% of bot allocation ($6.25 max risk) per trade.
CONTRACT_SIZING = {
    'BTC': {
        (1, 10):  15,   # 89% WR — 15ct @ avg 5¢ = $0.75 risk
        (11, 20): 8,    # 70% WR — 8ct @ avg 15¢ = $1.20 risk
        (21, 30): 5,    # 46% WR — 5ct @ avg 25¢ = $1.25 risk
        (31, 40): 3,    # 61% WR — 3ct @ avg 35¢ = $1.05 risk
    },
    'ETH': {
        (1, 10):  20,   # 96% WR — 20ct @ avg 5¢ = $1.00 risk
        (11, 20): 10,   # 86% WR — 10ct @ avg 15¢ = $1.50 risk
        (21, 25): 5,    # 54% WR — 5ct @ avg 23¢ = $1.15 risk
        (81, 99): 3,    # 83% WR high-price window — 3ct @ avg 90¢ = $2.70 risk
    },
    'SOL': {
        (1, 10):  15,   # 74% WR — 15ct @ avg 5¢ = $0.75 risk
        (11, 20): 8,    # 80% WR — 8ct @ avg 15¢ = $1.20 risk
    },
    'XRP': {
        (1, 10):  20,   # 84% WR — 20ct @ avg 5¢ = $1.00 risk
        (11, 20): 10,   # 81% WR — 10ct @ avg 15¢ = $1.50 risk
        (21, 30): 6,    # 86% WR — 6ct @ avg 25¢ = $1.50 risk
        (51, 65): 4,    # 80% WR — 4ct @ avg 58¢ = $2.32 risk
        (66, 80): 3,    # 100% WR (small sample) — 3ct @ avg 73¢ = $2.19 risk
        (81, 99): 2,    # 89% WR — 2ct @ avg 90¢ = $1.80 risk
    },
}

# -------------- MINIMUM CONFIDENCE (loose gate) ----------------
MIN_CONFIDENCE = 0.60

# -------------- HISTORICAL ACCURACY BY PRICE BUCKET -------------
# From analysis of 2,066 markets, Feb 1-19 2026
HISTORICAL_ACCURACY = {
    'BTC': {
        'NO_1_10':   (0.964, 278, 0.888),
        'NO_11_20':  (0.888, 187, 0.716),
        'NO_21_50':  (0.635, 52,  0.226),
        'YES_81_90': (0.813, 134, -0.093),
        'YES_91_95': (0.931, 145, -0.022),
        'YES_96_99': (0.961, 102, -0.029),
    },
    'ETH': {
        'NO_1_10':   (0.940, 84,  0.851),
        'NO_11_20':  (0.861, 101, 0.688),
        'NO_21_50':  (0.571, 21,  0.279),
        'YES_81_90': (0.905, 63,  -0.001),
        'YES_91_95': (0.833, 12,  -0.124),
    },
    'SOL': {
        'NO_1_10':   (0.946, 147, 0.879),
        'NO_11_20':  (0.851, 67,  0.703),
        'YES_81_90': (0.950, 20,  0.045),
        'YES_91_95': (0.941, 34,  -0.005),
    },
    'XRP': {
        'NO_1_10':   (0.935, 77,  0.864),
        'NO_11_20':  (0.828, 29,  0.671),
        'YES_81_90': (0.900, 20,  0.001),
        'YES_91_95': (0.943, 35,  -0.001),
    },
}

# -------------- SETTLEMENT BIAS --------------------------------
SETTLEMENT_BIAS = {
    'BTC': {'yes': 0.447, 'no': 0.553},
    'ETH': {'yes': 0.367, 'no': 0.633},
    'SOL': {'yes': 0.396, 'no': 0.604},
    'XRP': {'yes': 0.474, 'no': 0.526},
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
