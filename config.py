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
    'BTC': 90,   # Opens YES 81-90c bucket (91.8% WR, 552 trades)
    'ETH': 25,   # ETH_HIGH_PRICE_ALLOWED handles 81c+ separately
    'SOL': 99,   # Opens YES 81-99c bucket (96.4% WR, 137 trades)
    'XRP': 99,   # Unchanged — skip range handles dead zone
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
        (1, 10):  30,   # 94.9% WR — 30ct @ avg 5c = $1.50 risk
        (11, 20): 25,   # 89.0% WR — 25ct @ avg 15c = $3.75 risk
        (21, 30): 5,    # 58.5% WR — 5ct @ avg 25c = $1.25 risk
        (31, 40): 3,    # marginal — keep conservative
        (81, 90): 7,    # 91.8% WR — 7ct @ avg 85c = $5.95 risk
    },
    'ETH': {
        (1, 10):  30,   # 94.9% WR — was 20
        (11, 20): 25,   # 89.0% WR — was 10
        (21, 25): 5,    # unchanged
        (81, 99): 7,    # 91.6% WR — was 3
    },
    'SOL': {
        (1, 10):  30,   # 94.9% WR — 30ct @ avg 5c = $1.50 risk
        (11, 20): 25,   # 89.0% WR — 25ct @ avg 15c = $3.75 risk
        (81, 90): 7,    # 96.4% WR — 7ct @ avg 85c = $5.95 risk
        (91, 99): 6,    # 96.4% WR — 6ct @ avg 95c = $5.70 risk
    },
    'XRP': {
        (1, 10):  30,   # 87.9% WR — was 20
        (11, 20): 25,   # 87.9% WR — was 10
        (21, 30): 10,   # 87.9% WR — was 6
        (51, 65): 4,    # unchanged
        (66, 80): 0,    # 19% WR in full data — REMOVE this bucket
        (81, 99): 5,    # 90.8% WR — was 2
    },
}

# -------------- MINIMUM CONFIDENCE (loose gate) ----------------
MIN_CONFIDENCE = 0.60

# -------------- HISTORICAL ACCURACY BY PRICE BUCKET -------------
# From analysis of 2,066 markets, Feb 1-19 2026
HISTORICAL_ACCURACY = {
    'BTC': {
        'NO_1_10':   (0.949, 619,  0.90),
        'NO_11_20':  (0.890, 411,  0.72),
        'NO_21_50':  (0.585, 78,   0.23),
        'YES_81_90': (0.918, 552,  0.14),
    },
    'ETH': {
        'NO_1_10':   (0.949, 411,  0.90),
        'NO_11_20':  (0.890, 280,  0.72),
        'NO_21_50':  (0.514, 37,   0.12),
        'YES_81_90': (0.916, 154,  0.13),
    },
    'SOL': {
        'NO_1_10':   (0.932, 294,  0.88),
        'NO_11_20':  (0.890, 174,  0.72),
        'YES_81_90': (0.964, 137,  0.14),
    },
    'XRP': {
        'NO_1_10':   (0.879, 280,  0.85),
        'NO_11_20':  (0.879, 200,  0.68),
        'NO_21_30':  (0.879, 120,  0.65),
        'YES_81_90': (0.908, 228,  0.09),
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
