# FLIP MODE (WO-LANE-FLIP)

The flip doctrine as a **mode inside the proven `k_worker` engine** — the bot that placed
hundreds of live fills. This branch now carries that engine at root; the earlier from-
scratch build is archived under `flipdesk_reference/` as doctrine + test reference and
ships nothing.

## What it is
On this branch `engine.run_market_cycle` dispatches to `k_worker/flip_mode.py` instead of
the directional lanes (which stay intact and **dormant**). Flip is **on by default** —
`KW_MODE` unset ⇒ FLIP. `KW_MODE=OFF` is the one-variable **kill switch**: the engine
stays up and the flip lane goes quiet.

Per window:
1. **Arm at the open** — `discover_market` returns the active window; arm while
   `secs_to_expiry` is within the first `FLIP_ENTRY_SEC` (300s) of the 900s window.
2. **Opportunistic entry (WO-4)** — WATCH the early window rather than quoting both at the
   bell. Each side posts its bid (subpenny `v2_price_str`, `expiration_ts = close_ts − 90`)
   the **first time its join ≤ `FLIP_SIDE_MAX` (49¢)** — at most once per side, no re-peg.
   The **combined wall** holds: a second side posts only if `resting_first + join ≤
   FLIP_LINE` (99¢). No side ≤ max by phase end → `SAT (no side ≤ 49 in 300s)`.
3. **Netted / flip** — if **both** legs fill the bundle NETS FLAT and the exchange banks
   `+(100−cost)¢` (rung A). A **lone** leg is flipped at entry+`FLIP_X` (4¢) via the
   complement rule (`k_worker/flip_math.py`, pinned): exit held YES@q → buy NO@100−q; exit
   held NO@q → buy YES@100−q. A lone leg is keepable only ≤ `FLIP_LONE_MAX` (49¢).
4. **T-90 sweep** — cancel an unfilled exit, re-join at the current touch (rejoin
   `expiration_ts = close_ts − 2`, F-3, so it lives to the bell). An intact bundle rides
   its guaranteed floor; a lone leg (≤49¢) rides settlement as the accepted 1-lot residual.
   **No taker path is added.**
5. **Discipline** — one entry phase per window (`gateway.mark_traded`); two consecutive
   stopped windows (`−FLIP_STOP_CENTS`) pause the engine (reuses `discipline` halt).

Every filled leg — entry AND rung-B exit — is recorded as the engine's own `SurfaceRow`
ENTER row (`env=live-traded`), so the **existing** reconcile / settlement / treasury /
review-pack machinery books P&L — no new accounting.

## Accountable (WO-LANE-FLIP-3)
- **Two-rung ladder (§2, law).** One shot = one pre-committed sequence. **Rung A** = the
  entry pair; when both legs fill it nets flat and banks `+(100−cost)¢`. **Rung B** = the
  exit pair at entry+X, itself a second netted capture on a double fill (its exit IS rung B
  for a lone leg). **No rung C, ever.** The tape reads `rungA +5¢ · rungB +6¢ → +11¢`.
- **Rung-B guard (WO-4).** The double-fill exit pair costs `200 − cost − 2·FLIP_X`; the
  cheaper (juicier) rung A is, the more that pair overpays. Rung B posts only if that pair
  stays ≤ `FLIP_LINE` (at X=4: `cost ≥ 93`); below it, rung A is banked and the bundle rides
  its floor (`rung B skipped`). A lone-leg exit is a single complement buy and is exempt.
- **No-inventory proof (§1).** At every window DONE the broker — not the code — proves
  flatness: net 0 ⇒ `| flat ✓ (broker)`; a lone ride is the accepted ±1 residual; any other
  net pages Drew `🚨 INVENTORY`, is handed to the boot reconcile/orphan machinery, and an
  immediate complement-join (maker touch, exp close−2) tries to flatten it now.
- **Tagged windows (§3).** One immutable `flip_windows` row per window records entry/exit
  prices, both joins + book-sum at each post, per-rung captures, realized ¢, open-leg mtm,
  entry spreads, timings, broker-flat, and an `outcome_tag` ∈ {NETTED_2R, NETTED_1R,
  FLOOR_RIDE, LONE_FLIP, LONE_SALVAGE, LONE_FLATTEN, LONE_RIDE, LONE_DECLINED, SAT_*,
  STOPPED} — the desk's own crossing study for Saturday tuning.
- **Hourly pack v2 (§4, `flip_pack.py`).** Account in real **dollars** (proven `get_balance`)
  with Δ-vs-midnight, a per-window table, and a day rollup whose `Δ$` is **explained**:
  settled P&L + fees, any unexplained residual ≥ 2¢ printed 🔴 and paged.

## Overnight discipline (WO-6)
- **Salvage-aware stop.** A window's `realized` includes leg outcomes at DONE, not just flip
  captures: a salvaged/flattened leg counts `(100 − complement_fill) − entry`, a lone leg
  riding to settlement counts a provisional `−entry`. The two-stop pause is now fed the truth
  (`→ −33¢ realized (incl. salvage)` → `STOPPED` → streak → halt), so a losing salvage night
  actually arms it. Netted rungs and lone flips are unchanged.
- **Salvage/flatten story rows.** Every salvage (T-90 rejoin) and flatten (§1) fill books its
  own ENTER row (`FLIP_SALVAGE` / `FLIP_FLATTEN`, order-id lineage) so settlement resolves the
  pair and Δ$-explained stays ✓ on honest salvage nights.
- **Siren polarity.** 🚨 means danger only. A mismatch in the SAFE direction — flatter than
  modeled (the salvage filled, net 0 when a ±1 ride was expected) — renders as
  `ℹ️ position reconciled: flatter than modeled … salvage filled`, never the alarm. Only a
  risk-direction net (more exposure, or the wrong sign) pages Drew.

## Reused verbatim (not touched)
`place_order_maker`, `fetch_orderbook`, `get_fills`/`parse_fill`, `cancel_all_for_market`,
discovery, settlement backfill, boot reconcile + orphan handling, balance re-read, delta-
table self-provisioning, notify, review pack, discipline halt. The engine diff is tiny: a
lazy dispatch in `run_market_cycle` + a boot echo. New files: `flip_mode.py`, `flip_math.py`.

## Env
`KW_MODE` (unset/anything ⇒ FLIP; `OFF` ⇒ kill switch) · `FLIP_ENTRY_SEC=300` ·
`FLIP_SIDE_MAX=49` · `FLIP_REQUIRE_PAIR=1` (pair-or-nothing) · `FLIP_LINE=99` ·
`FLIP_LONE_MAX=49` · `FLIP_X=4` · `FLIP_FLAT_AT=90` · `FLIP_STOP_CENTS=25` ·
`FLIP_PAUSE_AFTER_STOPS=2` · `KW_SERIES_ALLOWLIST=KXBTC15M` (the only series the bot books) ·
`KW_CLEAR_HALT` (set `=1` for a one-boot halt clear, then remove) — all overridable.

## Resume posture (WO-RESUME)
- **Pair-or-nothing (`FLIP_REQUIRE_PAIR=1`, default).** At entry-phase end with exactly ONE
  filled leg, the leg is not kept — it is flattened at once via the complement touch (maker,
  exp close−2; one re-join if unfilled after ~30s), tagged `LONE_DECLINED`, realized = the
  flatten outcome (≈ breakeven). This closes the repeating loss shape (cheap lone fill into a
  decided book) while leaving the profit shape (netted bundles) untouched. Lone-keeping
  (rides/salvage) returns only with `FLIP_REQUIRE_PAIR=0` — a morning call made from the tags.
- **Boot-clear (`KW_CLEAR_HALT=1`).** Clears discipline halt / tail-loss kill + the flip stop
  streak ONCE at boot (`halt cleared by env (one-boot)`), no shell. A persisted marker stops a
  crash-loop from self-clearing; removing the env re-arms it.

## Guardrails (WO-5)
- **ENTER-row lineage.** `SurfaceRow` carries `order_id`; every flip leg's ledger row is
  written with its order id (the fix for the `unexpected keyword 'order_id'` warning) so
  reconcile and the Δ$-explained pack stay whole.
- **Series allowlist (§2).** The boot orphan scan adopts positions only in
  `KW_SERIES_ALLOWLIST`; anything else (e.g. a personal WNBA bet in the shared account) is
  logged once as `PERSONAL (ignored)` — no row, no sweep, no settlement, no treasury impact.
- **Treasury wipe (§3).** A one-time boot migration zeroes accrued/lifetime owed and sets
  book = account per Drew's 0715 ruling (`treasury wiped … owed → $0.00`); the waterfall
  plumbing stays dormant, no scrape until re-ruled.

## Before live — required validation (I could not do these here)
- The offline sandbox has a broken `cryptography` binding, so `flip_mode` and every
  `k_worker` test that imports the client run in **CI/deploy only**. Here, the crypto-free
  `tests/test_flip_math.py` (pinned complement math + mode switch) and `tests/test_flip_pack.py`
  (dollars rendering + unexplained-residual alert) execute green; the full-cycle
  `tests/test_flip_mode.py` (two-rung ladder, flat proof, rung tags, rejoin expiry) runs in CI.
- **Run `K_WORKER_MODE=observe KW_MODE=FLIP` first**: the mode logs "would quote …" per
  window and places nothing — confirm the arm timing and bundle math against real books.
- Then a **1-lot live window** with eyes on the Kalshi UI: two entry bids at the join,
  expiring T-90 (the visible proof). Confirm a fill flows through reconcile/settlement and
  the review pack books it before trusting unattended runs.

## Acceptance (from the order)
Each window's tape now reads a complete sentence:
`🔁 W20:15 — 47+48=95 | rungA +5¢ · rungB +6¢ → +11¢ [NETTED_2R] | flat ✓ (broker) | DONE`
and the hourly pack shows the account in real dollars with its change explained to the cent.
Anything held, anything unexplained, anything above two rungs pages Drew by name.

Tests: `test_flip_math.py` pins the four complement intents + the mode kill switch;
`test_flip_pack.py` pins dollars rendering + the unexplained-residual alert; `test_flip_mode.py`
(CI) covers the two-rung ladder, no-rung-C, the §1 flat proof + inventory flatten, the lone
paths, and the close−2 rejoin expiry. The judging number remains **time to first fill** —
quotes at ~50¢ fill orders of magnitude easier than quotes at 98¢.
