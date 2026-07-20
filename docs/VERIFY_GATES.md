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

## WO-P17 FINAL — "SHOW UP FOR EVERY MARKET"

Suite at this commit: **306 passed**; preflight **15/15**. Doctrine banked:
showing up is mandatory; trading is earned.

- **§1 TRUE — the terminal-row lattice**: terminal is MONOTONIC
  (PASS=0 < GAP_RESTART=1 < SETTLED/CUSTODIED_SETTLED=2), not immutable. PASS is
  PROVISIONAL (interim) mid-window — the terminal PASS is written only by
  `finalize_window` at close, carrying the last pass reason; a lane that passes
  at T-12 and enters at T-2 never writes a PASS terminal at all (the 9:30 FATAL
  shape is now the healthy path, integration-tested). Upgrades log
  `TERMINAL_UPGRADED` + `upgrade from PASS` detail, never fatal; a REGRESSION
  stays FATAL. Settlement is idempotent (settled fills can't re-book; a second
  settle books nothing). Open brackets RELOAD from the DB at boot
  (`restore_open_brackets_on_boot`) — the stuck 0930 heals on the first sweep
  retry, its receipt marked `(settled late — books healed)`. `gap_restart_scan`
  at boot writes GAP_RESTART for windows that spanned a restart — evidence
  holes counted, never papered. Two pre-P17 tests overturned, cited in place.
- **§2 TRUE — the leash counts late truths**: a settlement booked >300s after
  close is LATE — the streak still counts it and a retroactive second strike
  halts with `⛔ TWO-STRIKE (retroactive: {mkt} settled late)` (test-proven).
  Task-death pages state consequences in words (settle: "streak & brackets
  FROZEN until healed"; listener: "/reset_halt DEAF"). Boot tape prints the
  rail state in words: DISARMED below $50 / ARMED with headroom above.
- **§3 TRUE — supervision escalates**: same error ×5 → ONE `⚠ TASK_STUCK` page
  (with the consequence) → 60s paced retries, still supervised and counted; a
  different error resets. Tested both directions.
- **§4 TRUE — walls say their names**: PCT_OF_BOOK→BUDGET, SIZING_TIER→DEPTH,
  NET_RISK_CAP→NET_RISK, AT_RISK_CAP→DOLLAR_RISK, REJECT_WRONG_WAY_TICK→
  WRONG_WAY, REJECT_TAKER_ENTRY→TAKER_ENTRY, REJECT_FLIP_UNPAIRED→FLIP_UNPAIRED
  (the BUDGET storms at the $5 book were misread as depth for two passes —
  ambiguous tags invite confident misdiagnosis; the deposit proved it). Source-
  asserted no ambiguous tag remains; the grader's names now grade mechanisms.
- **§5 TRUE — rich why-tags**: F/H8 entries carry the gate values that admitted
  them (`F tier97 band95-99 confirms{n} ΔP0.970 spot118,500 dist0.42% yes@97`) —
  ladder-or-luck answerable from the row alone.
- **§6 TRUE — the window contract, tested end-to-end**:
  `test_window_lifecycle_end_to_end` drives pass-only · pass-early-enter-late ·
  restart-spanning windows → one PASS terminal, one ENTERED→SETTLED story with
  receipt, one GAP_RESTART, zero FATALs, streak correct. `windows_seen` counts
  up in the hourly line; a window closing with NO rows pages SILENT_WINDOW.
- **§7**: CHECKS_P17 in tape_grade (terminal regressions, silent windows,
  windows concluded, specific-tag storms, TASK_STUCK ≤1, stale open brackets,
  BATON) — third graded suite in the pack, own retirement.

## WO-P18 FINAL — "THE DETECTIVE" (the scalp profile's hunting arm)

Suite at this commit: **324 passed**; preflight **16/16**. The compare was right:
P18 was NOT in the tree — built here as FOUNDATION on the confirmed organs
(delta.p_survive, lane_p's confirm pattern, the spot task, custodian/crossfire,
mode-stamped rows).

- **§0/§2 TRUE — the needle-move equation** (`spotlead.py`, ONE trigger fn):
  `needle(anchor, spot, strike, t)` computes ΔP = p_side(d_after,t) −
  p_side(d_before,t) from the DELTA TABLE at current time-remaining — the
  clock-and-distance law in one line (test: the same $80 jump is a 38-point
  needle near the strike and a <1-point nothing $5k away; no hand-tuned time
  rules). p_side derives from `p_survive` (the shared brain D and H8 gate on);
  TABLE_ABSENT → None → no hunt, evidence-born. Side is ALWAYS with spot —
  the 0/80 never-build's mirror, structurally.
  Gates, ALL required: A) ΔP ≥ 5 pts · B) fair − join ≥ 4¢ (the lag in cents) ·
  C) convergence (a join that DROPS between confirms kills the pending — the
  crowd fighting the move) + 2-consecutive-evaluation confirm (lane_p's
  flicker-proof pattern) + t > FLIP_CURFEW + spot fresh (BLIND → no signal →
  no hunt, tested). Entry: MAKER JOIN at the touch, band (5,95) — no side-max,
  no price cap. ONE hunt per displacement EVENT: the runner re-anchors at the
  post-event spot on submit; a new hunt needs a NEW confirmed needle.
- **§1 TRUE — two modes, stamped**: HUNT casefiles carry `HUNT needle +Npts
  (d A→B, T-m:ss) · fair F · gap G · converging` in why/PROPOSED detail;
  PAIR is UNCHANGED (Ruling 2 stands; its 306-test surface passed untouched)
  and posts only when NO needle is live — the hunt owns the floor while one is.
- **§3 TRUE — the two jobs, mechanical**: JOB A: take at entry+4 proposed the
  instant the fill books (registered as the custodian's resting exit — baton
  intact). JOB B: mark ≤ entry → breakeven reprice IMMEDIATELY (cancel take,
  EXIT at entry); mark ≤ entry−2 OR not-out-in-20s → crossfire flatten NOW;
  TIME-BOX 60s → flatten. CURFEW: hunt entries forbidden and hunt inventory
  flattened at T-240 (PAIR keeps its ported T-90 law — §1's "unchanged"
  outranks; documented choice). Hunt exits realize against THEIR entry and
  feed the shared ratchet (scratches → sit-out, stop-streak, trips) — no
  averaging, no thesis-defense. Custodian FLIP params remain the backstop
  behind the lane's tighter jobs (P14 re-derive prevents any double-sell).
- **§4 TRUE — lanes talk, one yields**: the signal computes ONCE per cycle in
  ctx (engine); F/H8 append `spotlead:+Npts` to their why with gates UNTOUCHED;
  a CONFIRMED needle SUPPRESSES lane P on that market, logged as a
  `P_SUPPRESSED_BY_HUNT` row (the fee-bleed collision class, dead pre-birth).
- **§5 TRUE**: the casefile row carries (d_before, d_after, ΔP, fair, gap,
  converge); outcome rides the exit reason + round-trip line. The FLIP pack
  line prints the honest bar (`bar ≥50% to break even (+4/−4)`) and states
  HUNT volume is EXPECTED LOW — low count reads as discipline.
- **§7**: CHECKS_P18 (casefiles complete, zero sub-N needles, zero P-vs-HUNT
  collisions, hunts resolve, PAIR alive, the bar ships) — fourth graded suite.

## WO-P19 FINAL — "SALVAGE, SEAL, AND LET IT RUN" (the last order before the run)

Suite at this commit: **337 passed**; preflight **17/17**.

- **§1 read-rule correction**: the sweep's "P-suppression is NOT implemented" was
  TRUE of lane_p.py (zero spotlead references) but FALSE of the deployed system —
  P18 shipped the suppression at the CYCLE layer (engine skipped P + wrote the row,
  test-proven). The claimed live consequence could not occur. §3.1 is still the
  better shape and shipped: the suppression moved INTO the lane via the ctx flag
  (`needle_confirmed` → Pass `P_SUPPRESSED_SPOTLEAD +Npts`, logged through the
  ordinary row path) — one mechanism now serves both suppression rules (§2.4).
- **§2 TRUE — the custodian earns Lane F**:
  · 2.1 `OpenPosition` gains (d_entry, t_entry, p_entry), computed at custody
    registration via `fills.anchor_fn` (spot + strike + delta table at the entry
    instant); no anchor → salvage disabled for that position, tagged.
  · 2.2 the trigger: ΔP = p_held(d_now,t_now) − p_entry ≤ −15pts sustained 2
    consecutive ticks AND fair < entry − 10¢ AND t > 15s floor; spot BLIND → no
    salvage (CATASTROPHIC_PROB stays the backstop beneath everything).
  · 2.3 execution = Job-B + P14: tri-state cancel of the resting take (UNKNOWN
    stays FATAL) → resweep + ledger re-derive (flat → skip, logged) → MAKER at the
    held side's bid with the casefile reason → unfilled in R=10s → tri-state cancel
    → crossfire via execute_cut (re-derives again — partials bounded) → ONE attempt
    per position, latched. Page: `✂️ SALVAGE F sold yes@62¢ (cost 95) — needle
    −18pts (d 200→90, T-6:39) · est save 57¢`.
  · 2.4 mutual suppression, one mechanism: ctx `needle_confirmed` suppresses P;
    ctx `salvage_active` (custodian.salvage_in_progress) suppresses HUNT entries —
    one hand exits, the other waits; the reversal hunt fires AFTER, never during.
  · 2.5 `passthrough` → `salvage_enabled` (rename, all references); F AND H8
    register `salvage_params()` at wiring — the catastrophic backstop becomes
    REACHABLE, resolving P16's STOP-AND-REPORT (PROFILE line updated); `👑` page
    once per boot with the knobs.
  · 2.6 salvage rows carry (ΔP, d_entry→d_now, mark, fair, est_save); settlement
    writes the SALVAGE_VERDICT row (DODGED_LOSS vs SALVAGE_REGRET with the
    counterfactual arithmetic) — Saturday chart #2's data; K tunes from it only.
- **§3 TRUE**: 3.1 as above · 3.2 per-market state prunes at rollover (flip
  windows already; NOW ALSO fh8 ladders + decide cache + P states + hunt anchors)
  · 3.3 DB retention KNOB banked here: book_snapshots grows ~unbounded (~tens of
  MB/day at 1/s) — fine for days; a 14-day retention sweep belongs in a later
  order; do not rediscover this at 90% disk · 3.4 CHECKS_P19 (retired-class FATALs,
  salvage casefiles, suppression rows, hunts resolve, silent windows) — fifth
  graded suite, own retirement.
- **§4**: the expected tape is encoded; the watch guide is Drew's (quiet hours are
  SUCCESS; a ✂️ SALVAGE page is money SAVED; two 📊 negatives → the halt is the
  machine OBEYING; a silent phone + a ticking hourly line = fine. Trust the
  instrument you built).

## WO-2026-07-18-RELAY-P21 — "THE DOCTRINE ENGINE" (the last order of the build era)

Suite at this commit: **351 passed**; preflight **18/18**.

- **A1 TRUE — THE NETTING MODEL**: the venue nets one account's sides (Drew
  ruling: "Kalshi closed that loophole"; tape: `WINDOW_ECON_DIVERGENCE broker
  −7c vs fills −65c` at 10:45 and 11:30 — our stale hedge-model, not the
  broker's error). An opposite-side ENTRY-purpose buy on a held market books
  as an EXIT of the held side at 100−price (`fills.py` sweep), banks
  `SELF_NET_BOOKED` (row, no page — the wall should have refused it). Scope
  is the (event, market, lane) key the fills path owns; the wall covers the
  account.
- **A2 TRUE — REJECT_SELF_NET**: `gateway._wall_self_net`, canon position
  after FLIP_UNPAIRED: an ENTRY that would net down the WHOLE ACCOUNT's net
  on the market is refused; exits/cuts (risk-reducing) never reach it —
  netting is an exit's job. Overturned test-law: the old wrong-way sell test
  held +2 while short-selling (now correctly REJECT_SELF_NET first).
- **A3 TRUE — the grain**: `grain.py` reads OUR `window_outcomes` (written
  at `settle_traded_market` via `ledger.record_outcome`, idempotent) →
  last-K (K=4) streak {direction, length} → `ctx["grain"]` each cycle.
  Traded windows only today — that partial view is the registry's grain
  QUESTION.
- **A4 TRUE — Lane OPEN replaces PAIR (retired)**: setup = BOTH sides in
  [44,56] AND grain ≥2 (band without grain → pass, `OPEN_NO_GRAIN`, once
  per window); entry = maker join the GRAIN side ≤49¢, one lot, why-stamped
  (`OPEN grain nox3 · join 49c · band y48/n49`). The PAIR-era entry phase
  and OFI re-entry gate retired with it (waiting IS the setup; the herd's
  screen outranks the last four ticks). P15's lone-leg tape check exempts
  OPEN whys (one-sided BY LAW; join graded ≤49 in the P21 suite).
- **A5 TRUE — THE PATIENT HOLD**: `_open_custody`, every cycle before any
  gate. Inside [35,65]: NO stop, NO scratch, NO time-box (the −11¢ 10:30
  and −12¢ 11:17 round-trips were OPEN-intent killed by fast-intent stops —
  the last of their kind). Exits EXACTLY three: TAKE (entry+5 resting from
  the fill) · DETERMINED-AGAINST (mark leaves the band against us,
  immediate — the band IS the definition; or ΔP-collapse ≥15pts sustained
  2 polls → salvage-style out: maker at the join, crossfire after R=10s
  unfilled) · CURFEW (crossfire flat at T-240, HUNT's handoff law).
  `flip_cut_params` overturned to catastrophic-only (90¢/contract backstop);
  the legacy scratch mapping deleted (git history keeps it). OPEN exits
  count NO scratches — the sit-out never feeds off patience.
- **A6 TRUE — HUNT seniority**: a live needle (ΔP ≥ N) voids OPEN's premise
  (undetermined, herd-priced) — hunt owns the floor; OPEN entries suppressed
  while it lasts.
- **B1 — docs/SEMANTICS.md**: 16 KNOWNs, each `LAW · CODE (file:line) ·
  TAPE (a real tape_grade line)`, + the six QUESTIONS with collectors.
  Boot cites it (`DOCTRINE:` line, missing-from-tree is loud).
- **B2 — KNOWLEDGE_DRIFT**: `semantics.drift_section` runs EVERY KNOWN's
  tape test in EVERY pack (registry tests never retire); a failure demotes
  the answer to QUESTION in the pack, pages `⚠ KNOWLEDGE_DRIFT [{answer}]`
  ONCE per transition, blocks nothing; re-proof prints `✓ re-proven`.
- **B3 — the registry's own wall**: `semantics.validate_registry` +
  `test_registry_wall_*` — every entry must name an existing tape line;
  doctored-registry negative test included.
- **§7**: CHECKS_P21 (divergence silent, zero self-net bookings, OPEN whys
  stamped, zero PAIR entries, exits intentional + params catastrophic-only,
  registry green, drift-pages conditional) — sixth graded suite in the
  pack, own retirement; the DOCTRINE section follows it forever.

## WO-2026-07-18-RELAY-P22 — "THE CELL SCOREBOARD" (the ladder gets its floor sensors)

Suite at this commit: **364 passed**; preflight **19/19**.

- **Grounding finding TRUE, and worse**: `sizing.tier_for` had ZERO production
  callers (tests only) — and the lanes never called `size_order` AT ALL: every
  proposal hardcoded `TIER_PROBE, count=1`, so `size_order`'s only callers were
  boot's display line. The ladder was doctrine without a measurement organ OR a
  sizing path. Both orphans closed.
- **§1 the cell outcome store**: `cell_outcomes (ts, lane, price_cell, won,
  pnl_cents, fees_cents, market, kind)`, UNIQUE(market, lane, kind) — idempotent
  like record_outcome (a multi-trip window banks its FIRST trip; the same key
  makes custodian/sweep double-booking and backfill replays safe). Writes:
  · kind=trip inside `ledger.record_fill` on every non-ENTRY booking — the ONE
    point sweep exits AND custodian cuts both pass; pnl is net of fees; FLIP's
    intents split to OPEN/HUNT cells via the exit reason (`scoring.cell_lane`).
  · kind=settle in `settle_traded_market` beside record_outcome — held residue
    only (round-trips already banked at exit; never double-counted), attributed
    to the OPENING lane per the attribution law (custodian.py:46).
  · §1.2 backfill on first boot (guarded by engine_state): one row per
    (market, lane) — flat lanes bank round-trip math, held lanes bank the
    settlements verdict, kind=backfill; today's four F clips and the flip
    round-trips enter history.
- **§2 scoring.py — one aggregation, three consumers**: `cell_stats` (Wilson LB
  via the untouched sizing.wilson_lower_bound) · `breakeven` — hold cells
  L_eff/((100−mid)+L_eff), reducing to mid/100 raw and dropping once the DODGED
  curve has n ≥ 20 (SALVAGE_ADJ_MIN_N, §3.3 lens note honored) · trip cells
  (L+fee)/(T+L+fee) from each lane's take/bail geometry (HUNT 4/2, OPEN
  5/band-distance, generic symmetric — the P18 "≥50% bar" generalized per cell)
  with the taker fee from EXPECTED_FEE_MULTIPLIER · `margin = lb − breakeven` —
  THE number · `score()` returns the full verdict.
- **§3 price-adjusted bars**: TIER_BUFFER {LEAN +0.03, CLEAR +0.05} over the
  cell's own BE, floored at the old flat bars, CAPPED at {.98, .99} — the bar
  math never demands the impossible, only the honest. THE regression test: an
  F 95-99 cell's LEAN bar is .98, not the flat .65 that would promote four
  hold-wins into a leveraged coin-toss. PROBE stays a ruling, not a bar (R1/R2):
  scoring never returns SUPPRESS — a virgin cell probes.
- **§4 the ladder wired**: the runner's submit choke point scores EVERY ENTRY
  (`_score_and_size`: scoring.tier_for → size_order's untouched
  min(tier, kelly, depth)); a sizing zero keeps count=1 and lets the walls
  refuse BY NAME. Tier changes page once (📶/📉 with LB, bar, n, lots), persist
  in engine_state (survive restarts), bank a TIER_CHANGE row (alert=False), and
  demotion applies at the NEXT proposal — computed fresh every time, no grace.
  Gateway wall untouched (it enforces what it is handed). Custody counts ride
  the fill (note_fill gains count) so an earned 2-lot fill exits at full size.
- **§5 the scoreboard**: `scoring.scoreboard_lines` — sorted by margin, ⚠ on
  red, (hold)/(trip) kinds, lots@book, `*salvage-adj pending` until the DODGED
  curve earns it; empty lanes still listed. In every daily pack unasked, and on
  demand via `/scoreboard` — the whitelist's ONE commanded addition (read-only;
  the two whitelist tests updated with the §5 citation).
- **§6**: tests 13 new (cell writes both kinds + opening-lane attribution,
  backfill idempotent, breakevens per lane-kind, salvage-adjusted BE, the .98
  bar regression, tier page-once/persist/demote-instant, runner sizing from the
  score, the no-static-PROBE grep-test, scoreboard render + command);
  CHECKS_P22 (cell coverage, scoreboard ships, tier changes earned with their
  Wilson math, zero static sizing) — seventh graded suite. Registry: answer 17
  "The score decides the size" banked with its tape line.

## WO-2026-07-18-RELAY-P24 COMBINED FINAL — "SHIELD, FEES, AND THE ZERO IN THE REVERSAL"

Suite at this commit: **374 passed**; preflight **20/20**.

- **P22 certification re-verified**: cell_outcomes writer/backfill in ledger.py,
  tier_for consumed via scoring at the runner's submit path, /scoreboard
  whitelisted — TRUE, nothing ordered there, nothing changed there.
- **§1 the anchor never goes missing**: fills.py adoption — a table miss adopts
  the PRICE-implied anchor (None, None, cost/100), banks ANCHOR_FROM_PRICE
  (WARN class, the INFO-silence class retired) naming WHICH organ missed
  (§1.2: `_salvage_anchor` now returns its cause as a string —
  spot | strike | close | table — four organs, no longer indistinguishable);
  no anchor AND no price pages ⚠ SHIELDLESS (impossible class). Every ENTERED
  row is tagged `anchor=table|price`. §1.3 counterfactual banked as a test:
  the 1715-15 shape with the price anchor (None, None, 0.95) fires the
  collapse trigger mid-slide and rests the salvage maker ≥40¢ instead of
  riding 95→16.
- **§2 fees are read, never imagined**: the fee ladder is a (key,
  per_contract) TABLE — total-of-record keys keep precedence;
  `average_fee_paid` (order-response form, PER-CONTRACT dollars) books
  ceil(avg × count × 100); VENUE_SEMANTICS.md row added. ONE parser, both
  entrances: `venue._resolve_fee` serves parse_fill AND the new
  `parse_response_fee` (nested-'order' and flat shapes). The gateway's
  SubmitResult now carries the live response; `execute_cut` (the 21:13:44
  entrance) books the response's own fee into the fill row AND the cell row —
  scoreboard margins include booked fees. EXPECTED_FEE_MULTIPLIER demoted to
  display/estimate-only (tripwire expectation + breakeven estimates), stated
  in config.
- **§3 the zero in the reversal** (the sweep's catch, verified at source:
  fills.py hardcoded entry_p_win=0.0 → custodian's gain_above_entry was
  always ~+1.0 → the TIGHTENED profit-reversal threshold applied to EVERY
  position; salvage unaffected, it reads p_entry): FIXED AT THE WRITER —
  adoption sets entry_p_win to the anchor's p_entry (post-§1 always present)
  — and BELTED AT THE CONSUMER — `entry_prob = pos.entry_p_win or
  entry_price/100`, a zero can never mean "infinite profit" again. Tests:
  entry 95 / peak 96 computes gain +0.01 and holds on the STANDARD threshold
  (pre-P24 this exact shape cut REVERSAL every time); a legacy 0.0 position
  belts to the same hold; entry 49 / peak 80 cuts on the TIGHTENED branch —
  the doctrine as ported.
- **§4**: CHECKS_P24 (anchors tagged, misses named, response-fee entrance,
  reversal belt, WINDOW_ECON silent) — eighth graded suite in pack + grader.
  Registry: answers 18 "Fees are read, never imagined" and 19 "The anchor
  never goes missing" banked with their tape lines (19 KNOWNs).
- **§5 final standings**: F-bar blind spot CLOSED · fee truth CLOSED ·
  shieldless positions CLOSED · reversal mis-tune CLOSED (found by reading
  the consumer, not by bleeding) · salvage-K COLLECTING (every position now
  contributes) · win-audit SATURDAY.

## WO-2026-07-18-RELAY-P26 COMBINED FINAL — "EVERY WHY IS A PROOF"

Suite at this commit: **390 passed**; preflight **21/21**.

- **Root cause VERIFIED at source, and worse**: `delta._lookup` returns None
  unloaded (delta.py:189-190) and the builder + A1-A5 gates + hot-load
  (delta_builder.py) were NEVER provisioned by the engine — no call to
  `delta.load()` or `start_background_build` anywhere in the runner; only
  lane_fh8 consulted `is_loaded`. One unloaded table gagged HUNT, H8's dual
  gate, and table-grade salvage anchors while grain-only OPEN did all the
  trading and all the leaking.
- **§1 one brain, loaded or explained**: boot provisions the table — disk
  load (manifest+SHA gated) or delta_builder's own A1-A5-gated background
  build with hot-load on PASS (`TABLE_AUTOBUILD`, env-off for air-gapped
  runs; tests disable in conftest). The boot page carries the verdict
  (🧠 loaded · cells · built / 🧠 building / ⛔ FAILED — proven lanes
  mute); the hourly line gains `brain: ok|absent`; while unloaded, every
  window's proven-lane silence tags itself ONCE (`UNPROVEN pass — brain
  absent`) — R5 applied to the brain itself. §1.3: a (d,t) cell miss WITH
  a loaded table logs the pair (once each, capped) — grid gaps become
  Saturday data.
- **§2 THE PROOF LAW** (registry law #20 — Drew's "law #17"):
  `REJECT_UNPROVEN_WHY` in the wall chain after SELF_NET — per-lane proof
  fields (F/H8: tier+surv · D: table verdict · P: displ · FLIP: HUNT
  casefile or OPEN margin|PROBE); unregistered lanes reject outright.
  F/H8 gain the NON-REVERSAL proof: table survival at (d,t) must beat the
  price paid or the lane passes (`TABLE_NON_REVERSAL`), why-stamped
  `surv0.97≥0.95` — or `surv~price` tagged when the table is absent.
  P's fade now prints its displacement arithmetic. OPEN fires on its OWN
  cell margin ≥ 0, or PROBE while cells fill (n < 20) — a mature negative
  cell SITS (`OPEN_CELL_NEGATIVE`): the scoreboard as entry gate, its
  destiny. ~30 test fixtures gained proof whys — the law is furniture now.
- **§3 P25 merged verbatim**: 3.1 `open_consumed` on ANY OPEN exit (takes
  too — the won-window re-bet dies), cleared only at rollover; re-proposals
  tag `OPEN_WINDOW_CONSUMED`. 3.2 the evacuation fork: TAKE rests; 
  DETERMINED/YIELD cross at best NOW (the maker stage deleted;
  OPEN_BAIL_R_S retired) — evacuation fills land ≤2¢ from trigger by
  construction. 3.3 schedule: entries T-15→T-8 (`OPEN_ENTRY_CUTOFF=480`),
  flat by T-6 (`OPEN_FLAT_BY=360`, reason YIELD_TO_F) — the SELF_NET storm
  class dies by schedule; FLIP_CURFEW=240 remains HUNT's. 3.4 geometry v2:
  determined trigger = max(band floor, entry−6); entry risk ≤ TAKE+1 or
  `OPEN_BAD_GEOMETRY` (P21's −12¢ wiggle-tolerance test overturned with
  citation — the patient hold now holds within entry−6). 3.5 folds:
  `page_once` ledger-keyed dedup (the 6:22 restart double-page; 👑 now
  once per deploy) · OPEN whys stamped `geometry=v2` · bench stays Drew's
  one-word lever.
- **§5**: CHECKS_P26 (zero unproven entries, one OPEN story/window, zero
  evacuation maker-waits, receipts on OPEN whys, brain explained) — ninth
  graded suite; registry at 20 KNOWNs, all bound; P21's open-exit tape
  check now admits "open yield"; boot PROFILE speaks the schedule, the
  gate, and the proof law.

## WO-2026-07-19-RELAY-DIAG-1-FINAL — "THE INTERROGATOR"

Suite at this commit: **404 passed**; preflight **22/22**.

- **§0 the units question, verified at source**: the builder computes
  `max_move = max(max_high − start_close, start_close − min_low)` over the
  window's candles — **p_cross measures ANY-TOUCH**, not the at-close
  outcome. The builder docstring now states it verbatim ("p_cross measures
  ANY-TOUCH: …") — the exact line DIAG-001 quotes onto the phone, so one
  page settles the definition half of the question; the histogram settles
  the rest.
- **§1 diagnostics.py — the standing pattern**: a registry of one-time
  diagnostics; sentinel `diag_done:{id}` = one run EVER; executed in run()
  after reconcile, before the first cycle; chunked Telegram paging
  (≤3500/chunk, line-boundary splits, nothing lost); read-only; errors page
  honestly and still close; missing data says so ("NO F pass rows…
  honestly absent"). DIAG-001 F-SILENCE: 24h F pass rows → reason
  histogram · surv-vs-bar gap min/median/max · uniform-2-6pt? · 8-sample
  with (d,t) (new rows carry them — `_table_survival` returns (surv, d, t)
  and the pass_reason is enriched; pre-DIAG rows honestly noted) · the
  quoted TABLE SEMANTICS line · verdict hint (uniform → H-SEMANTICS with
  the exact env flip; table-mutes minority → DISCIPLINE; else H-MARKET).
  DIAG-002 TABLE-COVERAGE: top-10 (d,t) misses (now PERSISTED as
  TABLE_CELL_MISS failures rows — the in-memory set died at restart) +
  loaded grid bounds.
- **§2 the fix ships blind, armed by env**: `F_PROOF_MODE` (default v1).
  v1 = any-touch survival ≥ price paid (current law). v2 = the AT-CLOSE
  comparison — interim (no at-close column yet): bar = price −
  `F_PROOF_V2_BUFFER_PTS` (4), stamped `proof=v2-buffer`; the proper
  at-close column is the follow-up. The Proof Law untouched — the proof
  now measures what the position IS. Boot prints `F-proof: {mode}`;
  why-tags and pass reasons stamp `proof={stamp}`; cell_outcomes gains a
  `proof` column (migration) stamped at write — eras separable forever.
  Drew's move: H-SEMANTICS page → Render env `F_PROOF_MODE=v2` → done.
- **§3 the daily answers**: permanent DIAGNOSTICS pack section —
  pass-reason histogram per lane (24h) · anchor sources table×/price× +
  (d,t) miss count · `F-proof: {mode}` + closed risk by era. Every morning
  answers "why didn't we trade" before it's asked.
- **§6**: 14 new tests (sentinel once-only incl. restart shape · chunk
  bounds + reassembly · error-still-closes · uniform/varied/discipline/
  empty verdicts · DIAG-002 both branches · v1 mute with full cell + era ·
  v2 buffered bar admits and stamps · cell-row era stamp · boot line both
  modes · pack section + daily_pack render); CHECKS_DIAG1 (answers
  delivered, eras stamped, section ships) — tenth graded suite; registry
  #21 "The phone is the console" (21 KNOWNs, all bound).

## WO-P27-FINAL — "THE GOVERNOR IS THE HALT" (the constitution as written)

Suite at this commit: **411 passed**; preflight **23/23**.

- **Compare verdict CONFIRMED at source, and extended**: every cited governor
  was live at its cited line. The kill-list grew by its own class-members —
  the same rule wearing different badges: fh8 Wall 3c (F kill) AND H8's
  per-lane kill, probe-kill (3-losses-before-win), and daily probe budget;
  FLIP's stop-streak kill (the port doc itself called it "the per-lane kill
  rule, R2"); the gateway's sizing-tier wall (the tier governor's
  enforcement arm — with it alive, full Kelly would have died at the wall).
- **§1 sizing = full Kelly**: `size_order(book, price, depth)` =
  min(kelly, depth, NET_RISK_CROSS_LANE_CAP) — the risk-cap clamp because
  the kept walls are LAW: a 7-lot Kelly proposal dying whole at the ≤3 wall
  would be a governor by accident (caught by the replay corpus). The tier
  ladder REMAINS as reporting: tier_for still scores/pages/persists; the
  order's size_tier is the reporting stamp (custody scaling reads it);
  RULING 3's depth floor stands (depth doctrine, not a governor). All
  reporting stats keep accumulating (hourly exposure, lane losses, probe
  ledgers) — they inform the packs and govern nothing.
- **§2 whys report, doctrine gates**: (a) TABLE_NON_REVERSAL gate DELETED —
  surv still computes and prints (`surv0.93 (any-touch, info) d= t=
  proof=`); returns as a gate only by Drew ruling with v2 at-close units.
  (b) OPEN's margin gate + OPEN_CELL_NEGATIVE sit DELETED — entry proceeds
  on band + grain + geometry + one-shot; margin prints either way. (c)
  REJECT_UNPROVEN_WHY relaxed to the NARRATION LAW: non-empty why string,
  never a threshold (PROOF_REQUIRED per-lane demands retired; the lanes
  still print their arithmetic because the packs learn from it). (d) the
  keep-list verified untouched: SELF_NET · TAKER_ENTRY · pending/gross ·
  net-risk ≤3 · $-at-risk · BUDGET · OPEN one-shot/geometry/yield/fast
  evacuations · HUNT one-per-displacement · all custody.
- **§3 the one governor, confirmed**: the mixed-lane strike test (the
  Adversary's requirement) — two lanes trade one window, F +4 / OPEN −10 →
  net −6 → EXACTLY one strike; two consecutive mixed-red windows → halt;
  persists across restart; /reset_halt only. And the mirror: OPEN −5 but
  F +12 → net green → zero strikes. Lane-blind both directions.
- **§4 era stamp**: cell_outcomes gains `governor` column, every row from
  this deploy stamped `halt-only`; boot prints the constitution
  (`GOVERNOR: halt-only — … NOTHING stops trading`).
- **Golden tape**: declared change D2 (same class as D1) — where the
  vendored LIVE tree stops on a retired governor code, the relay
  intentionally proceeds; everything else stays byte-parity (26 ladder +
  2941 eval cases, 0 mismatches; report regenerated). The dry run now
  places 2 lots at 97¢ — §5's tape, proven end-to-end.
- **§5**: CHECKS_P27 (kelly-only sizing, governors dead in source AND on
  tape, streak+reset alive, era stamped) — eleventh graded suite; registry
  #22 "The governor is the halt" (22 KNOWNs, all bound); overturned
  test-laws updated with citations (tier caps, tier wall, stop-streak
  kill, proof-field wall, margin gate, TABLE_NON_REVERSAL mute, 1-lot
  expectations in the dry run/port wiring/sizing line).

## WO-FLIP-COUNT-1 — "THE ORPHANED SECOND CONTRACT" (hot fix, one commit)

**Root cause (read-rule TRUE at source):** `note_fill` popped the
`w.posted[side]["mode"]` marker on the FIRST fill; a SECOND same-side fill
fell through to the legacy `w.fills` rung-A dict, whose only exit hardcoded
`count=1`. Today's tape: OPEN bought no@48¢ ×2, sold ×1 (+5¢), the
surviving ×1 rode to $0 (−$0.43).

- **§2.1 the merge**: a same-side fill with an existing HUNT/OPEN custody
  bucket MERGES — count-weighted blended entry (Engineer: the take math
  stays honest), resting take cancelled, `take_proposed` cleared so
  custody re-proposes at the merged size. Never routes to `w.fills`.
- **§2.2 booked-size exits**: `_booked_held` reads the ledger's unsettled
  ENTRY−exit counts per (market, FLIP, side) — `None` when no ENTRY rows
  (no truth to clamp to; memory governs, ledger-less unit paths honest).
  The rung-A take sells booked size, never a literal 1; every custody
  exit (hunt take/bail/breakeven, OPEN take/yield/determined) sells
  `max(0, min(memory, booked))` (Adversary) — zero proposes nothing and
  marks the bucket done. `note_exit` gains `count`: realizes ×count,
  decrements, pops only when depleted; runner passes fill count into
  BOTH `note_fill` and `note_exit`.
- **§2.3 FLIP_UNCOVERED_LEG**: per cycle, a side whose booked-held
  exceeds the covered resting-exit counts — with no exit proposed this
  cycle — writes a failures row and pages, ONCE per (market, close_ts,
  side), with bucket provenance (Scientist: which dict lost the
  contract). Take COUNTS register beside oids at `on_submitted`.
- **§5 scope guard**: no bands, gates, take cents, ratchet trips, or
  sizing tiers touched — custody routing and exit sizing only. Boot
  PROFILE gains the P-FLIP-COUNT-1 line.
- **§3**: `tests/test_flip_count1.py` — 9 tests (merge OPEN + HUNT,
  LEAN ×2 single fill, rung-A booked size on today's tape shape, clamp
  to booked, zero-clamp no-order, full/partial note_exit realization,
  uncovered-leg pages once with provenance). Suite 434 · preflight 23/23.

## WO-CASH-FATAL-1 — "THE DENY-CASH REBOOT BREACH" (governance hot fix, one commit)

**Incident (2026-07-19 ~09:44-09:56, read-rule TRUE at source):** a
`/deny_cash` integrity FATAL lived only in memory (`CashProtocol.__init__`
hardcoded `fatal=False`; `_go_fatal` wrote nothing; the runner builds a
fresh protocol every boot). Each restart re-baselined the disputed cash to
the venue and traded again — three deny → reboot → rebuy cycles. Upstream:
a 99¢ WINDOW_ECON_DIVERGENCE phantom entered the book via a settlement
value the fills could not reproduce. **Widened during the build (Adversary
was right to be strict): `cash.entries_halted`/`fatal` were read by NO
code path at all — the "entries HALTED" prompt was narration; the gateway
wall never heard about any cash stop, reboot or no reboot.**

- **§4.1-§4.2 durable + restored**: `_go_fatal` writes `cash_fatal` to
  engine_state the moment it fires (the two-strike halt's proven pattern);
  the negative-delta prompt writes `cash_pending` (Adversary: a reboot
  mid-prompt resumes PROMPTED, never trading; the 30-min silence law
  counts wall-clock across the restart). `restore_on_boot()` mirrors
  `window_econ.restore_halt_on_boot` and both stops now halt the GATEWAY
  WALL (`CASH_FATAL` / `CASH_PROMPT` reasons) — enforcement, not narration.
- **§4.3 deny outranks baseline (Engineer: the ORDER is load-bearing)**:
  `ShadowEngine.boot()` restores cash stops as its FIRST act, before any
  baseline; `live_boot_reconcile` refuses to baseline under a restored
  fatal (`FATAL_RESTORED`) — a crash loop can no longer launder a disputed
  delta to zero. Custody of existing risk continues (halts stop new risk).
- **§4.4 the only key**: `/clear_cash_fatal` (COMMANDS whitelist grows by
  exactly one; symmetric with `/reset_halt`). Clearing is NOT accepting:
  no re-baseline happens; a persisting delta re-prompts from scratch.
- **§4.5 all-stops boot audit**: `audit_durable_stops()` — one boot
  assertion that every persisted stop (two-strike, cash-fatal,
  cash-pending) is loaded AND honored on the wall before the first cycle;
  any stop the DB knows and the wall doesn't → STOP_AUDIT_FAILED, FATAL
  loud rather than trade. Closes the class, not the instance.
- **§4.6 DIVERGENT quarantine (stopgap until E1)**: settlements gain a
  `divergent` flag excluded from `book_cents`/lifetime; on
  WINDOW_ECON_DIVERGENCE the window's settlement rows are tagged and the
  window re-books at fills-truth, paged with the removed phantom. A
  broker number the fills can't reproduce never again silently inflates
  the book that cash reconciles against.
- **Out of scope honored**: zero changes to F/FLIP order placement, bands,
  gates, sizing (the orders were valid; the gate was missing). E1
  (99¢ root cause) and E2 (reboot provenance) remain routed to Saturday.
- **Tests**: `tests/test_cash_fatal1.py` — 10 (deny persists → reboot
  restores → wall refuses → /clear_cash_fatal only; the exact 3× loop
  dead; pending prompt survives + confirm still works + expiry across
  reboot; live-boot refuses baseline under fatal; stop audit honors and
  FATALs; divergent quarantine + clean-settlement no-op; command routing).
  COMMANDS-tuple test-laws updated with citation. Suite 444 · 23/23.

## WO-VERIFY-LOSSTERM-1 — verification + legibility only (4 commits, no behavior change)

Governing doctrine (Drew 2026-07-19): one goal — maximize EV to grow the
live book, no deposits; safety mechanisms are the honestly-computed
loss-term, nothing governs upside. **Hard rail honored: zero changes to
KELLY_FRACTION_CEILING, DEPTH_FRACTION, NET_RISK_CROSS_LANE_CAP,
LANE_D_FLOOR_CENTS, any band, tier bound, or gate.**

- **B1** salvage self-test at every boot (throwaway in-memory ledger):
  adoption must land SALVAGE_ARMED (anchor values) or
  SALVAGE_DISABLED_TAGGED (named reason) and the catastrophic backstop
  must fire anchorless — unreachable → SALVAGE_SELFTEST_FAILED, FATAL
  before the first cycle. Sabotage-tested (a verification that cannot
  fail cannot verify).
- **B2** flip coverage verified: full 2-lot same-side OPEN lifecycle
  (fill → resting take → second fill → merged 2-lot re-take → 2-lot
  exit) with ZERO FLIP_UNCOVERED_LEG pages; forced held-2/covered-1
  still pages exactly once. **The 12:18/191230 live firing root-caused
  and encoded as a test: FlipWindow custody is in-memory — the
  FLIP-COUNT-1 deploy's own restart orphaned the lane bucket
  (buckets=none in the row), the merge itself holds. Custodian owns the
  risk via boot adoption; lane re-hydration is a future order.**
- **B3** cash-fatal engine-level: deny → full engine reboot → restored
  page + cash-fatal=HONORED on tape + book byte-identical (no
  re-baseline) + wall refuses ENTRY by name; pending-prompt reboot stays
  PROMPTED, consent still works; two-strike untouched and green.
- **B4** legibility (pure logging): the SIZING line states the binder in
  words (`kelly-bound: 1 lot @97¢ (0 @98¢) — throttle is book size, not
  a wall; self-scales ~$24→2 @97¢, ~$35→3` — all computed from live
  constants; the ~$35 differs from the order's ~$36 example because the
  printed number is 97¢ arithmetic, not 98¢); SIZE_ZERO_BY_KELLY logs
  once per (market, price) when Kelly zeroes a favorite, count=1 walls
  path unchanged. P14 sizing-line test-law updated to prefix-match with
  citation.
- Suite 456 · preflight 23/23. Out of scope, untouched: edge
  measurement (step 2), Kelly re-derivation (step 3), D-floor (data).

## WO-FLIP-COUNT-2 — "THE LAST LOSS-TERM LEAK" (custody-routing only, one commit)

**Root cause (191030 tape, read-rule TRUE at source):** not a missing
merge — a fill-vs-exit RACE. A 2-lot entry the venue fills as two 1-lot
records can have a determined-against exit fire BETWEEN the halves; the
merge then re-incremented a record whose exit had already fired, leaving
the second contract invisible to every exit (two 1-lot exits +
FLIP_UNCOVERED_LEG "held 1 > covered 0").

- **§3.1 quiescence gate**: `_entry_in_flight` reads venue truth (order
  still in `gateway.resting` with `filled_counts > 0`); every custody exit
  DEFERS while the entry is partially filled. Adversary(b): after
  FLIP_STUCK_PARTIAL_POLLS=3 unresolved polls the remainder is CANCELLED
  and the position exits at booked size — FLIP_STUCK_PARTIAL pages (fail
  toward a known state, loud).
- **§3.2 idempotent late merge**: a fill landing on a CLOSING (done)
  bucket never re-increments it — it buffers in `w.late_fills` (blended)
  and `FLIP_LATE_FILL_REOPEN` opens a fresh position-aware record the
  moment the old leg's exit accounting concludes (note_exit pop), clamped
  to booked-net (a venue that holds nothing opens no leg). The reopened
  record carries the fill's own entry (Engineer: the loss-term geometry —
  take/determined/yield — stays armed on the reopened leg).
- **§3.3 UNCOVERED self-heals**: the page stays loud AND the gap is
  covered — the closed record revives (own entry keeps realization
  honest) or a fresh record opens at the ledger's booked entry, sized to
  held−covered; the normal exit path owns it next cycle. Adversary(c):
  cover ONCE — a leg still uncovered after its heal is
  FLIP_UNCOVERED_UNHEALABLE, FATAL. A live record whose take is merely
  pending proposal is the normal path, never a false page.
- **§3.4**: `_exit_count` (booked-net clamp ≥0) confirmed authoritative on
  every exit including the reopen and heal paths — phantom sells
  (the 9:27 REJECT_SELF_NET class) impossible.
- **HARD RAIL honored**: no change to KELLY_FRACTION_CEILING,
  DEPTH_FRACTION, NET_RISK_CROSS_LANE_CAP, LANE_D_FLOOR_CENTS, any band,
  tier bound, take-cent, or gate. Boot PROFILE FLIP line → P-FLIP-COUNT-2.
- **§4 tests** (`tests/test_flip_count2.py`, 9): the 191030 replay (two
  1-lot fills straddling determined → defer → merged → ONE 2-lot covered
  exit, zero pages); late-fill buffer → REOPEN → position-aware exit,
  zero orphan; reopen clamps to booked (no phantom); single 2-lot fill
  regression; two-fills-no-exit regression; uncovered pages AND heals
  covered-next-cycle; uncovered-after-heal FATALs; stuck partial defers
  then cancels loudly; exit never exceeds booked-held. Suite 465 · 23/23.

## WO-FLIP-THESIS-1 — "SCALP-OR-HOLD + F COORDINATION" (4 staged commits)

**RAIL EXCEPTION honored as written**: ONLY the §3-named constants moved
(OPEN_TAKE_CENTS 5→20 · OPEN_ENTRY_CUTOFF 480→600 · OPEN_FLAT_BY 360→600
with §3.5 semantics · OPEN_PATIENCE_S=300 new); Kelly, depth, net-risk,
and every integrity path untouched. **CEO condition standing: strategy
change — proves itself at one lot before any size.**

- **Stage 1 — the patience floor (§2)**: determined-against fires only
  after OPEN_PATIENCE_S from FIRST FILL (Engineer: the merge keeps the
  first fill's ts). First-minute dips are noise (the 48→41 −9¢ evacuate
  at 9s is dead); a sustained collapse counts through the window and
  fires the moment it ends; post-window the cut stays HARD.
- **Stage 2 — the retune (§3)**: scalp target entry+20 (fee = 10% of
  edge, was 40%; OPEN cell breakeven now BELOW coin-flip:
  (12+fee)/(32+fee)≈0.40); entries T-15→T-10.
- **Stage 3 — T-10 handoff + coordination (§3.5/§4)**: blind YIELD_TO_F
  replaced by the book-aware per-position handoff — winner converts to
  hold-to-settle at FLIP's basis (FLIP_HOLD_TO_SETTLE), loser SOLD
  before F's window, no mark = retry (never blind), flat hands off
  nothing. Hold-instead-of-scalp pre-T-10 when the mark crosses F's
  band floor (F-agrees). Holds stay under the determined floor +
  custodian backstop (anchored at fill, B1) and are exempt from the
  UNCOVERED page (deliberate no-resting-exit). Shared inventory:
  `LaneFlip.held` wired to `FH8Shared.flip_inventory` (one object; F
  evaluates first, fills book between cycles — the atomic cycle-start
  snapshot). F_STANDS_DOWN pass + once-per-(market,side) log with
  basis/would-pay. **Read-rule finding: no main-loop rule ever forbade
  multi-lane same-market entries (SINGLE_ENTRY is lane-scoped;
  NET_RISK≤3 is the only cross-lane brake) — F could double-buy
  blindly; coordination replaces blindness, not a ban.**
- **Stage 4 — continuity (§1, Scientist)**: OPEN_CONTINUITY logs
  prior-window direction vs chosen side, once per window, LOG-ONLY —
  votes only after measurement shows edge.
- **Overturned test-laws (with citations)**: P21/P26 yield tests → T-10
  handoff; determined tests re-timed past the patience floor; OPEN
  breakeven above-coin-flip assertion inverted; geometry gate dormant at
  the ruled take (both knobs shifted in-test to keep the mechanism
  exercised); entry-window times moved inside T-15→T-10; PROFILE tokens.
- Suite 479 · preflight 23/23. UNPROVEN, routed to measurement:
  continuity edge; 20¢-vs-5¢ superiority (the one-lot tape decides).

## THE A-PLAYER DOCUMENT (build 33) — the machine that doesn't need watching

**Doctrine (banked):** every dead run died to operator intolerance of a
single loss, not to the market — so the fix removes the hand: the book
speaks the venue's units, noise re-baselines silently, only a loss-RATE
halts, size is the premise (Drew's dial), and exit thresholds never read
their own P&L. **Staged as the Adversary mandated: B1-B3 (overnight
survival) committed and proven standalone, then B4-B5.**

- **B1 half-cent precision** (root cause proven: `int(round(cost))` at
  the fills booking truncated the venue's half-cents into the day's
  phantom 1-2¢ deltas): the book stores exact cents ('0.9650' → 96.5,
  round-trip asserted; INTEGER affinity keeps old rows valid — no
  destructive migration); sums round ONCE so residues cancel.
- **B2 silent small re-baseline**: `CASH_SILENT_REBASE_CENTS=5`
  (DREW-DEFAULT) — noise deltas re-baseline silently (SILENT_REBASE row,
  log only, zero pages); above the bound the WO-CASH-FATAL-1 machinery is
  byte-identical. The overnight book runs untouched.
- **B3 the rate halt** (OVERTURNS two-consecutive-strikes): 2-of-last-4
  settled traded markets negative → halt (per-market BROKER P&L is the
  unit; settled-only). One loss NEVER halts; a win no longer forgives
  (the rate rolls — red-win-red halts; test_win_resets_streak overturned
  with citation). Persistence/custody//reset_halt unchanged; wall reason
  → RATE_HALT; consecutive streak reports as info.
- **B4 the fraction is Drew's dial**: `KELLY_FRACTION_CEILING` reads the
  KELLY_FRACTION env (default holds 1/12 until Drew turns it — code
  never chooses the fraction); boot SIZING line states the live value;
  Kelly/depth/net-risk math untouched.
- **B5 the P&L-blind cut**: the determined trigger is the UNDETERMINED
  band floor + the table's ΔP-collapse — never the entry basis (the
  basis-anchored entry−6 trigger retired; `OPEN_DETERMINED_DROP` now
  feeds only the dormant geometry law). Acceptance: up-5 and down-5 with
  identical book/table/time state get the SAME decision (hold at 48,
  cut at 34). The take stays pre-committed at entry+20 from fill time —
  never moved by unrealized P&L. Determined law-tests re-marked to the
  band floor with citations.
- **HARD RAIL held**: genuine-dispute cash-FATAL, salvage loss-term,
  flip-cover custody untouched and green. Suite 490 · preflight 23/23.

## WO-FLIP-CHEAP-LIVE — prove the swing, live, measured (build 34, one commit)

**Drew's ruling:** cheap-entry FLIP ships LIVE at one lot; the 2-of-4
rate-halt is the backstop; the live proof MEASURES instead of guessing.

- **§2.1 band** (DREW-RULED): OPEN_BAND (44,56) → (39,56). Upper bound
  kept per ruling — noted for Drew: a tight-spread 39/59 book stays
  refused by the 56¢ complement bound; only wide/uncertain books admit
  the 39¢ side.
- **§2.2 two-sided swing gate**: p_cross(|spot−strike|, t_rem) ≥
  OPEN_SWING_MIN_P=0.55 (DREW-DEFAULT, permissive — live-proof wants
  data). Apples-to-apples per the Engineer: the take REQUIRES the
  strike-touch and every cut-first path is a no-touch path, so ONE
  p_cross cell answers both legs; ≥0.55 > 0.5 ⇒ P(reach take) >
  P(reach cut) by the no-touch bound. Refusals log OPEN_SWING_REFUSED
  ("losing-cheap, not oversold-cheap"); the gate value narrates on the
  why (`swing p=0.72` / `swing~untabled` when the table is absent —
  permissive, already UNPROVEN-tagged by P26).
- **§2.3 floor confirmed**: the B5 P&L-blind band-floor cut is the
  salvage floor — test-proven to cut the non-swinger at 34¢ (−5 on a 39¢
  entry; the −15¢ region at a 49¢ entry), never a −39¢ ride.
- **§3 Instrument 1 — FLIP_SWING**: every cheap OPEN entry logs at
  CONCLUSION (exit, or hold-conversion with held_to_settle=true):
  entry/exit/gross/took_swing/salvaged/secs_to_swing. The pack gains
  `FLIP SWING (24h): rate a/b (x%) · avg win · avg salvaged ·
  net/market · floor breaches` — 20-30 rows replace the guessed 80%.
- **§3 Instrument 2 — FLIP_LOSER_CUT** (CEO: mandatory): every loser
  audits ok = loss ≤ (entry − band floor) + FLIP_FLOOR_SLIP_CENTS=5; a
  ride past the floor writes ok=false AND pages FLIP_FLOOR_BREACH on
  the FIRST loser — the EV-inverting assumption, flagged before the
  rate-halt could see a streak.
- **RAIL held**: Kelly untouched (one lot at current book — the
  Adversary's one-lot-until-measured is structural: sizing waits on
  Instruments 1+2), cash-integrity B1/B2 untouched, rate-halt B3
  untouched (asserted in-test). Suite 501 · preflight 23/23.

## WO-BLEED-DIAGNOSIS — the two live bleeds (build 35, one commit; §3 RESERVED)

The rate-halt diagnosis working as designed: OPEN swing PRINTED (+20/+12,
keep), Instrument 2 flagged the floor slip on the FIRST bad loser, the
halt bounded the damage. Two bleeds fixed, one ruling surfaced with data.

- **Bleed 1 — HUNT averaging down (read-rule TRUE: the 20→13→8 tape;
  `join >= pend["last_cost"]` only checked frame-to-frame pauses)**: one
  direction per window (locked at the first hunt's submit); re-entry at
  or below the prior HUNT entry refused (Adversary: <=), logged
  HUNT_REFUSE_LOWER once per window (Engineer: compared to the prior
  ENTRY price, never the mark — an above-prior re-displacement is a
  recovering needle and stays legal); one HUNT loss sits the window out.
  OPEN untouched (tested: posts after a hunt loss). Fewer re-entries =
  fewer FLIP-COUNT-2 race windows (§2.3 verified at the test level; any
  live UNCOVERED that remains is the partial-fill race, not this).
- **Bleed 3 — the −25¢ floor slip (Instrument 2's measurement)**: root
  cause was the PATIENCE GATE holding the through-floor cut during a
  collapse and firing at the bottom. Fixed: a book SUSTAINED through the
  band floor (2 polls) is a DECISION and cuts NOW, any minute; a
  one-frame flicker holds (counter resets); the ΔP-collapse leg (a spot
  signal that can flicker early) stays patience-gated. The cut lands AT
  the floor (loss 14¢ on a 48¢ entry — Instrument 2 audits ok=true).
  EV note: at the restored floor the §1 break-even stays ~55%.
- **§3 F passthrough — RESERVED to Drew, no F code**: pack gains
  `F TAIL (§3 ruling data): markets · wins avg · tails avg ·
  decided-against rate (passthrough breaks even ~95%+ win-rate)` from
  settled per-market F P&L — the ruling decides from this number, not
  from one −93.5¢. If B: `salvage_enabled` for F, shadow-first.
- **HARD RAIL held**: OPEN swing gate, Kelly, cash-integrity, rate-halt
  untouched. Suite 511 · preflight 23/23.

## WO-UNCOVERED-FLATTEN — the naked leg Drew watched (build 36, one commit)

Drew watched an uncovered FLIP leg ride 45→70→settlement doing nothing
(192145, −$0.45). Root cause (read-rule TRUE): `_heal_uncovered` added
the side to `uncovered_healed` on INTENT — reviving the record created
the intent to cover but guaranteed no resting exit ORDER landed; every
cover self-net-rejected into the void (the WALL_STORM n=21), the engine
believed it healed, and the leg rode bare.

- **§2.1 heal ≠ healed until CONFIRMED**: `covered` counts only confirmed
  resting exits (take_oid / takes_posted / flatten_oids); a leg clears
  only when `held ≤ covered`. `heal_grace` gives a cover attempt up to
  FLIP_HEAL_GRACE_CYCLES (2) to confirm (a determined CUT books via
  async fill; a maker take rests); a confirmed cover raises `covered` and
  resets the clock.
- **§2.2 self-net reconcile**: past grace, `_reconcile_side` clamps every
  custody record to the booked ledger (a phantom gap — broker says
  smaller — dies here, nothing to cover) and cancels every conflicting
  resting FLIP sell (the stale-order artifact behind the self-net storm)
  so a fresh cover can land; then ONE retry.
- **§2.3 cover-or-flatten deadline**: cover still unconfirmed after
  reconcile + retry → FLATTEN at market NOW (crossfire, booked-net
  clamped per the Engineer so it can't re-self-net), FLIP_UNCOVERED_
  FLATTENED paged — a bounded loss beats an unbounded ride.
- **§2.4 flatten-before-fatal**: the flatten confirming (on_submitted →
  flatten_oids) holds the FATAL off; FLIP_UNCOVERED_UNHEALABLE fires only
  when the flatten itself never registers a close — never FATAL with a
  naked untried leg.
- **Escalation**: one stage per cycle (detect+reconcile+heal → retry →
  flatten → fatal); `heal_attempts` drives it, a booked exit resets the
  clock (a reopened leg deserves fresh grace).
- **Overturned test-law (with citation)**: FLIP-COUNT-2 §3.3's
  heal-once-then-FATAL became flatten-first; the pages-and-heals test now
  covers the WHOLE reconciled leg (the stale ×1 take is cancelled).
- **HARD RAIL held**: cover/heal path only — Kelly, cash-integrity,
  rate-halt, HUNT (BLEED-1), OPEN-swing gate untouched (asserted
  in-test). Suite 518 · preflight 23/23.

## WO-HALT-ORPHAN + OVERNIGHT NOISE (build 38, one commit)

Drew found the engine frozen at $35.94 for 25+ min: `/reset_halt` said
"no halt active" while every proposal was WALL_REJECT[ENTRIES_HALTED]
ORIENTATION_DIVERGENCE. Root cause (read-rule TRUE): two halt producers
(rate + orientation) share one gateway reason SET, but `reset_halt`
cleared only `HALT_REASON` and its guard read only the rate DB flag — a
durable stop with no operator key and a status light lying green.

- **§1.1 the key clears ALL reasons**: `gateway.resume_entries_all(keep=)`
  clears the whole `entries_halted_reasons` set except reasons with their
  own lifecycle (cash-fatal/prompt → /clear_cash_fatal; DEGRADE_LADDER →
  auto-resumes on WS_LIVE — the Engineer's persistent-reasons guard).
- **§1.2 the status tells the truth**: `reset_halt`'s guard reads the
  gateway SET, not `self.halted()` alone — an orientation-only halt no
  longer reports "no halt active"; the reply names the reasons cleared
  and any still held by their own key.
- **§1.3 orientation auto-heals**: a live ORIENTATION_DIVERGENCE stamps
  its market; `process_divergence_watches` re-pulls a FRESH record each
  cycle and `resume_entries("ORIENTATION_DIVERGENCE")` the moment it
  reads clean (≤3¢) — a transient staleness halt self-heals; /reset_halt
  is the backstop. A real inversion keeps failing the fresh recheck and
  stays halted (Adversary).
- **§2C fresh-record strike**: a strike counts only against a re-pulled
  FRESH record (`_fresh_record_touches`) — a stale discovery record can
  no longer cast a strike (the 7¢ movement-lag false halt).
- **§2A page the real leg, not its transient**: FLIP_UNCOVERED_LEG pages
  only post-grace (a cover that failed to confirm — the FLATTEN
  escalation); the in-flight transient logs at INFO. (Already post-grace
  after WO-UNCOVERED-FLATTEN; made explicit.)
- **§2B budget reject once/window**: a lane/side/price the BUDGET (or
  NET_RISK/DOLLAR_RISK) wall refused is not re-submitted that window —
  the byte-identical book can't change the wall's verdict; 21 retries →
  1. Cleared at rollover.
- **HARD RAIL held**: rate-halt still persists across boot and needs
  /reset_halt (restore_halt_on_boot unchanged, asserted); cash-integrity
  unchanged; only orientation auto-heals; no Kelly change. Suite 527 ·
  preflight 23/23.

## WO-SWING-GATE-EVENT — the gate measured the wrong thing (build 39)

At 3 lots the FLIP bleed became visible: every OPEN cheap entry showed
`swing p=0.89` and lost anyway (200945 −35¢, 201045 −50¢). Root cause
(read-rule TRUE at lane_flip.py:756): `_swing_gate` computed
`p_cross(|spot−strike|, t)` = P(BTC twitches to a strike already under
its nose) — trivially ~high near 50/50 — NOT P(the contract swings +20¢).
The input distance is structurally tiny for exactly the cheap entries the
gate should filter, so it rubber-stamped falling knives; the constant
0.89 across bands is the symptom.

- **§4.1 (LIVE) gate on the MEASURED event**: the gate now tests
  Instrument 1's rolling `took_swing` rate for the entry's price band
  (`_measured_swing_rate`); ok = rate ≥ OPEN_SWING_MIN_P once
  n ≥ OPEN_SWING_MIN_SAMPLES (20). Below that it is permissive
  (calibrating) and the 1-lot cap bounds the risk — measurement over a
  structurally-wrong model.
- **§4.2 (DREW-RULED 2026-07-20: 1-lot cap, keep trading)**: FLIP entries
  cap at FLIP_SIZE_CAP=1 in `_score_and_size` until the gate tracks
  measurement; F untouched (its survival gate is correct). Bounds the
  3-lot bleed while Instrument 1 keeps accumulating.
- **§2 (SHADOW) the two-barrier price model**: `_shadow_two_barrier` +
  `delta.distance_for_p` translate each CONTRACT-PRICE barrier (join+20
  take, band-floor cut) into the spot move that reprices the contract
  that far (the same table the book prices with, inverted — Engineer);
  p_up vs p_down. Logged per entry (SWING_GATE_COMPARE), drives NOTHING
  until it tracks Instrument 1 (Adversary b: shadow-compare first).
- **§3 calibration**: the daily pack prints old-proxy (~const = the bug)
  vs measured took_swing vs shadow p_up/p_down — the shadow earns the
  wheel only when it tracks measured. Every prior `swing p=0.89` on the
  tape is meaningless (Adversary d) — the old proxy rides on as the
  `old_proxy_p` shadow so the bug stays visible, quantified.
- **HARD RAIL held**: only the swing gate's event definition + FLIP size
  cap; no Kelly/cash/rate-halt/F change (asserted). F-passthrough A/B
  matters more now that F carries more weight (Adversary c) — still
  Drew's open ruling. Suite 536 · preflight 23/23.

## WO-FLIP-EXIT-DOCTRINE — the cut is about the decision, not the price (build 40)

**BANKED — not for immediate deploy** (built, lens-certified, ready when
Drew opens a window). Drew watched FLIP self-exit almost immediately on
nearly every entry (201215: yes@44¢ → cut 35¢ same window, −11¢). Not a
bug — a DOCTRINE GAP: a scalper's stop bolted onto a position trade. Root
(read-rule TRUE at lane_flip.py:1109): `floor_polls >= 2` (~2s) bypassed
the 5-min patience, and the band floor (35¢) was doing two jobs — marking
where swings happen AND where the cut fires — 9¢ below a 44¢ entry, inside
the swing.

**Atomic three-change commit (all together or a known bleed reopens):**
- **Change 1 — separate the floors**: `OPEN_CATASTROPHE_FLOOR=20` (fixed,
  P&L-blind, not basis-anchored — the Engineer's flag) becomes the only
  price cut; the band floor (35¢) stays for swing semantics. A cheap entry
  breathes through its swing; the price backstop lives below it.
- **Change 2 — spot+time primary**: the ΔP-collapse (SPOT decided) leads
  and cuts on 2 sustained polls ANY time; the band-floor price cut is
  demoted. The T-10 handoff (time decided) already leads above.
- **Change 3 — real patience**: the ~2s floor-poll bypass is deleted; an
  in-band/near-band dip inside patience is HELD, however long. Only the
  catastrophe floor and a sustained spot collapse act inside patience; the
  ordinary band-floor cut ("the swing did not come") fires only AFTER full
  patience.
- **Overturned test-laws (with citations)**: WO-BLEED-3's floor-poll
  bypass replay now cuts via SPOT not price; the ΔP leg's patience-gate
  removed (it's primary); the in-band dip holds through patience.
- **HARD RAIL held**: no Kelly/cash/rate-halt change; HUNT's scalper cut
  unchanged (HUNT is correctly a scalper); the swing ENTRY gate untouched
  (EXIT doctrine only). The lower floor is safe only at the 1-lot FLIP cap
  (WO-SWING-GATE-EVENT §4.2) — re-evaluate when FLIP sizes up (CEO).
- **Part D**: 7 acceptance tests, all pass together (dip holds; spot cuts
  any time; ride-to-catastrophe cuts bounded; take still fires; cut reason
  names the decision; band-floor cut only post-patience). Suite 544 ·
  preflight 23/23.

## WO-FLIP-SIDE-ORIENT — the book must invert to the held side (build 41)

**BANKED — not for immediate deploy** (built, mirror-proven, ready when
Drew opens a window). FLIP is two-directional (it buys YES or NO), so its
exit geometry must be oriented to the HELD side: a NO@46 and a YES@46 have
to receive mathematically identical treatment. F is untouched — F needs
only who-wins-at-settlement, so one direction is CORRECT for F.

**READ-RULE FINDING (honest, reported to Drew): the WO's core premise is
FALSE at source for the LIVE path.** The claim — that the exit geometry is
YES-scale and treats NO asymmetrically — does not hold when read at source.
The custody exit already computed `mark = best_yes_bid()` for YES and
`best_no_bid()` for NO (the HELD-side bid), and every threshold (band floor
35, catastrophe 20, entry, take) is compared against THAT mark. So the live
geometry was already side-symmetric **by construction** — the mirror test
(NO@X ≡ YES@X) passes on the pre-existing exit code. This is verified, not
asserted: `test_exit_geometry_mirror_no_equals_yes` sweeps 15 held prices
and the two sides' decisions are byte-identical at every tick.

**What this WO actually delivers:**
- **Formalizes the symmetry**: a canonical `LaneFlip.held_price(side, book)`
  accessor (the bid for the held side) now names the value the exit path
  already read, and the two custody marks (`_hunt_custody`, `_open_custody`)
  route through it — so no raw-YES value can ever leak into FLIP exit math.
- **Fixes the ONE genuine orientation bug** (Part C.3, read-rule TRUE at the
  old `_shadow_two_barrier`): the §2 SHADOW two-barrier mapped a NO
  contract's price straight into P(cross), but the table's p_cross =
  P(spot crosses strike) = P(YES wins), so a NO's implied P(cross) is the
  COMPLEMENT (1 − price/100). A NO@46 was read as P(cross)=0.46 when its
  real implied value is 0.54. The shadow now takes a `side` arg and looks
  up the complement for NO. It drives NOTHING live (logged for calibration
  until it tracks Instrument 1) — but the sign is now correct.
- **The mirror test is the permanent CI gate** (Part D): NO@X ≡ YES@X
  identical exit decisions — swept inside patience, post-patience (band
  floor), and at the T-10 handoff; catastrophe fires at 20¢ on either side;
  a climb to 66¢ is held (not cut) on either side; a mirrored ΔP-collapse
  cuts both. Plus the shadow-sign proof (NO queries the complement) and the
  point-symmetric-table mirror (NO@44 ≡ YES@44 p_up/p_down).
- **HARD RAIL held**: FLIP exit + swing-gate geometry ONLY. F stays
  one-directional (`test_f_stays_one_directional`: no `held_price` /
  `_shadow_two_barrier` in `lane_fh8`). No Kelly/cash/rate-halt change; the
  take (20¢), catastrophe (20¢), band (35/65), and FLIP_X unchanged. Ships
  at the 1-lot cap. Suite 556 · preflight 23/23.

## WO-FLIP-GOAL-TAKE — bank the nickel, don't wait for the swing (build 42)

**BANKED — not for immediate deploy** (built, tested, ready when Drew opens a
window). 201430 caught live: bought YES@44¢, rode to the 20¢ catastrophe
floor, −27¢, FLIP_FLOOR_BREACH firing. The build-41 side-orientation fix was
IN and did not stop it — proving the other half: the take was priced to a
rare +20 swing (64¢ from a 44¢ entry) that almost never fires, so nearly
every FLIP position lived long enough to drift down and ride to the floor.
Reward required the rare event; loss ran to the common one.

**Read-rule finding (reported to Drew): the WO's line citation is
imprecise.** The WO cited `lane_flip.py:795` as "the fixed `join +
OPEN_TAKE_CENTS` take." At source that line is the **shadow** two-barrier's
take *barrier* (calibration only — drives nothing live). The **live resting
take that actually rested at 64¢ on 201430 and never fired** is in
`_open_custody` (`o["entry"] + OPEN_TAKE_CENTS`). The fix's primary target is
the live take; the shadow barrier and the took-detection were routed through
the same helper for consistency.

**The fix (DREW-RULED 2026-07-20 via AskUserQuestion — `WINDOW_BOOK_GOAL_CENTS
= 5`, "bank the nickel NOW"):** the resting take floats to the reliable
convergence move, bounded to the per-book goal:
- `take_cents = clamp(ceil(WINDOW_BOOK_GOAL_CENTS / booked-held), OPEN_TAKE_MIN,
  OPEN_TAKE_MAX)`, added as `LaneFlip._take_cents` / `_take_target`.
- New constants (all DREW-ruled/defaulted): `WINDOW_BOOK_GOAL_CENTS=5`,
  `OPEN_TAKE_MIN=5` (fee-safe: clears the ~2¢ round-trip taker fee, nets ~+3¢),
  `OPEN_TAKE_MAX=20` (= today's fixed take).
- **At the 1-lot cap the take rests at entry+5** — a move convergence gives
  all day — so the position EXITS on a win instead of riding to the floor.
  As FLIP sizes up, the per-contract take shrinks toward MIN and volume
  carries the goal (4 contracts × 5¢ = 20¢ clears the same book goal as one
  +20). Sized to **booked-held** (Engineer's flag), recomputed on a second
  fill (the merge cancels and re-proposes at the new size).
- **Instrument 1 aligned**: `_log_swing_outcome`'s `took_swing` now measures
  the reachable target (`OPEN_TAKE_MIN`), so the measured hit-rate rises where
  the +20 bar rarely printed (§4 SCIENTIST — the reachable take is measurable).
- **Overturned test-laws (with citations)**: `test_scalp_take_rests_at_entry_
  plus_20` → `..._at_the_goal_bounded_move` (the +20 was the rare event that
  rode to the floor); `test_open_take_posted_after_fill`,
  `test_patient_hold_ignores_wiggles`, and the FLIP-COUNT-1 merge take all
  re-anchored to the goal-bounded price.

**HARD RAIL held**: TAKE only. The cut (spot-decided, catastrophe floor,
patience) is UNCHANGED — a non-converging loser still cuts by the exit
doctrine (test proves it). The entry geometry gate and cell scoreboard keep
`OPEN_TAKE_CENTS=20` as the notional reward ceiling (a deliberate scope
boundary — flag for a future WO if Drew wants entry admission re-tuned to the
goal-bounded reward). HUNT's scalper take (`HUNT_TAKE_CENTS`) untouched. No
Kelly/cash/rate-halt/F change. Ships at `FLIP_SIZE_CAP=1`. **Part D**: 12
acceptance tests (the clamp; 44→49 core; orientation mirror; size-shrink;
recompute-on-fill; cut-unchanged; instrument; fee floor). Suite 568 ·
preflight 23/23.

## HARD STOP honored

Chunks 5 (demo verification), 6 (shadow-lane promotion), 7 (cutover) NOT built — separate
orders at Drew's word. Standing input requests: ~~B1~~ **DELIVERED** (gate 5 green),
**B2** (legacy CSVs → unblocks §E), **charter + lens verbatim texts** (→ closes the
gate-1 placeholder). Remaining open action: deploy the shadow to Render (or any
Kalshi-reachable environment) to start gate 7's 24-hour tape.
