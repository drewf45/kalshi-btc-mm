# VERIFY GATES — WO-2026-07-17-RELAY §D — status report (2026-07-17, rev 2: B1 DELIVERED)

Read-rule (§F) in force: every claim cites files/lines and carries TRUE / FALSE / UNPROVEN.
Test evidence: `python -m pytest tests/ -q` → **62 passed** at the commit carrying this report.

**Rev 2 delta:** Drew delivered B1 (two zips: the AtDcG live tree and the flipdesk build) and
granted full-repo branch access. Gate 5 is now GREEN — see the Gate 5 section and
`docs/GOLDEN_TAPE_REPORT.md`. F/H8 are ported and wired; the delta table + candle fetcher are
borrowed WHOLE.

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

## Gate 5 — Golden-tape regression for F and H8 [REV 2: B1 DELIVERED]

- **TRUE — GREEN.** `docs/GOLDEN_TAPE_REPORT.md` committed: **26 watch-ladder tapes +
  2,941 evaluate sweep cases, 0 mismatches.** The live side is the ACTUAL live code —
  `reference/live_k_worker/{engine,gateway,delta_table_loader,sessions}.py` vendored
  byte-identical from the B1 zip (SHA-256 of vendored engine.py == zip's engine.py,
  verified at vendor time), executed with I/O stubbed and clock faked. The new side is
  `relay_engine/lane_fh8.py`. Comparison key: (passed, lane, side, cost_cents,
  cost_exact, reject_code, why_tag) per frame.
- **TRUE** — byte-identical scope honored: v4 watch-confirm ladder floors 99/98/97
  (+ the 95 floor of the final-window band), confirms 9/6/3, dropout/side-flip/
  counterparty (R2) resets (`relay_engine/lane_fh8.py:404-470` vs
  `reference/live_k_worker/engine.py:35-39,455-597`); every evaluate wall and why_tag
  string (`lane_fh8.py:203-401` vs `gateway.py:199-410`); H8 delta-gate ≥99% as the
  wilson_ub ≤ 0.01 dual-gate verdict (`relay_engine/delta.py:220-245`).
- **TRUE** — harness has TEETH (mutation-tested): four injected bugs — tier-2 floor
  97→96, tier-1 confirms 6→5, H8 max_secs 60→90, F band-lo 95→94 — each turned the
  tape RED; restored code is green. The first mutation initially survived (corpus had
  no 96¢ tape); floor-edge and confirm-edge tapes were added before trusting green.
- **TRUE** — declared changes isolated with their own treatment (D1-D5, listed in
  `relay_engine/lane_fh8.py:20-40`): EPOCH 2 balance arithmetic (live treasury accrual
  stubbed to zero — the equivalence the tape pins), relay cap plumbing downstream,
  custodian passthrough, evidence-query adapter, queue instrumentation deferred to the
  live submit path.
- **TRUE** — delta table + candle fetcher borrowed WHOLE per C.3
  (`relay_engine/delta.py`, `relay_engine/delta_builder.py`, `relay_engine/sessions.py`
  from `k_worker/{delta_table_loader,delta_table_builder,sessions}.py`; wiring-only
  adaptations, noted in each header). R1 synthetic refusal and the
  floor_strike/expiration_value settlement-anchor law test-enforced
  (`tests/test_shadow_cycle.py::test_delta_module_laws`).
- **TRUE** — BUILD_SEQUENCE 3.3 read-first done, scope proven: `is_traded` has exactly
  ONE consumer in the live tree — the main-loop cross-lane skip
  (`reference/live_k_worker/`-lineage `k_worker/__main__.py:704`) — which this tree
  deletes by design (every lane evaluates every market, `tests/test_shadow_cycle.py`);
  the lane-misattribution marker is the boot-recovery cost-band lane GUESS
  (`__main__.py:713-717`) — fixed here because lane is recorded per-fill at birth and
  never inferred (`relay_engine/ledger.py` fills schema).
- **TRUE** — lanes wired end-to-end: watch ladder → confirm → relay walls → shadow
  order → surface rows, exercised in `tests/test_lane_port_wiring.py` (ladder confirms
  at tier 0 on live semantics, proposal rests as SHADOW order, H8 writes
  COST_IN_F_BAND, single-entry blocks a second proposal).
- **NOTE** — no recorded live tapes existed in the B1 zips (code only), so the golden
  tape replays a deterministic scenario corpus through the vendored live code itself
  rather than through recorded market data. The recorder (`book_snapshots`) is banking
  real tape from first boot; re-running the harness over recorded frames when they
  exist is the standing upgrade path.

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
- **TRUE [REV 2]** — `claude/trading-contract-math-2biFE` recovered: Drew granted full
  branch access; the real `should_dump_position` + `_btc_is_safe` (branch `bot.py:2325-2680`
  at commit `85b6c9c`) were read and their MECHANICS folded into the custodian
  (`relay_engine/custodian.py:214-306`): worst-of(bid, prob) dollar stops firing first,
  late hold-to-settle with danger-buffer override, early-exit-underwater, grace period,
  bankroll/position caps preceding spot safety, the spot-safety MASTER OVERRIDE ("if spot
  is on our side, the book is lying — hold"), rapid-drop bail, windowed-peak reversal
  (profit-tightened, settling-widened), and the probability floor — in the recovered
  order, all exercised in `tests/test_custodian.py` (18 tests green). Parameters NOT
  borrowed (§F): every number is a per-lane CutParams set at wiring, pending Drew's
  per-lane rulings; the engine ships no defaults.

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

## WO-2026-07-17-RELAY-P1 — "AUTH: SIGN THE HANDSHAKE" (patch order, closed at build side)

- **TRUE** — finding confirmed as stated: the delivered tree had no auth layer; the 401 was
  structural. The fix is `relay_engine/auth.py`: env `KALSHI_API_KEY_ID` + `KALSHI_PRIVATE_KEY`
  (PEM, literal-`\n` normalized), `signed_headers()` signing `timestamp_ms + METHOD + path`
  with RSA-PSS(SHA-256, salt=DIGEST_LENGTH) — verified byte-identical in scheme to the live
  tree's `kalshi.py:59-75` — and `signed_request()` as THE signed-REST helper (§2.3; Coinbase
  candle fetch explicitly exempt, noted in `delta_builder.py` header).
- **TRUE** — handshake wired: `shadow_runner.py` connects with
  `additional_headers=auth.signed_headers("GET", <ws path>)`, signed at connect time per
  attempt, never import time.
- **TRUE** — §4 doctrine implemented and tested: absent/unparseable creds → FATAL at boot
  BEFORE any connect (`auth.boot_check`, wired first in `run()`); 401/403 handshake →
  three-strike FATAL via `auth.HandshakeRejections` (success resets; non-auth statuses never
  count); all other socket deaths stay ladder events, unchanged.
- **TRUE** — boot tape gains exactly one line: `AUTH: key id …last4 loaded, PEM parsed`;
  test-enforced that neither the full key id nor key material appears.
- **NOTE (spec deviation, declared)** — §2.5 asked for an "exact expected header" signature
  vector; RSA-PSS salts are random, so exact signature bytes are impossible. The vector test
  pins the exact header set / key id / mocked timestamp and CRYPTOGRAPHICALLY VERIFIES the
  signature over the exact message with the exact PSS parameters, plus a mutated-message
  negative check (`tests/test_auth.py::test_signature_vector_fixed_timestamp`).
- **TRUE** — §3.1: suite green at **80 passed** (7 new auth tests: vector, missing-creds
  boot-stop, last4-only tape line, 3×401 FATAL, 1×401-then-success no-false-FATAL,
  runner boot-stop-before-connect, PEM normalization).
- **AWAITING DREW (§3.2-3.4)**: set `KALSHI_API_KEY_ID` + `KALSHI_PRIVATE_KEY` on Render,
  redeploy, confirm `AUTH:` line → `WS subscribed` → recorder confirmed; the 24h gate-7
  clock starts at the first clean subscribed frame.

## WO-2026-07-17-RELAY-P3 — "ALL LANES LIVE" (supersedes the P2 scope guard per R1)

Suite at this commit: **128 passed** (`python -m pytest tests/ -q`).

- **P3.1 TRUE** — venue hands (`relay_engine/venue.py`, flipdesk kalshi.py whole), live
  gateway branch (RUN_MODE=LIVE + phrase only; balance re-read per write; sells translate
  to complement buys — the proven engine buys only), fills loop (`relay_engine/fills.py`:
  exactly-once, order-index attribution, foreign fills counted not claimed, reconcile
  sweep), REJECT_TAKER_ENTRY (strictly-through prices; exact-boundary joins left to venue
  post_only whose rejection is a normal reprice). **The Adversary's wall honored: the
  booking path shipped proven (tests) before any lane beyond F/H8 landed.**
- **P3.2 TRUE** — Lane FLIP ported (`lane_flip.py` + `flip_math.py` verbatim): internal
  walls whole at this morning's traded knobs (all DREW-DEFAULT), pure signals
  byte-identical, orders through gateway.submit (flip bundle reconstructible per-lane
  from surface rows — acceptance test green), one exit owner (scratch reasons =
  custodian cut-params; custodian executes), cross-400 normalized, stop-streak -> lane
  kill. Gateway single-entry wall scoped per side (FLIP quotes both sides; the post-fill
  second leg is risk-reducing at the canonical layer).
- **P3.3 TRUE** — Lane D ported from d_worker shapes (`lane_d.py`): classify with
  evidence rows, reserve-before-seed with the absolute veto and append-only decisions,
  watchdog re-verify -> abandon through the custodian, recovery baton, 50-80c band with
  the 60c DREW-DEFAULT floor, delta-gated (evidence-born: TABLE_ABSENT = no trade),
  hard-pinned KXBTC15M (multi-series scanner = noted seam, not built).
- **P3.4 TRUE** — custodian live-cut tick first in every cycle (crossfire on CUT only),
  D-abandon wiring, the Scientist's concurrent-lane stamp on every surface row, ops
  parity pack: WORST-DAY BOUND as a number (min of cap x events / kill clamp / drawdown
  rail), LANES LIVE vs NOT YET BUILT, foreign-fills alarm polarity.
- **P3.5 TRUE** — Lane P built new: displacement fade (0.05 trigger in the 0.03-0.10
  band), sustained-fresh-frame confirmation, tape stand-down; THE NEGATIVE SPEC SHIPPED
  FIRST (8 never-trigger tests: flickers, stale frames, re-reads, whipsaws, off-band,
  tape-confirmed, no-prior). All five lanes live in the registry.
- **P3.6 PARTIAL** — arbitration ENCODED and test-proven (custodian exits first, FLIP
  takes lead, entries F->H8->FLIP->D->P: `tests/test_arbitration.py`). The demo session
  script ships (`scripts/demo_mechanics_check.py`: place/amend/cancel, stacked TROV,
  netting_enabled probe, fee read) — **UNPROVEN until run where the venue is reachable**
  (this sandbox's policy blocks it). Any surprise in its report = WALL, back to Drew.
- **GO-LIVE remains Drew's act**: RUN_MODE=LIVE + `I_UNDERSTAND_LIVE` phrase on the
  (separate) relay service. Nothing auto-deploys live. Constants at go-live per Part IV,
  all DREW-DEFAULT.

## WO-2026-07-17-RELAY-P4 — "MAKE IT WATCH" (build side closed; deploy verify pending)

Suite at this commit: **136 passed** (8 new P4 tests).

- **F1 TRUE** — subscription lifecycle: signed REST discovery at connect
  (`venue.list_open_markets`), subscribe grammar fixed and SNAPSHOT-TESTED —
  `market_tickers`, never `series_tickers`, channels orderbook_delta/ticker_v2/
  market_lifecycle_v2/fill (`shadow_runner.py::subscribe_cmd`); rollover via lifecycle
  events AND the 60s rediscovery sweep; closed windows pruned everywhere (meta, books,
  windows, FLIP state — folds F4). Error frames CLASSIFIED (F1d/Adversary): per-order →
  routed + counted, never fatal; config/subscribe → FATAL **with the sent payload
  echoed**; unknown → FATAL (default preserved) (`feed.py::_classify_error`).
- **F2 TRUE** — the blind eye opened: spot task on its own 1.5s cadence
  (venue.get_btc_spot, ported with its staleness law), BLIND bound 30s (stale serves as
  None; lanes degrade exactly as before), boundaries + close_ts from exchange-truth
  market_meta. Test-proven both ways: H8's gate FIRES on wired spot (a live H8 shadow
  proposal) and degrades to H8_NO_SPOT without it; the custodian's spot-safety master
  override holds a losing-looking mark when spot is safely ours and cuts when it isn't
  — through the runner, not just the unit.
- **F3 TRUE** — cycle throttle: frames update books continuously; the five-lane sweep
  runs on the CYCLE_SECONDS=1.0 monotonic gate (custodian tick inside — 1s risk
  latency, matching live cadence).
- **F5 TRUE** — a 30s quiet stretch pings (5s pong window) and continues; only a failed
  pong walks the ladder. Real closes/exceptions unchanged.
- **F6 TRUE (shipped + tested; gates nothing in shadow)** — `reconcile.py::
  live_boot_reconcile`: venue balance baselines (first boot) or routes through the cash
  protocol (later boots); positions OUR fills explain are adopted with true attribution;
  unexplained positions QUARANTINED with an alert (never cost-band-guessed — 3.3's law);
  foreign resting orders alerted, untouched; unreadable balance = FATAL. Runs
  automatically when RUN_MODE=LIVE + phrase; a bare RUN_MODE=LIVE without the phrase now
  refuses to run at boot.
- **F7 TRUE** — the pack prints on its own hourly timer task (decoupled from market
  activity) and carries the worst-day tuition line + foreign-fill count (both landed in
  P3.4; the deployed zip predated that commit).
- **DEPLOY VERIFY (open, needs Render)**: boot tape → `WS subscribed:` with the market
  list → RECORDER confirmed → first PASS/WATCHING rows → one clean 15-minute rollover
  (old pruned, new subscribed) → first timer pack. That tape closes gate 7's machinery.

## WO-2026-07-17-RELAY-P5 — "SPEAK ITS DIALECT" (build side closed; the tape is the gate)

Suite at this commit: **150 passed** (14 new P5 tests); golden tape 0/2,941 mismatches.

- **§1 TRUE** — per-channel subscription (`shadow_runner.py::ChannelSubscriber`): one cmd
  id per channel so a stranger name can only kill itself; code 8 tries the fallback name
  ONCE (`ticker_v2`->`ticker`, `market_lifecycle_v2`->`market_lifecycle`); the essential
  `orderbook_delta` failing every name is FATAL with echo (no book, no engine); any other
  channel failing degrades with a WARN and the engine continues (lifecycle loss covered
  by the 60s sweep, fill loss by the REST fills sweep). Subscription replies route to the
  subscriber BEFORE the feed's error classifier. The accepted vocabulary is logged once
  (`WS channels accepted: [...] / degraded: [...]`) and rollover subscribes speak the
  LEARNED dialect (test-proven).
- **§2 TRUE** — code-first classifier (`feed.py`): PER_ORDER_CODES route (25, 27 — the
  set grows empirically, each growth a reviewed commit); CONFIG_CODES (2, 6, 8) and ALL
  unknown codes die loud with the payload echo; keywords are a tiebreak for CODELESS
  frames only. Both adversary directions test-pinned: a config error whose message says
  "order" still FATALs; a per-order code never does.
- **§3 TRUE** — first-frame unit assertion (`feed.py::_assert_units`): int 1-99 = cents,
  decimal-string/float <= 1.00 = dollars (converted at parse), `WS book units: {form}`
  logged once per connection, units re-asserted after every socket death, neither form =
  FATAL with the raw level echoed. Deltas parse through the same learned units.
- **§4 TRUE** — BOOT_LOOP (ledger `boots` table + `engine_state`): >10 boots/hour fires
  one Telegram alert tagged BOOT_LOOP carrying the last recorded FATAL line; the runner
  records every FatalIntegrityError before re-raising.
- **DEPLOY VERIFY (the gate this day points at, needs Render):** boot tape → `WS
  channels accepted: [...]` → first snapshot with `WS book units:` line → **RECORDER
  [confirmed writing]** (Scientist: the curriculum starts at that line) → PASS/WATCHING
  rows → one clean rollover (`window closed + pruned` + new ticker subscribed) → hourly
  timer pack. Green tape = nothing left between the machine and Drew's two env vars.

## WO-2026-07-17-RELAY-P6 — "FAIL LOUD, WRITE IT DOWN, PAGE ME" (R5/R6 recorded)

Suite at this commit: **168 passed** (18 new P6 tests); golden tape 0/2,941 mismatches.

- **§1 TRUE** — frame-shape honesty: NO DEFAULTS at the frame edge. Delta price/delta
  extracted through key ladders (`price/price_dollars/price_fp/yes_price/
  yes_price_dollars`; `delta/delta_fp/change`); a frame that misses the ladder is
  FRAME_SHAPE_UNKNOWN — banked WITH THE RAW FRAME, book dropped, ladder resync, engine
  continues; three consecutive = WS_DELTA_UNPARSEABLE FATAL, evidence banked BEFORE the
  raise. 0/None are shape failures, never unit failures — the unit assertion only ever
  sees wire values. Snapshot levels take the same ladder (tuple AND dict forms). The
  original v5 crash frame replays green in the suite.
- **§2 TRUE** — Telegram wired (R6): real transport from TELEGRAM_BOT_TOKEN/CHAT_ID
  (k_worker notify shape — retry, 400 plain-text logic, NEVER raises); absent in LIVE =
  boot-stop (PAGER_UNWIRED_LIVE), absent in SHADOW = one loud log line. Sends: BOOT
  (boot #N, mode, markets, WORST-DAY line, accepted channels), every FATAL via the
  funnel, BOOT_LOOP, ladder transitions, cash prompts, hourly one-liner, 9AM ET full
  pack, clean-shutdown notice. Inbound long-poll dispatches EXACTLY /confirm_cash and
  /deny_cash with offset persisted; everything else gets the refusal line (Adversary:
  the old tree's richer command set did NOT port).
- **§3 TRUE** — the Failure Ledger (R5, banked as law): `failures` table (ts, why_tag,
  what, how_json, where_src, run_mode, boot_id) + ONE funnel `failures.fail()` — writes,
  alerts (FATAL always; WARN throttled with counts), THEN raises for fatal-class.
  Pre-configure failures buffer and flush at boot. EVERY FatalIntegrityError in the tree
  routes through it — grep-ENFORCED by test (zero bare raises outside failures.py). The
  funnel itself never fails the engine (dead pager + dead ledger both test-proven
  harmless). The pack gains the FAILURES section (count by tag, first/last seen).
  Doctrine banked: "A failure that didn't write why/how/what/when before raising is
  itself a failure."
- **§4 TRUE** — recorder batches; commits on the 1s cycle gate + shutdown flush;
  confirmed_writing counts the buffer.
- **§5 TRUE (env in render.yaml; Drew's dashboard applies it)** —
  `RELAY_DB_PATH=/var/data/relay_shadow.db`: the curriculum survives the classroom.
- **DEPLOY VERIFY:** the phone buzzes with the BOOT message → accepted channels →
  book units line → RECORDER [confirmed writing] → PASS/WATCHING rows → a rollover →
  the hourly one-liner ON THE PHONE. Any failure instead arrives with its tag and its
  full record is in the table — R5 working even when nothing else is.

## HARD STOP honored

Chunks 5 (demo verification), 6 (shadow-lane promotion), 7 (cutover) NOT built — separate
orders at Drew's word. Standing input requests: ~~B1~~ **DELIVERED** (gate 5 green),
**B2** (legacy CSVs → unblocks §E), **charter + lens verbatim texts** (→ closes the
gate-1 placeholder). Remaining open action: deploy the shadow to Render (or any
Kalshi-reachable environment) to start gate 7's 24-hour tape.
