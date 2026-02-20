# config.py — Shared constants, data-driven sizing, price caps
# Used by all 4 bots (BTC, ETH, SOL, XRP)
#
# DATA-DRIVEN V8 (Feb 20, 2026)
# Source: 1,526 clean single-entry 15M markets, Jan 24 – Feb 20 2026
# P&L method: true net = Profit_In_Dollars - (contracts * price / 100)
#
# SYSTEM TRUTH:
#   Edge = NO contracts at low prices only
#   Everything else = killed by correct math
#
# PROVEN EDGE (NO contracts, clean entries):
#   BTC NO  1-5¢:  100% WR | $3.26/mkt | 109 markets | kelly 1.0
#   BTC NO  6-10¢:  97.9% WR | $3.34/mkt |  97 markets | kelly 0.98
#   BTC NO 11-15¢:  96.7% WR | $3.01/mkt |  92 markets | kelly 0.96
#   BTC NO 16-20¢:  85.3% WR | $3.86/mkt |  34 markets | kelly 0.83
#   ETH NO  1-5¢:   94.7% WR | $2.85/mkt |  19 markets | kelly 0.94
#   ETH NO  6-10¢:  96.7% WR | $2.47/mkt |  60 markets | kelly 0.97
#   ETH NO 11-15¢:  90.2% WR | $2.41/mkt |  51 markets | kelly 0.89
#   ETH NO 16-20¢:  83.7% WR | $2.04/mkt |  49 markets | kelly 0.80
#   SOL NO  1-5¢:  100.0% WR | $2.46/mkt |  31 markets | kelly 1.0
#   SOL NO  6-10¢:  92.6% WR | $1.82/mkt |  54 markets | kelly 0.92
#   SOL NO 11-15¢:  85.7% WR | $1.67/mkt |  49 markets | kelly 0.84
#   SOL NO 16-20¢: 100.0% WR | $3.40/mkt |  10 markets | kelly 1.0
#   XRP NO  1-5¢:  100.0% WR | $2.73/mkt |  20 markets | kelly 1.0
#   XRP NO  6-10¢:  85.0% WR | $2.31/mkt |  40 markets | kelly 0.84
#   XRP NO 11-15¢:  70.0% WR | $2.23/mkt |  20 markets | kelly 0.67
#   XRP NO 16-20¢:  91.7% WR | $2.92/mkt |  12 markets | kelly 0.89
#
# SECONDARY EDGE (smaller sample, keep conservative sizing):
#   BTC NO 21-30¢:  73.7% WR | $2.61/mkt |  19 markets
#   BTC NO 31-50¢:  90.9% WR | $3.00/mkt |  21 markets
#   ETH NO 21-30¢:  65.2% WR | $1.05/mkt |  23 markets
#   ETH NO 31-40¢:  61.9% WR | $0.81/mkt |  21 markets
#   XRP NO 31-40¢:  73.9% WR | $1.41/mkt |  23 markets
#
# KILLED (every bucket above 50¢, all YES below 50¢):
#   Net/mkt ranges from -$1.40 to +$0.42 — not worth blowup risk
#   One loss at 85¢ (7ct = $5.95) wipes 15-60 wins in low-price buckets

# ======================== IRON RULES ========================
# RULE 1: NO contracts only — YES entries have no confirmed edge
# RULE 2: Max price 50¢ — everything above is killed
# RULE 3: Single entry per market — TRADED_TICKERS + API position check
# RULE 4: Data-driven contract sizing per asset per price bucket
# RULE 5: Never exceed MAX_COST_PER_MARKET = $6.25
# =============================================================

# -------------- BANKROLL ALLOCATION ----------------------------
TOTAL_BANKROLL = 100.00
BOT_ALLOCATION = 25.00
MIN_BOT_BALANCE = 5.00
MAX_RISK_PER_TRADE_PCT = 0.25  # $6.25 max per trade

# -------------- PRICE CAPS — NO CONTRACTS ONLY, MAX 50¢ -------
# Above 50¢: confirmed negative or marginal after correct P&L math
# YES entries: no edge below 50¢ (60% WR, $0.37/mkt on 20 clean markets)
MAX_ENTRY_PRICE_CENTS = {
    'BTC': 50,
    'ETH': 50,
    'SOL': 20,   # SOL data shows no edge above 20¢ (no 21-50¢ sample)
    'XRP': 40,   # XRP 41-50¢ has insufficient sample, XRP 31-40¢ confirmed
}

# Force NO direction only — YES has no edge at any price in clean data
ALLOWED_DIRECTIONS = ['no']   # bots must check this before any entry

# ETH high-price: DISABLED — $0.02-0.36/mkt not worth blowup risk
ETH_HIGH_PRICE_ALLOWED = False
ETH_HIGH_PRICE_MIN = 81

# Hard cost ceiling
MAX_COST_PER_MARKET = 6.25

# -------------- CONTRACT SIZING — HALF KELLY, CONSERVATIVE ----
# Sizing = half kelly of max contracts allowed by $6.25 cost cap
# Half kelly chosen over full kelly: protects against variance in smaller buckets
# 21-50¢ buckets: capped at 5 contracts until sample size grows past 100 markets
#
# At 5¢: max by cost = 125ct → half kelly ~30ct (capped at 30)
# At 15¢: max by cost = 41ct → half kelly ~20-23ct
# At 45¢: max by cost = 13ct → capped at 5ct (small sample)

CONTRACT_SIZING = {
    # BTC — strongest edge in system
    # 1-5¢:   100% WR, kelly 1.0  → 30ct max
    # 6-10¢:   97.9% WR, kelly 0.98 → 30ct
    # 11-15¢:  96.7% WR, kelly 0.96 → 23ct (half kelly of 41ct max)
    # 16-20¢:  85.3% WR, kelly 0.83 → 14ct (half kelly, CI ±12% so conservative)
    # 21-30¢:  73.7% WR, kelly 0.72 → 5ct (only 19 markets, small sample)
    # 31-50¢:  90.9% WR, kelly 0.90 → 5ct (21 markets, small sample)
    'BTC': {
        (1,  5):  30,   # 100% WR  | 30ct @ avg 3¢  = $0.90 risk
        (6,  10): 30,   # 97.9% WR | 30ct @ avg 8¢  = $2.40 risk
        (11, 15): 23,   # 96.7% WR | 23ct @ avg 13¢ = $2.99 risk
        (16, 20): 14,   # 85.3% WR | 14ct @ avg 18¢ = $2.52 risk
        (21, 30):  5,   # 73.7% WR |  5ct @ avg 25¢ = $1.25 risk  (small sample)
        (31, 50):  5,   # 90.9% WR |  5ct @ avg 38¢ = $1.90 risk  (small sample)
    },

    # ETH — solid edge in 1-20¢, thinner above
    # 1-5¢:   94.7% WR, kelly 0.94 → 30ct
    # 6-10¢:  96.7% WR, kelly 0.97 → 30ct
    # 11-15¢: 90.2% WR, kelly 0.89 → 20ct
    # 16-20¢: 83.7% WR, kelly 0.80 → 14ct
    # 21-30¢: 65.2% WR, kelly 0.56 →  5ct (23 markets)
    # 31-40¢: 61.9% WR, kelly 0.41 →  5ct (21 markets)
    'ETH': {
        (1,  5):  30,   # 94.7% WR | 30ct @ avg 3¢  = $0.90 risk
        (6,  10): 30,   # 96.7% WR | 30ct @ avg 8¢  = $2.40 risk
        (11, 15): 20,   # 90.2% WR | 20ct @ avg 13¢ = $2.60 risk
        (16, 20): 14,   # 83.7% WR | 14ct @ avg 18¢ = $2.52 risk
        (21, 30):  5,   # 65.2% WR |  5ct @ avg 25¢ = $1.25 risk  (small sample)
        (31, 40):  5,   # 61.9% WR |  5ct @ avg 35¢ = $1.75 risk  (small sample)
    },

    # SOL — strong in 1-20¢, no data above
    # 1-5¢:   100% WR, kelly 1.0  → 30ct
    # 6-10¢:   92.6% WR, kelly 0.92 → 30ct
    # 11-15¢:  85.7% WR, kelly 0.84 → 20ct
    # 16-20¢: 100% WR, kelly 1.0  → 18ct (only 10 markets — conservative despite 100%)
    'SOL': {
        (1,  5):  30,   # 100% WR  | 30ct @ avg 3¢  = $0.90 risk
        (6,  10): 30,   # 92.6% WR | 30ct @ avg 8¢  = $2.40 risk
        (11, 15): 20,   # 85.7% WR | 20ct @ avg 13¢ = $2.60 risk
        (16, 20): 10,   # 100% WR  | 10ct @ avg 18¢ = $1.80 risk  (only 10 markets)
    },

    # XRP — 1-20¢ confirmed, 31-40¢ has sample, skip 21-30¢ (only 6 markets)
    # 1-5¢:   100% WR, kelly 1.0  → 30ct
    # 6-10¢:   85.0% WR, kelly 0.84 → 30ct
    # 11-15¢:  70.0% WR, kelly 0.67 → 16ct
    # 16-20¢:  91.7% WR, kelly 0.89 → 15ct (only 12 markets — conservative)
    # 31-40¢:  73.9% WR, kelly 0.59 →  5ct (23 markets)
    'XRP': {
        (1,  5):  30,   # 100% WR  | 30ct @ avg 3¢  = $0.90 risk
        (6,  10): 30,   # 85.0% WR | 30ct @ avg 8¢  = $2.40 risk
        (11, 15): 16,   # 70.0% WR | 16ct @ avg 13¢ = $2.08 risk
        (16, 20): 15,   # 91.7% WR | 15ct @ avg 18¢ = $2.70 risk
        (31, 40):  5,   # 73.9% WR |  5ct @ avg 35¢ = $1.75 risk  (23 markets)
    },
}

# XRP: skip 21-30¢ (only 6 markets, CI ±40%) and 41-50¢ (no sample)
XRP_SKIP_RANGE = (21, 50)

# -------------- MINIMUM CONFIDENCE ----------------------------
MIN_CONFIDENCE = 0.60

# -------------- HISTORICAL ACCURACY --------------------------
# True win rates from 1,526 clean markets with correct P&L math
# Format: (win_rate, sample_size, kelly_fraction)
HISTORICAL_ACCURACY = {
    'BTC': {
        'NO_1_5':    (1.000, 109, 1.00),
        'NO_6_10':   (0.979,  97, 0.98),
        'NO_11_15':  (0.967,  92, 0.96),
        'NO_16_20':  (0.853,  34, 0.83),
        'NO_21_30':  (0.737,  19, 0.72),
        'NO_31_50':  (0.909,  21, 0.90),
    },
    'ETH': {
        'NO_1_5':    (0.947,  19, 0.94),
        'NO_6_10':   (0.967,  60, 0.97),
        'NO_11_15':  (0.902,  51, 0.89),
        'NO_16_20':  (0.837,  49, 0.80),
        'NO_21_30':  (0.652,  23, 0.56),
        'NO_31_40':  (0.619,  21, 0.41),
    },
    'SOL': {
        'NO_1_5':    (1.000,  31, 1.00),
        'NO_6_10':   (0.926,  54, 0.92),
        'NO_11_15':  (0.857,  49, 0.84),
        'NO_16_20':  (1.000,  10, 1.00),
    },
    'XRP': {
        'NO_1_5':    (1.000,  20, 1.00),
        'NO_6_10':   (0.850,  40, 0.84),
        'NO_11_15':  (0.700,  20, 0.67),
        'NO_16_20':  (0.917,  12, 0.89),
        'NO_31_40':  (0.739,  23, 0.59),
    },
}

# -------------- SETTLEMENT BIAS --------------------------------
SETTLEMENT_BIAS = {
    'BTC': {'yes': 0.532, 'no': 0.468},
    'ETH': {'yes': 0.514, 'no': 0.486},
    'SOL': {'yes': 0.469, 'no': 0.531},
    'XRP': {'yes': 0.727, 'no': 0.273},
}

# -------------- ASSET CONFIGURATION ---------------------------
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

# -------------- SESSION LIMITS --------------------------------
ENABLE_SESSION_LIMITS = True
DAILY_MAX_LOSS_PERCENT = 0.75
SESSION_CONSECUTIVE_LOSSES_LIMIT = 4
SESSION_COOLDOWN_MINUTES = 15
BALANCE_CHECK_DELAY_SECONDS = 300

# -------------- SHARED CONSTANTS ------------------------------
FEE_CENTS_PER_CONTRACT = 0
NUM_CONCURRENT_BOTS = 4

# -------------- POSTER BOT SETTINGS --------------------------
# Bot now operates as maker/poster, not taker
# Posts NO contracts immediately at market open, amends price every 30s
# Auto-expires unfilled remainder 90s before close

POSTER_START_SECONDS = 800     # Enter at 13m20s remaining (early for fill window)
POSTER_AMEND_INTERVAL = 30     # Amend price every 30 seconds
POSTER_EXPIRY_BUFFER = 90      # Auto-cancel unfilled remainder 90s before close
POSTER_PRICE_FLOOR = 10        # Minimum NO posting price in cents
POSTER_PRICE_CEILING = 50      # Maximum NO posting price in cents
POSTER_QUEUE_DISCOUNT = 2      # Post 2¢ below best NO ask to front-run queue

# NOTE: CONTRACT_SIZING table still applies
# Posting price determines which bucket → how many contracts to post
# If book NO ask is 35¢, post price = 33¢ → use 31-50¢ bucket sizing
