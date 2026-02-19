# config.py — Shared constants, historical accuracy tables, entry price caps
# Used by all 4 bots (BTC, ETH, SOL, XRP)
#
# DATA-DRIVEN REDESIGN (Feb 2026)
# Analysis of 1,749 markets showed:
#   - 95% of profit comes from buying cheap contracts (1-20¢)
#   - Expensive entries (81-99¢) are net negative even at 90% accuracy
#   - NO side wins more often across all assets (settlement bias)

# ======================== IRON RULES ========================
# RULE 1: One direction per market — once positioned, DONE
# RULE 2: Risk-based position sizing (max $3 risk per market, max 30 contracts)
# RULE 3: Max entry price per asset (data-driven caps below)
# RULE 4: Never buy both YES and NO on same market
# =============================================================

# -------------- MAX ENTRY PRICE PER ASSET (DATA-DRIVEN) -----
# Side-specific caps based on 135 trades of live data (Feb 16-19 2026).
# YES above 65¢: 41 trades, $0.20 total profit, 0.2x W/L — not worth it.
# NO entries: 79 trades, $119.30 total profit, up to 17x W/L.
MAX_YES_PRICE_CENTS = 65  # Hard cap for YES on ALL assets

MAX_NO_PRICE_CENTS = {
    'BTC': 50,
    'ETH': 70,   # ETH NO up to 70¢ is profitable per data
    'SOL': 50,
    'XRP': 50,
}

# Hard cost ceiling per market (belt-and-suspenders with risk sizing)
MAX_COST_PER_MARKET = 3.50  # $3 target + small rounding buffer

# -------------- RISK-BASED POSITION SIZING -------------------
# contracts = floor(MAX_RISK_PER_MARKET / entry_price_dollars)
# Capped at MAX_CONTRACTS and bankroll limit.
MAX_RISK_PER_MARKET = 3.00       # Max $3.00 risk per market
MAX_CONTRACTS = 30               # Hard cap per market
MIN_CONTRACTS = 1                # Always buy at least 1
MAX_BANKROLL_PER_TRADE = 0.50    # Never risk >50% of available balance
MIN_EV_PER_CONTRACT = 0.001      # Minimum $0.001 EV per contract

# -------------- MINIMUM CONFIDENCE (loose gate) -------------
# The price gate does the real filtering work.
# Confidence just filters out coin-flip situations.
MIN_CONFIDENCE = 0.60

# -------------- HISTORICAL ACCURACY BY PRICE BUCKET ----------
# From analysis of 1,749 markets, Feb 1-18 2026
# Format: (accuracy, sample_size, avg_net_profit_per_trade)
HISTORICAL_ACCURACY = {
    'BTC': {
        'NO_1_10':   (0.964, 278, 0.888),
        'NO_11_20':  (0.888, 187, 0.716),
        'NO_21_50':  (0.635, 52,  0.226),
        'YES_81_90': (0.813, 134, -0.093),  # NEGATIVE — avoid
        'YES_91_95': (0.931, 145, -0.022),  # NEGATIVE — avoid
        'YES_96_99': (0.961, 102, -0.029),  # NEGATIVE — avoid
    },
    'ETH': {
        'NO_1_10':   (0.940, 84,  0.851),
        'NO_11_20':  (0.861, 101, 0.688),
        'NO_21_50':  (0.571, 21,  0.279),
        'YES_81_90': (0.905, 63,  -0.001),  # Breakeven at best
        'YES_91_95': (0.833, 12,  -0.124),  # NEGATIVE
    },
    'SOL': {
        'NO_1_10':   (0.946, 147, 0.879),
        'NO_11_20':  (0.851, 67,  0.703),
        'YES_81_90': (0.950, 20,  0.045),
        'YES_91_95': (0.941, 34,  -0.005),  # Breakeven
    },
    'XRP': {
        'NO_1_10':   (0.935, 77,  0.864),
        'NO_11_20':  (0.828, 29,  0.671),
        'YES_81_90': (0.900, 20,  0.001),   # Breakeven
        'YES_91_95': (0.943, 35,  -0.001),  # Breakeven
    },
}

# -------------- SETTLEMENT BIAS ---------------------------------
# NO wins more often across all assets
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
OBSERVE_START_SECONDS = 720   # Start watching at 12 min before close
BUY_START_SECONDS = 600       # Can enter from 10 min before close
ENTRY_LAST_SECONDS = 5        # Stop entering at 5s before close
POLL_SECONDS = 1.0            # Check every second
META_REFRESH_SECONDS = 10.0   # Refresh market list every 10 seconds

# -------------- SESSION LIMITS ---------------------------------
ENABLE_SESSION_LIMITS = True
DAILY_MAX_LOSS_PERCENT = 0.75  # Stop at 75% daily loss
SESSION_CONSECUTIVE_LOSSES_LIMIT = 4
SESSION_COOLDOWN_MINUTES = 15
BALANCE_CHECK_DELAY_SECONDS = 300

# -------------- SHARED CONSTANTS --------------------------------
FEE_CENTS_PER_CONTRACT = 0
NUM_CONCURRENT_BOTS = 4
