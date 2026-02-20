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
    # BTC
    # NO  1-10¢:  98.7% WR, 19x ratio  → max aggression
    # NO 11-20¢:  95.0% WR, 5.7x ratio → push to 30
    # NO 21-30¢:  73.7% WR, 3.0x ratio → was 4, now 10
    # NO 31-50¢:  90.0% WR, 1.5x ratio → was 4, now 10 (small sample, conservative cap)
    # NO 51-65¢:  44.4% WR, 0.75x ratio → thin edge, keep minimal
    # NO 66-80¢:  84.6% WR, 0.37x ratio → keep small
    # YES 81-90¢: 90.6% WR, 0.18x ratio → correctly sized, no change
    # YES 91-99¢: 97.1% WR, 0.05x ratio → correctly sized, no change
    'BTC': {
        (1,  10):  30,   # 98.7% WR — 30ct @ avg 5¢  = $1.50 risk
        (11, 20):  30,   # 95.0% WR — 30ct @ avg 15¢ = $4.50 risk  ↑ was 25
        (21, 30):  10,   # 73.7% WR — 10ct @ avg 25¢ = $2.50 risk  ↑ was 4
        (31, 50):  10,   # 90.0% WR — 10ct @ avg 40¢ = $4.00 risk  ↑ was 4
        (51, 65):   2,   # 44.4% WR —  2ct @ avg 57¢ = $1.14 risk
        (66, 80):   3,   # 84.6% WR —  3ct @ avg 73¢ = $2.19 risk
        (81, 90):   7,   # 90.6% WR —  7ct @ avg 85¢ = $5.95 risk  no change
        (91, 99):   6,   # 97.1% WR —  6ct @ avg 95¢ = $5.70 risk  no change
    },

    # ETH
    # NO  1-10¢:  96.1% WR, 19x ratio  → max aggression
    # NO 11-20¢:  87.1% WR, 5.7x ratio → push to 30
    # NO 21-30¢:  66.7% WR, 3.0x ratio → was 5, now 10
    # NO 31-40¢:  61.9% WR, 1.9x ratio → keep conservative
    # NO 41-50¢:  63.6% WR, 1.2x ratio → keep conservative
    # NO 51-65¢:  71.4% WR, 0.75x ratio → keep
    # NO 66-80¢:  71.4% WR, 0.37x ratio → keep
    # YES 81-99¢: 91.2% WR, 0.14x ratio → correctly sized, no change
    'ETH': {
        (1,  10):  30,   # 96.1% WR — 30ct @ avg 5¢  = $1.50 risk  no change
        (11, 20):  30,   # 87.1% WR — 30ct @ avg 15¢ = $4.50 risk  ↑ was 20
        (21, 30):  10,   # 66.7% WR — 10ct @ avg 25¢ = $2.50 risk  ↑ was 5
        (31, 40):   4,   # 61.9% WR —  4ct @ avg 35¢ = $1.40 risk  no change
        (41, 50):   4,   # 63.6% WR —  4ct @ avg 45¢ = $1.80 risk  no change
        (51, 65):   5,   # 71.4% WR —  5ct @ avg 57¢ = $2.85 risk  no change
        (66, 80):   4,   # 71.4% WR —  4ct @ avg 73¢ = $2.92 risk  no change
        (81, 99):   7,   # 91.2% WR —  7ct @ avg 88¢ = $6.16 risk  no change
    },

    # SOL
    # NO  1-10¢:  92.9% WR, 19x ratio  → push to 30
    # NO 11-20¢:  88.2% WR, 5.7x ratio → push to 30
    # NO 41-50¢:  83.3% WR, 1.2x ratio → small sample, keep conservative
    # YES 81-90¢: 90.0% WR, 0.18x ratio → correctly sized, no change
    # YES 91-99¢: 95.0% WR, 0.05x ratio → margin is razor thin, reduce to 4
    'SOL': {
        (1,  10):  30,   # 92.9% WR — 30ct @ avg 5¢  = $1.50 risk  ↑ was 25
        (11, 20):  30,   # 88.2% WR — 30ct @ avg 15¢ = $4.50 risk  ↑ was 20
        (41, 50):   4,   # 83.3% WR —  4ct @ avg 45¢ = $1.80 risk  no change
        (81, 90):   7,   # 90.0% WR —  7ct @ avg 85¢ = $5.95 risk  no change
        (91, 99):   4,   # 95.0% WR —  4ct @ avg 95¢ = $3.80 risk  ↓ was 6
        # Note: SOL YES 91-99¢ sits exactly at mathematical breakeven (95% WR needed).
        # Reducing from 6 to 4 contracts limits downside if accuracy dips slightly.
    },

    # XRP
    # NO  1-10¢:  89.5% WR, 19x ratio  → max aggression
    # NO 11-20¢:  77.8% WR, 5.7x ratio → push to 30
    # NO 31-40¢:  76.5% WR, 1.9x ratio → was 5, now 10
    # NO 51-65¢:  71.4% WR, 0.75x ratio → keep
    # NO 66-80¢:  66.7% WR, 0.37x ratio → keep small
    # YES 81-99¢: 84.6% WR, 0.14x ratio → correctly sized, no change
    'XRP': {
        (1,  10):  30,   # 89.5% WR — 30ct @ avg 5¢  = $1.50 risk  no change
        (11, 20):  30,   # 77.8% WR — 30ct @ avg 15¢ = $4.50 risk  ↑ was 20
        (31, 40):  10,   # 76.5% WR — 10ct @ avg 35¢ = $3.50 risk  ↑ was 5
        # 41-50¢ skipped via XRP_SKIP_RANGE — no entry here
        (51, 65):   4,   # 71.4% WR —  4ct @ avg 57¢ = $2.28 risk  no change
        (66, 80):   3,   # 66.7% WR —  3ct @ avg 73¢ = $2.19 risk  no change
        (81, 99):   5,   # 84.6% WR —  5ct @ avg 88¢ = $4.40 risk  no change
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
