# WO-2026-07-26-S · PART 1c — THE COLD READ (the old multi-asset era vs the new doctrine)

**Status:** the rollout's Part-3 step 0, delivered before XRP's room opens (per Drew's
unlock protocol: each series gets a cold read + seven lenses + a daily-pack chapter).
**Artifact read:** the archived `kalshi-btc-mm-…-audit-bots-branch-AtDcG` (received 07-26),
two eras — the **crypto four-bot era** (`legacy/*_bot.py`) and the later **weather era**
(`d_worker/` + `k_worker/`). Read at source; every mechanism tagged **SALVAGE** (with its
modern home), **SUPERSEDED** (by which law), or **DEAD** (by which verdict).

---

## THE HEADLINE FINDING (honest divergence from the order's premise)

The order says the correlated-risk machinery is *"salvaged from the old branch."* **It is not
there to salvage.** Read at source: the cross-asset ensemble cap and the correlated-loss
detector **never existed in the crypto era.** The four production bots (`legacy/btc_bot.py`,
`bot_xrp.py`, `sol_bot.py`, `eth_bot.py`) are **byte-identical forks** (diff differs only in
`BOT_ID`/`SERIES_TICKER`/`SPOT_URL`/log strings), each a **standalone process** with its own
`KalshiClient`, `main()` loop, and health server. Each sized independently off the **entire
account balance** — `MAX_RISK_PCT = 0.20` × full `live_balance_usd` (`legacy/btc_bot.py:97,
727-728`), with no read of the others' positions, no shared ledger, no aggregate cap, no
correlated-loss logic. Four bots live ⇒ **up to ~80% of the account at risk simultaneously
with zero coordination** — the precise "blind twins" pathology §1b of this order rejects.
`NUM_CONCURRENT_BOTS = 4` (`legacy/config.py:216`) is **dead code** — the V5 bots never import
`config.py`. A search of the whole tree for `correlat|cross-asset|ensemble|simultaneous|
aggregate|portfolio` returns only unrelated hits (statistical sample-correlation comments,
the Kalshi `/portfolio` API path).

**Consequence for the build:** the correlated-tail governor (Part 2) is **built FRESH, not
ported.** This changes nothing about *what* gets built — Part 2 fully specifies it — only its
provenance. The blueprint is salvaged from a *different* era (below); the crypto era
contributes only anti-patterns to avoid.

---

## SALVAGE — with its modern home

- **The weather-era reservation ledger — `d_worker/budget.py` (the real blueprint).** A
  single-process, single-DB gate stack: **book cap** (total at-risk + reserved + cost ≤ cap,
  `budget.py:97-107`), **per-class cap** (`_class_exposure_usd`, `:109-124`), per-market lot
  cap, and a `reserve → convert → release` lifecycle (`:141-148`). The at-risk **summation is
  already written** in SQL (`dstore.py:613-640`, sum over an append-only `budget_decisions`
  table by status/`dclass`). → **Modern home: Stage 2.** The **book cap becomes the ensemble
  cap** (total simultaneous at-risk ≤ 50% of tradeable); the **per-class cap becomes the
  per-series cap**; the summation pattern becomes the one summed check above the lane walls.
  We build fresh against our own `fills`/`deployed_cents` ledger, but the shape is this.
- **The single-process + one-ledger topology.** `k_worker/__main__.py:55` — "against one
  Kalshi account + one DB simultaneously"; `start.sh` runs a single `python -m d_worker`. The
  one shared SQLite DB is *what made the aggregate cap possible.* → **Modern home: §1b, already
  our architecture** (one process, one ledger, one gateway). This cold read RATIFIES §1b's
  "one process is REQUIRED" against the evidence: the four-bot topology is why the old era
  could not sum exposure.
- **The counterparty-liquidity gate.** `legacy/bot_xrp.py:699-713` ("verify opposite side has
  liquidity before entering … order will never fill") and the cleaner modern `k_worker/
  engine.py:504-514` "R2" (reject if `book.no_bid is None` when buying YES, etc.). → **Modern
  home: Stage 3**, live in every room from window one (Part 3 item 2's thin-book guard).
- **XRP series mechanics (observed, not assumed).** Series **`KXXRP15M`** (15-min; daily is
  `KXXRPD`, not us), 1¢ integer tick (`bot_xrp.py:263,279` clamp 1..99), **maker-only**
  `post_only=True` ("protect margins on thin book", `bot_xrp.py:385`), `FEE_CENTS_PER_CONTRACT
  = 0` (`config.py:215`), 15-min **hold-to-settlement**, Coinbase XRP-USD spot **logging/soft-
  sanity only** (`bot_xrp.py:101,414`), **no TWAP anywhere**, resting orders cancelled 10s
  before close (`EXPIRY_BUFFER_SEC=10`). → **Modern home: Stage 3** XRP room config + the
  first-day mechanics watchlist (maker $0 must be *observed on the XRP tape*, per the Broker
  lens — the archive's $0 is an assumption to verify, not a fact to trust).
- **The settlement-source registry pattern — `d_worker/registry.py`** (series → NWS station /
  settlement-truth source, `:1-8`). → **Partial modern home:** our WO-P data-question registry
  already carries this shape; the per-series settlement-source idea directly informs the
  Adversary-ii guard (surv/settle lines are per-series or BLIND — never a BTC-trained table
  silently answering an XRP question).

---

## SUPERSEDED — by which law

- **Per-asset session limits** — `legacy/config.py:207-212`: `DAILY_MAX_LOSS_PERCENT=0.75`,
  `SESSION_CONSECUTIVE_LOSSES_LIMIT=4`, `SESSION_COOLDOWN_MINUTES=15`. → **Superseded by the
  money-based rate halt** (P27: `RATE_HALT_LOSSES`-of-`RATE_HALT_WINDOW`, drawdown-sized), now
  keyed **per (series, lane)** in Stage 2. Concrete threshold values kept only as sanity
  references; the calendar-day/consecutive-count *mechanism* does not return (WO-Q's verdict:
  a single loss is noise; a RUN halts).
- **The weather-era global kill — `d_worker/budget.py:151-162`** (`deny_all` → `live_halt`,
  one flag denies all future `reserve()`; watchdog auto-trips on `EVIDENCE_BROKEN`,
  `watchdog.py:71`). → **Superseded-and-generalized:** our global kill (`RUN_MODE` /
  `I_UNDERSTAND_LIVE`) already exists above all rooms; Stage 2 generalizes the *idea* of a
  scoped halt into **per-series halts** so one room parks without stopping the others.
- **The per-asset gate constant zoo** — the legacy `PROB_MIN/EDGE_MIN/MODEL_BLEND_ALPHA/
  SETTLEMENT_LOCK_*/KELLY_MULTIPLIER` divergences per asset (`BRANCH_COMPARISON_REPORT.md`),
  the YES/NO side biases, trailing stops, profit-lock tiers, market-conviction overrides. →
  **Superseded by F's proven gates** (band, confirms, ΔP, One-Shot, maker-only). Part-0 law:
  the gates carry unchanged; coverage is an ensemble property, caution is a lane property. A
  new room inherits F's doctrine, **not** a per-asset re-tuning — ETH later is the one Drew
  flagged for its own tuning, and only then.

---

## DEAD — by which verdict

- **Four separate bot processes.** → DEAD by §1b ("one process is REQUIRED, not merely
  convenient") and this cold read's headline: separate processes are why exposure could not be
  summed. Explicitly rejected: copying the repo per series.
- **`MAX_RISK_PCT=0.20` off the full balance, per uncoordinated bot** (~80% uncapped ensemble
  exposure). → DEAD by the ensemble cap (Part 2: ≤50% of tradeable, one summed check).
- **`NUM_CONCURRENT_BOTS=4` static cash division** (and the older `NUM_ACTIVE_BOTS=4` /4
  divisor the report cites). → DEAD: a static divisor is blind to which rooms are actually at
  risk this instant; the live summed cap replaces it.
- **The V5 "no cooldowns, no session pauses — never miss a market" doctrine**
  (`legacy/btc_bot.py:15`). → DEAD by the governor-is-the-halt doctrine (P27): the machine
  *does* stop, on money, per room.
- **Per-asset rolling 2-hour time-based drawdown pauses** (BTC −$2/30min, SOL −$3/60min, ETH
  −$3/30min; XRP streak/profit-lock tiers — `BRANCH_COMPARISON_REPORT.md:186,213`). → DEAD as
  a *mechanism* (wall-clock pauses, size-throttling on winning); superseded by the money rate
  halt. The per-room-halt *intent* survives; the calendar-timer implementation does not.
- **The crypto lane in the weather engine.** `d_worker/registry.py:20-25` `CRYPTO_BLACKLIST`
  (`KXBTC15M, KXETH15M, KXSOL15M, KXXRP15M`) — the one engine with a real aggregate cap
  **explicitly refused to trade these four assets.** → DEAD context, cited for honesty: the
  budget-cap prior art and the crypto assets never met in the old tree. We are the first to
  put a real ensemble cap around crypto rooms.

---

## SEVEN LENSES (XRP unlock)

- **🔧 ENGINEER (Priya):** The salvage is a *pattern* (`budget.py` gate stack + SQL at-risk
  sum), not code to lift — our ledger/`deployed_cents` already sums live exposure; the ensemble
  cap is one more summed check above the lane walls. The (series, lane) key is one refactor, not
  a fork. The delta/settle tables are BTC-trained and stay **BTC-ONLY until per-series tables
  exist** — the surv/settle line names its series or says BLIND (Adversary-ii).
- **💰 BROKER (Marta):** The archive's `FEE_CENTS_PER_CONTRACT=0` and `post_only=True` for XRP
  are *assumptions to verify on the XRP tape*, not facts. First-day mechanics watchlist confirms
  maker $0, settlement attribution, tick/strike live — pages, does not pre-block.
- **📈 TRADER:** XRP is thin (the old bot's whole posture — maker-only, counterparty gate,
  `CONFIRM_CHECKS=2` "signals shorter-lived"). Thin books mean smaller clips at fatter spreads;
  the binder logs will say whether the per-contract economics are better in-band. The
  counterparty gate is the thin-book trap refused at entry, not discovered at exit.
- **🏛 CEO (Dana):** The one aggregate cap the old tree ever had (`budget.py` book cap)
  explicitly blacklisted crypto — so the board's bounded-loss story for crypto is something we
  build, not inherit. The ensemble cap (≤50% tradeable) is that board condition, made real.
- **🧪 SCIENTIST:** The old era's per-asset records are **not evidence** — the four-bot topology
  never measured cross-asset loss correlation (it *couldn't*). The 45% ensemble headroom is a
  DREW-DEFAULT until the cross-series-loss-correlation registry question returns data; only then
  is it DERIVED. XRP starts with no inherited confidence — its own Wilson record, THIN at birth.
- **🧍 OFF-THE-STREET:** Four bots meant four things that could each quietly bet most of the
  account. One machine with rooms, one book, one /owed, one summed cap — nobody wonders which
  bot made (or lost) the money.
- **🕵 ADVERSARY:** (i) The correlated tail was **never governed** in the old era — this is the
  first time; built and MEASURED so the cap moves on evidence. (ii) A BTC-trained delta table
  silently informing XRP — the exact `CRYPTO_BLACKLIST`-adjacent trap; forbidden, surv/settle is
  per-series or BLIND. (iii) The old `MAX_RISK_PCT` off full balance is the cross-series leak the
  ensemble cap closes — additive **above** lane walls, never replacing them. (iv) Ghost-to-live
  optimism on thin books: Drew ruled no ghost — the per-series halt is the stated bound on the
  cost of learning XRP's mechanics live.

---

## VERDICT FOR THE ROLLOUT

The reusable prior art is the **weather-era budget ledger as a blueprint** and the
**counterparty-liquidity gate** and the **XRP series mechanics** (observed, not trusted).
Everything in the crypto four-bot era is an **anti-pattern**: independent processes, each
risking 20% off the full shared balance, blind to the others. The correlated-risk governor is
**new work**, built to Part-2 spec against our one book and one ledger — the house's first real
cross-room cap, on the one shock (a cross-crypto air-pocket) that can reach every room at once.
