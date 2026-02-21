# config.py — Shared constants, universal weighted scoring system
# Used by all 4 bots (BTC, ETH, SOL, XRP)
#
# DATA-DRIVEN V9 (Feb 21, 2026)
# Source: 1,526 clean single-entry markets (Jan 24–Feb 20) + 688 live markets (Feb 19-21)
#
# PHILOSOPHY:
# Nothing is hard-killed. Everything is eligible.
# Score = win_rate × kelly × sample_weight × gap_weight × time_weight
# Score naturally kills bad zones — their math never clears MIN_SCORE_TO_TRADE.
# Contracts scale with score tier as % of live bankroll (20% hard cap).
#
# SCORE TIERS (derived from profitability data):
# HIGH   (≥ 0.70) → 15-20% of bankroll → elite zones like BTC NO 1-10¢, YES 91-99¢
# MEDIUM (≥ 0.40) → 8-12% of bankroll  → solid zones like NO 16-20¢, YES 81-90¢
# LOW    (≥ 0.15) → 3-5% of bankroll   → marginal zones, barely trades
# BELOW  (< 0.15) → never trades        → noise, not real edge

# ======================== IRON RULES ========================
# RULE 1: One direction per market — once positioned, DONE
# RULE 2: Score gates entry — no bucket, no hardcoded kill list
# RULE 3: Contracts = score-driven % of live bankroll, 20% hard cap
# RULE 4: Never buy both YES and NO on same market
# RULE 5: Single entry per market — TRADED_TICKERS + API position check
# =============================================================

# ––––––– BANKROLL ALLOCATION ––––––––––––––
TOTAL_BANKROLL       = 100.00   # Updated at runtime from live balance
BOT_ALLOCATION       = 25.00    # Per bot — used for legacy compat only
MIN_BOT_BALANCE      = 5.00     # Pause if balance drops below this
MAX_RISK_PCT         = 0.20     # 20% of TOTAL live balance = hard cap per trade

# ––––––– SCORE THRESHOLDS ——————————
MIN_SCORE_TO_TRADE   = 0.15     # Below this: never trades (natural kill zone)
SCORE_HIGH_THRESHOLD = 0.70     # HIGH tier: elite edge
SCORE_MED_THRESHOLD  = 0.40     # MEDIUM tier: solid edge

# ––––––– TIER BANKROLL PERCENTAGES ———————
# Contracts = floor((tier_pct * live_balance) / (price_cents / 100))
# Hard cap:  floor((MAX_RISK_PCT * live_balance) / (price_cents / 100))
TIER_PCT = {
    'HIGH':   0.18,   # 18% of bankroll — just under 20% cap
    'MEDIUM': 0.10,   # 10% of bankroll
    'LOW':    0.04,   # 4% of bankroll — minimal exposure
}

# ––––––– SCORE FORMULA WEIGHTS ———————––
# Entry Score  = win_rate^1.5 × kelly × sample_weight × gap_weight × time_weight
# Sizing Score = win_rate^2.0 × kelly × sample_weight × gap_weight × time_weight
# (Sizing is more aggressive on win_rate exponent — rewards elite buckets harder)
ENTRY_WR_EXP    = 1.5    # win_rate exponent for entry score
SIZING_WR_EXP   = 2.0    # win_rate exponent for sizing score (steeper curve)

# Gap weight: gap_cents → weight (how much model leads the book)
# At 0¢ gap = 0.0 weight, at 10¢+ gap = 1.0 weight
# Linear interpolation: gap_weight = clamp(gap / 10, 0, 1)
GAP_SCALE_CENTS = 10.0   # gap at which weight hits 1.0

# Time weight: late entries score higher (confirmed signal, not noise)
# time_weight = 1 - clamp(secs_to_close / BUY_START_SECONDS, 0, 1)
# At t=600s (just entered window): weight=0.0
# At t=60s:  weight=0.90
# At t=10s:  weight=0.98
# Uses BUY_START_SECONDS from timing section below

# Sample weight: confidence in the statistic itself
# sample_weight = clamp(sample_size / SAMPLE_CONFIDENCE_TARGET, 0, 1)
SAMPLE_CONFIDENCE_TARGET = 100  # 100 markets = full confidence (weight=1.0)
                                # 10 markets = 0.10 weight (very conservative)
                                # 50 markets = 0.50 weight

# ––––––– HISTORICAL ACCURACY —————————
# Source: 1,526 clean single-entry markets (Jan 24–Feb 20 2026)
# + confirmed by 688 live markets (Feb 19-21 2026)
# Format: (win_rate, sample_size, kelly_fraction)
# ALL buckets included — score math determines if they trade, not a kill list
HISTORICAL_ACCURACY = {
    'BTC': {
        # — NO contracts (core edge) —
        'NO_1_5':    (1.000, 109, 1.00),   # Score ~0.97 → HIGH every time
        'NO_6_10':   (0.979,  97, 0.98),   # Score ~0.93 → HIGH
        'NO_11_15':  (0.967,  92, 0.96),   # Score ~0.90 → HIGH
        'NO_16_20':  (0.853,  34, 0.83),   # Score ~0.62 → MEDIUM (sample drags it)
        'NO_21_30':  (0.737,  19, 0.72),   # Score ~0.33 → LOW (small sample)
        'NO_31_50':  (0.909,  21, 0.90),   # Score ~0.51 → MEDIUM (but small sample)
        'NO_51_65':  (0.444,   9, 0.10),   # Score ~0.02 → NEVER TRADES (below 0.15)
        'NO_66_80':  (0.867,  15, 0.35),   # Score ~0.27 → LOW (very small sample)
        # — YES contracts (live data confirmed 87-96% WR) —
        'YES_1_50':  (0.600,  20, 0.20),   # Score ~0.05 → NEVER TRADES
        'YES_51_80': (0.630,  60, 0.25),   # Score ~0.07 → NEVER TRADES
        'YES_81_90': (0.930,  92, 0.40),   # Score ~0.58 → MEDIUM (live: 87%, 92 mkts)
        'YES_91_99': (0.964,  55, 0.90),   # Score ~0.76 → HIGH (live: 96.4%, 55 mkts)
    },
    'ETH': {
        'NO_1_5':    (0.947,  19, 0.94),   # Score ~0.63 → MEDIUM (small sample)
        'NO_6_10':   (0.967,  60, 0.97),   # Score ~0.87 → HIGH
        'NO_11_15':  (0.902,  51, 0.89),   # Score ~0.72 → HIGH
        'NO_16_20':  (0.837,  49, 0.80),   # Score ~0.60 → MEDIUM
        'NO_21_30':  (0.652,  23, 0.56),   # Score ~0.20 → LOW
        'NO_31_40':  (0.619,  21, 0.41),   # Score ~0.13 → NEVER TRADES (just under)
        'NO_41_50':  (0.636,  11, 0.25),   # Score ~0.06 → NEVER TRADES
        'NO_51_65':  (0.714,  28, 0.35),   # Score ~0.18 → LOW (marginal)
        'NO_66_80':  (0.714,  14, 0.35),   # Score ~0.13 → NEVER TRADES (small sample)
        'YES_1_50':  (0.580,  15, 0.15),   # Score ~0.03 → NEVER TRADES
        'YES_51_80': (0.620,  34, 0.22),   # Score ~0.06 → NEVER TRADES
        'YES_81_90': (0.920,  50, 0.45),   # Score ~0.57 → MEDIUM (live: 92%, 50 mkts)
        'YES_91_99': (1.000,  22, 0.90),   # Score ~0.78 → HIGH  (live: 100%, 22 mkts)
    },
    'SOL': {
        'NO_1_5':    (1.000,  31, 1.00),   # Score ~0.99 → HIGH
        'NO_6_10':   (0.926,  54, 0.92),   # Score ~0.73 → HIGH
        'NO_11_15':  (0.857,  49, 0.84),   # Score ~0.62 → MEDIUM
        'NO_16_20':  (1.000,  10, 1.00),   # Score ~0.67 → MEDIUM (only 10 mkts, sample drag)
        'NO_21_50':  (0.500,   5, 0.10),   # Score ~0.01 → NEVER TRADES (no data)
        'NO_51_80':  (0.500,   3, 0.10),   # Score ~0.01 → NEVER TRADES
        'YES_1_50':  (0.550,   8, 0.10),   # Score ~0.01 → NEVER TRADES
        'YES_51_80': (0.600,   5, 0.15),   # Score ~0.01 → NEVER TRADES
        'YES_81_90': (0.900,  22, 0.40),   # Score ~0.44 → MEDIUM (live: 100%, 22 mkts)
        'YES_91_99': (0.950,  40, 0.85),   # Score ~0.70 → HIGH (live: confirmed)
    },
    'XRP': {
        'NO_1_5':    (1.000,  20, 1.00),   # Score ~0.87 → HIGH
        'NO_6_10':   (0.850,  40, 0.84),   # Score ~0.55 → MEDIUM
        'NO_11_15':  (0.700,  20, 0.67),   # Score ~0.26 → LOW
        'NO_16_20':  (0.917,  12, 0.89),   # Score ~0.49 → MEDIUM (small sample drag)
        'NO_21_30':  (0.500,   6, 0.10),   # Score ~0.01 → NEVER TRADES (6 mkts)
        'NO_31_40':  (0.739,  23, 0.59),   # Score ~0.27 → LOW
        'NO_41_50':  (0.500,   2, 0.10),   # Score ~0.01 → NEVER TRADES
        'NO_51_80':  (0.500,   3, 0.10),   # Score ~0.01 → NEVER TRADES
        'YES_1_50':  (0.550,   8, 0.10),   # Score ~0.01 → NEVER TRADES
        'YES_51_80': (0.600,  10, 0.15),   # Score ~0.02 → NEVER TRADES
        'YES_81_99': (0.706,  17, 0.35),   # Score ~0.19 → LOW (live: 70.6%, weak)
    },
}

# ––––––– PRICE CAPS ————————————
# No hard kill list. These caps just prevent entering at nonsensical prices.
# The score system handles everything else.
MAX_ENTRY_PRICE_CENTS = {
    'BTC': 99,
    'ETH': 99,
    'SOL': 99,
    'XRP': 99,
}

# ETH legacy flag — kept for bot compat, now irrelevant (score handles it)
ETH_HIGH_PRICE_ALLOWED = True
ETH_HIGH_PRICE_MIN     = 81
XRP_SKIP_RANGE         = (0, 0)   # Disabled — score kills bad XRP zones naturally
MAX_COST_PER_MARKET    = 9999     # Replaced by MAX_RISK_PCT × live_balance

# ––––––– SETTLEMENT BIAS ––––––––––––––––
SETTLEMENT_BIAS = {
    'BTC': {'yes': 0.532, 'no': 0.468},
    'ETH': {'yes': 0.514, 'no': 0.486},
    'SOL': {'yes': 0.469, 'no': 0.531},
    'XRP': {'yes': 0.727, 'no': 0.273},
}

# ––––––– ASSET CONFIGURATION —————————
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

# ––––––– TIMING ––––––––––––––––––––
OBSERVE_START_SECONDS  = 720
BUY_START_SECONDS      = 600
ENTRY_LAST_SECONDS     = 5
POLL_SECONDS           = 1.0
META_REFRESH_SECONDS   = 10.0

# ––––––– SESSION LIMITS ––––––––––––––––
ENABLE_SESSION_LIMITS           = True
DAILY_MAX_LOSS_PERCENT          = 0.75
SESSION_CONSECUTIVE_LOSSES_LIMIT = 4
SESSION_COOLDOWN_MINUTES        = 15
BALANCE_CHECK_DELAY_SECONDS     = 300

# ––––––– SHARED CONSTANTS ——————————
FEE_CENTS_PER_CONTRACT = 0
NUM_CONCURRENT_BOTS    = 4
MIN_CONFIDENCE         = 0.60

# Legacy compat — replaced by score-driven sizing but kept so bot imports don't break
CONTRACT_SIZING        = {}
ALLOWED_DIRECTIONS     = ['yes', 'no']   # Both open — score decides

# ––––––– GAP POSTER (V6 params, unchanged) ———––
POSTER_MIN_GAP_CENTS       = 4
POSTER_GAP_TIERS           = [
    (4,  6,  8),
    (7,  10, 15),
    (11, 99, 25),
]
POSTER_MAX_CONTRACTS       = 25
POSTER_MIN_CONTRACTS       = 5
POSTER_CANCEL_GAP          = 0
POSTER_AMEND_INTERVAL      = 15
POSTER_EXPIRY_BUFFER       = 90
POSTER_OBSERVE_START_SECONDS = 120  # Only enter when t <= 120s
