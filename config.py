# config.py — Shared constants, data-driven sizing, price caps
# Used by all 4 bots (BTC, ETH, SOL, XRP)
#
# DATA-DRIVEN V7 (Feb 20, 2026)
# Full clean-entry analysis: 1,575 settled 15M markets, Jan 24 – Feb 20 2026
# Single-entry verified: only markets with no re-entry violations included
#
# V6 WAS WRONG — it was built on contaminated (multi-entry) data.
# Clean data shows YES 81-99¢ is net POSITIVE on every asset.
# V7 restores all profitable buckets. Only true negatives are removed.
#
# TRUE NEGATIVES (only things actually losing money on clean data):
#   XRP NO 41-50¢  — 0% WR, -$0.07/mkt  (2 markets)
#   SOL NO 51-65¢  — 33% WR, marginal negative
#
# PROVEN WINNERS — NET PROFIT PER MARKET (clean entries):
#   BTC NO  1-10¢:  $3.26/mkt | 98.7% WR | 235 markets
#   BTC NO 11-20¢:  $3.70/mkt | 95.0% WR | 120 markets
#   BTC YES 81-90¢: $3.60/mkt | 90.6% WR |  96 markets
#   BTC YES 91-99¢: $2.53/mkt | 97.1% WR | 279 markets
#   BTC NO 31-50¢:  $5.06/mkt | 90.0% WR |  10 markets  ← best $/mkt
#   ETH NO  1-10¢:  $2.66/mkt | 96.1% WR |  77 markets
#   ETH NO 11-20¢:  $2.41/mkt | 87.1% WR |  93 markets
#   ETH YES 81-90¢: $3.66/mkt | 91.2% WR |  80 markets
#   ETH YES 91-99¢: $4.17/mkt |100.0% WR |  22 markets
#   ETH NO 51-65¢:  $2.93/mkt | 71.4% WR |  28 markets
#   SOL NO  1-10¢:  $2.06/mkt | 92.9% WR |  85 markets
#   SOL NO 11-20¢:  $1.71/mkt | 88.2% WR |  51 markets
#   SOL YES 91-99¢: $2.67/mkt | 95.0% WR |  40 markets
#   SOL YES 81-90¢: $1.78/mkt | 90.0% WR |  10 markets
#   XRP NO  1-10¢:  $2.49/mkt | 89.5% WR |  57 markets
#   XRP NO 11-20¢:  $2.82/mkt | 77.8% WR |  27 markets
#   XRP NO 31-40¢:  $2.82/mkt | 76.5% WR |  17 markets
#   XRP YES 91-99¢: $2.22-2.39/mkt | 80-96% WR

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
# All buckets with positive net/mkt on clean 1,575-market dataset are open.
# ETH_HIGH_PRICE_ALLOWED handles YES 81¢+ for ETH separately.
MAX_ENTRY_PRICE_CENTS = {
    'BTC': 99,   # Full range open — every bucket net positive on clean data
    'ETH': 30,   # ETH_HIGH_PRICE_ALLOWED handles 81c+ separately
    'SOL': 99,   # YES 81-99¢: 90-97% WR, strongly positive
    'XRP': 99,   # Skip range below handles only true dead zone
}

# XRP: only true negative bucket is 41-50¢ (0% WR, 2 markets)
# 31-40¢ is PROFITABLE (76.5% WR, $2.82/mkt, 17 markets) — do NOT skip it
XRP_SKIP_RANGE = (41, 50)

# ETH high-price window: YES 81-99¢ is net positive on clean data
# ETH YES 81-90¢: 91.2% WR, $3.66/mkt (80 markets)
# ETH YES 91-99¢: 100% WR, $4.17/mkt (22 markets)
ETH_HIGH_PRICE_ALLOWED = True
ETH_HIGH_PRICE_MIN = 81

# Hard cost ceiling per market (belt-and-suspenders)
MAX_COST_PER_MARKET = 6.25    # 25% of $25 bot allocation

# -------------- CONTRACT SIZING TABLE --------------------------
# Based on clean-entry win rates from 1,575 markets (Jan 24 – Feb 20 2026).
# Sizing philosophy:
#   - 98%+ WR buckets: max aggression (25-30 contracts)
#   - 90-97% WR buckets: high confidence (7-15 contracts)
#   - 75-89% WR buckets: moderate (3-7 contracts)
#   - 60-74% WR buckets: conservative (2-4 contracts)
# Cost cap: MAX_COST_PER_MARKET = $6.25 is the hard ceiling regardless.
CONTRACT_SIZING = {
    # BTC — clean data win rates
    # NO  1-10¢:  98.7% WR, $3.26/mkt — highest confidence bucket
    # NO 11-20¢:  95.0% WR, $3.70/mkt — excellent
    # NO 21-30¢:  73.7% WR, $3.52/mkt — positive, conservative size
    # NO 31-50¢:  90.0% WR, $5.06/mkt — small sample (10 mkts), conservative
    # NO 51-65¢:  44.4% WR, $1.80/mkt — positive but thin, minimal size
    # NO 66-80¢:  84.6% WR, $1.50/mkt — positive, small size
    # YES 81-90¢: 90.6% WR, $3.60/mkt — strong
    # YES 91-99¢: 97.1% WR, $2.53/mkt — very strong
    'BTC': {
        (1,  10):  30,   # 98.7% WR — 30ct @ avg 5¢  = $1.50 risk
        (11, 20):  25,   # 95.0% WR — 25ct @ avg 15¢ = $3.75 risk
        (21, 30):   4,   # 73.7% WR —  4ct @ avg 25¢ = $1.00 risk
        (31, 50):   4,   # 90.0% WR —  4ct @ avg 40¢ = $1.60 risk (small sample)
        (51, 65):   2,   # 44.4% WR —  2ct @ avg 57¢ = $1.14 risk (thin edge)
        (66, 80):   3,   # 84.6% WR —  3ct @ avg 73¢ = $2.19 risk
        (81, 90):   7,   # 90.6% WR —  7ct @ avg 85¢ = $5.95 risk
        (91, 99):   6,   # 97.1% WR —  6ct @ avg 95¢ = $5.70 risk
    },

    # ETH — clean data win rates
    # NO  1-10¢:  96.1% WR, $2.66/mkt
    # NO 11-20¢:  87.1% WR, $2.41/mkt
    # NO 21-30¢:  66.7% WR, $1.63/mkt — positive, conservative
    # NO 31-40¢:  61.9% WR, $1.86/mkt — positive, conservative
    # NO 41-50¢:  63.6% WR, $2.48/mkt — positive
    # NO 51-65¢:  71.4% WR, $2.93/mkt — solid
    # NO 66-80¢:  71.4% WR, $2.10/mkt — solid
    # YES 81-90¢: 91.2% WR, $3.66/mkt — strong
    # YES 91-99¢: 100% WR,  $4.17/mkt — best ETH bucket
    'ETH': {
        (1,  10):  30,   # 96.1% WR — 30ct @ avg 5¢  = $1.50 risk
        (11, 20):  20,   # 87.1% WR — 20ct @ avg 15¢ = $3.00 risk
        (21, 30):   5,   # 66.7% WR —  5ct @ avg 25¢ = $1.25 risk
        (31, 40):   4,   # 61.9% WR —  4ct @ avg 35¢ = $1.40 risk
        (41, 50):   4,   # 63.6% WR —  4ct @ avg 45¢ = $1.80 risk
        (51, 65):   5,   # 71.4% WR —  5ct @ avg 57¢ = $2.85 risk
        (66, 80):   4,   # 71.4% WR —  4ct @ avg 73¢ = $2.92 risk
        (81, 99):   7,   # 91-100% WR — 7ct @ avg 88¢ = $6.16 risk
    },

    # SOL — clean data win rates
    # NO  1-10¢:  92.9% WR, $2.06/mkt
    # NO 11-20¢:  88.2% WR, $1.71/mkt
    # NO 41-50¢:  83.3% WR, $4.07/mkt — strong (small sample, 6 mkts)
    # YES 81-90¢: 90.0% WR, $1.78/mkt
    # YES 91-99¢: 95.0% WR, $2.67/mkt — strong
    # SKIP: NO 51-65¢ — 33.3% WR, marginal negative
    'SOL': {
        (1,  10):  25,   # 92.9% WR — 25ct @ avg 5¢  = $1.25 risk
        (11, 20):  20,   # 88.2% WR — 20ct @ avg 15¢ = $3.00 risk
        (41, 50):   4,   # 83.3% WR —  4ct @ avg 45¢ = $1.80 risk (small sample)
        (81, 90):   7,   # 90.0% WR —  7ct @ avg 85¢ = $5.95 risk
        (91, 99):   6,   # 95.0% WR —  6ct @ avg 95¢ = $5.70 risk
    },

    # XRP — clean data win rates
    # NO  1-10¢:  89.5% WR, $2.49/mkt
    # NO 11-20¢:  77.8% WR, $2.82/mkt
    # NO 31-40¢:  76.5% WR, $2.82/mkt — SKIP RANGE was wrong, this is profitable
    # NO 41-50¢:  0.0%  WR — TRUE dead zone, skip via XRP_SKIP_RANGE
    # NO 51-65¢:  71.4% WR, $2.86/mkt — solid
    # NO 66-80¢:  66.7% WR, $2.30/mkt — positive
    # YES 81-90¢: 80.0% WR, $2.22/mkt
    # YES 91-99¢: 95.7% WR, $2.38/mkt
    'XRP': {
        (1,  10):  30,   # 89.5% WR — 30ct @ avg 5¢  = $1.50 risk
        (11, 20):  20,   # 77.8% WR — 20ct @ avg 15¢ = $3.00 risk
        (31, 40):   5,   # 76.5% WR —  5ct @ avg 35¢ = $1.75 risk
        # 41-50¢ skipped via XRP_SKIP_RANGE
        (51, 65):   4,   # 71.4% WR —  4ct @ avg 57¢ = $2.28 risk
        (66, 80):   3,   # 66.7% WR —  3ct @ avg 73¢ = $2.19 risk
        (81, 99):   5,   # 80-96% WR — 5ct @ avg 88¢ = $4.40 risk
    },
}

# -------------- MINIMUM CONFIDENCE (loose gate) ----------------
MIN_CONFIDENCE = 0.60

# -------------- HISTORICAL ACCURACY BY PRICE BUCKET -------------
# From clean-entry analysis of 1,575 markets, Jan 24 – Feb 20 2026
# Format: (win_rate, sample_size, kelly_fraction)
HISTORICAL_ACCURACY = {
    'BTC': {
        'NO_1_10':   (0.987, 235, 0.95),
        'NO_11_20':  (0.950, 120, 0.90),
        'NO_21_30':  (0.737, 19,  0.30),
        'NO_31_50':  (0.900, 10,  0.40),   # small sample — conservative kelly
        'NO_51_65':  (0.444, 9,   0.10),   # thin edge
        'NO_66_80':  (0.867, 15,  0.35),
        'YES_81_90': (0.906, 96,  0.40),
        'YES_91_99': (0.971, 279, 0.90),
    },
    'ETH': {
        'NO_1_10':   (0.961, 77,  0.90),
        'NO_11_20':  (0.871, 93,  0.72),
        'NO_21_30':  (0.667, 24,  0.30),
        'NO_31_40':  (0.619, 21,  0.25),
        'NO_41_50':  (0.636, 11,  0.25),
        'NO_51_65':  (0.714, 28,  0.35),
        'NO_66_80':  (0.714, 14,  0.35),
        'YES_81_99': (0.912, 102, 0.45),
    },
    'SOL': {
        'NO_1_10':   (0.929, 85,  0.88),
        'NO_11_20':  (0.882, 51,  0.72),
        'NO_41_50':  (0.833, 6,   0.30),   # small sample
        'YES_81_90': (0.900, 10,  0.40),
        'YES_91_99': (0.950, 40,  0.85),
    },
    'XRP': {
        'NO_1_10':   (0.895, 57,  0.85),
        'NO_11_20':  (0.778, 27,  0.65),
        'NO_31_40':  (0.765, 17,  0.55),
        'NO_51_65':  (0.714, 14,  0.40),
        'NO_66_80':  (0.667, 3,   0.25),   # small sample
        'YES_81_99': (0.846, 51,  0.40),
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
