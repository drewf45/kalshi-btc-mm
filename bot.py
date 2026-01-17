import os
import time
from datetime import datetime, timezone

# =====================
# CONFIG (from Render env vars)
# =====================
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "20"))
MARKET_SYMBOL = os.getenv("MARKET_SYMBOL", "BTC-15MIN")

# =====================
# HELPERS
# =====================
def utc_day():
    return datetime.now(timezone.utc).date()

# =====================
# MAIN BOT LOOP
# =====================
def main():
    print("=== Kalshi BTC Market Maker Bot ===")
    print(f"Market: {MARKET_SYMBOL}")
    print(f"Max Daily Loss: ${MAX_DAILY_LOSS}")
    print("Bot started successfully")

    current_day = utc_day()
    daily_pnl = 0.0

    while True:
        try:
            # Reset daily PnL at UTC midnight
            if utc_day() != current_day:
                print("🔄 New UTC day — resetting PnL")
                current_day = utc_day()
                daily_pnl = 0.0

            # Hard stop if loss limit hit
            if daily_pnl <= -MAX_DAILY_LOSS:
                print("🛑 DAILY LOSS LIMIT HIT — sleeping until reset")
                time.sleep(60)
                continue

            # ===== PLACEHOLDER FOR TRADING LOGIC =====
            # Next step we will:
            # - read BTC 15m orderbook
            # - place maker bid + ask
            # - track fills
            # - update daily_pnl
            # ========================================

            print(f"[{datetime.utcnow()}] Cycle running | PnL=${daily_pnl:.2f}")

            # Run every 15 minutes
            time.sleep(15 * 60)

        except Exception as e:
            print("❌ ERROR:", e)
            time.sleep(5)

if __name__ == "__main__":
    main()