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

## WO-2026-07-18-RELAY-P8 (+P7) — "GO LIVE: BRACKET EVERY MARKET, STOP AT TWO"

Suite at this commit: **181 passed** (13 new); golden tape 0/2,941 mismatches.

- **§1 TRUE** — `window_econ.py`: OPEN BRACKET at first order submit (account truth:
  venue balance+positions in LIVE, paper book in shadow), CLOSE BRACKET after confirmed
  settlement + booked fills; window_pnl = close − open − confirmed-cash-moves-inside
  (a mid-window deposit provably does not masquerade as trading profit); broker-vs-fills
  divergence > 2c banks WINDOW_ECON_DIVERGENCE with BOTH numbers and pages; WINDOW_ECON
  surface row per traded market; 📊 one-liner with streak; untraded markets write no
  bracket. Settlement sweep task (30s) closes brackets from venue `get_settlement_result`.
- **§2 TRUE** — the two-strike leash: consecutive-negative streak over traded markets,
  account-level; streak==2 → TWO_STRIKE_HALT (entries engine-wide; custody of existing
  risk continues — test-proven an EXIT still submits under the halt); halt PERSISTS in
  the DB across restarts (test: new engine, same DB, still halted); `/reset_halt` from
  the configured chat is the only key — entries only, streak reset, HALT_RESET surface
  row, reply carries book value. Pack shows streak/halts/resets.
- **§3 TRUE** — boot tape prints `SIZING: 1/12-Kelly ceiling · book $Y · current max
  lots {n}` — n computed by the ladder itself (at a $100-class book: 1 lot, which is
  what 1/12 Kelly IS at this bankroll; the same math grows size as the book compounds).
- **§4 TRUE** — LIVE path: RUN_MODE+phrase → live boot reconcile → LIVE banner + pager
  boot-stop if Telegram unwired; ✅ FILL one-liner per booked fill; render start command
  stays the shadow runner until Drew flips the env vars.
- **P7 (RESOLVED — the parts are the spec)**: Drew's ruling 2026-07-18 — no WO-P7
  document exists; the re-delivered part trees (flipdesk `_21`, AtDcG `_32`,
  byte-identical to the B1 originals) ARE the spec, compared only where needed.
  The five items built from P8 §4.3's list stand: single book adapter
  (`book.touch_view`, THE one view every lane reads) + 200-case property test
  (adapter fields == book canon; favorite never fabricated) + replay regression
  (`replay.py`: live-shaped fp-dollars tape → F tracks the favorite on every frame,
  never blind; plus `replay_from_db` — the recorder's named reader, real) + venue
  reject backoff (REJECT_BACKOFF wall, 30s market rest, risk reduction exempt, failure
  rows banked) + storm telemetry (frames/rejects/venue-rejects in the hourly line).
  **Comparison verdict**: one fidelity gap found and closed — the parts rest orders
  at the TRUE TOUCH (`place_order_maker` sends `v2_price_str`, subpenny fp-dollars)
  while the relay book floored to int cents and dropped the fp. Now: `OrderBook`
  keeps per-level fp strings (`yes_fp`/`no_fp`, `best_fp`), `Feed` preserves them
  from dollars-form snapshots AND deltas, `touch_view` carries `yes_bid_fp`/
  `no_bid_fp`, `favorite_side` prices on the exact Decimal (subpenny decides ties,
  byte-matching the live `_favorite_side`), and every entry lane (F/H8, FLIP, D)
  passes `rest_fp` into its `Order` → `_submit_live` → `v2_price_str`. Cents-int
  books yield `None` at every stage — never a fabricated string; sell intents
  translate to complement buys and re-derive (fp cleared). Proven end-to-end in
  `tests/test_true_touch_fp.py` (wire → book → adapter → eval → Order).
- **DEPLOY (two steps, the second is Drew's):** deploy → watch one shadow window price
  correctly (F tracking the live favorite in the log within ~15 min) → flip
  RUN_MODE=LIVE + I_UNDERSTAND_LIVE phrase → LIVE BOOT page → first ✅ FILL → first 📊
  bracket → trading, bracketed, leashed at two.

## WO-2026-07-18-RELAY-P9 — "REAL NUMBERS ONLY" (the last order before live)

Suite at this commit: **205 passed** (17 new). Audit verdicts (read-rule), then the build:

- **Chain 2 hole TRUE** — `account_value_cents` (pre-P9 shadow_runner.py) fell back to
  `ledger.book_cents()` whenever the live venue read failed or the client was absent —
  a bracket/streak/halt verdict could consume a paper number while live.
- **Chain 3 TRUE** — the listener died cross-thread: `poll_updates_once` reads
  `ledger.get_state` (ops.py) OUTSIDE its try block, via `asyncio.to_thread`
  (shadow_runner listener task) against a same-thread-checked sqlite connection
  (ledger.py `sqlite3.connect` default) → ProgrammingError in the worker thread →
  task death, "exception was never retrieved". Inbound /reset_halt was DEAD.
- **Found while fixing (same class, worse silence)**: the settle task's
  `settlement_sweep` and `fills.reconcile_sweep` also touch the ledger via
  `to_thread` — the same cross-thread error was being SWALLOWED by that task's own
  `except Exception: log.warning` every 30s: settlement sweeps never completed in the
  threaded path and said almost nothing. §1a fixes all three callers at the connection.

- **§1 TRUE** — `Ledger` now opens `check_same_thread=False` behind `_LockedConnection`:
  ONE connection, ONE `threading.RLock` around every execute/executemany/executescript/
  commit (single-writer law intact, now thread-proof; cross-thread get/set proven via
  `asyncio.to_thread` in tests). ALL five background tasks (spot, pack, listener, settle,
  reconcile) run under `supervise()`: any exception → tagged failure row
  (`TG_LISTENER_DOWN` for the ear) → outbound page → 5s backoff → restart. The settle
  task's silent `except` is deleted — the supervisor is the only net. Grep-test asserts
  every `create_task` in the tree is supervise-wrapped. Hourly line carries
  `listener: ok|ok(n restarts)|down(n restarts)`.
- **§2 TRUE** — `account_value()` returns `(cents, source)` split by mode. LIVE: venue
  read only; a failure banks `ACCOUNT_VALUE_UNREADABLE` per attempt (3 attempts paced
  ≥5s apart = retry ×3 over 15s, across cycles so the loop never blocks), pages at
  attempt 3, and returns `(None, "venue")` — it NEVER returns the ledger book in live
  (test-proven). Brackets DEFER on None: pending opens/closes complete on the next
  successful read with the deferral stamped on the row (`open+Ns`/`close+Ns`); a
  deferred close never re-settles (settlement booked exactly once), and a deferred red
  window still counts its strike (no laundering the leash). Every bracket row carries
  `source` (`venue|paper`); live rejects `paper` FATAL (`BRACKET_PAPER_IN_LIVE`).
  SHADOW: paper book as before — papers the money, never the market.
- **§3 TRUE** — standing reconcile task, every 60s in live: venue cash+pv vs ledger
  expectation, routed to the EXISTING cash protocol — quiescence window (in-flight
  orders/unsettled fills → DEFERRED), negative drift → entries halt + breakdown page +
  /confirm_cash | /deny_cash, venue-unreadable → no verdict (never a paper-number
  prompt). Drift pages between brackets, not at them.
- **§4 TRUE** — the BOOT page now waits for channel negotiation (sends when the last
  subscribe reply resolves, 30s cap) and carries: accepted channel list, SIZING
  1/12-Kelly line, WORST-DAY bound, `boot #N`, `listener: ok`.
- **§5 (Drew's half)**: deploy → BOOT page with accepted channels + listener ok → text
  the bot garbage → the refusal line comes back (the ear is ALIVE) → one shadow window
  where F's cost tracks the live book → RUN_MODE=LIVE + phrase → LIVE BOOT page →
  first ✅ FILL → first 📊 bracket with `source=venue`.

## WO-2026-07-18-RELAY-P10 (FINAL, lens-amended) — "THE BOOK CANNOT LIE"

Suite at this commit: **224 passed** (19 new).

- **§1 (root cause)**: the banked tape lives on the DEPLOYED worker's disk — the
  verbatim-frame conviction is one Render-shell command away
  (`python -m scripts.autopsy_replay /var/data/relay_shadow.db <market>`; starts at
  the banked snapshot per A1, legacy-vs-fixed dual replay, prints the corrupting
  frame verbatim, states the evidence gap honestly if no snapshot was banked).
  Three parser holes convicted by inspection and CLOSED — see
  `docs/P10_FIX_REPORT.md`: (1) the defaulted delta side `m.get("side","yes")` —
  leading suspect, one side-less frame turns yes45/no55 into the exact observed
  yes97+no55=152; (2) delta-built books after `drop_book` with no snapshot
  foundation (v5 tape proves this path ran live); (3) no seq checking. Fixes:
  SIDE_KEYS no-default ladder, `has_snapshot` foundation law, seq-gap guard —
  each with reconstructed-frame regression tests (stated as reconstructed, per
  the read-rule; the verbatim frame joins the suite after the autopsy runs).
- **§2 TRUE** — the coherence invariant (P7 §1c, now actually built): after EVERY
  apply, yes+no > 101 → `BOOK_INCOHERENT` row (alert=False; the page is the
  episode line, once) → book POISONED → snapshot resubscribe forced (sid-based
  `update_subscription` delete+add; plain resubscribe fallback; tolerant replies;
  5s per-market send floor) → clean snapshot clears. Lanes refuse a poisoned book
  (WATCHING `BOOK_POISONED`, never FATAL). **A6**: the custodian's marks flip to
  the REST book on poison — cuts proceed on REST truth, custody never stalls
  (test-proven: the substituted book reaches `custodian.tick` with transport=REST).
  **A5**: 3 episodes in one window → QUARANTINE until window close (entries off all
  lanes, custody on, exactly one ⛔ page naming the self-heal time); clears at
  rollover AND time-expires as a belt.
- **§3 TRUE** — reconcile-to-book: 60s supervised task, REST `orderbook_fp`
  touches vs the WS book per market. **A2**: >2¢ → `BOOK_DIVERGENCE` row EVERY trip
  (R5, alert=False) + silent resync on the FIRST; the phone only on the SECOND
  consecutive trip. Unreadable REST = no verdict. §3.3 multi-series note banked in
  the runner. §3.4: hourly line carries `book✓ {n}`.
- **§4 TRUE** — wall-reject backoff: a rejected ENTRY's identical re-propose
  (same proposal AND same touch — the fingerprint includes the book bests) rests
  30s pre-wall as `WALL_BACKOFF`; suppressed attempts still count (R5) and still
  feed storm detection. Same-key rejects >20/60s → ONE `⚠ WALL_STORM` page per
  episode. Exits/cuts never suppressed (risk-reducing branch skips it entirely,
  test-proven). §4.3: the runner logs first + every 10th with a count.
- **§5 TRUE** — the pack renders BOOK EPISODES (by market, first/last seen) and
  calls out quarantines / 3+ poison episodes as FINDINGS.
- **§6 [A4] TRUE** — the end-to-end replay runs the DEPLOYED wiring
  (`Feed.handle_frame → touch_view → FH8Shared.decide → gateway.submit`): both
  01:25 corruption shapes produce ZERO proposals; the honest mid-priced book after
  resync gets F's PASS — a quiet log is the discipline working.
- **DEPLOY (Drew's half)**: deploy → run the autopsy in the Render shell → paste
  the verbatim frame back (it becomes the §1.4 permanent test) → watch the next
  window: TRUE-book behavior, `book✓` heartbeats hourly → the go-live checklist
  resumes exactly where it left off.

## FINAL CHUNKS (0718) — "MAKE IT TRADE"

Suite at this commit: **230 passed**. `python -m scripts.preflight` → **9/9**.

- **CHUNK A (gap recorded per A3)**: no autopsy output was delivered with the order —
  the fix report records the gap; the standing conviction remains the three
  reconstructed-frame tests. The dual-direction harness is LIVE:
  `test_corrupting_frame_teeth_both_directions` proves its tape corrupts the v7-shim
  applier (yes97+no55=152 exactly) AND stays coherent through the fixed pipeline —
  teeth both directions. The verbatim frame drops into its `FRAMES` list the moment
  the Render-shell autopsy names it.
- **CHUNK B TRUE** — the calm invariant: sum 102-105 poisons only on the 2nd
  CONSECUTIVE incoherent apply (the persistence clause is realized at the next
  apply; a coherent apply clears the pending trip); a single-frame trip writes the
  row (transient=True, R5 fidelity) but does NOT poison, page, or count toward the
  A5 ceiling. Sum > 105 poisons IMMEDIATELY — 51/51 can be an honest race, 97/55
  never is. The pack splits `BOOK_INCOHERENT(transient trips)` from poison episodes;
  transients are never FINDINGs. Tests: single-frame 102 → row only; two-frame 102 →
  poison+page+resync; single-frame 152 → immediate; pack split.
- **CHUNK C TRUE** — `python -m scripts.preflight`: nine PASS/FAIL lines (full suite,
  01:25 A4 replay + teeth harness, golden tape, ±$5 both branches, two-strike
  halt/restart//reset_halt, live-rejects-paper, poison→REST→resync→quarantine chain,
  listener round-trip, the Chunk D dry run), each backed by named tests executed for
  real; ends `PREFLIGHT: n/n — the book cannot lie, the money cannot lie, the ear is
  alive.` THE STANDING DEPLOY GATE from now on.
- **CHUNK D TRUE** — `test_go_live_dry_run.py`: the whole live spine, no network —
  real-PEM auth boot-check (full key id never printed) → live_boot_reconcile
  (venue balance baselined; quarantine rule fires on a foreign position; foreign
  resting sweep tolerated) → boot tape [LIVE]+SIZING, worst-day line + listener ok →
  the DEPLOYED cycle drives F through the gateway's live branch — venue payload
  snapshot-asserted `{yes, 97c, x1, v2_price_str="0.97", post_only=True}` → mocked
  fill books through FillBooker (✅ FILL page, custodian adopts) → bracket
  source=venue → mocked settlement closes it: window_pnl==fills_pnl==3c, 📊 line,
  streak 0. **Dry-run-caught wiring bug, fixed**: on a FIRST live boot the caps were
  snapshotted at book=0 before the venue baseline — PCT_OF_BOOK budget 0 would have
  rejected every entry until a restart; `live_boot_reconcile` now re-snapshots caps
  after baselining (C.2: a confirmed movement re-baselines).

**After this there is NOTHING left for code to prove.** The GO-LIVE block is Drew's
hands only: autopsy + preflight in the Render shell → one quiet shadow window →
RUN_MODE=LIVE + the phrase → the phone tells the rest.

## WO-P11 "PROVEN GROUND" + P11.1 fold-ins — the REST reversion (A3)

Suite at this commit: **241 passed**; `python -m scripts.preflight` → **10/10**.

- **A3 RULING recorded** (`config.RULINGS`, printed every boot): go-live transport is
  REST 1s at one-lot / ≤3 net — yesterday's proven live surface; WS is SHELVED with
  its lessons and fixes, returns as an upgrade re-certified separately, never again
  gating a go-live. `WS_ENABLED=false` is the default; the flag restores the WS loop.
- **P11.1-a TRUE — the runner never rewired**: `rest_feed.RestFeed` SUBCLASSES Feed —
  the FeedLike surface (books/book/drop_book/resync_needed/on_poison/frames_seen) is
  inherited, not reimplemented. A poll fetches `venue.fetch_orderbook_raw` (FULL
  depth, fp-dollar strings preserved) and pipes the synthesized snapshot through the
  inherited `handle_frame` — so the unit assertion, true-touch fp law, coherence
  debounce, and recorder all ride along verbatim. Poison/resync are near-no-ops by
  construction: the next 1s poll IS the resync. All 230 prior tests passed unchanged.
- **P11.1-b TRUE (Marta's flag confirmed then closed)**: the request core had NO
  governor — only 429/5xx retry backoff. Now `_RestGovernor` (ONE thread-safe token
  bucket, pace-not-reject) wraps EVERY attempt of EVERY REST call
  (`KalshiClient.request`); budget `bucket=10, refill=8/s` printed in the boot tape —
  the printed number is the enforced number. Source-asserted by test.
- **P11.1-c TRUE**: the REST recorder format IS the synthesized snapshot through the
  existing `record()` path — replay-compatible by construction (test: poll →
  `replay_from_db` prices the favorite at the subpenny touch). `transport` stamped on
  surface rows (REST|WS|EXPLORATION via `transport_label()`) and on brackets
  (window_econ `transport` column, migrated in place).
- **P11.1-d TRUE — no-defaults extended to books**: a failed fetch = NO book this
  cycle: `BOOK_FETCH_FAILED` banked + counted, the old book left untouched with its
  old timestamp (never a fabricated freshness), lanes skip tagged; custody reads the
  last book only WITHIN `REST_BOOK_STALE_CUSTODY_S=30`, beyond it custody marks DEFER
  (the custodian receives no book and holds — P9's deferral shape applied to books);
  recovery on the next good poll. The ws path is import-inert under the flag
  (ast-proven: no top-level websockets import; the import lives inside the WS branch).
- **Cadences (Marta-certified)**: books 1/s (cycle-paced), spot 1.5s, fills 3s (new
  supervised `fills` task; settle task keeps 30s settlements), discovery 60s. The
  pack header names the 3s fill lag as accepted-at-scope, not a bug (Trader's knob).
  The P10 book_check auditor stands down in REST mode (no second truth to arbitrate).
- **Boot page (§C)**: `transport: REST 1s` + SIZING + worst-day + listener, sent
  after the first discovery+poll. Hourly line gains `fetch_fail={n}`.

## WO-P13 "SAY WHAT YOU DID" — the 6¢ lesson, structural

Suite at this commit: **260 passed**; preflight **11/11**.

- **Part I (evidence-honest)**: `scripts/autopsy_fills.py` committed;
  `docs/AUTOPSY_0715.md` records the reconstruction (−4¢ scratch + ~2¢ fee = −6¢,
  the flipdesk discipline executing correctly) AND the evidence status: the deployed
  DB was EPHEMERAL (`DB: relay_shadow.db`, RELAY_DB_PATH unset) — run the tool on the
  worker before the next deploy or the gap stands. The engine now PAGES at boot when
  LIVE runs on an ephemeral DB (halt persistence + dedup + autopsies depend on it).
- **§1 TRUE — no mute fills**: `Order.why` (thesis at submit: FLIP pair-post w/ touches,
  F/H8 favorite+band+distance, D verdict) and `Order.reason` (FLIP take entry+X,
  custodian trigger names) ride the PROPOSED surface row and the pages:
  `✅ ENTRY … buy no@46¢ — why: …` / `✂️ EXIT … sell no@42¢ (fee 2¢) — PER_CONTRACT_STOP`
  / `↔ round-trip −4¢ + fee 2¢ = −6¢` (the desk's unit of thought; also the
  inversion tripwire — a sell paging as a sell makes a true inversion visible in one
  window). Fee lands in the fills ledger (`fee_cents`, migrated) via parse→book→page.
- **§2 TRUE — forms pinned**: the `val < 1` heuristic is DEAD; `_field_to_cents` +
  `DOLLAR_FORM_KEYS`/`CENT_FORM_KEYS` classify every key (unclassified = never
  parsed); `*_dollars` ×100, legacy `yes_price`/`no_price`/fees = cents (Engineer's
  KNOB honored). Boundary tests: dollars "1.00" = 100¢; legacy 46 = 46¢. Fixtures
  verbatim from the wire (0718 fill shape, ORDER-V2 resp counts, orderbook_fp
  arrays). `docs/VENUE_SEMANTICS.md` cites every field, dated — the future diff is
  the audit. Pre-P13 fixtures that hid dollars in legacy keys were themselves the
  bug and were corrected to the documented wire.
- **§3 TRUE — orientation measured by the venue itself**: boot self-test (our book
  vs the market record's `yes_bid_dollars`; mirror match = FATAL
  `ORIENTATION_MIRROR`), settlement cross-check every window (winner must have been
  our high side; `ORIENTATION_SUSPECT` + page), 30s post-entry divergence watch
  (>3¢ ×3 checks → entries halt + page), processed by a supervised `orientation`
  task. Discovery now stores the record's own touches (explicit ×100 form).
- **§4 TRUE**: `REJECT_FLIP_UNPAIRED` — the named wall, ordered FIRST — refuses a
  second same-side FLIP entry while the prior leg stands (the opposite leg nets
  toward flat and remains risk-reducing by construction); the sit-out pages once:
  `🚪 FLIP sitting out {mkt} — n scratches`. CEO knob: pack itemizes
  `scratches · scratch cost · fees`.
- **Tape-0718 hygiene (folded)**: foreign fills counted once per unique id (the
  200-every-3s counter is meaningful again; NONZERO-AFTER-CUTOVER alarm restored),
  reconcile log prints only on change, orders-route probe wired at boot (the
  `/portfolio/events/orders` 404).

## WO-P14 "CUT ONLY WHAT YOU HOLD" — the 7:34 accident, made law

Suite at this commit: **271 passed**; preflight **12/12**. Trigger: the first live
profitable round-trip (+4¢, fully narrated) followed by FATAL [BATON_VIOLATION] —
the RAPID_DROP cut raced the take-exit's fill; the FATAL was accidentally
protective (an unchecked cut would have SOLD a lot we didn't hold).

- **§1 TRUE — tri-state cancel**: `gateway.cancel_tristate` → CANCELED (venue
  confirmed) · ALREADY_TERMINAL (not resting with us, or the venue says
  filled/already-canceled — GONE, the venue's word, never a violation) · UNKNOWN
  (unverifiable — put back, said so). Only UNKNOWN remains FATAL [BATON_VIOLATION].
  The old test asserting gone-id=FATAL was overturned to the new law, cited.
- **§2 TRUE — re-derive before EVERY cut (all triggers)**: (1) on-demand fills
  sweep for that market (`custodian.resweep`, wired to `venue.get_fills` in live,
  no-op in shadow; failure degrades to the current ledger); (2) position recomputed
  from BOOKED fills (`ledger_remaining` — a (market,lane) with no fills rows at all
  falls back to the pos object: no contrary evidence to outrank it, which is every
  production position since entries always book); (3) remaining 0 → `CUT SKIPPED
  [{trigger}] {mkt} — position already flat (race with fill)`, no page, no FATAL,
  pos cleared; (4) remaining >0 → cut EXACTLY that count, reason-signed. THE LAW,
  BANKED: a cut may only ever sell what the ledger proves we hold at this instant.
  Belt: when a take's fill books the position flat, custody of the stale pos object
  ends at booking — the race cannot even arise.
  Tests: the 7:34 replay (flat → skip, no FATAL, no new position) · partial-fill
  race (entry ×2, take half-filled → cut exactly 1) · genuinely-stuck-resting →
  still FATAL · resweep-at-the-boundary · no-rows fallback · custody-ends-at-flat.
- **§3 TRUE — sizing narration**: `sizing.py` was CORRECT (45¢ budget // 39¢ = 1);
  the boot line printed lots at a 99¢ reference. One shared `sizing_line()` now
  feeds the boot tape and BOTH boot pages:
  `SIZING: 1/12-Kelly · book $5.41 · budget/window 45¢ · max lots: 1 @39¢ · 0 @99¢`
  — the budget is the invariant; lots depend on price; say both. Exact-string test.
- **§4 (Drew's env action)**: `RELAY_DB_PATH=/var/data/relay_live.db` on the
  service — the engine's own ⚠ page (P13) asks for exactly this; once set, the
  warning disappears from the boot sequence.

## WO-P15 FINAL — RATIFICATIONS AS LAW + THE TAPE-ACCOUNTABILITY LAW

Suite at this commit: **284 passed**; preflight **13/13**. (Read-rule note: the
"P15 compare" document never reached this chat — Fix A was implemented from the
wall code's demonstrable gap, stated below; everything else is from P15 FINAL.)

- **RULING 1 (LAW) — ORPHAN adoption**: a boot position our fills cannot explain
  is ADOPTED under lane ORPHAN — custodied to conclusion (D-grade cut params),
  gateway-position registered, `ORPHAN_ADOPTED` surface row + `ORPHAN_FOUND`
  failure row + `🧾` page — and NEVER lane evidence (no Wilson cell reads it;
  P14's no-fills-rows fallback governs its cuts by design). Every dollar owned.
- **RULING 2 (LAW) — pair-formable or nothing**: FLIP's first trip posts only
  when BOTH sides can legally post (each ≤ side-max, combined ≤ the line) — a
  lone leg is never OPENED on purpose (the 00:14 tape's no@34, 8¢ at 8:01).
  Pair-grace still governs a pair whose second leg dies later; the cross-cycle
  combined wall still binds. Two pre-ruling tests overturned, cited in place.
- **RULING 3 (LAW) — depth floor at one lot**: with ≥1 visible lot the thin-book
  backoff FLOORS at PROBE and the depth cap admits ≥1 (the 7:58 depth-starvation
  storms); an empty book still admits nothing. One pre-ruling test overturned.
- **FIX A (pending/gross exposure — best-evidence implementation)**: the wall
  code's demonstrable gap: positions track NET yes-terms, so a FILLED yes+no
  pair (+1/−1) netted to ZERO and vanished from SINGLE_ENTRY and the event
  caps. `gateway.gross_open` now tracks gross open contracts per (event, market,
  lane); SINGLE_ENTRY refuses on net OR gross; `_event_exposure` counts
  max(|net|, gross); gross clears on exit fills, settlement, and rollover.
- **R-1 (banked)**: "proven" must name WHAT was proven. FLIP's knobs are
  explicitly UNPROVEN-margin / PROVEN-mechanism; the `FLIP R6:` line (trips ·
  WR · net/trip · WR-WilsonLB) renders in EVERY daily pack — required reading.
- **§1 THE TAPE-ACCOUNTABILITY LAW**: `scripts/tape_grade.py` grades the deploy
  from the DB (shadow windows count — R-2); the pack carries a DEPLOY GRADE
  section until every line passes twice, then retires; >24h unmet =
  `DEPLOY_GRADE_INCOMPLETE` FINDING, auto-paged. The weekly Saturday retro is
  law (docs/RETRO_2026-07-18.md — the first one, with R-1/R-2/R-3 banked and
  the Adversary owning class-spotting).

### P15 EXPECTED TAPE (§3 — graded by scripts/tape_grade.py, in-pack)
Within 24h: ✅ zero FLIP entries where either side >49 at the open · ✅ zero
PCT_OF_BOOK/SIZING storms at depth ≥1 · ✅ zero same-side double entries any
lane · ✅ ORPHAN adoption recorded for any quarantine (incl. the current mystery
YES at next boot) · ✅ FLIP R6 pack line rendering daily · ❌ no BATON_VIOLATION
FATALs on cut-vs-fill races (P14 in force).

## WO-P16 FINAL — "THE SCALP PROFILE, FUNDED" (the capstone)

Suite at this commit: **295 passed**; preflight **14/14**.

- **§1 TRUE — env-name resilience**: `config.resolve_db_path` chain:
  `RELAY_DB_PATH` → `dirname(K_WORKER_DB)/relay_live.db` (the DISK is the
  constant; `k_worker_surface.db` itself is NEVER opened — different engine,
  different schema, single-writer law) → the ephemeral default with the standing
  warning. Boot tape notes the source; the LIVE ephemeral page now fires only on
  a truly ephemeral resolution. All three branches tested.
- **§2 — the scalp profile, verified with ONE STOP-AND-REPORT**: every listed
  rule confirmed present and reachable (pair-formable, depth floor, orphan
  adoption, gross wall, orientation sentinels, narration, brackets, tape_grade,
  H8 delta gate, D baton, P negative-spec) EXCEPT: **F's "custodian passthrough,
  catastrophic-only" is UNREACHABLE** — the passthrough mechanism exists in
  `should_cut` (custodian.py, honors CATASTROPHIC_PROB) but no CutParams was
  ever registered for lane F, so `should_cut` returns None before reaching it.
  F holds to settlement UNCONDITIONALLY (loss bounded at one-lot by entry cost).
  Per §2: reported, NOT patched — the fix is one registration line
  (`set_lane_params("F", CutParams(..., passthrough=True))`) awaiting Drew's
  ruling. The PROFILE block prints on every boot tape, this finding included.
- **§3 TRUE — deposit day**: a confirmed movement (auto-positive AND
  /confirm_cash branches) now re-baselines the caps immediately
  (`snapshot_caps_at_boot`) and pages the NEW book's sizing line. Rescale math
  tested at $35/$50/$100 (budgets 291/416/833¢; PROBE still caps at one lot);
  the worst-day rail arms nonzero at $50 (=$25 over the floor) and reads $0
  under it. Note: positive deltas auto-confirm without a prompt (the ratified
  P8 cash law) — stricter than §3's prompt-then-confirm description.
- **§4 TRUE — the deposit-day expected tape**: `CHECKS_P16` in tape_grade
  (deposit confirm landed [boot baselines excluded], F's first entries, FLIP
  pairs only, no doubles, orphan settlements attribute to ORPHAN, closed
  brackets source=venue, no BATON FATALs, no depth storms, no unexplained
  orientation pages). The pack grades BOTH suites with SEPARATE retirement
  (P15 retires on its clean tape while P16 stays active until deposit day);
  the 24h FINDING law applies per suite.

**Then: HANDS OFF except bugs** — the accumulation window runs to Saturday's
retro, which judges lanes on margin. The machine writes most of that document
itself.

## HARD STOP honored

Chunks 5 (demo verification), 6 (shadow-lane promotion), 7 (cutover) NOT built — separate
orders at Drew's word. Standing input requests: ~~B1~~ **DELIVERED** (gate 5 green),
**B2** (legacy CSVs → unblocks §E), **charter + lens verbatim texts** (→ closes the
gate-1 placeholder). Remaining open action: deploy the shadow to Render (or any
Kalshi-reachable environment) to start gate 7's 24-hour tape.
