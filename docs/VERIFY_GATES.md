# VERIFY GATES — WO-2026-07-17-RELAY §D — status report (2026-07-17)

Read-rule (§F) in force: every claim cites files/lines and carries TRUE / FALSE / UNPROVEN.
Test evidence: `python -m pytest tests/ -q` → **58 passed** at the commit carrying this report.

---

## Gate 1 — Canon committed · monolith quarantined · README states the mode

- **TRUE** — `docs/WORK_ORDER.md` and `docs/BUILD_SEQUENCE.md` committed VERBATIM, first
  commit (`6b56b5e`).
- **UNPROVEN (missing input)** — `docs/CHARTER.md` and `docs/LENS_READ_CHARTER.md` are LOUD
  PLACEHOLDERS: the verbatim texts of KAL_RELAY_ENGINE_FULL_CHARTER_0717 and
  SEVEN_LENS_CHARTER_READ_0717 were not among the delivered inputs. Standing input request to
  Drew recorded in each placeholder. The work order wins conflicts either way (§A2).
- **TRUE** — monolith quarantined: `reference/legacy_dump_bot.py` exists, `bot.py` gone, never
  imported (`tests/test_epoch2_grep.py:34-41`, green).
- **TRUE** — `README.md:3-16` states: parallel paper-shadow build, live Kal elsewhere, trades
  nothing until cutover ruling. `render.yaml:7` starts the shadow runner, not the monolith.

## Gate 2 — Boot tape on paper · synthetic ±$5 both branches

- **TRUE** — boot tape prints EPOCH 2 header, all five ratification assumptions, the three
  DREW-DEFAULT constants, SHADOW mode, recorder confirmation (`relay_engine/boot.py:14-45`;
  asserted line-by-line in `tests/test_boot_tape.py:10-33`; transcript below).
- **TRUE** — synthetic −$5: halt + breakdown prompt → `/confirm_cash` re-baselines, writes
  CASH_MOVEMENTS, resumes (`relay_engine/ledger.py:195-256`;
  `tests/test_cash_protocol.py::test_negative_five_confirm_branch_resumes`).
- **TRUE** — synthetic −$5: `/deny_cash` → FATAL, stays stopped, confirm afterwards refused
  (`tests/test_cash_protocol.py::test_negative_five_deny_branch_stays_fatal`); 30-min silence
  → FATAL (`::test_thirty_minute_silence_goes_fatal`); +$5 confirms without halt
  (`::test_positive_five_confirms_without_halt`); quiescence defers (`::test_quiescence_defers…`).
- **TRUE** — grep-proof: zero prior-era accounting tokens in `relay_engine/`
  (`tests/test_epoch2_grep.py:20-31`); `/paid` never existed (`relay_engine/ops.py:24`).

## Gate 3 — WS degrade ladder fire-drill

- **TRUE** — socket killed mid-shadow → ALL-lane entry halt + custodian on REST → snapshot
  resync → resume only at the 10th clean frame, 9 is not enough; transitions on tape
  (`relay_engine/feed.py:34-79`;
  `tests/test_degrade_ladder.py::test_fire_drill_full_ladder_walk`).
- **TRUE** — risk reduction allowed in every ladder state (`relay_engine/feed.py:76-79`);
  venue error frames FATAL (`relay_engine/feed.py:110-111`); garbled frame = transport damage;
  staleness stamps live (`relay_engine/book.py:73-74`).

## Gate 4 — Wrong-way-tick + net-risk walls, synthetically

- **TRUE** — REJECT_WRONG_WAY_TICK: buy improving down and zero-tick rejected, sell improving
  up rejected, sign derived per order (`relay_engine/gateway.py:248-257`;
  `tests/test_walls.py::test_wrong_way_tick_*`).
- **TRUE** — NET-RISK cross-lane cap ≤3 per settlement event; separate events separate caps
  (`relay_engine/gateway.py:233-246`; `tests/test_walls.py::test_net_risk_cross_lane_cap`).
- **TRUE** — the named case: a resting EXIT does NOT consume cap
  (`relay_engine/gateway.py:219-231` counts ENTRY orders only;
  `tests/test_walls.py::test_resting_exit_does_not_consume_cap`).
- **TRUE** — $-at-risk cap enforces independently (`::test_at_risk_cap_trips_independently`);
  note: at the DREW-DEFAULT constants (3 lots × 99¢) the count cap and $ cap coincide exactly.
- **TRUE** — risk-reducing orders exempt, classified at the canonical layer
  (`relay_engine/gateway.py:127-135`; `::test_risk_reducing_orders_exempt_from_walls`);
  band + single-entry, %-of-book, sizing-tier, fee tripwire (multiplier AND designation list),
  post-only-everywhere, single-threaded path FATAL, governor printed=enforced with carried
  counts — all exercised in `tests/test_walls.py` (green).

## Gate 5 — Golden-tape regression for F and H8

- **UNPROVEN — BLOCKED ON B1.** No F/H8 logic exists in this tree (§A5 honored: nothing
  ported from memory or the monolith). Lanes registered as first-class PASS
  (`relay_engine/lanes.py:37-45`). Unblocks when Drew supplies the live k_worker zip; the
  recorder is already banking replay material for the golden tape from first boot.

## Gate 6 — Attribution acceptance test on a synthetic stacked market

- **TRUE** — three lanes filled on one market (F yes-hold, D no-hold, P custodian-exited),
  two lanes passing; settlement split by fill mapping; per-lane P&L reconstructed FROM SURFACE
  ROWS ALONE and matches; custodian exit attributed to the OPENING lane
  (`relay_engine/surface.py:72-113,116-129`;
  `tests/test_attribution.py::test_stacked_market_reconstructs_per_lane_from_surface_rows_alone`).
- **TRUE** — one terminal row per lane/market/window enforced (different second terminal =
  FATAL); interim rows on state change only, counters otherwise (`tests/test_attribution.py`).

## Gate 7 — 24h shadow against the live WS feed

- **TRUE (machinery, synthetic)** — every-lane-every-market census clean over repeated
  cycles (5 lanes × 2 markets = exactly 10 terminal rows), zero orders placed, daily pack
  renders per-lane sections (`tests/test_shadow_cycle.py`, green).
- **UNPROVEN (environment-blocked)** — the 24-hour run against the LIVE feed cannot execute
  from this build sandbox: the network policy denies `api.elections.kalshi.com:443`
  (proxy CONNECT → HTTP 403, verified 2026-07-17). The runner is deploy-ready
  (`render.yaml` → `python -m relay_engine.shadow_runner`, RUN_MODE=SHADOW pinned).
  Needs: deploy to Render (creds per B3, read-only) or any environment whose policy
  allows the Kalshi host, then 24h of tape.

## Chunk 4 verify (custodian, §C.5)

- **TRUE** — synthetic cut with live resting exit → atomic cancel-THEN-cut on tape; FATAL on
  uncancelable partial, position not silently dropped (`relay_engine/custodian.py:139-166`;
  `tests/test_custodian.py::test_baton_lifecycle_cancel_then_cut`, `::test_baton_fail_loud…`).
- **TRUE** — kill semantics: entries halted, open position still custodied and cuttable
  (`::test_kill_semantics_entries_only`); Lane F passthrough catastrophic-only
  (`relay_engine/custodian.py:117-118`); size buys discipline (`::test_size_buys_discipline`);
  custodian exit attributes to opening lane (`::test_cut_attributes_to_opening_lane`).
- **NOTE** — the branch `claude/trading-contract-math-2biFE` named in C.5 is not present in
  this repo's remotes (only `main` and this build branch exist). DUMP mechanics were recovered
  from `reference/legacy_dump_bot.py` alone (trigger family at its lines 155-162); its
  truncated tail means the mechanics came from the config surface, not a function body.
  Parameters were NOT borrowed (§F).

## §E — Parallel analysis track

- **UNPROVEN — BLOCKED ON B2.** `docs/analysis/` empty until the legacy CSVs arrive.

---

## Boot tape transcript (on paper, this commit)

```
==================================================================
RELAY ENGINE BOOT — EPOCH 2 (born; tradeable = live balance)
==================================================================
RUN_MODE=SHADOW  [PAPER SHADOW: zero capital, zero orders]
RATIFICATIONS ASSUMED (unruled — printed every boot until Drew rules):
  ASSUMED: Loss-Asymmetry
  ASSUMED: transport-walls
  ASSUMED: streams-name-readers
  ASSUMED: P22-custodies-new-lanes-only
  ASSUMED: fast-selection/slow-calibration
DREW-DEFAULT constants in force:
  DREW-DEFAULT at_risk_cap_per_settlement_event = 3x one-lot max loss = 297c
  DREW-DEFAULT lane_d_floor = 60c (pending Chunk 2 data)
  DREW-DEFAULT depth_fraction = 25%
RATE GOVERNOR: bucket=10 tokens, refill=2.0/s (printed number IS the enforced number)
BOOT CAPS SNAPSHOT: book=0c order_budget=0c (10% of book, byte-identical until a confirmed movement)
RECORDER: ON — book snapshots -> book_snapshots (reader: replay harness + shadow verdicts) [awaiting first frame]
DB: relay_shadow.db (single-writer: this engine's own database)
==================================================================
```

## HARD STOP honored

Chunks 5 (demo verification), 6 (shadow-lane promotion), 7 (cutover) NOT built — separate
orders at Drew's word. Standing input requests: **B1** (k_worker zip → unblocks gate 5,
delta table, F/H8), **B2** (legacy CSVs → unblocks §E), **charter + lens verbatim texts**
(→ closes the gate-1 placeholder).
