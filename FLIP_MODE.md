# FLIP MODE (WO-LANE-FLIP)

The flip doctrine as a **mode inside the proven `k_worker` engine** — the bot that placed
hundreds of live fills. This branch now carries that engine at root; the earlier from-
scratch build is archived under `flipdesk_reference/` as doctrine + test reference and
ships nothing.

## What it is
`KW_MODE=FLIP` makes `engine.run_market_cycle` dispatch to `k_worker/flip_mode.py`
instead of the directional lanes (which stay intact and **dormant**). It is **off by
default** — with `KW_MODE` unset the engine behaves exactly as before.

Per window:
1. **Arm at the open** — `discover_market` returns the active window; arm while
   `secs_to_expiry` is within the first `FLIP_ENTRY_SEC` (120s) of the 900s window.
2. **Quote both sides** — join the best YES bid and best NO bid (subpenny `v2_price_str`),
   two `place_order_maker` calls, each `expiration_ts = close_ts − 90`. WALL: combined
   ≤ `FLIP_LINE` (99¢).
3. **Flip** — on both fills, post exits at entry+`FLIP_X` (4¢) via the complement rule
   (`k_worker/flip_math.py`, pinned): exit held YES@q → buy NO@100−q; exit held NO@q →
   buy YES@100−q. Kalshi nets complements to flat. A lone leg is keepable only ≤
   `FLIP_LONE_MAX` (49¢).
4. **T-90 sweep** — cancel unfilled exits, re-join at the current touch; an intact bundle
   rides its guaranteed floor, a lone leg (≤49¢) rides settlement as the accepted 1-lot
   residual. **No taker path is added.**
5. **Discipline** — one entry phase per window (`gateway.mark_traded`); two consecutive
   stopped windows (`−FLIP_STOP_CENTS`) pause the engine (reuses `discipline` halt).

Fills are recorded as the engine's own `SurfaceRow` ENTER rows, so the **existing**
reconcile / settlement / treasury / review-pack machinery books P&L — no new accounting.

## Reused verbatim (not touched)
`place_order_maker`, `fetch_orderbook`, `get_fills`/`parse_fill`, `cancel_all_for_market`,
discovery, settlement backfill, boot reconcile + orphan handling, balance re-read, delta-
table self-provisioning, notify, review pack, discipline halt. The engine diff is tiny: a
lazy dispatch in `run_market_cycle` + a boot echo. New files: `flip_mode.py`, `flip_math.py`.

## Env
`KW_MODE=FLIP` · `FLIP_ENTRY_SEC=120` · `FLIP_LINE=99` · `FLIP_LONE_MAX=49` · `FLIP_X=4` ·
`FLIP_FLAT_AT=90` · `FLIP_STOP_CENTS=25` · `FLIP_PAUSE_AFTER_STOPS=2` (all overridable).

## Before live — required validation (I could not do these here)
- The offline sandbox has a broken `cryptography` binding, so `flip_mode` and every
  `k_worker` test that imports the client run in **CI/deploy only**. Here, only the
  crypto-free `tests/test_flip_math.py` (the pinned complement math) executes — 8/8 green.
- **Run `K_WORKER_MODE=observe KW_MODE=FLIP` first**: the mode logs "would quote …" per
  window and places nothing — confirm the arm timing and bundle math against real books.
- Then a **1-lot live window** with eyes on the Kalshi UI: two entry bids at the join,
  expiring T-90 (the visible proof). Confirm a fill flows through reconcile/settlement and
  the review pack books it before trusting unattended runs.

## Acceptance (from the order)
`tests/test_flip_math.py` pins the four intents' complement numbers; `tests/test_flip_mode.py`
covers the one-shot flag + stop counter (CI). The judging number is **time to first fill** —
quotes at ~50¢ fill orders of magnitude easier than quotes at 98¢.
