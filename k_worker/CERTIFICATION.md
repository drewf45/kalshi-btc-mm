# K-Worker Certification Checklist

## Tier 0 — Non-negotiable walls (gateway.py)
- [x] KXBTC15M tickers only
- [x] Favorite side by cost: 95-99c cost band only
- [x] 90-94c and below REJECT (shadow only)
- [x] Flat 1 contract
- [x] One entry per ticker, verified against exchange position state
- [x] Never both sides of one ticker
- [x] Maker (post_only) only — no taker path exists
- [x] Live balance re-read inside submit
- [x] Per-hour exposure cap $3.00

## Tier 1 — Engine logic (engine.py)
- [x] Final 180s entry window
- [x] Rest post-only limit at favorite's touch
- [x] No repricing in 99c band
- [x] Max one reprice in 95-98c bands
- [x] Unfilled at T-10s → cancel, log SKIP NO_FILL
- [x] No chase, no scorer, no probability model

## Tier 2 — Discipline (discipline.py)
- [x] Tail-loss kill: 3 losses in 60 min → halt + ALERT
- [x] Drawdown rail: balance < $5.00 → halt
- [x] Operator reset via /tmp/k_worker_reset file

## Data integrity (store.py)
- [x] Append-only SQLite store
- [x] Every row has side, cost_per_contract_cents, yes_quote_cents, breakeven_pct
- [x] "price" standing alone is banned — Phrasing Law enforced
- [x] env field: live-traded or live-observed
- [x] Shadow rows for every skip

## Monitoring
- [x] Telegram notifications for: boot, fill, settlement, alerts
- [x] Scoreboard with Wilson bounds per cost band
- [x] External watcher: heartbeat silent >5 min → ALERT
- [x] Project kill detection: all bands locked → thesis dead

## Environment
- [x] All secrets from env vars, never hardcoded
- [x] KALSHI_ENV demo/live with live confirmation gate
- [x] Clock skew check at boot
