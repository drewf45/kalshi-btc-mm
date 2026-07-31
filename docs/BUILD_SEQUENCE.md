# BUILD CHUNK SEQUENCE — RELAY ENGINE (AMENDED, 0717 night)
## CANONICAL. Supersedes charter §14's phase sketch. Folds in: all five lens walls, the parts/knobs,
## the legacy-mining finds, and every 0717 ruling (waterfall dead · capital compounds · Telegram
## cash-confirm · all-lanes-always-looking · sizing born-in · size-buys-discipline · wrong-way tick).
## Claude Code builds chunk by chunk; NO chunk starts until the previous chunk's VERIFY gate is green.
## Work orders are cut from chunks at Drew's word — this list is the order they'll be cut in.

---

# CHUNK 0 — RULINGS GATE (Drew; nothing builds before 0.1)

0.1 **Migration mode** [OPEN — the one blocking ruling]: (a) parallel-build, relay runs as PAPER SHADOW
    against the live feed with ZERO capital while current Kal keeps trading, cutover when relay's F/H8
    decisions match live for N clean days (lens-recommended, N proposed = 5); or (b) refactor-in-place.
    Single-writer rule either way: exactly one process writes the surface DB (Render-overlap lesson).
0.2 Ratifications #2–#6 (Loss-Asymmetry · transport-walls · streams-name-readers ·
    P22-custodies-new-lanes-only · fast-selection/slow-calibration). Charter assumes them; boot tape
    prints the assumption until ruled.
0.3 Confirm safe defaults or set numbers: $-at-risk/settlement-event (default 3× one-lot max loss) ·
    D band floor (default 60¢ pending Chunk 2 data) · depth_fraction (default 25%).
**VERIFY:** rulings logged in the daily log; charter's §13/§14 updated to match.

# CHUNK 1 — INSTRUMENT FIRST (ships to the LIVE tree regardless of migration mode)

1.1 **Ledger EPOCH 2** (waterfall killed): remove scrape/owed/mark_paid entirely (grep-proof); retire
    `/paid`; tradeable = live balance; %-of-book caps snapshotted at boot + confirmed movements only,
    deploy-day byte-identical percentages; honest lifetime backfill with EPOCH BOUNDARY defined (which
    eras/healed rows count, stated in the first EPOCH 2 pack header); invariant re-pointed at
    book == balance ± in-flight; drawdown rail keeps absolute-floor semantics until book > [threshold].
1.2 **Cash-movement protocol**: quiescence window (compute delta only with zero in-flight orders and
    zero unsettled fills; else defer a cycle); negative → halt entries + Telegram prompt WITH BREAKDOWN
    (last reconcile, fills since, fees since, delta); `/confirm_cash` re-baselines + surface row +
    CASH_MOVEMENTS table; `/deny_cash` or 30-min silence → FATAL, stay stopped; positive → confirm
    without halt; monthly true-up line in the pack vs Kalshi statement (confirmation is consent, the
    pack is verification).
1.3 **Book-snapshot recorder** live NOW (reader named: replay harness + shadow verdicts). Every day
    unrecorded is fuel lost.
1.4 **Attribution spec** (document + schema): per-fill (lane, market, side, cost-basis, size-tier);
    stacked-market settlement split by fill mapping; custodian exits write to the OPENING lane's cells;
    per-lane scoreboard + census provenance; acceptance test = a stacked market reconstructible
    per-lane from surface rows alone.
1.5 **Surface aggregation policy**: one terminal row per lane per market per window + interim counters;
    full rows on state changes only. Regret accounting reads terminal rows.
**VERIFY:** grep zero waterfall refs · synthetic ±$5 delta test passes both branches (confirm resumes /
deny stays FATAL) · first EPOCH 2 pack renders honest lifetime · recorder writing · attribution schema
reviewed by a lens spot-check.

# CHUNK 2 — LEGACY MINING + DECOMPOSITIONS (analysis only; runs PARALLEL to Chunk 1; no engine changes)

2.1 **Poster-price extraction** (the Feb 21 design, finally run): from the 1,500+ market taker CSV —
    what prices posters offered per asset, fill timing vs window clock, poster-activity density by
    price cell. Feeds D's baton placement + MM's future quoting.
2.2 **Forgone-clip decomposition** by price cell × time band → rules the D band (unlocks 0.3's 50¢ vs
    60¢) and names which lane catches the caution ledger's cost.
2.3 **Caution-ledger cost-vs-savings decomposition** (the standing divergence).
2.4 **Feb 27 whipsaw tape → Lane P negative spec**: the flicker/stale-frame sequences P must NOT
    trigger on, as a test set.
2.5 **Session/liquidity priors across BTC/ETH/SOL/XRP** from the legacy CSV (multi-series
    prep, zero commitment).
2.6 (Curiosity, recorder-era only): the 1¢-side win pattern — flagged for future study, no build.
**VERIFY:** four analysis artifacts banked; D-band ruling unblocked.

# CHUNK 3 — SPINE (per migration mode; new tree if 0.1=a)

3.1 **Feed/book**: WebSocket-first (orderbook_delta/ticker/trade/fill/positions/lifecycle), venue error
    codes as fail-loud paths, staleness stamps; **degrade ladder**: WS loss → ALL-lane entry halt →
    custodian on REST (risk-reduction always allowed) → snapshot resync → resume after N clean frames.
    REST poll = shadow/exploration only.
3.2 **Gateway**: single-threaded submit path (stated in code); ONE canonical YES-terms conversion
    function; walls in order — band + lane-scoped single entry · **NET-RISK cross-lane cap ≤3 +
    $-at-risk cap per settlement event, risk-reducing orders EXEMPT and classified at the canonical
    layer** · REJECT_WRONG_WAY_TICK (sign from the bids-only reciprocal book) · %-of-book budgets ·
    sizing-tier authorization · fee tripwire incl. maker-designation list. Exits/cancels skip walls.
3.3 **Fresh stacking code read FIRST**: every consumer of `is_traded` + all per-market accounting under
    multi-lane; converts "one check" from ESTIMATE to proven scope. THEN delete the main-loop cross-lane
    skip + fix the lane-misattribution marker.
3.4 **Port F and H8**: byte-identical = LADDER + GATES ONLY, proven by golden-tape regression (recorded
    markets replayed through old and new F → identical proposals/passes required). Cap plumbing and
    custodian passthrough are declared changes with their own verifies.
**VERIFY:** golden-tape regression green · wrong-way-tick and net-risk walls exercised synthetically ·
WS degrade ladder fire-drilled (kill the socket mid-shadow, watch the ladder walk).

# CHUNK 4 — CUSTODIAN (P22)

4.1 Recover DUMP from `claude/trading-contract-math-2biFE` + legacy bot.py; generalize to per-lane cut
    params scaled by size tier (size buys discipline).
4.2 **Baton lifecycle**: cancel-resting-exit THEN cut, atomically verified, fail-loud on partial;
    venue `decrease/reduce_by` available for partial reductions.
4.3 **Kill semantics**: lane-kill halts ENTRIES only; open positions custodied to conclusion, loudly.
4.4 Lane F in PASSTHROUGH (cut-disabled-except-catastrophic) — custodian earns F later per §12.
**VERIFY:** synthetic cut with live resting exit → atomic cancel-then-cut on tape · custodian exit
attributes to opening lane's cell (attribution acceptance test re-run).

# CHUNK 5 — DEMO VERIFICATION (gate for ANY live stacking)

5.1 Place the relay's worst-case order set on the demo account; read **`Get Total Resting Order
    Value`**; measure collateral treatment of stacked resting orders.
5.2 Test the **`netting_enabled` first-order lock**: does the first lane's order characteristics affect
    subsequent lanes' collateral on the same event?
5.3 Confirm per-leg fee treatment matches the designation-list read.
**VERIFY:** a one-page demo report with numbers; any surprise = wall, back to Drew.

# CHUNK 6 — SHADOW LANES (paper, feed-parity enforced)

6.1 **Lane D** (band per 2.2 ruling): cheap-side entry + resting recovery baton, delta-gated, sizing
    ladder born-in, custodied from birth.
6.2 **Lane P**: spike-fade with external priors (0.03–0.10 displacement) + the 2.4 negative spec as its
    unit tests.
6.3 **Feed-parity law in force**: promotion-grade shadow evidence ONLY on the live transport (WS);
    poll-shadow rows tagged EXPLORATION, excluded from promotion cells. Measure P/D overlap.
**VERIFY:** both lanes producing census-clean attributed shadow rows; zero live capital touched.

# CHUNK 7 — CUTOVER & PROMOTIONS (per 0.1 mode)

7.1 If parallel: relay's F/H8 shadow decisions match live Kal N clean days → cutover, old engine
    retired, single writer preserved through the switch.
7.2 Promotions strictly per §12: Wilson cells clear at the lower bound + census-clean + custodian
    exercised + Drew's gate. Demote instantly forever.
**VERIFY:** first live stacked market reconstructed per-lane from surface rows alone — the charter's
acceptance test, on real tape.

# CHUNK 8 — LATER TIER (each gated on relay-green + its own ruling)

MM evaluation path (inventory skew + naked-leg reqs) · multi-series pool (BTC+ETH first, using 2.5
priors) · bundle-arb detector (netting-aware per Chunk 5) · P16-B ladder promotions per gate march ·
P13 Phase A full · P14 locked-average H8 qualifier · custodian-earns-F ruling.

---

## THE CRITICAL PATH, ONE LINE
**0.1 → 1.1–1.5 (∥ 2) → 3.3 → 3 → 4 → 5 → 6 → 7.** Chunks 1 and 2 can start the moment 0.1 is ruled —
and 1.3 (recorder) plus all of Chunk 2 could start tonight, since they touch no order path at all.

## SEAL
Every lens wall now lives inside a numbered chunk with a verify gate; every 0717 ruling is structural;
the past's unmined data is Chunk 2 instead of a regret. One ruling (0.1) holds the door.
