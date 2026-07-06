# K-Worker Certification Checklist

Every box requires a live-log citation (timestamp + line) from the first deployed day.
Boxes marked PENDING-TAPE await that first day's data.

## Tier 0 — Non-negotiable walls (gateway.py)
- [ ] KXBTC15M tickers only — PENDING-TAPE
- [ ] Favorite side by cost: 95-99c cost band only — PENDING-TAPE
- [ ] 90-94c and below REJECT (shadow only) — PENDING-TAPE
- [ ] Flat 1 contract — PENDING-TAPE
- [ ] One entry per ticker, verified against exchange position state — PENDING-TAPE
- [ ] Never both sides of one ticker — PENDING-TAPE
- [ ] Maker (post_only) only — no taker path exists — PENDING-TAPE
- [ ] Live balance re-read inside submit — PENDING-TAPE
- [ ] Per-hour exposure cap $3.00 — PENDING-TAPE

## Tier 1 — Engine logic (engine.py)
- [ ] Final 180s entry window — PENDING-TAPE
- [ ] Rest post-only limit at favorite's touch — PENDING-TAPE
- [ ] No repricing in 99c band — PENDING-TAPE
- [ ] Max one reprice in 95-98c bands — PENDING-TAPE
- [ ] Unfilled at T-10s -> cancel, log NO_FILL — PENDING-TAPE
- [ ] No chase, no scorer, no probability model — PENDING-TAPE

## Tier 2 — Discipline (discipline.py)
- [ ] Tail-loss kill: 3 losses in 60 min -> halt + ALERT — PENDING-TAPE
- [ ] Drawdown rail: balance < $5.00 -> halt — PENDING-TAPE
- [ ] Operator reset via `python -m k_worker.reset` — PENDING-TAPE
- [ ] Discipline state persists across restarts — PENDING-TAPE

## Spine — Single path to exchange
- [ ] `place_order_maker` only called from gateway.py (submit + reprice) — PENDING-TAPE
- [ ] Reprice goes through gateway.reprice() — PENDING-TAPE

## Phrasing Law (store.py)
- [ ] Every row has side, cost_per_contract_cents, yes_quote_cents, breakeven_pct — PENDING-TAPE
- [ ] Fill cost correctly resolved for YES and NO sides — PENDING-TAPE
- [ ] Phrasing-law assert fires on out-of-band fill cost — PENDING-TAPE

## Data integrity
- [ ] Append-only SQLite store on persistent disk — PENDING-TAPE
- [ ] env field: live-traded or live-observed — PENDING-TAPE
- [ ] Shadow rows for every skip — PENDING-TAPE
- [ ] Gateway state (traded tickers, hourly exposure) persists — PENDING-TAPE
- [ ] Spread (yes_ask, no_ask, spread_cents) captured per row — PENDING-TAPE

## Monitoring
- [ ] Telegram notifications for: boot, fill, settlement, alerts — PENDING-TAPE
- [ ] Scoreboard with Wilson bounds per cost band — PENDING-TAPE
- [ ] Measured friction per band (not a constant) — PENDING-TAPE
- [ ] External watcher: heartbeat silent >5 min -> ALERT — PENDING-TAPE
- [ ] Project kill detection: all bands locked -> thesis dead — PENDING-TAPE

## Observe mode
- [ ] K_WORKER_MODE=observe logs rows, places zero orders — PENDING-TAPE
- [ ] Boot banner includes mode — PENDING-TAPE

## Environment + API
- [ ] All secrets from env vars, never hardcoded — PENDING-TAPE
- [ ] KALSHI_ENV demo/live with live confirmation gate — PENDING-TAPE
- [ ] Clock skew check at boot (Date header, >2s = fatal) — PENDING-TAPE
- [ ] API base: external-api.kalshi.com (prod), external-api.demo.kalshi.co (demo) — PENDING-TAPE
- [ ] PSS salt_length=DIGEST_LENGTH per current Kalshi docs — PENDING-TAPE
- [ ] Exponential backoff with jitter on 429/5xx — PENDING-TAPE
- [ ] REST_PRICE_GONE inserts a SKIP row — PENDING-TAPE
