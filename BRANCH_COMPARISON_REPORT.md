# Branch Comparison Report: All Bots vs XRP (Reference)

**Date:** 2026-02-17
**Reference branch:** XRP (`claude/multi-market-bot-trading-xCwMJ` — `bot_xrp.py`)

## Branch Mapping

| Bot | Branch | Main file | Lines |
|-----|--------|-----------|-------|
| XRP (ref) | `claude/multi-market-bot-trading-xCwMJ` | `bot_xrp.py` | 4044 |
| BTC | `claude/trading-contract-math-2biFE` | `bot.py` | 4563 |
| SOL | `claude/create-sol-bot-branch-l3uQG` | `sol_bot.py` | 4424 |
| ETH | `claude/kalshi-bot-markets-9rDjQ` | `eth_bot.py` | 4140 |

---

## 1. BTC vs XRP

### BTC has that XRP doesn't:
- **Trailing stop** — activates at +$0.15 unrealized P&L, trails $0.10 below peak
- **Early exit on underwater positions** — exits if down >30% of max loss within first 5 min of holding
- **Rolling 2-hour drawdown breaker** — -$2.00 in 2hr window triggers 30min pause
- **Market conviction override** — when book shows >=85% on a side, overrides model blend to trust market entirely (XRP explicitly disables this)
- **YES daytime restriction** — blocks YES trades during 8am-8pm EST
- **YES position size halving** — `YES_POSITION_SIZE_MULT = 0.50`
- **4-condition ultra-strict YES gate** — prob + move + time + price (XRP's is 2-condition: prob + move)
- **`YES_REQUIRE_BOTH_TRENDS`** — both 60min and 30min trends must align for YES
- **`YES_MIN_BTC_DISTANCE = $200`** — YES only if BTC is $200+ from boundary
- **NO_ONLY mode** — env variable to restrict to NO-side only
- **`MAX_NO_CONTRACTS = 5`** — hard cap on NO contracts
- **`NO_MIN_CONFIDENCE = 0.80`** — separate confidence floor for NO
- **Nuke prevention** — caps position so one loss doesn't erase 5+ wins
- **`MIN_EXPECTED_PAYOUT_USD = 0.10`** — rejects trades with projected win < $0.10
- **`validate_position_size()` function** — universal pre-order gate
- **`SOFT_STOP_LOSS_USD = $0.30`** — fires before the $0.40 hard stop (dual-method: uses max of prob and bid for loss estimate)
- **`SpotTrend.reset()` method** — re-observation after cooldown
- **`YesHourlyTracker`** and **`FillRateTracker`** as separate classes

### XRP has that BTC doesn't:
- **Streak circuit breaker** — single loss >$0.50 pauses 30min; 2 losses in 60min pauses 60min
- **Profit lock tiers** — at +$2 session P&L: 50% size; at +$3: 25% size
- **Two-tier dump dollar caps** — soft $0.50 (limit sell) + hard $0.75 (market sell)
- **Universal `STOP_LOSS_USD = $0.40` using actual bid prices** — fires FIRST, before all other dump logic
- **Scalp entry protection in dump** — entries at >=95c never get hold-to-settlement suppression
- **`MAX_LOSS_AT_EXPIRY_USD = $1.00`** — pre-trade gate on max possible settlement loss
- **`XRP_SIZE_BOOST_MULT = 1.15`** — 15% position size boost
- **NO side relaxed gate** — allows NO entries at 80% prob (vs 85% general) when price >= 85c
- **Settlement lock edge minimum** — requires 1% edge even in settlement lock (BTC skips edge check)
- **`POST_ONLY = True`** — maker orders (BTC uses `False`)

### Key constant divergences:

| Constant | XRP | BTC |
|----------|-----|-----|
| `PROB_MIN` | 0.85 | 0.83 |
| `EDGE_MIN` | 0.02 | 0.03 |
| `MODEL_BLEND_ALPHA` | 0.35 | 0.20 |
| `MAX_CONTRACTS` | 3 | 100 |
| `PROB_EARLY_MIN` | 0.85 | 0.90 |
| `SETTLEMENT_LOCK_MIN_PROB` | 0.92 | 0.85 |
| `SETTLEMENT_LOCK_MAX_PRICE` | 97c | 99c |
| `SESSION_CONSECUTIVE_LOSSES_LIMIT` | 5 | 4 |
| `POST_ONLY` | True | False |
| High-certainty price formula | `prob*100 - 1` (positive edge) | `prob*100 + 1` (spread slack) |

---

## 2. SOL vs XRP

### SOL has that XRP doesn't:
- **Cold-start protection** — graduated ramp: 25% size for first 2 trades, 50% for trades 3-5, 100% from trade 6+
- **Session drawdown breaker** — -$3.00 in rolling 2hr triggers 60min pause
- **Independent stop-loss monitor** — runs separately from dump function, uses actual bid prices
- **Trailing stop** — activates at +$0.10, trails $0.06 below peak
- **Overnight restrictions** — blocks YES trades 8pm-8am EST, max 3 trades/hr overnight
- **Time-of-day sizing** — daytime 1.25x, overnight 0.75x
- **Overnight soft stop** — $0.25 (tighter than daytime $0.30)
- **`MIN_PAYOFF_CENTS = 8`** — blocks any trade with payoff < 8c/contract (prevents penny wins)
- **Market conviction override** — trusts market at >=85% (XRP disables this)
- **Post-cooldown edge boost** — requires 2% extra edge on first trade after loss streak
- **NO-side tiebreaker** — when both sides qualify, prefers NO
- **Side bias system** — `NO_SIZING_MULTIPLIER = 1.20`, `YES_SIZING_MULTIPLIER = 0.60`
- **Loss analysis on cooldown** — logs breakdown of recent losses by side

### XRP has that SOL doesn't:
- **Universal `STOP_LOSS_USD = $0.40` using actual bid prices in dump function** (SOL's dump function doesn't accept bid prices at all)
- **Two-tier dump dollar caps** — soft $0.50 + hard $0.75 (SOL has single $0.40)
- **Scalp entry protection in dump** — 95c+ entries never suppressed
- **Streak circuit breaker** — per-loss-amount pause logic
- **Profit lock tiers** — progressive size reduction as session P&L grows
- **`XRP_SIZE_BOOST_MULT = 1.15`** — 15% position boost
- **Multi-bot cash division** — `NUM_ACTIVE_BOTS = 4` divides available cash
- **`TrackedPosition` dataclass + multi-market position tracking** (SOL uses flat scalar fields, single-position only)
- **`MAX_LOSS_AT_EXPIRY_USD = $1.00`** pre-trade gate
- **Settlement lock edge minimum** — requires 1% edge even in settlement lock
- **`POST_ONLY = True`** maker orders

### Key constant divergences:

| Constant | XRP | SOL |
|----------|-----|-----|
| `PROB_MIN` | 0.85 | 0.78 |
| `EDGE_MIN` | 0.02 | 0.03 |
| `MODEL_BLEND_ALPHA` | 0.35 | 0.20 |
| `KELLY_MULTIPLIER` | 0.25 | 0.20 |
| `MAX_ENTRY_PRICE_CENTS` | 96 | 92 |
| `MAX_CONTRACTS` | 3 | 100 |
| `DUMP_REVERSAL_THRESHOLD` | 0.06 | 0.08 |
| `YES_GATE prob threshold` | 0.88 | 0.93 |
| `SETTLEMENT_LOCK_MIN_PROB` | 0.92 | 0.85 |
| `POST_ONLY` | True | False |
| High-certainty price formula | `prob*100 - 1` (positive edge) | `prob*100 + 1` (spread slack) |

---

## 3. ETH vs XRP

### ETH has that XRP doesn't:
- **`FLIP_AFTER_DUMP = False`** — flip is DISABLED (XRP has it enabled). Comment says "flip is -EV with low prob thresholds (60% @ 99c = -39c/ct)"
- **Rolling drawdown breaker** — -$3.00 in 2hr triggers 30min pause
- **4-condition `yes_ultra_gate()`** — prob 95% + move 70% + time 300s + price 93c
- **YES conviction multiplier** — YES needs 150% of normal conviction distance
- **YES Kelly reduction** — YES gets 50% of normal Kelly sizing
- **`NO_ONLY` mode** — env variable
- **`NO_PROB_ADJUSTMENT = -0.05`** — NO threshold 5 points lower for more volume
- **Whipsaw/momentum filter** — `SpotMomentum` class skips entries when ETH moves too fast
- **`MIN_TAKE_PROFIT_USD = $0.08`** — prevents dumping profitable positions with tiny gains (>2min remaining)
- **"Hold winners" logic** — reversal bail only fires on LOSING positions, not profitable ones
- **`_realistic_exit_cents()` helper** — uses worst-of model and bid for exit estimate
- **Market conviction override** — trusts market at >=85% (XRP disables this)
- **Market-based cooldown** — skips 1 market instead of time-based pause (preserves trend data)
- **Scalp loss cap** — adds absolute $0.40 cap on top of fraction-based limit

### XRP has that ETH doesn't:
- **`FLIP_AFTER_DUMP = True`** — flip is ENABLED with full flip logic
- **`PositionTracker` class** — full internal position isolation with crash recovery from API positions
- **Streak circuit breaker** — per-loss-amount pause logic
- **Profit lock tiers** — at +$2: 50% size, at +$3: 25% size
- **Two-tier dump dollar caps** — soft $0.50 + hard $0.75 (ETH has single $0.40)
- **Universal `STOP_LOSS_USD = $0.40` using actual bid prices** (ETH has no separate bid-based stop)
- **Scalp entry protection** — 95c+ entries never get hold suppression
- **`XRP_SIZE_BOOST_MULT = 1.15`** — 15% position boost
- **Multi-bot cash division** — `NUM_ACTIVE_BOTS = 4`
- **`MAX_LOSS_AT_EXPIRY_USD = $1.00`** pre-trade gate
- **NO side relaxed gate** — 80% prob / 85c price
- **Settlement lock edge minimum** — requires 1% edge
- **`POST_ONLY = True`** maker orders

### Key constant divergences:

| Constant | XRP | ETH |
|----------|-----|-----|
| `BUY_START_SECONDS` | 600 (10min) | 420 (7min) |
| `PROB_MIN` | 0.85 | 0.80 |
| `MAX_ENTRY_PRICE_CENTS` | 96 | 93 |
| `MODEL_BLEND_ALPHA` | 0.35 | 0.20 |
| `MAX_CONTRACTS` | 3 | 100 |
| `MAX_SETTLEMENT_LOSS_FRACTION` | 0.08 | 0.02 |
| `DUMP_REVERSAL_THRESHOLD` | 0.06 | 0.10 |
| `DUMP_REVERSAL_THRESHOLD_PROFIT` | 0.04 | 0.12 |
| `DUMP_CATASTROPHIC_LOSS_CENTS` | 20 | 15 |
| `CONFIRMATION_HOLD_SECONDS` | 15 | 8 |
| `FLIP_AFTER_DUMP` | True | **False** |
| `POST_ONLY` | True | False |
| High-certainty price formula | `prob*100 - 1` (positive edge) | `prob*100 + 1` (spread slack) |

---

## 4. Cross-Cutting Observations

### Features ONLY XRP has (all 3 others are missing):
1. **`STOP_LOSS_USD` using actual bid prices as the FIRST check** in the dump function
2. **Two-tier soft/hard dump caps** ($0.50/$0.75)
3. **Scalp entry protection** in late-hold suppression
4. **Profit lock tiers** (reduce size when winning)
5. **Streak circuit breaker** (per-loss-amount pause)
6. **`PositionTracker` with `TrackedPosition` dataclass** and multi-market support
7. **`XRP_SIZE_BOOST_MULT = 1.15`**
8. **`MAX_LOSS_AT_EXPIRY_USD = $1.00`** pre-trade gate
9. **Settlement lock edge minimum** (1%)
10. **`POST_ONLY = True`** (maker orders)
11. **High-certainty price formula `prob*100 - 1`** (guarantees positive edge — all others use `+1` which allows slightly negative EV)

### Features the other 3 have that XRP is missing:
1. **Market conviction override** (all 3 have it, XRP explicitly disabled)
2. **Rolling drawdown breaker** (BTC has 2hr/$2; SOL has 2hr/$3; ETH has 2hr/$3)

### Naming artifacts / bugs noticed:
- SOL's safety function is named `_btc_is_safe()` with `DUMP_BTC_SAFE_BUFFER_*` and `HOLD_BTC_DANGER_BUFFER` — copy-paste from BTC, never renamed
- ETH's safety function is also named `_btc_is_safe()` with `DUMP_BTC_SAFE_BUFFER_*` — same issue

---

## 5. Dump/Flip Logic Summary

| Feature | XRP | BTC | SOL | ETH |
|---------|-----|-----|-----|-----|
| Flip after dump | YES | YES | YES | **NO** |
| Two-tier dump caps | $0.50/$0.75 | No (single $0.40) | No (single $0.40) | No (single $0.40) |
| Universal bid-based stop | $0.40 (first check) | $0.40 (dual-method) | Separate monitor | No |
| Scalp entry bypass | YES | No | No | No |
| Early exit underwater | No | YES (30%/5min) | No | No |
| Trailing stop | No | YES ($0.15/$0.10) | YES ($0.10/$0.06) | No |
| Hold winners logic | No | No | No | YES |
| Min take profit | No | No | No | YES ($0.08) |

## 6. Safety Features Summary

| Feature | XRP | BTC | SOL | ETH |
|---------|-----|-----|-----|-----|
| Streak circuit breaker | YES | No | No | No |
| Profit lock tiers | YES ($2/$3) | No | No | No |
| Rolling drawdown breaker | No | YES (2hr/$2) | YES (2hr/$3) | YES (2hr/$3) |
| Cold-start ramp | No | No | YES (25/50/100%) | No |
| Overnight restrictions | No | No | YES | No |
| YES daytime restriction | No | YES (8am-8pm) | No | No |
| Nuke prevention | No | YES (5 wins) | No | No |
| Market conviction override | **DISABLED** | YES | YES | YES |
| Whipsaw/momentum filter | No | No | No | YES |
| Max loss at expiry gate | YES ($1.00) | No | No | No |
| Settlement lock edge min | YES (1%) | No | No | No |
| Post-cooldown edge boost | No | No | YES (2%) | No |
| Market-based cooldown | No | No | No | YES (skip 1 mkt) |

## 7. Entry Logic Summary

| Constant | XRP | BTC | SOL | ETH |
|----------|-----|-----|-----|-----|
| `PROB_MIN` | 0.85 | 0.83 | 0.78 | 0.80 |
| `EDGE_MIN` | 0.02 | 0.03 | 0.03 | 0.03 |
| `MODEL_BLEND_ALPHA` | 0.35 | 0.20 | 0.20 | 0.20 |
| `KELLY_MULTIPLIER` | 0.25 | 0.25 | 0.20 | 0.25 |
| `MAX_ENTRY_PRICE_CENTS` | 96 | 96 | 92 | 93 |
| `MAX_CONTRACTS` | 3 | 100 | 100 | 100 |
| `POST_ONLY` | True | False | False | False |
| `BUY_START_SECONDS` | 600 | 600 | 600 | 420 |
| `SETTLEMENT_LOCK_MIN_PROB` | 0.92 | 0.85 | 0.85 | 0.85 |
| HC price formula | prob-1 | prob+1 | prob+1 | prob+1 |
