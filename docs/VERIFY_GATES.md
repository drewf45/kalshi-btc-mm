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

## WO-FLIP-GEOMETRY-COHERENCE — make the four numbers agree (build 43)

**BANKED — halt stays until Drew resets it.** The rate-halt (2-of-4) fired and
its lens pass started as a one-liner (WO-GEOMETRY-GATE-STALE: the entry
geometry gate checks the retired +20 take). Read-rule at source found the
gate was stale on **BOTH** sides, and underneath, the four FLIP numbers did
not describe one trade.

**The finding (both sides stale + the structural root):**
- Gate stale on the TAKE (`lane_flip.py`): checked `risk > OPEN_TAKE_CENTS(20)+1`,
  but the real take is the goal-bounded `_take_cents` = 5.
- Gate stale on the RISK: computed `risk = join − band_floor(35)`, but the
  **bail audit** (driving a losing position down, no spot signal) proved a
  loser rides to the **catastrophe floor (20)** inside patience, not the band
  floor. A 44¢ entry's honest risk was `44 − 20 = 24¢`, not 9¢.
- **Structural root**: WO-FLIP-EXIT-DOCTRINE (build 40, patience → ride to 20)
  and WO-FLIP-GOAL-TAKE (build 42, 5¢ nickel) describe **different trades** —
  risk 24 to win 5 is structurally negative, and that IS the churn that
  tripped the halt. No constant-swap fixes it: a fix to the take side alone
  closes the lane (only join≤41 passes, rejecting the 44–49¢ thesis); a fix to
  both sides honestly rejects everything. The gate correctly reported the
  GEOMETRY ITSELF was incoherent.

**DREW-RULED Option B (SCALP), via AskUserQuestion:** keep the small take, add
a matching **tight stop** so the four numbers form one ~1:1 trade.
- New `OPEN_SCALP_STOP_CENTS = 6`: a loser exits at `entry − 6` (2-poll
  sustained, ANY time), added as the primary loss-side cut in `_open_custody`
  (`scalp_polls`). **Patience-to-catastrophe is OFF on the loss side** —
  build-40 removed the tight stop only because a +20 take needed room; the 5¢
  take does not, so the tight stop is coherent again.
- The geometry gate validates `risk (= join − real_bail = OPEN_SCALP_STOP_CENTS)
  ≤ take+1`, so the whole 39–49¢ thesis band passes by construction (6 ≤ 6).
  The gate now enforces a coherent trade instead of admitting risk-24-win-5
  churn; log prints the real take and real bail.
- The catastrophe floor (20) and band floor (35) survive as deeper /
  post-patience backstops; the take, hold-to-settle, and T-10 handoff (the
  **winner side**) are UNCHANGED.

**Overturned test-laws (with citations)**: the build-40 loss-side-hold tests
are rewritten to the scalp reality — `test_44_entry_dips_to_34_holds` →
`..._dip_below_stop_cuts_scalp`; `test_cut_reason_names_the_decision` and
`test_band_floor_cut_only_after_patience` (now an entry-39 backstop case) in
the exit-doctrine suite; the two bleed-diagnosis in-band-hold tests →
scalp-cut / spot-cut; `test_bad_geometry_passes` now forces incoherence via
the scalp stop (the real risk lever), not the inert `OPEN_TAKE_CENTS`.

**HARD RAIL held**: only the NUMBERS changed to make the geometry coherent —
the gate CHECK was never loosened to admit risk>win. No Kelly/cash/rate-halt/F
change (`test_f_untouched`: no scalp geometry in `lane_fh8`). **Part D**: 9
coherence tests (thesis band passes; incoherent stop rejects; real bail matches
the risk denominator; the 44¢ churn is bounded to ~6 not 24; within-tolerance
holds). Suite 577 · preflight 23/23. **The halt stays in place until Drew
resets it** — this build is banked, not deployed.

## WO-FLIP-LIQUIDITY-HOLD — post and wait, don't react (build 45)

**BANKED — halt stays until Drew resets it.** Drew's reconception of the FLIP
lane: a low price after a FLIP buy is **ILLIQUIDITY** (the opening pile-in,
nobody buying your side yet), **NOT a losing position**. The job is to buy the
inventory the panic is dumping, POST the take (entry+5, already resting), and
HOLD as the liquidity provider until the reversion lifts it — managing only
the endgame if unfilled. This replaces the reactive exit (the build-43 scalp
stop, which fired during the exact early illiquidity the model must hold
through).

**Read-rule findings:**
- The resting take is already built (`_open_custody`, TRUE at source) — that
  IS the "post entry+5 and wait" mechanic (§1). Confirmed the only early exit.
- The reactive stop to remove: the build-43 scalp stop (entry−6, any-time)
  and the post-patience band-floor cut (TRUE at source) — both sold inventory
  during illiquidity (§2).
- **Cross-cutting consequence the WO did not name (reported):** the build-43
  risk/reward geometry gate was *grounded on the scalp stop* as its "real
  bail." Removing the stop un-grounds the gate — an honest risk-to-catastrophe
  check would close the lane (24>6), contra the hold-through-illiquidity
  thesis. So the gate is retired too: the liquidity model's entry filter is
  band membership, and the empirical reversion rate (§4) is the real gate.
  (This follows the standing coherence doctrine — never leave a gate
  validating a bail that no longer fires.)

**What shipped (the NOW-piece):**
- **Removed** the reactive loss-side stops from `_open_custody`: the scalp
  stop and the band-floor early cut. The constant `OPEN_SCALP_STOP_CENTS` and
  the `scalp_polls` tracking are gone.
- **Removed** the risk/reward geometry gate (un-grounded by the stop removal).
- **Kept** the collapse backstop (Adversary-mandated §2.3): SPOT-decided
  sustained (2 polls) and the CATASTROPHE floor (20) still cut even in the
  passive hold — a real move, not illiquidity noise.
- **Kept** the resting take (the exit), the hold-to-settle, and the T-10
  handoff — now the **primary loss-side endgame exit** for an unreverted
  position. Custody machinery (uncovered-flatten, self-net, partial-defer)
  UNCHANGED (§7).

**§4 empirical gate**: the reversion / resting-take fill rate is UNPROVEN.
Run at `FLIP_SIZE_CAP=1`; Instrument 1's take-fill rate is the gate before any
size increase — proven, not trusted on faith.

**§5 F-covers-FLIP — BANKED, NOT BUILT (hard martingale constraint)**: F
sizes to its OWN edge, ALWAYS; F may NEVER size up *because FLIP lost*
("scale up to recover the miss" is martingale). Not shipped until F-sizing is
provably independent of FLIP P&L. Guarded by test:
`test_f_covers_flip_not_built_no_martingale_hook` (no FLIP P&L read in F).

**Overturned test-laws (with citations)**: the build-40/43 loss-side price-cut
tests are re-anchored to the liquidity-hold reality — a low mark now HOLDS
(illiquidity), and the retained backstops (catastrophe/spot) or the T-10
handoff carry the cut. Rewritten across `test_flip_exit_doctrine`,
`test_bleed_diagnosis`, `test_p21_doctrine`, `test_p26_proof_law`,
`test_flip_cheap_live`, `test_flip_thesis1`, `test_flip_count2` (custody
machinery intact — only the *trigger* moved), and `test_aplayer` (the
P&L-independence property still holds, re-anchored to the catastrophe floor).
The build-43 scalp acceptance suite (`test_flip_geometry_coherence`) is
superseded and replaced by `test_flip_liquidity_hold` (12 acceptance tests:
take rests; illiquidity dips held; catastrophe/spot backstops cut; one-poll
flicker held; T-10 endgame; entries admitted; take-fill measured; scalp
retired; F not built). Suite 580 · preflight 23/23. **Halt stays in place
until Drew resets it** — banked, not deployed.

## WO-INFRA-HARDENING — make the body match the brain (build 46)

**Execution-hardening phase. Doctrine frozen; execution only.** The app
($34.68 / $0 positions) vs the book ($37.67) showed a ~$3 phantom surplus.

**Read-rule audit OVERTURNED the WO's central diagnosis (reported to Drew):**
- **P&L is already booked from confirmed fills, NOT window-econ.** `book_cents`
  = `Σcash_movements + Σsettlements(divergent=0)` (ledger.py:198); the sole
  P&L writer is `surface.settle_market → record_settlement` (surface.py:163),
  computed by walking the confirmed fills table. The window-econ number never
  writes to the book — it is a check that, on divergence, **quarantines and
  re-books at fills-truth** (ledger.py:222). INFRA-1's "re-source P&L" is a
  no-op against already-correct code.
- **Settlement is exchange-gated:** it fires only after `close_ts+10` and only
  when `venue.get_settlement_result` returns the exchange's own `mkt["result"]`
  (venue.py:817) — outcome is exchange-truth, not a spot guess. Arithmetic is
  correct for held and exited legs; `record_settlement` marks `settled=1` (no
  re-settle double-book).
- **A reconcile already exists** (INFRA-3 largely built): boot reconcile +
  60s standing reconcile against exchange balance/positions (reconcile.py,
  shadow_runner.py:775), pending-aware, halting via the cash protocol.
- **Genuinely true:** the maker price is fixed at DECISION time and submitted
  unchanged — no reprice at placement (INFRA-2, a real but separate gap); and
  transport is REST-1s (INFRA-4/WebSocket, a separate large build unbuildable
  in-sandbox).

**Conclusion:** the phantom is a runtime/data condition, not a code defect the
repo can point to — most plausibly a settlement double-counted across a
restart, a wrong-market outcome on a stacked ticker, or an unmatched leg.
Pinning the exact +$3.14 needs the live `settlements`/`cash_movements` tape.

**DREW-RULED: hunt the source + build the tracer ("both").** Shipped **E1 —
the settlement source tracer** the code's own quarantine comment long named
("stopgap until E1 traces the source"):
- Every settlement writes a **SETTLE_AUDIT** provenance row — booked pnl, the
  exchange outcome, the per-fill contribution breakdown, `net_held` per side,
  and the running book **after** it books (surface.py). An **unmatched leg**
  (exits exceeding entries — a phantom over-exit) is flagged and written as a
  durable `SETTLE_UNMATCHED_LEG` row (alert=False).
- The reconcile records **RECON_BOOK_VENUE_DELTA** when the book disagrees
  with the venue and NOTHING is pending (0 unsettled, 0 resting,
  `|delta| > RECON_AUDIT_FLOOR_CENTS`) — the exact phantom signature —
  timestamped against the SETTLE_AUDIT trail (shadow_runner.py).

**Pure instrumentation** — no strategy/geometry/Kelly/cash/halt change; the
booked P&L is unchanged (`test_e1_does_not_change_the_booked_pnl`). 7 acceptance
tests (provenance sums to pnl; exited-leg matched; unmatched-leg flagged;
phantom-signature recorded; pending-explained gap stays silent). Suite 587 ·
preflight 23/23.

**Deferred (Drew's call, per the WO's own Part E sequencing):** INFRA-2
(placement-time live-book pricing — confirmed gap, separate build) and INFRA-4
(WebSocket transport — unbuildable/untestable without live Kalshi WS). The
historical +$3.14 awaits the live divergent tape to pin via the three queries
in the report; E1 makes every future divergence self-pinning from the next
window forward.

## WO-FLIP-IMMEDIATE-ENTRY + DAILY-PACK-FILL-ECON (build 47)

**Part A — the bug (ships and triggers the build):** FLIP structurally could
not enter. Read-rule TRUE at `lane_flip.py`: the entry gate rejected on
`grain is None or length < OPEN_MIN_GRAIN` and returned no proposals — the
transcript's repeated `OPEN_NO_GRAIN … waiting IS the setup`. That grain-wait
is the exact belief the liquidity-hold capstone overturned; while F traded,
FLIP sat out every window.
- **Fix:** the grain-wait is retired. FLIP now enters the opening IMBALANCE
  immediately — the entry side is the **cheap side of the band** (the lower
  bid, the pile-in-abandoned side), not `grain["direction"]`. Grain, if
  present, only informs the `why`. A true 50/50 (equal bids) skips (no
  imbalance); the cheap side above `OPEN_MAX_ENTRY_CENTS` skips (paid up).
- **Trend-guard (Adversary-mandatory) — already in force:** `needle_active`
  (HUNT seniority, `delta_p >= HUNT_NEEDLE_POINTS = 5`) returns *before* the
  entry gate on ANY live spot trend — **stricter** than the WO's "decisive
  trend (≥15) against the cheap side" — so removing the grain gate does not
  remove trend protection. FLIP never buys into a market that's genuinely
  running.
- Band, max-entry, the swing gate (the Instrument-1 measured-rate gate,
  permissive while calibrating), the resting-take exit, liquidity-hold, and
  the catastrophe backstop are all UNCHANGED. Overturned test-laws (grain
  gates / posts-grain-side) re-anchored to imbalance across test_lane_flip,
  test_p15, test_p26, test_flip_thesis1.

**Part B — the fill-economics pack (banked, rides along, read-only):** now
that the book is honest (E1) and FLIP enters, the take-vs-fee question gets
DATA. `ops.fill_economics(ledger)` adds a daily-pack section and
`flip_fill_rate_hourly` a compact hourly line, per lane (FLIP, F, HUNT):
avg entry/exit price, gross spread, fees, **net after fees**, and the
**maker/taker split derived from `fee_cents`** (maker = 0-fee, taker > 0¢ —
no schema change, the fee column already carries the truth) — the sharp
answer to "is the +5 nickel eaten by taker fees?" Plus FLIP's **resting-take
fill (reversion) rate** (from the FLIP_SWING instrument) and its distribution
**by realized take distance** — the "post-here-to-fill" curve that turns
"+5 vs +7 vs +10" into a number. All read-only off the honest fills table; it
informs the take ruling, Drew still rules the number.

**HARD RAIL:** Part A touches ONLY the FLIP entry gate; Part B is read-only.
No Kelly/cash/rate-halt/F/geometry change. Ships at `FLIP_SIZE_CAP=1`; the
fill-rate gate still governs size. 6 immediate-entry test rewrites + 6 new
fill-economics tests. Suite 593 · preflight 23/23.

## WO-MAKER-REST-BACK — stop posting into the cross (build 48, overnight)

**The last fix before the overnight run.** F (and now FLIP) kept getting
`VENUE_REJECTED "post only cross"`: a maker BUY posted at a price the fast book
had already crossed, and `post_only=True` made Kalshi reject it. Read-rule
TRUE: `venue.py:774` documents the exact behavior; `gateway._wall_taker_entry`
(gateway.py:502) allows an entry to rest AT the derived ask (`price ≤ ask`,
touch-joining), and the venue's post_only then rejects the exact-cross.

**The fix — rest back, or cross deliberately (never post_only into a cross):**
- At **LIVE placement**, a maker BUY entry is re-priced by `_rest_back_price`
  to rest **at/inside the held-side bid, strictly below the derived ask**
  (`min(intended, held_bid, ask−1)`) — it can never turn into a
  post_only-into-a-cross. It runs **after** the taker-entry wall, so a lane
  pricing an entry *through* the ask still rejects loudly (the wall stays);
  rest-back only cushions the legitimate at-touch entry the fast book crossed.
- **FLIP** rests an extra `FLIP_REST_BACK_CENTS` (=2, DREW-DEFAULT) **below**
  the cheap bid — not a patch but its liquidity doctrine (sit under the
  pile-in, get hit as it falls).
- A rest-back that would breach the lane band **SKIPS** the window
  (`REST_BACK_SKIP` — a maker who can't rest in-band waits, never chases).
- **Deliberate taker crossing stays CUT-only** (`post_only=False`, crossfire) —
  unchanged. Entries never become takers; the `REJECT_TAKER_ENTRY` wall stays.
- **Live-only:** the post-only-cross is a live-venue reject; shadow has no
  venue, so shadow prices are untouched (no test churn, honest scoping).

**HARD RAIL:** only maker ENTRY pricing at live placement changes. No
Kelly/cash/rate-halt/strategy/CUT change. Ships at `FLIP_SIZE_CAP=1`. 10
acceptance tests (F joins the bid; FLIP cushions below; strictly-below-ask on
a crossed book; band-breach skips; live applies / shadow doesn't; through-price
still rejects; CUT still crosses). Suite 603 · preflight 23/23. **This is the
last build before the overnight untouched run** — watch the maker/taker split
(build-47 pack): entries should show as MAKER (0-fee), only CUTs as TAKER, and
"post only cross" → ~0.

## WO-FLIP-CATASTROPHE-ILLIQUIDITY — the 2-minute dump (build 48, last of the night)

**The last fix before the overnight run.** Drew observed ALL FLIP positions
exiting catastrophic within ~2 minutes. A full cold read pinned the cause:
the catastrophe floor was firing on **illiquidity** and dumping inventory at
the bottom — the exact anti-thesis.

**Read-rule TRUE at source** (`lane_flip.py:1189`): the catastrophe price
branch (`elif mark <= catastrophe`) fired on a **single poll** of the held-side
bid — no sustain, no depth, no time guard. On a fresh cheap entry into a thin
opening book, the held-side bid sits ~19-20¢ because there are no buyers *yet*
(the opening pile-in = the illiquidity the thesis holds through), and the
branch market-dumped it. The spot-decided branch (line 1185, sustained 2 polls)
was correctly guarded; the price branch had neither.

**The fix — the price floor must tell illiquidity from collapse.** The
catastrophe PRICE branch now fires only on a REAL low:
1. **Sustain ≥2 polls** (`catastrophe_polls`, mirroring `collapse_polls`) — no
   single-poll dump.
2. **Real depth** on the held side (`book.visible_depth(side, mark) ≥
   OPEN_CATASTROPHE_MIN_DEPTH`, =3) — a 1-lot thin quote is book emptiness, held.
3. **Past the opening-illiquidity window** (`now − fill_ts ≥
   OPEN_OPENING_WINDOW_S`, =90s) — a fresh entry's low bid is the setup, not a
   verdict.

The **spot-decided branch is UNCHANGED** — a genuine sustained move still cuts
any time, on SPOT, not on waiting for the price floor. The price floor is the
deep backstop *with guards*; a real, deep, sustained low past the opening
window still cuts (the backstop remains).

**Full-engine cold read (Part C):** `_check_uncovered` cleared — it correctly
treats a resting take / custody bucket as cover (not the dumper); `scratch_reason`
is retired doc; no other live bug. The only fix is the catastrophe price branch.

**HARD RAIL:** only the catastrophe price-branch trigger changes. Spot-decided
cut, T-10 handoff, goal-take, uncovered-flatten, rest-back, Kelly, cash,
rate-halt, F — all unchanged. `FLIP_SIZE_CAP=1`. 11 overturned test-laws
re-anchored to the guarded backstop (aged past the opening window + sustained
2 polls; depth already present) + 3 new tests (fresh sub-20¢ thin dip HELD;
thin-depth HELD past the window; real-deep-sustained CUTS). Suite 604 ·
preflight 23/23. **The last build before the overnight untouched run** — FLIP
finally holds through the pile-in, so the night measures the reversion instead
of the dump. Watch: catastrophe-at-2min → ~0, `flip_fill` rising.

## WO-FLIP-EVERY-MARKET-LIQUIDITY — be the liquidity, every window (build 49)

**The thesis, completed.** FLIP is the market's LIQUIDITY PROVIDER: buy the
cheap side of EVERY biased open, hold, and sell back to the forced hedgers at
the MIDDLE. Three atomic parts, shipped together (B3 is load-bearing and new).

**B1 — ENTER EVERY MARKET.** The "both sides in `OPEN_BAND`" gate is RETIRED
(`lane_flip.py:669`): it rejected biased opens (the expensive side out of band)
and made FLIP wait for a balanced book — backwards for a liquidity provider. The
only entry filter now is the CHEAP side being BUYABLE
(`OPEN_ENTRY_FLOOR..OPEN_MAX_ENTRY` = [25,50]¢), the **true-50/50 skip** (equal
bids → no cheap side to provide against), the **two-sided-book requirement**
(need both bids to find the cheap side), and the pre-existing **needle
trend-guard** (HUNT seniority returns before this gate on any live spot trend —
FLIP never buys a market genuinely running). Book coherence
(`yes_bid + no_bid ≤ 101`) guarantees the cheap side is always ≤ 50, so the
raised `OPEN_MAX_ENTRY_CENTS=50` admits every coherent biased open; the surviving
range gate is the FLOOR (a near-worthless cheap side < 25 is a falling knife,
skipped).

**B2 — REST TOWARD THE MIDDLE, SCALED BY ENTRY DEPTH.** The resting take is
`_take_price(entry) = clamp(OPEN_MIDDLE_TARGET=52, entry+OPEN_TAKE_MIN=5, 99)`
(`lane_flip.py:772`). The 50/50 middle is where hedgers are forced to transact;
the cheaper the entry, the bigger the gouge — buy 39 → rest 52 (+13); buy 44 →
rest 52 (+8); buy 49 → rest 54 (+5, fee-floored). This is entry-relative and
size-independent (the goal-bounded size-scaling survives only in the
`_take_target` helper, which no longer drives the live post).

**B3 — ACTIVE LATE-WINDOW WALK-DOWN (load-bearing, new).** A position whose
middle take never fills must NOT ride unfilled into a catastrophic bell dump. As
the clock runs from `OPEN_WALK_START_S=780` down to `OPEN_FLAT_BY=600`, the
resting MAKER take steps DOWN from the middle toward scratch — `stepped =
max(entry, round(entry + frac·(middle−entry)))`, `frac = (secs−FLAT_BY)/span` —
re-posting lower each step (`lane_flip.py:1249`, a new `elif` after the
determined-cut block). It is gated `past_opening` (age ≥ `OPEN_OPENING_WINDOW_S`
= 90s) so it is **late-window management, never a reactive early cut**: a fresh
position inside the opening-illiquidity window is HELD by the catastrophe guards
(build 48). The walk **floors at scratch** (entry) — a genuine loser below
breakeven is the T-10 handoff's to clear (`secs ≤ FLAT_BY`), never sold below
cost by the walk.

**Genuine interactions found & fixed (not just overturns).** (a) The walk-down
cancels-then-re-posts the take; a test that never submitted the re-post left the
leg looking uncovered across cycles → `_check_uncovered` escalated to a FLATTEN.
(b) A fresh SPOT-collapse poll got a walk-down EXIT that broke a `== []` cut
assertion. Both are resolved by the `past_opening` gate (the walk only fires for
positions clearly past reversion) — the fresh-position test now holds, and the
collapse test isolates the CUT it actually asserts (the coexisting walk-down
maker re-post is not a cut; the position still holds).

**HARD RAIL:** no Kelly / cash / rate-halt / F change; catastrophe-illiquidity
guards (build 48) intact; `FLIP_SIZE_CAP == 1`. New constants DREW-DEFAULT:
`OPEN_MIDDLE_TARGET=52`, `OPEN_MAX_ENTRY_CENTS=50` (was 49), `OPEN_ENTRY_FLOOR=25`,
`OPEN_WALK_START_S=780`. 11 overturned test-laws re-anchored (middle-target take
prices ×6; every-market entry breadth ×4 — yes=30 enters, cheap no@34 enters,
floor-skip replaces the now-unreachable ceiling-skip, two-bid biased book enters
where the genuinely one-sided book still posts nothing; the P26 why-string
"imbalance"→"liquidity") + a new acceptance file `test_flip_every_market.py` (14
tests: B1 breadth & skips, B2 middle-target scaling, B3 walk-marches-to-scratch,
floors-at-scratch, dormant-when-fresh, dormant-outside-window). Suite 619 ·
preflight 23/23. Watch live: FLIP entering biased opens (not just balanced
books), resting takes clustering at ~52¢, and late unfilled positions walking to
scratch instead of dumping at the bell.

## WO-BOTH-LANES-MARKET-TRUE — the doctrine made time-aware (build 50)

**One doctrine, both lanes:** hold through noise, act only on confirmed real
moves, time-aware, recover — never panic. Five days live, up; this is the build
where the safety became TIME-AWARE.

**Read-rule at source (all premises TRUE except 2E):**
- **2A time-blind cut** — TRUE. The spot-decided cut (`lane_flip.py`, the
  `determined` block) fired on `collapse_polls>=2` with **no** `secs_since_entry`
  guard — the 12:46 first-minute stop-out path. Catastrophe had only the 90s
  `past_opening` guard.
- **2D F rides to ~−90** — TRUE. F's needle salvage (`_salvage_tick`) needs a
  spot + a table cell; **BLIND or table-gap → it disables** and only the
  `catastrophic_loss_cents=90` backstop remains. `OpenPosition.entry_price_cents`
  exists, so a raw price stop is buildable.
- **2E F entry post-only-cross** — **already SHIPPED (build 48).** WO-MAKER-
  REST-BACK's call site (`gateway.py:277`) is **lane-agnostic**, and F's
  `to_order` already carries `band=(95,99)`; `test_maker_rest_back::
  test_rest_back_joins_the_bid_for_F` already proves an F entry rests at the
  bid. No code change — reported honestly, re-asserted in the new acceptance.

**A — the 4-minute hard no-sell** (`lane_flip.py`, the exit loop). `age = now −
fill_ts`; while `age < FLIP_NO_SELL_S(240)` the position **`continue`s** with
`collapse_polls`/`catastrophe_polls` reset — NOTHING sells (spot cut AND
catastrophe floor both suppressed), the only exit is a FILL of the resting
middle-take. The opening pile-in is noise; a cheap (<=50) entry + the 1-lot cap
bound the loss through the hold. Polls reset so a pile-in never counts toward the
post-hold 2-poll sustain.

**B/C — time-aware management + the decision point.** The handoff/decision moved
from **T-10 (`OPEN_FLAT_BY`=600 s-left)** to **`FLIP_DECISION_S`=240 s-left
(~minute 11 of the 15-min window)** — *this is the one Drew-locked constant this
WO overrides, flagged here and in the boot tape for veto from the tape.* Drew's
ruling: FLIP's job is to SCALP THE WIN and leave **zero inventory by minute 11**;
whatever remains, **F is the inventory-aware authority** — a WINNER (mark >=
basis) is left to F as hold-to-settle at FLIP's cheap basis (F rides it, stands
down via the shared inventory), a LOSER is sold. F's ΔP/table proof *is* the
"confirmed real move" that rules an earlier sell (the spot-decided cut). The
walk-down is re-based on `[FLIP_DECISION_S, WALK_START]` and gated by the same
hard-hold — it steps an unfilled take toward scratch to clear inventory by the
decision, never below scratch.

**D — F's 40-point price salvage** (`custodian.py::_salvage_tick`). A new
**table-free, spot-blind-proof** floor placed BEFORE the anchor/spot guards: when
`mark <= entry_price_cents − F_SALVAGE_SLIP_POINTS(40)`, F cuts IMMEDIATELY
(crossfire) — recover ~−40 instead of riding to −90 (the −$2.77 3-lot dump). One
attempt (`salvage_attempted`); **no re-entry** is already guaranteed by Wall 3
(single-entry keeps the ticker — both sides — out of lane F for the window).
Gated to lane F. A slip < 40pt stays the gentler needle salvage's zone.

**HARD RAIL:** `FLIP_SIZE_CAP=1`; catastrophe-illiquidity guards (build 48)
intact; no Kelly / cash / rate-halt / deny-fatal change; F's hold-to-settle
unchanged except the 40pt salvage. New constants DREW-DEFAULT: `FLIP_NO_SELL_S=
FLIP_DECISION_S=240`, `F_SALVAGE_SLIP_POINTS=40`. 21 overturned test-laws re-
anchored (cut tests aged past the 240s hard-hold; T-10 handoff asserts moved to
`FLIP_DECISION_S`; the p24 95→40 needle test moved to a 35pt slide since a
≥40pt slip now preempts with the immediate price salvage) + new acceptance
`test_flip_both_lanes.py` (13 tests: hard-hold holds a first-minute collapse &
re-arms after; catastrophe suppressed in-hold; decision winner→F / loser→sold;
walk extends past the old T-10; F 40pt slip salvages immediately table-blind, a
35pt slip does not, one attempt; F rest-back + band-skip). Suite 632 · preflight
23/23. **Watch live:** a FLIP position HELD through a first-minute adverse move
(not the 12:46 −17c cut) then filled/walked; an F favorite salvaged at ~−40 on a
40pt slip (not the −$2.77 dump); F rejects → 0; both lanes printing.

## WO-INSTRUMENTATION-AND-FLIP-TIMING — every place a data point (build 51)

**Before a 25-hour run:** fix the one proven bug (FLIP entry timing) and light
every instrument so the run produces a dataset, not just a P&L number.

**Read-rule at source (all premises TRUE):**
- **Part A — tags went silent:** TRUE. `_log_swing_outcome` (the per-trade
  record) carried entry/exit px, gross, timing-to-swing — but NOT entry/exit
  SPOT prices, spread, secs-into, exit reason, or book state.
- **Part B — pack exists, didn't fire:** TRUE. `daily_pack()` (ops.py:275) is
  called by `pack_task` (shadow_runner.py). The full pack was gated on
  `now_et.hour == 9` — a **1-hour window** a deploy/restart after 9am missed
  entirely (the real cause; NOT a deploy-grade gate).
- **Part C — no early entry cutoff:** TRUE. The entry gate (`lane_flip.py:665`)
  only checked the LATE boundary (`secs <= OPEN_ENTRY_CUTOFF`); `FLIP_WINDOW_SEC
  =900` and `OPEN_OPENING_WINDOW_S=90` both exist to reuse.

**A — rich per-trade tagging.** Entry state is stashed at the proposal
(`w.entry_meta[side]` = spot, secs-into, spread, cheap-bid, depth) and copied
onto the position at `note_fill`; every custody poll stamps the running exit
observation (`o["exit_obs"]`), and each active exit path tags `o["exit_reason"]`
(TAKE_FILL / WALK_DOWN / SPOT_DECIDED / CATASTROPHE / HANDOFF_LOSER /
HANDOFF_WINNER). `_log_swing_outcome` writes the COMPLETE FLIP_SWING record:
entry/exit spot PRICES, captured spread, secs-into at entry AND exit (the
≤90s-vs-mid distinction), the exact reason tag, book depth both ends, and the
posted gouge level. The entry `why` (the Telegram-readable line) now carries
`spot`, `into Ns`, and `book y../n.. sprd.. depth ../..`.

**B — the daily pack.** Trigger robustified: `now_et.hour >= 9`, once per
calendar day — a post-9am restart still delivers the day's pack (reads DB
post-reconcile truth, never a deploy grade). Added the **money curve**,
`flip_fill_rate_by_price` (ops.py): of the takes posted at each gouge level,
what % FILLED (exit_reason TAKE_FILL) vs walked/cut — bucketed by posted price,
surfaced in the pack. The at-a-glance 24h views (`fill_economics`,
`flip_fill_rate_hourly`, FLIP SWING) already ship in the pack.

**C — the 90-second entry cutoff (the proven fix).** `secs_into =
FLIP_WINDOW_SEC − secs`; if `secs_into > OPEN_OPENING_WINDOW_S(90)` FLIP does
not enter (logs `OPEN_PAST_OPENING`). The tape proved it: FLIP wins buying the
opening pile-in (≤90s, the +16/+23 gouges) and loses wandering in mid-market
(the −15/−16 catastrophics — no pile-in left, no reversion). The two-sided +
real-cheap-side + trend-guard requirements stay (Adversary guard a: never fire
into a one-sided book). The safety-net doctrine — fire on the first valid
opening book, never wait for a "misplacement" — is the existing immediate-entry
(build 47/49) now bounded to the opening window. **Note (for veto):** the
opening-window floor (25) and true-50/50 skip are retained per the mandatory
Adversary "real cheap side" guard; Part A's data will show if any skip class
should relax — as Drew ruled ("data decides skip classes; no volatility
sit-out").

**HARD RAIL:** F unchanged; sizing-with-book unchanged; `FLIP_SIZE_CAP=1`;
rate-halt, cash + deny-fatal, catastrophe-illiquidity guards, reset intact; no
Kelly change. New/reused constants: FLIP entry cutoff = `OPEN_OPENING_WINDOW_S`
(90, reused); pack `hour>=9` ungated. 61 overturned test-laws re-anchored (entry
drives moved from `secs_left=800`/mid-window into the opening 90s) + new
acceptance `test_instrumentation_flip_timing.py` (10 tests: cutoff enters ≤90s /
refuses >90s / uses secs-into not secs-left / needs a two-sided cheap side; the
FLIP_SWING record carries the full data point; entry why carries spot+timing+
book; walk-down tagged; the fill-rate-by-price curve + pack inclusion). Suite
642 · preflight 23/23. **Watch live:** an ENTRY/EXIT line carrying spot prices +
secs-into + reason; a FLIP entry REFUSED for secs-into>90 (`OPEN_PAST_OPENING`);
the daily pack firing with the fill-rate-by-price curve; the last two losers
pulled, each with full why + what-happened.

## WO-FULL-COLD-AUDIT — the FLIP exit machine, fixed as a cluster (build 52)

**Stop the whack-a-mole.** One cold read of the whole FLIP path found the root:
two exits fighting, the violent one winning. Shipped the core cluster (Findings
1+3+4) together; deferred the one-clock refactor (2); corrected the swing-gate
misread (5) and added telemetry; F untouched (6).

**Read-rule at source (verified; two divergences reported honestly):**
- **Finding 1 (root)** — TRUE. The `if determined:` (SPOT_DECIDED, `crossfire=
  True` at mark) sat *before* the `elif` walk-down, so the market-dump pre-empted
  the gentle exit; `OPEN_DETERMINED_K_POINTS=15`. **My divergence:** the
  catastrophic-loss generator is K=15 firing on *drift*, not that crossfire is
  the wrong tool (in a genuine collapse a maker walk-down rides down unfilled —
  worse). Drew ruled: raise K **and** route SPOT_DECIDED through the walk-down
  (acceptance #1: zero market-dump on SPOT_DECIDED). Both done.
- **Finding 4** — TRUE. `OPEN_MAX_ENTRY_CENTS=50` (one correction: a 50c entry
  gouges +5 fee-floored, not +2, but the near-coinflip point stands).
- **Finding 5** — largely FALSE. The swing gate is **live-but-permissive below
  `OPEN_SWING_MIN_SAMPLES=20`**, not dead; the tape's `0.89` is the explicitly-
  labeled retired shadow proxy. Accepted; added a telemetry line instead of a
  rework.

**Finding 1 (ROOT):** raised `OPEN_DETERMINED_K_POINTS` **15 → 40** (a real
decision, not drift). The exit block is now split by severity (`lane_flip.py`):
`if catastrophe_polls>=2` → crossfire out (genuinely gone, unchanged); `elif
collapse_polls>=2` → **route through the walk-down** — a MAKER exit at scratch
(entry, never below cost, `crossfire=False`), no market-dump; `elif` the
time-based walk-down. Losses route through the gentle exit; only the deep
backstop crossfires.

**Finding 3 (GATE):** the volatility skip. `OPEN_TREND_SKIP_USD=200` — if the
opening BTC-spot has already run >= this one-directionally across the observed
ticks (>=2), OPEN skips its reversion entry (logs `OPEN_TREND_SKIP` with the
reading; the reading rides every open's `entry_meta` for calibration). **F is
byte-identical** (criterion #5), so the "skip BOTH lanes" is delivered as the
OPEN-half now; extending it to F is a follow-up (it would touch F). HUNT
(momentum) is left to ride trends.

**Finding 4 (ENTRY):** `OPEN_MAX_ENTRY_CENTS` **50 → 42** — only real-gouge
entries (42c → +10 to the 52 middle); the near-coinflip top of the band is cut.

**Finding 5 (MEASURE):** a per-window `swing pass/block n=… p=… p_up=… p_down=…`
line, so the sample-floor decision is made from data. Gates nothing today (the
permissive gate rides); it will gate SIZE, never entry, when proven (Adversary).

**Finding 2 (DEFERRED — its own build):** unifying the FLIP lifecycle to one
clock (secs-into-window) is a load-bearing refactor; per Drew's cadence law it
is a Saturday work order, not bundled with three tape-verified fixes (keeps the
build auditable). Written up for follow-up.

**HARD RAIL:** F unchanged (byte-identical — the swing-gate/p22 F-sizing tests
were kept on their original books); sizing-with-book unchanged; `FLIP_SIZE_CAP=
1`; rate-halt, cash + deny-fatal, catastrophe-illiquidity guards, reset intact;
no Kelly change. New/changed constants: `OPEN_DETERMINED_K_POINTS=40`,
`OPEN_MAX_ENTRY_CENTS=42`, `OPEN_TREND_SKIP_USD=200`. ~60 overturned test-laws
re-anchored (SPOT_DECIDED CUT→maker-walk-to-scratch; entry cheap side moved into
[25,42]) + new acceptance `test_full_cold_audit.py` (9 tests: K is a real
decision; sub-K drift does not cut; a decision walks to scratch never crossfire;
catastrophe still crossfires; trending open skips; calm open enters; entry
ceiling 42; the money-line telemetry). Suite 651 · preflight 23/23. **Watch
live:** zero market-dump exits on a SPOT_DECIDED (every loss shows the walk-down
path); `OPEN_TREND_SKIP` events with the volatility reading; no FLIP entries
above 42c; the `swing …` telemetry line every window.

## WO-2026-07-21-FLIP-SELECTION — the tape-derived Part A (build 53)

**The tape's arithmetic (7:09–9:15 PM 0721, 9 windows):** F +30c · FLIP −11c ·
net +$0.19. The build-52 cluster WORKED (worst loss 18c vs the −$1.40 class);
this is a **selection** problem, not a bleed. Part A ships the weekday-legal
subset (observed bugs + instrumentation); Part B (margin-gate SUPPRESS, per-lane
rate halt, threshold calibration, the 17c reconciler gap) is **Saturday-class,
held for Drew's ruling — NOT built here.**

**Read-rule at source (all TRUE):**
- **A1** — the engine graded itself out of doctrine: 2/3 FLIP trades exited via
  CATASTROPHE (A5-forbidden for OPEN), and `FLIP_FLOOR_BREACH` fired ("loser cut
  17c past the 2c band-floor expectation"). `OPEN_CATASTROPHE_FLOOR=20` is
  ABSOLUTE (config.py:199) while entries are 25-42 — an undeclared size-by-entry-
  price, and the hold *defers* the cut until the price is worse.
- **A2** — zero `OPEN_TREND_SKIP` in 9 windows: a $200/60s move at 66k is a
  once-a-month event, so the gate structurally cannot fire; `trend_usd` was
  logged only on skip.
- **A4** — `FLIP_UNCOVERED_LEG` pages on 100% of entries (`held 1 > covered 0`,
  the routine 1-lot post-fill state) — a warning that always fires is how A5 got
  scrolled past.

**A1 — the catastrophe floor is RELATIVE to entry.** `salvage_floor = max(
OPEN_CATASTROPHE_FLOOR, entry − OPEN_SALVAGE_BUDGET_C)`, new
`OPEN_SALVAGE_BUDGET_C=8` (the EV table's own 2c band-floor + 5c slip, rounded —
pinned to that assumption or the table re-inverts). Every FLIP loss is now
bounded at ~8c instead of riding to the absolute 20c (a 22c loss on a 42c
entry). The relative-floor exit is a **MAKER** (`crossfire=False`, tagged
`SALVAGE_FLOOR`, A5-legal — a DETERMINED-class exit, not a CATASTROPHE); **only
the absolute 20c floor** (genuinely gone) keeps its crossfire. All build-48
guards intact (depth ≥ min, `past_opening`, sustained ≥ 2).

**A2 — `trend_usd` on every OPEN entry line** (enter AND skip), so
`OPEN_TREND_SKIP_USD` is set from the observed distribution on Saturday, not
guessed. **The threshold is NOT changed in this build** (no distribution yet).

**A3 — `depth_ratio` (held ÷ other) recorded** on the FLIP entry row and the
`SWING_GATE_COMPARE` row — the tape's most promising unexploited signal (winner
1.40×, both losers 0.59-0.60×). **It GATES ON NOTHING** (n=3; a hypothesis to
rule on at n ≥ 20, never a live gate — the comment says so in the code, per the
Adversary).

**A4 — the routine cover-pending leg no longer pages.** `FLIP_UNCOVERED_LEG` at
`esc==0` is demoted to non-alert **when a cover intent exists** (the take
proposed, its oid not yet confirmed — the routine 1-lot state); the RECONCILE
still runs and the genuine self-net void still escalates to a paged FLATTEN. The
always-on warning that masked A5 is silenced without losing the real protection.

**HARD RAIL:** F byte-identical (Part A touches only OPEN's floor + entry
telemetry + one uncovered-page level); no Kelly / cash / rate-halt change;
`FLIP_SIZE_CAP=1`; catastrophe-illiquidity guards intact. New constant:
`OPEN_SALVAGE_BUDGET_C=8`. 1 build-52 acceptance re-anchored (a mark that now
hits the relative floor before SPOT_DECIDED) + new acceptance (A1 relative-floor
maker; A1 absolute-floor crossfire; A2 trend on the line; A3 depth_ratio record-
only). Suite 655 · preflight 23/23. **Watch tomorrow's tape (acceptance):** zero
A5 lines; zero `FLIP_FLOOR_BREACH`; any FLIP loss ≤ ~8c + fee; `trend_usd` and
`depth_ratio` on every entry; `FLIP_UNCOVERED_LEG` no longer on every entry; F
byte-identical.

**Deferred to Saturday (Part B — Drew's ruling):** B1 margin gate as SUPPRESS
(shadow/record-only when margin<0, promote at n≥20 — dissolves the bootstrap);
B2 per-lane rate halt (FLIP's losses must not halt F); B3 calibrate
`OPEN_TREND_SKIP_USD` from A2's data; B4 explain the 17c `WINDOW_ECON_DIVERGENCE`
before any sizing. Standing ceiling unchanged: the treasury waterfall asymmetry
(wins scraped 35%, losses booked 100%) remains the #1 pre-scaling fix.

## WO-2026-07-21-B — HOT FIX: the reboot bypassed the 4-minute hold (build 54)

**A1 confirmed on tape first** (10:17 PM: `salvage floor → 28c … −8¢ + fee 0¢`
vs last night's −18c/−16c crossfire — loss cut ~55%, maker, no dump). Then two
observed bugs.

**Read-rule at source (all TRUE):**
- **Finding 1 (root)** — the self-heal (`_heal_uncovered`) recovered the entry
  price from the fills DB but rebuilt the record with **`fill_ts=0.0`**; the
  hard-hold guard `age = now − fill_ts` then read ~1.78×10⁹ s (**~56 years**), so
  `age < FLIP_NO_SELL_S` was False and the 4-minute hold — plus the poll resets —
  was **BYPASSED on every reboot** (fails DANGEROUS: a missing timestamp
  *maximised* apparent age). Pre-existing since build 53; A1 only made the
  resulting exit small enough (−8c) to become legible.
- **Finding 2** — `FLIP_FLOOR_BREACH` fired on a correct 8c salvage:
  `floor_expected = entry − OPEN_UNDETERMINED_BAND[0]` = 36−35 = 1c, threshold
  6c, actual 8c → breach. The alarm's expectation and A1's `OPEN_SALVAGE_BUDGET_C`
  weren't pinned to one source (the exact drift A1 flagged); the message still
  said "the −15c salvage assumption".

**Finding 1 (fix):** `_heal_uncovered` now selects the fill **timestamp** with the
price (one extra column) and uses it for `fill_ts`; if unrecoverable it **fails
SAFE** — `fill_ts = now` (treat the adopted position as FRESH → full 4-minute
protection), **never 0.0** — and pages. `now` is threaded evaluate →
`_check_uncovered` → `_heal_uncovered`. The audit item (`_log_swing_outcome`'s
other `fill_ts` default) is an instrument, not a guard, but its 0.0 default was
retired to `now` too — the *class* of bug (0.0 on a timestamp) is gone.

**Finding 2 (fix):** `floor_expected = max(entry − OPEN_UNDETERMINED_BAND[0],
OPEN_SALVAGE_BUDGET_C)` — the breach test now polices the budget the engine
actually operates under, so a correctly-bounded salvage never trips it and a
genuine over-budget cut still does. The message quotes the live constant
(`{OPEN_SALVAGE_BUDGET_C}c`), never a stale "−15c".

**Finding 3 (corrects build-53 A4):** do NOT silence the uncovered page — on the
10:16 tape it was the true signal that caught the reboot orphan. SPLIT it: the
**routine 1-lot cover-pending** state (held 1, covered 0, take proposed — the
`FLIP_SIZE_CAP=1` post-fill look) is `FLIP_UNCOVERED_EXPECTED` at DEBUG, no page;
a genuine gap still pages `FLIP_UNCOVERED_LEG`; and the reboot orphan (no
in-memory record) additionally pages **`FLIP_ORPHAN_ADOPTED`** from
`_heal_uncovered` with the recovered entry + fill_ts. A 1-lot self-net void looks
routine at first (EXPECTED) and reveals itself by escalating to the paged FLATTEN.

**HARD RAIL:** F byte-identical; no Kelly / cash / rate-halt / sizing change; both
fixes are minimal + reversible (a one-column query + a fail-safe default + an
arithmetic pin + a tag split). 4 test-laws re-anchored (the no-record shapes page
`FLIP_ORPHAN_ADOPTED`; the void's first look is EXPECTED so its page is the
FLATTEN; the floor_expected is pinned to the budget) + new acceptance
`test_reboot_hold.py` (6 tests: orphan adopts the real fill_ts; the reboot orphan
honors the 4-min hold; unrecoverable ts fails safe + pages; a bounded salvage
does NOT breach; a genuine over-budget cut still does with the live constant; the
orphan page carries entry + fill_ts). Suite 661 · preflight 23/23. **Watch
tomorrow (acceptance):** force a reboot with a live FLIP position → adopted with a
real fill_ts, no exit until 240s after the TRUE fill; `FLIP_ORPHAN_ADOPTED` pages
with entry + fill_ts; `FLIP_UNCOVERED_EXPECTED` no longer pages on ordinary
entries; zero `FLIP_FLOOR_BREACH` on exits at/inside the budget; the breach
message quotes the live constant; F byte-identical.

**Adversary's class item (noted for the audit):** what else fails dangerous on a
missing default? `0.0`/`None` on a *timestamp* silently maximises age. And any
historical FLIP loss that followed a boot is now suspect data — do not read it as
thesis failure. **Part B of WO-2026-07-21 (margin-gate SUPPRESS, per-lane halt)
and the treasury-waterfall asymmetry remain the standing Saturday/#1 items.**

## WO-2026-07-22 — THE OVERNIGHT WAS A CASH-RAIL HALT, NOT A GATE PROBLEM (build 55)

**The overnight was CORRECT.** 2:48–5:50 AM the engine sat frozen — 14 orders,
39% fill, 3660c book, unchanged across 12 elapsed windows. That was not a stall:
cash-integrity refused to enter on a book it could not reconcile to the venue.
The book claimed **+$1.83**, the venue said **−$0.16** — a **~$1.99** gap from a
bad settlement row. "CASH DELTA NEGATIVE −199c — entries HALTED" is the rail
doing its one job. The overnight data carries **no strategy signal** — nothing
about the FLIP/F thesis was tested; the engine simply never traded.

**Read-rule at source (all TRUE):**
- `book_cents()` = `SUM(cash_movements)` + `SUM(settlements.pnl_cents WHERE
  divergent=0)`. A settlement row booked ~200c too high inflates the book by
  exactly that much until it is quarantined. TRUE.
- `reconcile` computes the venue delta **only at quiescence**
  (`in_flight_orders==0 AND unsettled_fills==0`), so the −199c was a settled,
  real divergence — not an in-flight artifact. TRUE.
- `record_settlement` was a **bare INSERT** — no idempotency guard, unlike
  `record_outcome`'s `ON CONFLICT(market) DO NOTHING`. A retry or a reboot
  mid-settle could double-book. TRUE (root-cause class).
- `quarantine_divergent_settlements(market, fills_pnl_cents)` already tags rows
  `divergent=1` (excluded from `book_cents`) and re-books at fills-truth under
  lane `"ECON"`. The fix machinery **already exists** — it was never the missing
  piece. TRUE.
- `/confirm_cash` **re-baselines** (writes a cash_movement to paper the gap): it
  would bake the $1.99 error into the book **forever**. `/clear_cash_fatal` only
  unlocks the door — the next reconcile recomputes the same delta and re-halts
  (clear→re-halt→fatal loop). TRUE. Neither is the fix.

**Candidate root causes (UNPROVEN here — the live DB decides):** a losing F
booked as a **win** (~+200c instead of −cost), or a settlement recorded **GROSS
not NET** (~+194c instead of the net). Both land ~194–200c and both are exactly
what the new bound catches.

**Root-cause fixes (shipped, code-side):**
- **Idempotency** — `record_settlement` now skips a duplicate non-divergent
  `(market, lane)` settlement and logs `SETTLE_DUP_IGNORED`, mirroring
  `record_outcome`. The quarantine re-book (a different lane) is deliberately
  **not** blocked.
- **Net-vs-gross bound, LOUD at the call site** — before insert it computes the
  position's own cost/lots from ENTRY fills and asserts
  `pnl ∈ [-cost, lots·100−cost]` (±`SETTLE_NOTIONAL_SLIP_C=6` for fees/rounding).
  A gross-as-net or win-as-loss booking is outside that bound and pages
  `SETTLE_NOTIONAL_BREACH` (`alert=True`) **at the moment of the bad write** —
  not 6 hours later at the halt. The bound is `[-cost, lots·100−cost]`, not
  `[-cost, +small]`, so a legitimate cheap-FLIP win (entry 30 → +70) passes.
- **Secondary (deferred, noted):** the halt does not yet short-circuit proposal
  generation — rejects ran 978→2211 (~1200 wasted propose/deny cycles/hour while
  halted). It is wasteful, not wrong (nothing traded). Left for a follow-up so
  this WO stays scoped to the booking guards.

**The rail was NOT weakened.** No change to the delta threshold, the quiescence
condition, or the halt itself. The guards make a bad *write* impossible to do
silently; they do not make a divergent *book* tradeable.

**Operational step — Drew's to run on Render (I cannot reach the live DB):** the
row-level identification and quarantine require the live ledger. Shipped as
tooling — `scripts/cash_diverge_diagnose.py`:
- `--db <ledger.db> [--since <epoch>]` lists every settlement with its cost/lots
  and net-bound and flags each out-of-bound row `<== OUT OF BOUND` by **id +
  market + lane** — naming the culprit.
- `--quarantine <MARKET> --correct-pnl <TRUE_CENTS>` calls
  `quarantine_divergent_settlements` (divergent=1, re-book at fills-truth) and
  prints the book before→after. **Then, and only then, `/clear_cash_fatal` —
  NEVER `/confirm_cash`.** Acceptance #1/#2/#3/#6/#7 (offending row named,
  quarantined-not-rebaselined, delta→0, entries resume, orders increment)
  complete when that runs against the live book.

**HARD RAIL:** F byte-identical; no Kelly / sizing / rate-halt / cash-threshold
change. New acceptance `test_cash_settlement_guard.py` (7 tests: settlement is
idempotent per market·lane and the quarantine re-book is not blocked; gross-as-
net 200c breaches loud; a below-cost −220c breaches loud; the honest net (+6 win,
−194 total loss) is silent; a cheap-FLIP +70 win is within bound; quarantine
drops the inflated book **without** a new cash_movement absorbing the gap — it is
excluded, not papered over; the forensic tool names the suspect row). Suite 668 ·
preflight 23/23. **Watch on the live box (acceptance):** run the diagnose tool →
one row named OUT OF BOUND; quarantine it → book delta → 0, no new cash_movement;
`/clear_cash_fatal` → entries resume and the order counter increments;
`SETTLE_NOTIONAL_BREACH` would have paged at the original bad write. **NO
`/confirm_cash`.**

**Standing items (unchanged):** the halt→proposal short-circuit (secondary,
above), Part B of WO-2026-07-21 (margin-gate SUPPRESS, per-lane rate halt), and
the treasury-waterfall asymmetry remain the Saturday/#1 backlog.

## KAL — THE 50/50 BUILD, STAGE 0.1 — THE PER-LANE RATE HALT (build 56)

**The master spec (builds 52→58) opens a strategy pivot — buy at 50/50 and sell
UP, retiring the cheap-side FLIP thesis — explicitly Drew-overridden onto a
Wednesday against the banked-Saturday law.** It ships in stages, each separately
attributable on tape. This build is **Stage 0 (Clear the Deck)**, marked "first,
no exceptions." Its one code deliverable is **0.1 — the per-lane rate halt**, the
hard prerequisite: *FLIP's losses currently halt F, the only earner.*

**Read-rule at source (all TRUE):**
- The rate halt lived in `window_econ.py:_apply_streak` — outcomes were a rolling
  window of **per-market** broker P&L (`window_pnl`, the account-value delta),
  and on trip it called `self.gateway.halt_entries(HALT_REASON)` — a single
  **global** gateway reason. TRUE: a FLIP-losing market contributed to a halt
  that then stopped **all** entries, F included.
- `gateway.py`'s entry wall (the `if not risk_reducing:` branch) blocked an order
  whenever `entries_halted_reasons` was non-empty — **lane-blind**, though
  `order.lane` was right there. TRUE.
- Per-lane P&L was **already computed** at the settlement call site:
  `shadow_runner.py` builds `per_lane` (lane→pnl) and does
  `fills_pnl = sum(per_lane.values())` immediately before `close_bracket`. The
  attribution the halt needs already existed; it was just thrown away. TRUE.
- The broker `window_pnl` (account-value delta) **cannot** be attributed to a
  lane — only fills/settlements carry the lane. TRUE, and it is *why* the
  per-lane unit must be fills-truth while the broker number stays the
  cash-integrity unit and the summary line.

**Built (0.1):**
- **Lane-aware wall.** `gateway.entries_halted_for(lane)` returns the reasons
  that block a given lane: a reason of the form `RATE_HALT:<lane>` blocks only
  that lane; a bare `RATE_HALT` (legacy global) and **every** non-rate reason
  (LANE_KILL:*, ORIENTATION_DIVERGENCE, cash-fatal, DEGRADE_LADDER) block all
  lanes. The wall now raises `ENTRIES_HALTED` only when `entries_halted_for`
  is non-empty. LANE_KILL keeps its colon suffix but is untouched (it does not
  match the `RATE_HALT:` prefix) — no incidental change.
- **Per-lane halt decision.** `_apply_streak` gains an optional `per_lane`; when
  present (the live path always provides it) it dispatches to
  `_apply_streak_per_lane`, which runs the same N-of-M streak **per lane** on
  `rate_halt_outcomes:<lane>` and, on trip, halts only `RATE_HALT:<lane>` and
  pages `RATE_HALT` naming the lane ("other lanes trade on"). When `per_lane` is
  absent the **legacy global path is byte-identical** — every existing
  direct-`_apply_streak` test still asserts the old global halt.
- **Persistence + reset.** Lane halts live in `rate_halt_lanes`;
  `restore_halt_on_boot` re-adds each scoped reason after a redeploy;
  `reset_halt` clears every lane window and scoped reason (its "no halt active"
  guard now also sees lane halts). The ops stop-audit reports each lane halt.
- The threading: `close_bracket(..., per_lane=)`, carried through `pending_closes`
  and `flush_deferred`, and passed from `settle_traded_market`.

**Stage 0.2 — quarantine the residual bad settlement row — is OPERATIONAL, Drew's
to run on Render (I cannot reach the live DB).** `/confirm_cash` made the trading
book honest ($34.61 = venue) but the bad row still sits in `settlements`, so
lifetime P&L stays inflated; build 55's guards stop the *next* one, not this one.
The tool shipped in build 55 — `scripts/cash_diverge_diagnose.py --db <db>` to
name the row, then `--quarantine <MARKET> --correct-pnl <CENTS>` (divergent=1,
re-book at fills-truth). **NEVER `/confirm_cash`** (doctrine 16). **Stage 0.3 —
repurpose `OPEN_TREND_SKIP_USD` from a dead skip-gate into the direction signal —
ships with Stage 1's 50/50 entry**, where the computation becomes the signal;
touching it alone in Stage 0 would be a dead half-change, so it is deferred to
that build by design.

**HARD RAIL:** F byte-identical; no Kelly / sizing / cash-threshold / envelope
change (those are Stage 1). The change is additive at the wall and dispatched at
`_apply_streak` — the legacy global halt is fully preserved. New acceptance
`test_per_lane_halt.py` (8 tests: the wall scopes RATE_HALT and leaves F free;
LANE_KILL/orientation stay global; end-to-end submit lets F through while FLIP is
blocked; a FLIP streak halts FLIP only with F untouched; F never arms from its
own wins; the lane halt persists across a boot; reset clears the lane window; the
legacy global path is unchanged). 3 settlement-path laws re-anchored to the
per-lane form (p17 retroactive halt is now `RATE_HALT:F`; the go-live receipt
prints `lanes F` not `rate 0/N`; the stop-audit tolerates a stub without econ).
Suite 676 · preflight 23/23.

**Watch tomorrow (Stage 0 acceptance):** a FLIP rate halt does NOT stop F (Part 9
#7); the boot tape states the per-lane rail; the ops pack names any lane halt.
The Stage 1 lane (50/50 entry + disaster envelope), the recovery-surface backtest
(Stage 1.5), and Gate A (n≥20, Wilson LB > break-even) are the next rungs — and
**the Stage 2→3 treasury-waterfall prerequisite remains absolute: no extra lot
until wins and losses hit the book on the same footing.**

## WO-2026-07-22-E — FLIP THE SIDE: WE BUILT THE MACHINE BACKWARDS (build 57)

**Classification: design-error correction, not a strategy change → weekday-legal.** The tape said it
seven times in two days: **1 win in 7, and the one win was the only window where the cheap side had
depth (1.40×); the inverse of every other trade wins (−93c actual vs +110c inverse).** FLIP was
running the *inverse* of the strategy its own thesis describes — market making acquires inventory on
the side with **demand** and sells it to that demand; FLIP was buying the **abandoned** cheap side
and resting a take on the side nobody wants. **That is why `flip_fill = 38%` — the number nobody
could account for: we rested the sell against an empty side.** Correcting an inverted implementation,
observed live, is a bug fix under "let it run unless provably broken." It is provably broken.

**Read-rule at source (all TRUE):**
- The entry picked the cheap side: `lane_flip.py` `side = "yes" if yes_bid < no_bid else "no"`,
  band `OPEN_ENTRY_FLOOR (25)..OPEN_MAX_ENTRY_CENTS (42)`. TRUE — the exact inverted line.
- `_take_price` rested toward the 52c MIDDLE (`max(OPEN_MIDDLE_TARGET, entry+OPEN_TAKE_MIN)`) — a
  sell on the abandoned side. TRUE (the fill objection, resolved in the build's favour: the favored-
  side sell rests INTO the pile-in of buyers).
- The exit ran the cheap-side liquidity-hold stack: a 240s hard-no-sell (`FLIP_NO_SELL_S`), the
  F-agrees patience hold-conversion, the relative SALVAGE floor, the CATASTROPHE floor, the
  SPOT_DECIDED walk-to-scratch, the time-based WALK_DOWN. TRUE — all premised on "a low mark is
  illiquidity, hold through it," which is exactly backwards on the favored side.

**Built — one change, maximally attributable:**
- **Entry buys the FAVORED (higher-priced) side** — `side = "yes" if yes_bid > no_bid else "no"`,
  band `OPEN_ENTRY_MIN_C (50) <= join <= OPEN_ENTRY_MAX_C (70)`. **50 is a HARD floor** (the rest-back
  wall enforces `band_lo`, so no entry ever rests below 50 — the old bug cannot survive in a corner).
  `trend_usd` and `depth_ratio` are **LOGGED, never gated** (the confirms earn a gate from data, not
  from n=7): the entry `why` is now `OPEN50 favored {side}@{join}c (… ratio …x trend $… agree·logged)
  target {t}c (+…, cap 90) · stop {join−10}c · confirms logged-not-gated`.
- **The cheap-side swing gate and the volatility trend-SKIP are RETIRED** from the entry path (the
  `_swing_gate` method survives but no longer gates); the only entry filter is favored-side ∈ [50,70].
- **Target = `entry + OPEN_GOUGE_C` (20), capped 90** — `_take_price = min(90, entry+20)`; the +20
  sold into the pile-in.
- **The exit is ONE momentum stop** — `mark <= entry − OPEN_MOMENTUM_STOP_C` (10) for **2 sustained
  polls**, **NO hold** (an adverse move on the favored side means the thesis is already wrong — a 60c
  contract with depth is far less noisy than a 30c tail, so a tight stop is legible where it was
  fiction on the cheap side). **Maker-first:** rest at the stop; cross at mark only if the book has
  already gone through it. The 240s hold, F-agrees conversion, salvage, catastrophe-split, spot-
  decided walk, and walk-down are all retired into this one stop.
- **The endgame CURFEW is unchanged** (winner→F, loser→sold at `FLIP_DECISION_S`); the **DEAD-FLOOR
  backstop** is kept, now guarding a curfew-held winner that later craters (`<= 20c` with depth, 2
  polls → crossfire, `DEAD_FLOOR`).
- **`FLIP_FLOOR_BREACH` now polices the momentum stop** (`floor_expected = OPEN_MOMENTUM_STOP_C`): any
  FLIP loss beyond 10c+slip pages LOUD — kill condition #1, "the stop is fiction." With the per-lane
  rate halt (build 56) a FLIP breach never touches F.

**HARD RAIL:** F byte-identical; maker-only enforced at the gateway (entries 50-70 and exits 70-90
straddle the taker band, so a cross is a tax); 1 lot; one-shot per window; per-lane halt intact.
This is a Wednesday strategy change against the banked-Saturday law — **Drew's law, Drew's explicit
ship order, flagged deliberate.** n=7 is a real caveat: this ships at 1 lot with the target and stop
as DIALS, not laws; the mechanism (Bartlett adverse selection + the inventory argument) is
independent of the sample, and the flip_fill prediction is the falsifiable test.

**Watch tomorrow (acceptance):** every FLIP entry on the FAVORED side priced 50-70 (zero below 50);
the entry line states side, price, trend, ratio, agreement (logged-not-gated); zero taker fills on
entry (`fee 0¢`); no loss beyond ~12c (else `FLIP_FLOOR_BREACH` pages → halt the FLIP lane);
**`flip_fill` RISES from 38% — the primary success metric and the falsifiable test.** F byte-identical.

**Immediately after (not this build):** the disaster envelope fit from the recovery surface, the
residual bad-settlement-row quarantine (Drew on Render), the recovery surface from `book_snapshots`,
and — above everything — the **treasury-waterfall asymmetry**, still the ceiling before any extra lot.

## WO-2026-07-22-F — WAIT FOR THE PILE (build 58)

**Entry-discipline tuning of the favored-side lane — no new lane, no new plumbing.** The tape proved
FLIP has never once traded its intended setup: every logged entry fired **inside the first 57s**, on
a book that had either not moved (`trend $0`, two of three losses) or already finished (`skew 41`).
The engine was entering during the coin-flip and blaming the stop. Drew's rule, captured verbatim:
*"You wait for the pile. If the pile doesn't happen, the market should've decided — that's OK. But if
the pile happens, then all of these things should be true."*

**Read-rule at source (all TRUE):**
- `w.spot_ticks.append(ctx.get("spot"))` is the proven per-poll sampler; `skew_ticks` now mirrors it
  exactly (append-only, per-window, `(secs_into, skew)`). TRUE.
- The entry cutoff was `secs_into > OPEN_OPENING_WINDOW_S` (90) — a one-sided "as early as possible"
  gate, exactly backwards for the favored-side thesis. Replaced by the pile window. TRUE.
- `OPEN_GOUGE_C = 20` → the take at `entry+20`. Now 17. TRUE.
- The momentum-stop `crossed` branch (rest at the stop; cross only if the book is already through) is
  correct and left untouched, per the WO. TRUE.

**Built:**
- **`skew_ticks`** — the book skew (`|yes_bid − no_bid|`) sampled every poll like spot; **skew growth**
  = current skew − the first skew sampled at/after `OPEN_PILE_START_S` (the baseline).
- **The pile window** — entry is evaluated ONLY when `secs_into ∈ [OPEN_PILE_START_S (60),
  OPEN_PILE_END_S (180)]`. Before it, wait silently; after it, the window was skipped.
- **The all-of gate** — an entry fires ONLY if every condition agrees, in reason order: favored side
  ∈ [50,70] (`price_band`), `|trend| ≥ OPEN_MIN_TREND_USD (15)` (`flat_tape`), trend agrees with the
  side (`trend_disagree`), `skew ∈ [OPEN_MIN_SKEW_C (10), OPEN_MAX_SKEW_C (30)]` (`skew_low`/
  `skew_high`), `skew growth ≥ OPEN_SKEW_GROWTH_C (5)` (`no_growth` — **the pile itself: a static skew
  is a decision that already happened; a growing one is a stampede in progress**), and `depth_ratio ≥
  1` (`ratio_low`). Otherwise the window is SKIPPED.
- **`OPEN_SKIP`** — a skipped window logs once at window-out: `OPEN_SKIP <market> t=<s>s reason=<...>
  skew=.. growth=.. trend=$.. ratio=..`. **Skips are the primary data product — they tune the
  thresholds** (all six are PROBE values shaped by n=3).
- **Target 20 → 17** — `_take_price = min(90, entry+17)`; a nearer target exits before a *drifting*
  pile exhausts (the barrier math is only EV-neutral in a driftless walk — the whole point of waiting
  for the pile is that drift is present). The entry `why` now carries `skew<n>/grew<n>`.

**HARD RAIL:** exit plumbing unchanged (target level + the momentum stop, dead-floor, curfew all
stand); F byte-identical; maker-only; 1 lot; one-shot; per-lane halt intact. The lane will trade far
**less** — every logged entry so far is now skipped — and that is the intended cost: *coverage is
ensemble, caution is lane; the gate tightens and nothing is loosened to chase participation.*

**New acceptance `test_pile_gate.py` (14 tests):** enters when the pile forms; target `entry+17`,
stop `entry−10`; too-early (<60s) refused; past-window (>180s) skips + logs `OPEN_SKIP` with values;
each all-of condition isolated (`flat_tape`/`trend_disagree`/`skew_low`/`skew_high`/`no_growth`/
`price_band`/`ratio_low`); a no-favored-side window is `no_pile`; `skew_ticks` recorded every poll.
Suite green · preflight 23/23. Extensive FLIP entry re-anchoring (the two-poll pile prime replaces the
single-evaluate entry across the FLIP test suite).

**To resume the lane:** FLIP is under `RATE_HALT:FLIP` (2 of 4 negative); F is unaffected (per-lane
halt, build 56). Order: deploy → verify F still trading → `/reset_halt` → confirm the first window
produces either a qualifying entry or an `OPEN_SKIP` line. **Several skips in a row is the gate
working, not a failure.**

**Watch tomorrow (acceptance):** zero entries before 60s or after 180s; every skip logs `OPEN_SKIP`
with a reason and the four values; every entry shows skew, skew-growth, trend, agreement — all
conditions true; target `entry+17`; **zero entries on `trend $0`**; F byte-identical; a FLIP halt does
not stop F.

**Still open (not this build):** the exit-rejection cascade (a rejected cover still escalates to a
crossfire flatten below the stop — needs the Render line showing the rejected order's
`purpose`/`post_only`); `WINDOW_ECON_DIVERGENCE` back at 18c (**do not run 24h unattended until
explained**); the residual bad-settlement-row quarantine; and — above everything — the **treasury
waterfall asymmetry**, still the ceiling before any extra lot.

## WO-2026-07-22-G — PRE-FLIGHT: WHAT MUST BE TRUE BEFORE AN UNATTENDED RUN (build 59)

**The minimum set that lets FLIP run unattended.** F alone needs nothing in code (one operational
rule, below). If FLIP runs, two blockers had to close, plus three bundled fixes that prevent a future
misread.

### 🚨 Blocker 1.1 — EXIT OWNERSHIP (capital risk)
`_exit_count` clamps each exit to `min(record, booked_held)` — correct, but PER PROPOSAL. Two closing
authorities in one cycle (the uncovered-leg flatten and the lane's own bail) both read
`booked_held=1` and each clamped to 1 → **2 sold against a 1-lot position → an unintended short (the
10:19 −87c)**, the only path here that can produce an *unbounded* position on a 1-lot lane.

**Fix (safe form of the WO's (a)/(b)):** the safety **FLATTEN SUPERSEDES** — when it fires (esc2), it
removes every other FLIP sell already proposed for the side this cycle (a bail, a stale take) and
crosses the full booked-net, so the flatten is the ONE close. This defends the aggregate invariant
(two authorities never sell more than held) **without** letting a self-net-rejecting maker take starve
the flatten — the exact naked-leg bug this path exists to close. `FLIP_FLATTEN_SUPERSEDES` logs the
dropped sells. (A blanket per-cycle `_exit_count` reservation was tried first and rejected: it let a
rejected take reserve the lot and starve the flatten, reintroducing the ride-to-settlement bug —
caught by `test_192145_self_net_void`.)

### 🚨 Blocker 1.2 — SKIP LOG FIRED ON TRADED WINDOWS
The `PILE_END` `OPEN_SKIP` log sat above the `self._net(market, event) != 0` position check, so any
window that traded still logged `OPEN_SKIP` once past 180s — contaminating the run's primary data
product (the skip dataset) from the first trade, silently (a reboot orphan whose in-memory record is
empty is the clearest case). **Fix:** the net-position check now returns BEFORE the skip log. One move.

### Bundle
- **§2.1 — the skew LEVEL gate is retired (skew GROWTH stays).** On a coherent book `skew ≡ 2·join−99`,
  so the skew-level gate (∈[10,30]) and the price band were ONE gate; the effective range was 55-64
  and 50-54/65-70 were unreachable. Made **deliberate**: the band is now `[OPEN_ENTRY_MIN_C 55,
  OPEN_ENTRY_MAX_C 64]` (never below fair value + a real pile; never above = never late), and
  `OPEN_MIN_SKEW_C`/`OPEN_MAX_SKEW_C` are gone. Only skew GROWTH — the pile signal — survives.
- **§2.2 — `trend_usd` is measured from the pile-window baseline, not window-open.** `skew_ticks` now
  carries `(secs_into, skew, spot)`, and BOTH growth (Δskew) and trend (Δspot) share the one baseline
  (the first tick past `OPEN_PILE_START_S`). A move that FINISHED before the window now reads trend 0
  and is refused `flat_tape` — the exact "arrived late" failure the build exists to prevent.
- **§2.3 — two stale comments corrected** (the cheap-side "buys the CHEAP side / OPEN_ENTRY_FLOOR"
  block, and the "gates on nothing yet" line above the live `ratio_low` gate) — confidently-wrong
  comments above correct code are how a "frozen universe" misread happens.

### §3 — THE OPERATIONAL RULE (matters more than any code above)
`WINDOW_ECON_DIVERGENCE` is inverted (~26c: broker vs fills), and there have been **three
`/confirm_cash` re-baselines** (−17c → −199c in one night). Each buries an unexplained gap into the
book **permanently**. **Correct unattended behaviour: let it prompt, let it go fatal, let entries halt
— never a half-asleep `/confirm_cash`** (L4 book integrity outranks L5 coverage). Investigate in the
morning by **quarantining the divergent row**, never by confirming it. *(Operational — enforced by
discipline, not this build. The cash rail already halts on its own; build 55's guards + the
`cash_diverge_diagnose.py` tool are the morning fix.)*

**HARD RAIL:** F byte-identical (`lane_fh8`/custodian untouched); no Kelly / cash / rate-halt change;
exit plumbing otherwise unchanged; the per-lane halt (build 56) still keeps a FLIP halt off F. New
acceptance in `test_pile_gate.py` (flatten supersedes a competing bail — aggregate never exceeds held;
a traded window never logs `OPEN_SKIP`; trend measured from the pile baseline, not window-open; the
band edges are `price_band`). Suite 693 · preflight 23/23.

**Watch on the unattended run (acceptance):** two exits in one cycle never sell more than `booked_held`;
`OPEN_SKIP` never appears for a window that took a position; `trend_usd` and `growth` share one
baseline; F byte-identical; a FLIP halt does not stop F; **any overnight cash prompt goes UNANSWERED →
fatal → entries halt (zero `/confirm_cash`).**

**Explicitly NOT required before running (unchanged):** the exit-rejection cascade (still owed one
Render line showing the rejected order's `purpose`/`post_only`), the pack-to-Telegram sender, and the
treasury waterfall (accounting, not run-safety — still the ceiling on scaling).

## WO-2026-07-22-J — THE REGIME LANES, PHASE 0 (build 60)

**One coach, N profiles — same mechanism, different eyes.** The full WO fields a roster (a dispatcher
routing each window to DRIFT / GAP / MISPRICE / FLAT / LATE). This build ships **Phase 0 only** — the
three changes marked "SHIP IMMEDIATELY. SAFE ALONE. IMPROVES THE CURRENT RUN," one of which closes a
live capital leak. Phases 1-3 (type-stamp instrumentation, the dispatcher, restoring the GAP/MISPRICE
profiles from git history, the pre-committed tuning governance) are the next builds — their value only
lands once the roster exists, so shipping Phase 0 alone is the correct, low-risk move.

**Read-rule at source (all TRUE):**
- **§0.1** OPEN's entry wall was `self._net(market, event) != 0` (per-MARKET); HUNT's was
  `side in w.hunts or w.posted or w.fills` (per-SIDE only). **TRUE** — with OPEN holding `no`, HUNT's
  test for `yes` passed and it entered, and buying YES while holding NO **auto-nets at the exchange**
  (two fills, two spreads, two fees, zero position). This is the only path here that can execute the
  worst trade in the system.
- **§0.2** the entry gate had `depth_ratio < 1.0 → ratio_low` (a live gate). **TRUE.**
- **§0.3** `_pile_baseline` returned on `sk is not None` alone. **TRUE** — a first qualifying tick with
  a skew but no spot gave baseline `(sk, None)`, dropping `trend_usd` to 0, so the window read
  `flat_tape` and skipped for its whole remaining ~120s, never re-selecting once spot arrived.

**Built (Phase 0):**
- **§0.1 — the ONE entry wall.** Extracted to `_market_entry_blocked(market, event)` (a per-market
  FLIP-net check), and **every lane calls it** — OPEN at its gate, HUNT before proposing. No lane can
  define its own exclusion scope anymore. Exclusivity is at the POSITION level (one net per market),
  not a latched classification — so a lane can still act when its setup appears late.
- **§0.2 — `ratio_low` retired from the gate.** `depth_ratio` is still computed and printed on the why
  (`ratio 0.60x`), but it gates nothing: normalized held/other it showed no predictive value (the one
  winner read 0.71, inside the losers' 0.56-0.85), and a dead gate only costs coverage AND silently
  shapes every future lane's dataset.
- **§0.3 — the pile baseline requires a real spot** (`sp is not None`), so a spot-less first tick can
  never freeze a window into `flat_tape`.

**HARD RAIL:** exposure is unchanged by construction — one net position per market before and after.
F byte-identical (`lane_fh8`/custodian untouched); maker-only; 1 lot; one-shot; per-lane halt intact.
New acceptance in `test_pile_gate.py` (the shared wall blocks OPEN entry on a held market; the shared
wall blocks HUNT's auto-net against a held side; the pile baseline requires a real spot) and the
depth-ratio tests re-anchored to logged-not-gated. Suite 696 · preflight 23/23.

**Watch (acceptance):** zero windows with two lanes holding a position; zero auto-net events (a buy on
the side opposite a held position); zero taker fills on entry; no loss beyond the stop+slip; a FLIP
halt stops neither F nor another lane; **F byte-identical**.

**Roadmap (Phases 1-3, not this build):** Phase 1 — the **type stamp** on every proposal/fill/log
(load-bearing for the whole ruling), `velocity = skew/secs_into`, counterfactuals on skips, per-lane
scoreboards. Phase 2 — the **dispatcher** (MISPRICE if hunt_gap≥25; GAP if velocity≥1.0; FLAT/LATE by
skew; else DRIFT) and the profiles (DRIFT = build 58/59 as-is; GAP = restore-from-git; MISPRICE =
retune HUNT to +10/−8; FLAT/LATE = log-only). Phase 3 — the **pre-committed tuning rule** (a lane
that trips its per-lane halt is suppressed, its rows read before one change re-enables it at PROBE;
twice in a day = suppressed for the day; never tune a trading lane). **Every lane is PROBE** — DRIFT
n=5, GAP n=1, MISPRICE n=2 — and the recorder decides the thresholds, not a good week.

**Still open (carried):** exit-rejection cascade (one Render line owed), `WINDOW_ECON_DIVERGENCE`
(self-halting — **no `/confirm_cash` unattended**), the bad-settlement-row quarantine, the
pack-to-Telegram sender, the widened recorder (trade prints — the biggest data gap), and — above
everything — the **treasury waterfall asymmetry**, still the ceiling on scaling.

## WO-2026-07-22-K — THE DAILY BUNDLE (build 61)

**One command, one file, everything the engine logged, bounded to the day.** `/daily` builds a single
`.xlsx` and sends it to Telegram as a document — a sheet per logged table plus the computed
SCOREBOARD, tappable on a phone. **Read-only, off the trading path entirely** — no order code touched.

**Read-rule at source (all TRUE):**
- The engine writes 13 tables; `surface_rows` (the reasoning ledger — `state` + the free-text
  `detail`) and `cell_outcomes` (per-trade outcomes) are the two that matter most, and joining them is
  the daily analysis. `book_snapshots` is ~86k rows/day at 1 frame/s. `booked_fills`/`window_econ`/
  `failures`/`d_budget_decisions` are created lazily (absent in a fresh DB). `engine_state` is
  internal. TRUE — the builder introspects the live DB and skips tables that don't exist.
- The command surface is a whitelist (`ops.py`), grown once before by the read-only `/scoreboard`;
  `scoreboard_lines` is computed, not a table. TRUE — `/daily` joins the whitelist the same way.
- The Telegram transport sends text via `sendMessage`; there was no document path. TRUE — added
  `send_document` (multipart `sendDocument`, unwired-safe).

**Built:**
- **`/daily`** on the command whitelist → `daily_fn`, wired in the runner to build the workbook to
  `/tmp`, send it, and delete it (nothing accumulates on the cash-rail disk). `/daily N` pulls N days
  back; default today (`ts >= local start-of-day`).
- **`daily_bundle.py`** — a self-contained module. **Read-only by construction**: it opens its own
  `sqlite3.connect("file:<path>?mode=ro", uri=True)` connection, so it can never take a write lock on
  the settle path. Sheets: `SCOREBOARD` (the computed lines) · `decisions` (surface_rows) ·
  `cell_outcomes` · `fills` · `booked_fills` · `settlements` · `window_econ` · `window_outcomes` ·
  `cash_movements` · `failures` · `boots` · `budget` (d_budget_decisions) · `book_sample`.
- **`book_sample` decimation (§3)** — one frame every `decimate_s` (default 15) seconds, **plus** every
  frame within ±`trade_pad_s` (10) of a fill, so the moments that matter keep full resolution
  (~86k → ~5.7k rows/day). **Size guard:** if the finished file exceeds the 48 MB Telegram-safe limit,
  `book_sample` is dropped and the smaller workbook is sent with a note — never a silent failure.
- The xlsx is written with the **stdlib alone** (a zip of OOXML with inline strings) — no third-party
  dependency (openpyxl/xlsxwriter are not installed and adding one is a deploy risk on the live box).

**HARD RAIL:** read-only export — no trading path touched (`lane_flip`/`gateway`/`lane_fh8`/custodian
unmodified). F byte-identical. `Ledger` gained one field (`db_path`) so the RO connection can find the
file. New acceptance `test_daily_bundle.py` (8 tests: one valid xlsx with a sheet per existing table +
SCOREBOARD; scoped to the day; book_sample decimated AND keeping trade bursts; **read-only leaves the
DB unchanged and a mode=ro connection rejects writes**; the size guard drops book_sample over the
limit; dated filename; `/daily` on the whitelist and dispatching; `send_document` unwired falls back to
a log, never raises). Two command-surface laws re-anchored (the whitelist grew by `/daily`, exactly as
it did for `/scoreboard`). Suite 704 · preflight 23/23.

**Watch (acceptance):** `/daily` returns one `.xlsx` tappable on the phone; a sheet for every logged
table plus SCOREBOARD; book_sample decimated + full-res around trades and the file opens on a phone;
scoped to the day; read-only (cannot lock the settle path); built in `/tmp`, sent, deleted.

## WO-2026-07-22-L — ALL LANES ON: THE CROSS-LANE WALL (build 62)

**The ruling: every built lane on, every rail kept, tune from `/daily` once a day.** Three independent
limiters cap the cost of being wrong — 1 lot/trade, per-lane rate halts, the $10.22 drawdown floor —
so coverage expands while exposure does not. This build ships the **§2 blocker** ("ship before anything
else") and confirms the rest is already in place or is a capital-gate ruling.

**Read-rule at source (all TRUE, one honest divergence):**
- **§2** `_net` read only the `"FLIP"` position key. **TRUE** — F holds under `"F"`, so F could hold
  `yes@98` while OPEN bought `no@60` in the same market: an **auto-net at the exchange** (two fills,
  two spreads, zero position), and neither lane's wall blocked the other. The docstring already claimed
  the wall was "shared by EVERY lane"; the code wasn't. This is the 10:19 double-sell one level up —
  cross-lane instead of intra-FLIP.
- **DIVERGENCE from the WO's suggested fix (reported honestly):** the WO said "change `_net` to sum all
  lanes." `_net` has a **second caller** at the take-quote (`held = net>0 and side=='yes'`) that needs
  FLIP's OWN net — summing F's `yes@98` there would mis-orient FLIP's own take. So `_net` stays
  FLIP-only and a **new `_market_net`** sums all lanes; the wall (`_market_entry_blocked`) uses it.
  Same policy, no collateral corruption.
- **§3** `build_registry` returns "all five lanes, always" (F → H8 → FLIP → D → P). **TRUE** — H8/D/P
  are already registered and evaluated every cycle; they are n=0 because their bands never appeared,
  NOT because they're disabled. "Turn on every built lane" needs **no code change** — they are on and
  condition-gated.

**Built (§2):** `_market_net(market, event)` sums every lane's net in the market; the entry wall refuses
any FLIP entry (OPEN + HUNT) when it is non-zero — one net position per market, first lane there owns
it. F evaluates first in registry order, so its position is visible to FLIP's wall before FLIP
proposes.

**HARD RAIL:** F byte-identical (`lane_fh8`/gateway/custodian unmodified); maker-only; 1 lot; per-lane
halt intact. New acceptance in `test_pile_gate.py`: the cross-lane wall blocks a FLIP OPEN entry when F
holds the opposite side under its own key (and `_net` stays FLIP-only, 0); once ANY lane holds a market
(here D), both a FLIP OPEN pile and a HUNT needle are refused — no two lanes can hold opposing sides.
Suite 706 · preflight 23/23.

**Honest residual (the one the WO's F-byte-identical rule leaves open):** the wall closes the
OPEN/HUNT-side of the collision (a FLIP lane buying opposite a held position — the direction the WO's
own code targets). The **reverse** — F entering opposite a position OPEN already holds — is NOT closed,
because F is byte-identical and its only cross-lane check is a lane-COUNT cap (`CROSS_LANE_CAP=3`), not
a side-net exclusion. It is bounded by the narrow price/time overlap of the bands (F 95-99 late; OPEN
55-64 in the 60-180s pile) and the F-first registry order, and closing it fully would require touching
F, which this WO forbids. Flagged for the daily read.

**Not code this build (confirmed status):**
- **§3 enablement** — F/OPEN/H8/D/P already registered and on. H8 (n=0, band 80-94) is the genuine new
  test; it will trade when its band appears. **HUNT is the one open ruling (§3.1)** — the scoreboard
  says it loses in every cell over ~18 trades; enabling it buys more n on a table already shown to
  mis-predict. WO recommendation: OFF. It is currently ON (fires on a needle). **A capital-gate call —
  surfaced to Drew, not changed unilaterally.**
- **§4 measurement** — 4.1 (fill lane+cell) and 4.5 (window_econ both pnl columns) already work.
  4.2 (skip counterfactuals), 4.3 (surface_rows.detail on every decision), 4.4 (halt trigger cells),
  and **4.6 (the empty DODGED salvage curve mis-flagging F's ⚠)** are measurement gaps — 4.6 is a
  scoring-margin change worth doing carefully as its own build, not rushed alongside a capital blocker.
- **§6 the daily loop** — an operating procedure (pull `/daily`, read `fills_pnl` BY LANE, tune the
  cells the rows indict, log it, let tomorrow grade it), served by the build-61 `/daily` bundle.

**Watch (acceptance):** zero auto-net events (no market with opposing-side fills across lanes); per-lane
halts fire independently and never stop F; zero taker fills on entry; `window_pnl`/`fills_pnl` converge;
F's ⚠ clears once the DODGED curve populates; `/daily` delivers the workbook. **Read `fills_pnl` BY
LANE** — a green week could still be F carrying three losers.

## WO-2026-07-23-A — MAKE THE DAILY PACK TELL THE TRUTH (build 63)

**The ruling: put the truth NEXT TO the model.** Instrument only — no trading behaviour change. The pack
grades itself: realized P&L beside every cell's margin, and the model's break-even graded against the
loss that actually happened. Where the model was priced on a **retired** constant, the pack computes the
honest number; the **trading path keeps the old number, byte-for-byte** (acceptance #9, a KILL CONDITION).

**Read-rule at source (all TRUE):**
- **A1** `breakeven()` (scoring.py:85) for OPEN reads `bail = max(1.0, mid - OPEN_UNDETERMINED_BAND[0])`
  (= mid − 35) and `take = OPEN_TAKE_CENTS` (= 20). **TRUE** — `OPEN_UNDETERMINED_BAND` is the retired
  band-exit anchor and 20 is the stale take; the live exit is a flat `OPEN_MOMENTUM_STOP_C` (10) stop
  and an `OPEN_GOUGE_C` (17) take. The model's OPEN break-even was computed on geometry the lane no
  longer trades (0.588 modeled vs 0.37 honest at cell 60-64).
- **A2** the hold-lane branch sets `loss = float(mid)` — a **total** loss. **TRUE** — F salvage-cuts a
  loser at ~−40¢, not the full −97¢; assuming total loss inflates the hold break-even (0.97 modeled vs
  0.93 on the realized 40¢ average at cell 95-99).
- **A3** `SALVAGE_ADJ_MIN_N = 20` (config) is the gate before the DODGED recapture adjusts the hold
  loss. **TRUE and unreachable** — F has ~9 losses in ~285 trades, so the curve never reaches n=20 and
  the adjustment never fires. The pack uses a reachable `SALVAGE_ADJ_MIN_N_HONEST = 8`.

**Design decision (the honest divergence, reported):** the WO frames A1/A2/A3 as fixes to the scoring
model. Tracing `breakeven() → bars_for_cell() → score() → tier_for()`, the tier is a **custody-scaling**
input (`sizing.size_order` removes the tier from the ENTRY path — entries are full Kelly), and the live
tier→custody state **cannot be verified from here**. Since acceptance #9 (F/OPEN byte-identical) is a
KILL CONDITION, the corrected numbers are computed **PACK-SIDE ONLY**: new read-only
`breakeven_honest`/`loss_modeled_honest`/`scoreboard_rows`/`fills_pnl_by_lane` in scoring.py, all reading
through `ledger.db` alone. The trading-facing `breakeven()` and `SALVAGE_ADJ_MIN_N` are **left untouched**
— guaranteeing byte-identical trading. A test asserts `breakeven()` still returns the stale geometry
(0.97 for F 95, and ≠ `breakeven_honest` for OPEN), i.e. the honest math did not leak into the ladder.

**Built:**
- **Scoreboard, structured (B1/B2):** `scoring.scoreboard_rows` — one row per cell with `pnl_day_c` /
  `pnl_life_c` (realized MONEY, B1) beside the margin, and `loss_modeled_c` / `loss_actual_c` /
  `be_implied` / `model_error` (the model graded against the realized loss, B2). `daily_bundle` renders
  it as a real table, not text lines.
- **SUMMARY sheet, leading (§4, acceptance #8):** money (`fills_pnl_by_lane`, day+life, C2), expectation
  (n/wins/hit-rate), model health (cells whose `model_error` ≥ 8 win-rate points, ranked), fees by
  lane×action (B6), anomalies by `why_tag` ranked (C3), fills-by-size (B8), and open questions (thin
  cells n<10; the B4 deltas / B5 counterfactual gaps named honestly rather than left blank).
- **A1** OPEN honest loss = `OPEN_MOMENTUM_STOP_C`; take = `OPEN_GOUGE_C`. **A2** hold loss falls back to
  the realized average (`min(mid, realized_avg)`) when the DODGED curve is short. **A3**
  `SALVAGE_ADJ_MIN_N_HONEST = 8`.

**HARD RAIL:** `breakeven()`, `SALVAGE_ADJ_MIN_N`, `bars_for_cell`, `score`, `tier_for` UNCHANGED;
`lane_fh8`/gateway/custodian untouched; the pack opens its own `mode=ro` connection (a `_LedgerRO` shim
over it — reads only, no write path). New acceptance in `test_daily_bundle.py` (14 tests, all pass):
SUMMARY leads; scoreboard carries realized money + model_error; OPEN BE anchored on the live constant
not the retired band; hold loss falls back to realized not total; salvage gate reachable; **trading
break-even byte-identical**. Suite 712 · preflight 23/23.

**Deferred / assessed (reported honestly):** B3 (lifetime cell rows — the scoreboard already carries
`pnl_life_c`), B4 (day-over-day deltas — needs a persisted prior-day snapshot; flagged in SUMMARY open
questions), B5 (skips + counterfactuals — partial via failures/decisions sheets; a full counterfactual
join is its own build), B7 (expectation ±σ — SUMMARY carries n/wins/hit-rate, not the full σ band), C1
(split the TIER column into tier/kelly_lots/actual_lots — the text scoreboard is kept for continuity;
the structured sheet supersedes it). The **HUNT enable/disable ruling** (from WO-L §3.1) and **§4.6 the
empty DODGED curve** remain open capital-gate calls, surfaced to Drew, not changed here.

## WO-2026-07-23-B Part 2 — THE FLATTEN HAS A PRICE FLOOR (build 64)

**The ruling: the uncovered-leg flatten must not sell at whatever the book shows.** Ships BEFORE Part 1
(F scaling), because the leak scales linearly with contracts — 21c/night at 3 lots becomes 56c at 8.

**Read-rule at source (TRUE):** `lane_flip.py` esc-2 flatten built the Order with `price_cents=mark`
(`book.best_yes_bid()`/`best_no_bid()`), crossfire, with no reference to the declared `entry −
OPEN_MOMENTUM_STOP_C` stop. **TRUE** — measured on tape: four of six stops held to 1c, two blew through
(230230 entry 61 → exit 45, 6c through the stop; 222100 entry 59 → exit 34, 15c through), 21c of excess
loss on a night that netted 16c.

**Built:**
- **The floor** = `entry − OPEN_MOMENTUM_STOP_C − SLIP_TOLERANCE_C` (slip = 3). The entry is the
  count-weighted cost basis of the **unsettled ENTRY fills** (`_flip_entry_price`, read from the booked
  ledger so it survives an in-memory reconcile), not a bucket field that can be cleared.
- **Bounded ride, then counted cross:** when the book is already through the floor, the flatten rests
  ONE poll AT the floor (maker, `crossfire=False`) instead of dumping at `mark`. If that rest fills, the
  leg heals — no cross. If it does not, the next poll crosses at `mark` and logs `FLIP_FLOOR_BREACH`
  with `overshoot = floor − mark`. A cross below the floor is never silent.
- **No behaviour change when healthy:** `mark ≥ floor` crosses immediately with the ordinary
  `FLIP_UNCOVERED_FLATTENED` tag (the existing tests — marks all above their floors — stay green).
- **`SLIP_TOLERANCE_C = 3`** added to config.

**Root note (honest, partial):** §2.4 item 4 (the cover getting `VENUE_REJECTED: post only cross` should
re-price and re-post rather than escalate) is NOT changed here — the floor is the safety net that bounds
the damage regardless of why the cascade was reached. The re-price-the-cover root fix is a deeper change
to the cover path, assessed and left for a follow-up; the floor makes the current escalation safe to run.

**HARD RAIL:** FLIP-only (F/`lane_fh8` untouched — the WO's kill condition is "any F behaviour change
other than size"); the supersede invariant (§1.1) preserved — the floored flatten still drops competing
FLIP sells first. New acceptance in `test_uncovered_flatten.py` (rest-then-breach, rest-fills-heals,
above-floor-crosses-unchanged) + updated supersede test. Suite 715 · preflight 23/23.

## WO-2026-07-23-B §4.1 + Part 1 — REQUESTED-VS-FILLED, THEN SCALE F (build 65)

**§4.1 (ships with Part 1 as its instrument):** every booked fill now carries `requested_count` and
`requested_price` beside the filled `count`/`price` (ledger columns, migrated with `ALTER TABLE`;
populated at the fill-booking path from the originating order and at the custodian cut). "Requested vs
filled" is the field that answers **whether size travels** — the one unknown scaling F introduces, which
cannot be backtested. NULL on rows written before the column existed or with no originating order (never
a fabricated number). Acceptance #2 met.

**Part 1 — SCALE F.** F earns ~98% of the book's profit and was capped at 3 contracts.

**Read-rule divergence (reported, ruled):** the WO cited only `sizing.py:74` / `config.py:276`
(`NET_RISK_CROSS_LANE_CAP=3`). **Verified FALSE-as-complete:** F is *also* hard-capped by the gateway
`_wall_net_risk_and_at_risk` (NET_RISK count ≤3 **and** DOLLAR_RISK ≤`AT_RISK_CAP_CENTS`=297c) **and** by
`_wall_pct_of_book` (BUDGET: order notional ≤10% of book). Sizing F to 8 alone left every F>3 proposal
dying at the wall (`0+12 > 3`) — a governor by accident. Surfaced to Drew.

**Drew's ruling (2026-07-23):** *per-lane, book-proportional dollar wall — keep the gateway guard, don't
exempt F.* Implemented:
- **F self-sizes** to `F_NOTIONAL_PCT` (0.20) of book / price, bounded only by real depth — Kelly and
  the count cap no longer touch F (`sizing.size_order(..., lane="F")`). Every other lane keeps
  `min(kelly, depth, cap)`.
- **The wall is now per-lane proportional:** `AT_RISK_PCT = {F:0.20, H8:0.05, FLIP:0.05, D:0.02, P:0.02}`,
  `at_risk_cap_cents(lane, book)`. The retired count cap is subsumed (at 97c both bound at 3); the
  **NET_RISK / DOLLAR_RISK reason names are kept** so WALL_STORM telemetry stays comparable. When
  `book_cents ≤ 0` (boot/test) the legacy flat walls stand unchanged.
- **The per-order BUDGET wall is the same fixed-fraction cap** — Drew's principle applies identically, so
  it too is lane-proportional (F's per-order budget rises to 20%; every other lane keeps the 10% floor,
  since their proportions are smaller). *(This third wall was not in Drew's text; his stated principle —
  "a fixed cap holds F flat as the book rises" — governs it, and leaving it flat would silently re-cap F
  at ~4 lots, defeating the 8-lot ruling. Reported here as an honest extension.)*
- **Guards:** (a) total deployed ≤ 50% of book across ALL lanes (`ledger.deployed_cents()` clamps each
  entry in `_score_and_size`; skipped at book≤0); (b) any single F loss > `F_EVENT_TRIPWIRE_C`=60c per
  contract suppresses F for the day (`ledger.set_f_tripwire`/`f_suppressed`, day-scoped, self-clearing;
  fired from both F-loss choke points — held-to-settlement and the custodian conclusion; the rate halt
  structurally can't protect a 97%-win lane); (c) the rail is untouched; (d) kelly/depth/notional/at-risk
  terms and the binding one logged (`F_SIZE`).

**Why proportional not exempt (Drew):** the wall's real job is catching a **sizing bug**, not market risk —
three stale constants shipped in three days; F should not be the one lane with no gateway backstop. And a
fixed cap turns compounding into linear growth (4.7%/day at $41 → 0.98% at $200); proportional fixes the
decay. **Hard condition honored:** Part 2 (the flatten floor) shipped first, in build 64.

**HARD RAIL / kill conditions:** F behaviour unchanged OTHER than size; deployed never > 50% of book;
the F tripwire fires > 60c/contract; the rail untouched. New acceptance `test_scale_f.py` (9) + rewritten
`test_walls.py` (per-lane proportional, scales-with-book, lane-aware budget) + updated sizing tests.
Suite 725 · preflight 23/23.

**⚠️ The honest caveat (§1.6, carried forward):** F's edge is the salvage — +1.62c/contract at a −40c
salvaged loss, −0.20c if a loss goes full 97c — resting on 9 losses, **none in the last 81 trades**.
Scaling multiplies exposure to the unmeasured half. Guard (b) is the mitigation; §4.4 (next) is how the
loss size finally becomes readable. Treat the first F loss at 8 lots as the most informative event in the
project and read it immediately.

## WO-2026-07-23-B §4.4 + Part 3 diagnosis + rolling remainder (build 66)

**§4.4 — the F blocker (acceptance #7): DELIVERED.** `scoring.lifetime_cell_aggregates` returns, per
(lane, cell), lifetime `n / wins / losses / avg_win_c / avg_loss_c / realized_pnl_c` — read from the
whole `cell_outcomes` record, not the day. It rides the pack as a **LIFETIME_CELLS** sheet, and F's
lifetime `avg_loss` also leads the SUMMARY under **F BLOCKER (LIFETIME)** — the "what does an F loss
actually cost?" number that Part 1's ceiling rests on is now readable. Test: `test_daily_bundle.py`
(lifetime aggregates carry F's avg_loss; the sheet + SUMMARY line present).

**Part 3 — halt on money, not count: DEFERRED, with the §3.4 diagnosis the WO required.** The WO gates
Part 3 on trusting `fills_pnl` first ("a better rule on a wrong input is still wrong"). Diagnosis at
source:
- The per-lane P&L the rate halt consumes is `surface.settle_market`, computed from
  `SELECT lane, side, action, price_cents, count` — **`fee_cents` is not even selected**, so the halt's
  per-lane number is **GROSS of fees** while the round-trip receipt (`record_fill`'s `cell_outcome`) is
  net. Every window's `fills_pnl` is high by its fees.
- A closed round trip is re-derived through the **settlement-outcome lens** (entry "collects payoff",
  exit "forgoes payoff") rather than read as the realized (exit − entry) it already booked. For a clean
  same-side round trip these are algebraically equal, but a **SELF_NET-booked exit** (an opposite-side
  buy recorded as EXIT at 100 − price) or an **unmatched leg** (`net_held < 0`) breaks the equality —
  the likely source of the live `+17c → −4c` divergence Drew saw.
- **Ruling:** re-tuning the halt to SUM `fills_pnl` would inherit both errors. Part 3 is held until
  `fills_pnl` is reconciled (net-of-fees + the closed-round-trip path reconciled against the exit-booked
  cell_outcome). The live count-halt (2-of-4) is unchanged; nothing at risk moves. **Flagged for Drew:**
  the fee omission may also affect settlement book-cents — worth its own careful build, not rushed
  alongside a live-halt change.

**Part 4 rolling remainder (assessed):** §4.7 SUMMARY-sheet-first (DELIVERED, build 63, extended here).
§4.5 model-vs-reality per cell (be_modeled/loss_actual/model_error — DELIVERED build 63; `ev_per_trade`
as primary sort not yet). §4.6 failure counts WITH day-over-day delta and expectation-vs-outcome σ —
**deferred:** the delta needs a persisted prior-day snapshot (a data-plane addition), flagged honestly in
the SUMMARY "open questions". §4.2 market context at every decision and §4.3 per-trade MFE/MAE excursion
(the target×stop EV grid) — **deferred:** sizable new joins to `book_snapshots`, rolling. Acceptance #8
(failure counts carry a delta) is the one criterion NOT yet met — it is gated on the prior-day snapshot.

## COLD READ — THE PHANTOM BOOK BUG (build 67)

**The finding: the settlement math, the ledger, and the quarantine were all correct. The bug was that
`account_value()` read venue `cash + position_value` at a moment those two are not consistent — and that
unreconciled number was used as both the window-P&L basis and the reported "book."**

**Read-rule at source (every claim verified):**
- **§2 ruled out, all clean:** the settlement P&L math (`surface.py:158-165`, `(payoff−basis)·count` →
  `no@97×8` = +24c), `record_settlement`'s idempotency + net-vs-gross bound, the quarantine (`booked ==
  fills_pnl → no-op`, and it correctly did nothing because settlements held +24c), and `book_cents()`
  (cash_movements + settlements) — **all TRUE and clean.** The ledger was right the whole time.
- **§3.1 the source** — `shadow_runner.py:740` returns `int(round((cash + (pv or 0)) * 100)), "venue"`.
  **TRUE.** `cash` and `pv` settle on different venue clocks.
- **§3.2 the two worst moments** — `account_value()` is read at `open_bracket` (ENTRY submit, `:1358`)
  and `close_bracket` (settlement, `:1528`). **TRUE.** At entry the cash is debited but the position
  isn't reflected (reads LOW by the notional); at settlement the cash is credited but the position isn't
  cleared (reads HIGH by the notional). `window_econ.py:229` differences the two — **both errors push the
  same sign**, so the gap = the entry notional (99c/1 lot, ~795c/8 lots — matched every tape sample).
- **§4 the second bug** — `window_econ.py:266` passes `account_value_cents` into a param named
  `book_cents`, so the `📊 … book $X` line was the raw venue read mislabeled. **TRUE.**
- **§4 the safety-critical check (F sizing):** `_score_and_size` reads `self.ledger.book_cents()`
  (`shadow_runner.py:483, 542`), **NOT** the venue `account_value()`. **The phantom never fed F's
  size** — the one place §4 warned it could cost real money is clean.

**The fix (Fix 1 + Fix 4, which Fix 1 makes automatic):**
- A new `bracket_book(now)` returns `(ledger.book_cents(), "ledger")`. `open_bracket` and `close_bracket`
  use it instead of the venue read. `window_pnl = ledger_close − ledger_open − cash_moves` = the window's
  settlement delta, phantom-free; and since `close_bracket` now hands the ledger book to `_apply_streak`,
  the `📊 book $X` line is finally the real book (Fix 4).
- **The venue read is kept for `standing_reconcile` only** (`:858`, P9 §3 — venue-vs-ledger via the cash
  protocol, every 60s, on a cadence AWAY from fills/settlements). That is what it is good for (Fix 2);
  Fix 3 (validate-then-defer) is subsumed — the bracket no longer reads the venue, so there is nothing to
  validate.
- **The live invariant is amended:** `_reject_paper_in_live` now accepts `"ledger"` (a real reconciled
  number) as well as `"venue"`; only `"paper"` (a shadow-mode fabrication) still FATALs in live.
- In **SHADOW mode `account_value()` already returned `ledger.book_cents()`**, so this changes LIVE only
  (where the phantom lived); the whole suite was unaffected but for the go-live dry-run, which now
  *validates* the fix (bracket source `"ledger"`, `window_pnl == fills_pnl`).

**What it means:** no money was lost (Kalshi's $43.06 was the truth, account up 11%); the ledger was
never wrong; the divergence alarm was honest every time it fired (it reported a real inconsistency
between two reads); `fills_pnl` is the trustworthy number. **This also retires the WO-2026-07-23-B Part 3
blocker's root** — the `window_pnl` phantom that made the halt's input untrustworthy is gone (the halt's
per-lane input was always fills-truth; the phantom lived in `window_pnl`, now ledger-based).

**Honest residual:** `window_pnl` is a GLOBAL book delta, so if another market settles inside this
bracket's span its P&L contaminates the delta and `WINDOW_ECON_DIVERGENCE` may fire on the overlap — a
pre-existing property (the venue read was global too), benign (the quarantine no-ops when settlements ==
fills), not the entry-notional phantom this build removes. Flagged for a follow-up (make `window_pnl`
per-market = `fills_pnl`). New acceptance `test_cold_read_phantom.py` (3). Suite 730 · preflight 23/23.

## WO-2026-07-23-C — FOUR BUGS FROM A COLD READ (build 68)

**Bug 2 — the find — SHIPPED (root cause).** Read-rule TRUE: `gateway.py` rest-back (`:282-284`) is gated
`purpose=="ENTRY" and action=="buy"`; an EXIT is `action="sell", crossfire=False` → `post_only=True`
(`:335`) with no re-pricing, and `_rest_back_price` is buy-side only. So a maker exit priced at/through
the bid (a stop into a falling book) was refused `post only cross` → the cover never confirmed → grace
expired → the custodian flattened at market below the stop. **The custodian fix and the Part 2 floor were
both downstream; this is the cause.** Fix: `_rest_forward_price` — the sell-side mirror (rest STRICTLY
above the bid, at/above the derived ask, or `REST_BACK_SKIP`), wired into `submit` **outside** the `not
risk_reducing` block (an EXIT *is* risk-reducing — the very orders that needed it were skipping the
entry-only rest-back). **Lane F EXCLUDED** (acceptance #6): F's custodian salvage posts a maker sell too,
and this build leaves F byte-identical — F keeps its maker→crossfire-after-R salvage escalation. Honest
follow-up: F's salvage exhibits the same Bug 2; extending the mirror to F is a separate, F-touching build.

**Bug 1 — SHIPPED, with a read-rule divergence.** The WO's suggested fix (`if w.posted.get(side): return`)
**already exists** at `lane_flip.py:737` (`if w.opens or w.posted or w.fills: return proposals`), *before*
the `:761` wall — so "OPEN: none" is FALSE. The real gap: `w.posted` is set only by `on_submitted`, which
**never runs when a submit raises** — a `VENUE_AMBIGUOUS` reject (`gateway.py:347`) whose order actually
landed. Neither the intent guard nor `_market_net` (the FILLED map) then knows about the resting order, so
OPEN re-proposed over it. Fix (the WO's structural Bug 1b): `_market_has_live_order` counts the gateway's
own resting ENTRY orders; `_market_entry_blocked` now blocks on `net != 0 OR a live resting entry` —
closing it independent of the intent callback. FLIP-only; F untouched.

**§5 BANKED LAW:** *never gate an action on FILLED state when the action can precede the fill.* Gate on
INTENT (the order was submitted) and clear it on fill/cancel/reject. `state.traded` (F) and `w.posted`
(HUNT) were the correct pattern; the order-aware wall extends it to OPEN. Three bugs in three days shared
this shape (double-sell, exit clamp, OPEN triple-entry).

**Bug 3 — halt on money, not count: DEFERRED (again), per the WO's own gate.** `window_econ.py:316/329`
still counts negatives. The WO conditions the re-tune on "verify on the next tape that `window_pnl` and
`fills_pnl` now agree" — a LIVE-tape check not performable from here. Structurally, build 67 made
`window_pnl` ledger-sourced and the per-lane halt already read fills-truth, so the input is ready; the
change (sum over `RATE_HALT_WINDOW_N=8` < `-RATE_HALT_DRAWDOWN_C=40`) is a real-money halt-logic change
with a wide test blast radius, warranting its own build once Drew confirms the tape. Ship #4, held.

**Bug 4 — `ACCOUNT_VALUE_UNREADABLE` ×42: INVESTIGATION, not performable here.** `shadow_runner.py:735`
`venue.get_balance()` fails; the exact-42 regularity two days running is a pattern (rate limit / timeout /
endpoint condition), diagnosable only against the live venue this environment can't reach. Lower impact
post-67 (brackets no longer depend on it) but it still gates `standing_reconcile`, now the only venue
cross-check — flagged so a silently-stopped reconcile can't hide a real divergence.

**Acceptance:** #1 zero `post only cross` on exits — the mechanism is removed (sell rest-back); #2 zero
windows with >1 live OPEN entry/side — the order-aware wall enforces it; #3 `FLIP_UNCOVERED_FLATTENED`
drops toward zero — follows from #1; #6 **F byte-identical** — verified (F excluded from the sell
rest-back; nothing else touches F). #4/#5 (window/fills agree; halt on money) ride Bug 3, deferred. New
acceptance `test_maker_rest_back.py` (sell mirror + F exclusion + CUT unchanged) and `test_pile_gate.py`
(order-aware wall). Suite 737 · preflight 23/23.

## WO-2026-07-23-E — THE SIZE TEST (build 69)

**Two dials, no logic change:** `FLIP_SIZE_CAP 1→3`, `OPEN_GOUGE_C 17→10`. Same entry gate, same stop,
same band, same lanes. **F byte-identical** (`lane_fh8.py` untouched; F's notional sizing never reads
`FLIP_SIZE_CAP`) — only `config.py` changed.

**Read-rule prerequisites (verified shipped in build 68):** Bug 2 sell-side rest-back (`_rest_forward_price`)
and Bug 1 order-aware wall (`_market_has_live_order`) both present; the `_rest_forward_price` band-ceiling
check only fires when `order.band` is set, and **`band=` is set only on ENTRY orders** (`lane_flip.py:870,
1180`) — exits leave it `None`, so the +10 take (entry+10, e.g. 70 from a 60 entry, above the 64 band
ceiling) is never `REST_BACK_SKIP`'d. Confirmed at source.

**Read-rule DIVERGENCE — surfaced to Drew, ruled:** the WO's §2 said "nothing else moves" and asked for
4 lots, but `FLIP_SIZE_CAP 1→4` alone can't reach 4 — `size_order` caps non-F lanes at
`NET_RISK_CROSS_LANE_CAP=3` (so a 4-cap never bites) and the gateway FLIP at-risk wall is 5% of book
(≈3 lots @60c on a ~$43 book). Both bind below 4. **Drew's ruling: set the cap to an EXPLICIT 3, widen no
wall.** An explicit 3 is deterministic regardless of book (a 4-cap would drift to 3-today/4-later — a
book-dependent confound); 1→3 is a 3× read that answers the fill-curve question; widening the at-risk
wall for 5-6 lots comes *later, with this data behind it*, never in anticipation of it.

**Why +10×3 (Drew):** per-contract the deep target wins, but fill rate degrades with distance from the
touch (today's pack: F ENTRY 86%, F CUSTODIAN_EXIT 63%, gaps as wide as 7:1). +10 rests 41% closer than
+17 while still a genuine gouge; the size test reads whether the extra fills more than pay for the smaller
per-contract capture.

**The decision rule (fixed before the data, §4, now /3):** read `requested_count` vs `count` on FLIP
EXIT orders — **≥2.6 of 3** confirms (then widen the wall for 5-6, *with data*); 1.5-2.5 hold at 3 and
tune the target; **≤1.1 of 3** reverts to +17×1. Run to 12-15 filled FLIP exits (≈one overnight). The
requested-vs-filled instrumentation is already in place (build 65 §4.1).

**Edge case (Drew's guard):** 3 lots × 60c = 180c fits the 5% wall (215c) at a $43 book, **but the wall
binds below ~$36 of book**, where FLIP silently sizes to 2. The per-trade `requested_count`/`count` log
lets any sub-3 window be filtered out rather than averaged in.

**Noted for later (NOT this build, Drew):** the at-risk wall uses `side_basis = order.price_cents` (full
notional) as max-loss-per-contract, but FLIP's real risk is the −10 stop (~11.5c) — the wall measures
notional and calls it risk (the same category error as the retired count cap). A risk-denominated wall
would let FLIP run 15+ lots inside the same 5%; it's an architecture change, logged not shipped.

**HARD RAIL / kill conditions:** F byte-identical (verified); FLIP-only; no wall widened; per-lane halt
fires independently. New acceptance `test_size_test.py` (dials, take geometry, F-independence, cap binds);
`test_flip_*`/`test_p22`/`test_swing_gate` re-anchored to the +10 take and the 3-lot cap. Suite 741 ·
preflight 23/23.

## COLD AUDIT §2 + §3 — the unattended-safe pair (build 70)

**§2 — THE BLOCKER, fixed at the source.** Read-rule TRUE: `account_value()` sums venue `cash + pv`
(`shadow_runner.py:740`) and throws the components away; `standing_reconcile` (`:873`) passes only the
total to `cash.reconcile`, which differences it against `book_cents()`. The venue moves cash and pv on
different clocks, so a read across a settlement boundary is wrong by exactly the position notional
(819c/99c/196c/198c — always the position). The prior guard defers on the ENGINE's settlement clock
(`unsettled_fills`), not the venue's. **Fix:** preserve the pv component through the read, and DEFER the
reconcile unless the venue pv agrees with the engine's own open-position notional
(`ledger.deployed_cents()` — the function was in the ledger the whole time, called only by the portfolio
cap until now) within `PV_TOLERANCE_C` (20c). This makes it *impossible* to compute the delta on an
internally inconsistent read (the adversary's test), not merely unlikely — 4 builds moved the bad number
out of one consumer at a time; this stops it being produced.

- **Honest caveat (reported):** the venue pv is `portfolio_value` — **mark-to-market** — while
  `deployed_cents` is **entry cost**, so a large-unrealized open position also defers. That is safe: a
  deferral just skips one 60s cycle and retries, and the reconcile still runs cleanly every flat window
  (both sides ≈0). Over-deferring never causes a wrong reconcile; it only delays a correct one. The
  audit's exact `abs(pv − deployed) > tol` check is implemented as specified, with this confound named.

**§3 — the display no longer shows a measured-wrong number.** Read-rule TRUE: two break-even functions
exist — `breakeven()` (stale band-floor model, feeds `score()` → margin/tier/bars) and
`breakeven_honest()` (the real stop + realized-loss fallback, build 63). The pack's SCOREBOARD already
uses the honest one (build 63); the `/scoreboard` TEXT (`scoreboard_lines`) still showed the stale margin
— the number acted on this morning.

- **Read-rule DIVERGENCE from the audit's fix (surfaced):** the audit said "point `score()` at
  `breakeven_honest()` — safe because tier doesn't gate size." **Verified FALSE-as-safe:** `score()` →
  `tier_for` → `pos.size_tier` (`fills.py:214`) → `custodian.scaled(size_tier)` (`:532`, CLEAR×1.30) —
  the tier drives **custody cut-scaling**, so changing `score()` would alter F's cuts, breaking F
  byte-identical (a kill condition). The audit checked the *sizing* path (`:448` "never a cap") but not
  the *custody* path — the same reason `breakeven()` was left untouched in build 63. **So the goal (no
  wrong number on screen) is met the safe way:** `scoreboard_lines` now DISPLAYS `breakeven_honest` for
  BE and margin, while `score()` (and thus the tier column, and custody) stays exactly as before. The
  edge you read is honest; the tier you see is the operative one the machine uses. One display change,
  zero trading change.

**HARD RAIL:** F byte-identical (`score`/`breakeven`/`SALVAGE_ADJ_MIN_N` untouched; custody scaling
unchanged); the reconcile only ever defers (never reconciles more aggressively). New acceptance:
`test_infra_e1_tracer.py` (defers on pv≠deployed, runs on agreement) and `test_p22_scoreboard.py`
(display honest, `score()` stale). Suite 744 · preflight 23/23.

**Ship list remainder (audit §7), status:** §5 (one lane taxonomy across the tables — `fills.lane` writes
"FLIP" for OPEN/HUNT, silently emptying join-based traces) — NEXT, additive (a `cell_lane` column on
`fills`, mirroring the build-65 `requested_count` migration; `fills.lane` must stay "FLIP" for the
custody/attribution queries that key on it). §4 (halt on money, not events) — now unblocked by §2's
input fix, but a real-money halt change with a wide test blast radius, its own build. §6 cleanup (retire
`NET_RISK_CROSS_LANE_CAP` now the per-lane at-risk walls exist; migrate `SALVAGE_ADJ_MIN_N` to the honest
value pack-side) — low priority. All deferred with intent, not dropped.

## WO-2026-07-23-F Part 1 — FIX THE TRUNCATION (build 71)

**Ships ALONE and first** — it corrupts the exact settlement numbers the size test would be judged on.

**Read-rule at source (TRUE):** `to_yes_terms` (`book.py:16-23`) used `int()` — `int(97.3)=97`,
`100-int(97.3)=3` (true 2.7). Called at `surface.py:159` inside the settlement P&L. The venue ticks in
0.1c and fills carry exact fractions (`fills.price_cents` is REAL; 42% of today's fills fractional), so
the truncation understated the cost basis and OVERSTATED profit — **always in the same direction**. It
has exactly one caller (`surface.py:159`), so making it precision-preserving is surgical.

**Built:**
- `to_yes_terms(side, price_cents: float) -> float` — carries the fraction (`float`, `100.0 - float`),
  no truncation.
- `record_settlement(pnl_cents: float)` — accepts the fraction; the net-vs-gross bound keeps `cost` as
  `float` (the entry basis is subpenny); the `%d` receipts became `%.1f`; the fractional pnl is stored at
  full precision (SQLite keeps a REAL in the INTEGER-affinity column, the same pattern `fills.price_cents`
  already uses). `book_cents` sums at full precision and rounds ONCE — unchanged, and now correct.
- `quarantine_divergent_settlements` compares booked vs fills within a 0.5c tolerance (an exact `==` on
  floats would spuriously quarantine an agreeing settlement); the DIVERGENT alert prints `%.1f`.

**Stated choice (WO "separately decide"):** the orderbook LEVEL bucketing (`book.py:apply_snapshot/
apply_delta`, `int(price)`) is **left truncating** — a depth heuristic that merges 97.1/97.3/97.5 into one
level, deliberately NOT a settlement number. Flagged as an explicit choice, not a side effect.

**Acceptance:** #1 no `int()` on a venue price in the settlement path (only `to_yes_terms`, now float); #2
settlement P&L carries fractions and `book_cents` rounds once (`test_truncation.py` — a no@97.3 win books
2.7c not 3, and two of them book 5.4 → round-once); #3 **F's lifetime P&L will drop ~100c and stay
stable** — the correction landing, not a regression (Adversary's note); #5 **F byte-identical** (accounting
only, no trading path touched). New `test_truncation.py` (3); `test_cash_fatal1` divergent-alert updated to
the `.1f` precision. Suite 747 · preflight 23/23.

## WO-2026-07-24-C Part 1+2 — SCOPE THE HALT, SIZE THE HALT (build 72)

Ships FIRST (the test build needs it — without it FLIP halts at 2-of-4 and never reaches 12-15 exits).

**Part 1 — retire the global fallback.** Read-rule TRUE: `window_econ._apply_streak` (`:302-310`) fell
back, when `per_lane` was empty, to a GLOBAL path that set `HALT_KEY` and halted EVERY lane on aggregate
window P&L — including F, for losses F did not cause (the one path by which FLIP's behaviour could stop
the earner). The per-lane halt itself was correctly scoped. **Fix:** deleted the global body — `if not
per_lane: log + return`. Per-lane attribution now exists on every close (`shadow_runner` settle path), so
the fallback had no job. `HALT_KEY` stays **readable** and `/reset_halt` still clears a persisted legacy
halt; **nothing sets it going forward.**

**Part 2 — the halt counts money, and scales with size.** Read-rule TRUE: the per-lane path
(`:365-370`) counted negative windows (`len(losses) >= RATE_HALT_LOSSES`), so `−8,−7,+17` (net +2¢)
HALTED. **Fix:** sum the last `RATE_HALT_WINDOW_N=8` windows' fills-P&L against `RATE_HALT_DRAWDOWN_C`,
**derived from the lane's own size** — `4 · FLIP_SIZE_CAP · OPEN_MOMENTUM_STOP_C` (120¢ at 3 lots, 400¢ at
10). A flat 40¢ threshold sized for 1-lot FLIP would strangle a 10-lot lane before its first loss settled
— the same absolute-constant error as `NET_RISK`/`AT_RISK_CAP`. The pnl carries the 0.1¢ fraction (Part-1
truncation fix). **F is unaffected in practice** (96.9% wins never accumulate that drawdown across 8
windows); F's guard stays the per-event tripwire (`F_EVENT_TRIPWIRE_C`), the right shape for a
rare-and-large loser — **F byte-identical** (`lane_fh8` untouched; the halt is a governor, not F's logic).

**Acceptance:** #1 no path sets `HALT_KEY` forward, `/reset_halt` still clears a legacy one
(`test_per_lane_halt`); #2 a FLIP halt never in F's wall reasons (`test_flip_drawdown_halts_flip_only_f_
untouched`); #3 halt fires on summed P&L, threshold from `FLIP_SIZE_CAP × OPEN_MOMENTUM_STOP_C`; #4 FLIP
survives `−8,−7,+17` (`test_profitable_asymmetric_sequence_does_not_halt`, in `test_aplayer` and
`test_p8_golive`); #8 F byte-identical. The daily pack's RATE-HALT line and the whole halt test corpus
(`test_aplayer`, `test_p8_golive`, `test_per_lane_halt`, `test_p27_governor`, `test_p17_show_up`,
`test_p9_real_numbers`, `test_halt_orphan`) re-anchored to the money doctrine; the preflight halt gate
retargeted. Suite 749 · preflight 23/23.

## WO-2026-07-24-C Part 3+4 — SIZE THE LANE, COLLECT THE DATA (build 73)

Ships SECOND (clean attribution: a risk-parameter change lands on its own build, on top of the halt that
protects it). Authority: Drew — RULED.

**Part 3 — the +4×10 dials, and the lane the cap actually binds.** Read-rule TRUE (the recurring blocker):
`OPEN_GOUGE_C` and `FLIP_SIZE_CAP` are set, but `sizing.size_order`'s non-F branch (`:108-109`) capped
every non-F lane at `NET_RISK_CROSS_LANE_CAP=3` — so `FLIP_SIZE_CAP` alone **never bound** and a "10" would
have silently sized 3, exactly as it did through WO-E. **Fix:** FLIP is now **lane-aware** — a dedicated
branch sizes `min(kelly, depth, FLIP_SIZE_CAP)`, mirroring F's own-dial path; the retired count cap no
longer re-caps it. Dials: `OPEN_GOUGE_C 10→4` (target = entry+4, a reachable maker gouge), `FLIP_SIZE_CAP
3→10`, `AT_RISK_PCT["FLIP"] 0.05→0.15` (the per-lane book-proportional dollar wall Drew ruled up to carry
10 lots). The **REVERT condition** rides in the config comment beside both constants: conversion <75% OR ≤4
of 10 fill → `FLIP_SIZE_CAP→3` AND `AT_RISK_PCT["FLIP"]→0.05` **together** (acceptance #9). Depth is the
term the live test measures — on a thin book depth binds below 10 and the reason says so.

**Part 4 — the experiment emits its own data.** #7: a `FLIP_SIZE` log fires once per `(market, price)` on
every FLIP entry (mirroring the `F_SIZE` block, lane-gated) naming the **binding term** via `dec.reason`
(`→ cap/depth/kelly bound`) — "did the 10-cap ever bite, or was depth the ceiling" is read from the tape.
#6: `target_touched` (the book reached `entry+OPEN_GOUGE_C`, our posted take — high-water stamped every exit
poll, or the exit itself made it) and `target_filled` (our **resting** take concluded the trade —
`exit_reason` still `TAKE_FILL`, no stop/handoff/floor path overrode it) ride **every** `FLIP_SWING` row.
`touched && !filled` is the central question: the middle came to us and we missed the fill.

**Acceptance:** #5 `OPEN_GOUGE_C=4`, `FLIP_SIZE_CAP=10`, wall 15% (`test_part3_dials_are_set`,
`test_size_test`); #6 touched/filled on every trade — take-fill, momentum-stop, and touched-not-filled
cases (`test_size_test_data`); #7 binding term logged on every FLIP entry, F path writes `F_SIZE` never
`FLIP_SIZE` (`test_flip_size_log_is_flip_only_f_untouched`); #8 **F byte-identical** (`lane_fh8` untouched;
F sizes by notional, a path that never reads `FLIP_SIZE_CAP`; the FLIP_SIZE log is lane-gated); #9 revert
condition in the config comment. Suite 756 · preflight 23/23.

## WO-2026-07-24-D Part 2/3/4/5 — THE MORNING FOUR, build 1/2 (build 74)

Four incomplete fixes, each read-rule verified **TRUE at source** — every one a correct idea landed on one
path and not its sibling (the fourth instance of that pattern this week). No exposure change; ships before
the size build so the doubled lane lands on a floored stop and a visible reconcile.

**Part 2 — the momentum stop had no price floor.** Read-rule TRUE: `lane_flip.py` momentum stop priced
`mark if crossed` (unbounded down) while the flatten (`:1602-1652`, WO-2026-07-23-B Part 2) already had the
full `SLIP_TOLERANCE_C` floor — the same fix on one of two exit paths. Last night entry 58 / stop 48 sold
at 44 (−94¢), half the night on one exit. **Fix:** mirror the flatten exactly — below `stop_px −
SLIP_TOLERANCE_C` rest ONE poll at the floor (a bounded ride catches any floor liquidity), then cross at the
mark with a **counted** `FLIP_FLOOR_BREACH` (acceptance #1: no fill below the floor without a counted
breach). Within slip, unchanged. The 17 momentum-stop tests across 9 files re-anchored to the floor-then-
cross doctrine (the through-floor exit now takes a 3rd poll).

**Part 3 — ORIENTATION_DIVERGENCE could not auto-recover.** Read-rule TRUE: `shadow_runner.py:1143-1152`
pinned recovery to `feed.books.get(self._orientation_halt_market)` — but that market expires in minutes and
is pruned (`on_market_closed`), so `ours` is `None` forever and the clean-read condition can never fire
again. Two occurrences, 161 minutes of dead time, each cleared only by Drew's key; no time ceiling existed.
**Fix:** recover on the FIRST currently-open market (`feed.books`) that reads clean — orientation is our
book's property, not one market's — and arm `_orientation_halt_ts` so a halt stuck past
`ORIENTATION_HALT_MAX_S` (900s) pages `ORIENTATION_HALT_STUCK` instead of sitting silent (acceptance #2).

**Part 4 — the reconcile deferred silently, and a stale pv pinned it.** Read-rule TRUE: `:918-924` deferred
via `log.info` only (not Telegram/pack/hourly), and `_last_venue_pv_cents` refreshed only inside the
`if cash is not None` success block (`:757`) — a failed read preserved a stale non-zero pv, and with
`deployed_cents` later 0 the boundary gate deferred forever (and `abs(None − deployed)` would even crash).
**Fix:** a failed `account_value` sets pv `None` (invalidate, don't preserve); a `None` pv defers as
`RECON_NO_PV`; the hourly gains `recon_ok=<seconds since last clean cross-check>` + `recon_deferred=<streak>`
(acceptance #3); and a run of `RECON_STALL_STREAK` (10) un-cross-checked cycles pages `RECON_STALLED`
(acceptance #4). A completed reconcile resets the streak; a benign cash-protocol quiescence `DEFERRED` does
not advance it.

**Part 5 — the wall storm did not latch.** Read-rule TRUE: the `ENTRIES_HALTED` refusal (`gateway.py:240`)
never flowed through the `_note_wall_reject` storm latch, and `reject_counts` climbed every poll (747→1359
during one halt). **Fix:** count/skip the closed-gate reject ONCE per `(lane, market)` until the gate opens
(`_halt_reject_latched`, released the moment `entries_halted_for(lane)` is empty) — acceptance #6.

**Acceptance:** #1 floor + counted breach (`test_stop_rests_at_floor_then_crosses_below_with_a_counted_
breach`); #2 live-market recovery OR `ORIENTATION_HALT_STUCK` (`test_orientation_recovers_on_a_live_market…`,
`…stuck_pages_after_the_ceiling`); #3 `recon_ok=<s>` on the hourly + `RECON_STALLED`; #4 failed read →
`RECON_NO_PV`, no crash; #6 `WALL_STORM`/closed-gate latch; #7 **F byte-identical** (`lane_fh8` untouched;
the stop floor is FLIP-only). New `test_morning_four_build1.py` (11 tests). Suite 767 · preflight 23/23.

## WO-2026-07-24-D Part 1 — SIZE THE LANE, build 2/2 (build 75)

Ships LAST (it doubles FLIP's size — it must land on build-74's floored stop and visible reconcile). The
+4 thesis is proven (80% conversion, maker fills, zero fees); this closes the gap between what was ruled
and what traded.

**Read-rule TRUE:** `sizing.py:97` sized FLIP `min(kelly_max, depth_max, cap)` — Kelly still bound. At a
$43 book / 60c entry, `kelly = 4300 · (1/12) // 60 = 5`, so `FLIP_SIZE_CAP=10` never bit and the size test
ran at half the ruled size. `sizing.py:63-68` gave **F alone** a notional Kelly-bypass ("NOT by Kelly …
Every other lane is unchanged") — FLIP never got it. The third instance this week of a fix on one of two
siblings. **Fix:** FLIP self-scales by notional too — `notional = int(book · FLIP_NOTIONAL_PCT // price)`,
`contracts = min(notional, depth, cap)`, `FLIP_NOTIONAL_PCT = 0.14` (10 × 60c = 600c ≈ 14% of a $43 book).
Kelly is retired from FLIP's bind and rides the reason as `kelly n/a` for the audit. On the live book
notional gives exactly 10; on a thin book depth binds below (the term the test measures); on a large book
the cap binds. The 15% at-risk wall (`AT_RISK_PCT["FLIP"]`) is the gateway backstop — 10 × 64c (the take,
entry+`OPEN_GOUGE_C`) = 640c vs 645c at $43, fitting with nothing spare; below ~$42.67 the wall binds first
(correct — the backstop does its job). REVERT with the cap/wall (`config.py` comments, WO-C #9).

**Test hygiene (a real flake, fixed):** `test_swing_gate_event.py:178` did a bare
`scoring.tier_for = lambda …` with no restore, leaking globally; once FLIP's size shifted, a later test that
relies on the real tier (`test_p22_scoreboard`) began failing order-dependently — a flaky "DO NOT GO LIVE"
preflight. Converted to `monkeypatch.setattr` (restores at teardown); preflight is deterministic 23/23 again.

**Acceptance:** #5 FLIP sizes to 10 at the live book, binding term named (`test_flip_reaches_ten_at_the_live_
book`, `…no_longer_capped_by_kelly`, `…depth_still_binds`, `…cap_binds_on_a_large_book`); wall headroom
confirmed (ADVERSARY iii — `test_ten_lots_at_the_band_top_fits_the_at_risk_wall`); #7 **F byte-identical**
(`lane_fh8` untouched; F's `F_NOTIONAL_PCT` path never reads `FLIP_NOTIONAL_PCT` and vice-versa —
`test_f_notional_path_untouched_by_flip`); #8/#9 revert conditions remain in the config comments. New
`test_morning_four_build2.py` (7 tests). Suite 774 · preflight 23/23.

## WO-2026-07-24-E Phase 1 — THE SIGHTED STOP (instrumentation, build 76)

Trigger: `26JUL240845-45`, entry yes@57 ×9 — the book collapsed toward 80/20 against, climbed back through
the 40s, and the momentum stop sold at 48 INTO the recovery (−81¢). Drew's read: "you sold due to being
blind." Two-phase by cadence law — **Phase 1 (weekday) is instrumentation only, ZERO behavior change;**
Phase 2 (Saturday, on Drew's explicit go after the shadow read) flips the deferral live.

**Read-rule TRUE:** `lane_flip.py:1399-1402` — `o["stop_polls"]` increments on `mark <= stop_px`, a pure
LEVEL condition with no trajectory term. It counts a poll identically whether the book is falling 57→47 or
climbing 20→47, so a position reverting hard toward entry satisfies it on every poll of the climb until it
crosses back above the stop — exactly where the engine sells it. The data to distinguish "thesis wrong" from
"thesis being repaired" was already recorded and never read back (`skew_ticks` `:241`/`:668`, `exit_obs`
`:1296`) — the same disease as `scan_attrition`: an instrument lying by omission.

**Part 0 correction (reported honestly):** the WO's mental model referenced a 4-minute hold; there is **no
hold** in current code — WO-2026-07-22-E retired it on the favored side. The ~3 minutes on the tape was the
book's own path to the stop plus the 2-poll sustain, not a hold expiring. The finding stands regardless.

**Phase 1 build (SHADOW only):** every poll in `_open_custody` now records `low_mark` (min mark since entry)
and `mark_prev` (prior poll's mark). **BLIND/MUTE law (acceptance #4):** a None-mark poll updates neither
(carry forward) and never counts as recovery — a book-fetch failure cannot fabricate a low or a repair. When
the live stop fires, a shadow verdict is computed and stamped onto the `FLIP_SWING` row:
`{would_defer, low_mark, mark_at_cut, off_low_c, grace_polls_shadow}`. The sighted rule (Phase 2's future
behavior, computed here as a counterfactual): an adverse LEVEL is necessary but not sufficient —
`would_defer` = level `AND` recovering (`mark >= low + OPEN_RECOVERY_MIN_C=6` and not falling) `AND NOT` the
G1 hard floor (`mark <= stop − SLIP − OPEN_GRACE_HARD_C=8` — a 12→18 bounce is a dead position twitching)
`AND` grace budget (`< OPEN_RECOVERY_MAX_POLLS=20`). G3: recovery is measured off `low_mark`, so a decaying
sawtooth keeps making new lows and can't fake a repair. The live `stop_polls` counter and the cut (with
WO-2026-07-24-D P2's floor/breach) are **byte-identical** — only new `o[...]` keys are written.

**Sibling-path check (acceptance #5, banked law):** `stop_polls` has exactly ONE consumer (`:1400-1402`); no
other cut path reads it. The named twins reading bare `mark` — the HUNT `scratch_reason` (`:146-160`, a
deliberately fast scratch in a different mode) and the dead-floor hold backstop (curfew winners, G4/G5) — were
inspected and **left unchanged**; the sighted stop is the OPEN momentum stop only.

**Acceptance (Phase 1):** the `FLIP_SWING` record carries the shadow fields, the 0845 signature reproduces
(`would_defer=true, off_low=+28` — `test_stop_into_a_recovery_shadows_would_defer`), a genuine falling cut
shadows `would_defer=false`, a None-mark poll fabricates nothing (`#4`), G1/G3 hold as shadow verdicts;
behavior byte-identical (the 137-test momentum-stop corpus passes untouched); **F byte-identical** (`lane_fh8`
untouched). New `test_sighted_stop_phase1.py` (7 tests). **Phase 2 is Saturday's decision, gated on Drew's go
after reading the shadow tape — not shipped here.** Suite 781 · preflight 23/23.

## WO-2026-07-24-G — THE FLOODGATES ORDER (build 77)

Drew's ruling: deposit $50, scale F AND FLIP with the book. The dials to scale already existed (F/FLIP
notional); what blocked the floodgates was the WALL — it measured imaginary risk in the wrong scope, and on
the live tape it quietly halved F in the desk's two busiest windows (the 40115/40700 F underfills, exactly
the windows FLIP held a concurrent position).

**Part 1a — the wall summed CROSS-LANE but capped PER-LANE (read-rule TRUE, `gateway.py:487`/`:533`).**
`_event_exposure` returned exposure "across lanes" yet `_wall_net_risk_and_at_risk` compared it to
`at_risk_cap_cents(order.lane, book)` — so FLIP's position counted against F's wall. **Fix:** `_event_
exposure(event, lane=)` is lane-scoped for the wall (a lane's own risk vs its own cap); the cross-lane total
is still computed and, over `EVENT_TOTAL_AT_RISK_PCT`=40% of book, PAGES `EVENT_TOTAL_AT_RISK` (ADVERSARY i
backstop; the hard 50% portfolio cap still stops deployment).

**Part 1b — a held leg priced at 99¢ regardless of basis (read-rule TRUE, `:501`).** The resting-ENTRY
sibling in the same function already priced at `o.price_cents`. **Fix:** `pos_basis` tracks the weighted-
average entry basis on fills (cleared when the key goes flat); the held leg prices at basis (a 58¢ 6-lot is
348¢ at risk, not 594¢). Dials raised strictly UNDER their walls: `AT_RISK_PCT["F"]` 0.20→0.25 (dial 0.20),
`["FLIP"]` 0.15→0.18 (dial 0.14).

**Part 2 — FLIP's fixed cap froze it (read-rule TRUE, `config.py:501`).** At a $90 book notional says ~21
lots and a 10-cap would freeze FLIP where Drew ruled it scale (the count-vs-compound disease P27 killed for
Kelly). **Fix:** sizing FLIP = `min(notional, depth)`, no cap (the redundant re-cap in `_score_and_size`
removed too). The halt follows the size: `rate_halt_drawdown_c(book)` = 4 stop-outs at CURRENT FLIP size,
recomputed live in `window_econ` and printed in the boot banner + hourly (a threshold frozen at yesterday's
size is the count-vs-money bug reborn). `FLIP_SIZE_CAP` is kept only as the book==0 fallback + revert
narrative.

**Part 3 — the sighted stop goes LIVE (read-rule on the shadow-stamp bug: FALSE as cited — the stamps were
already unconditional in build 76; reported honestly).** `OPEN_SIGHTED_STOP` (default on) makes an adverse
LEVEL necessary-but-not-sufficient: a book off its low by `OPEN_RECOVERY_MIN_C` and not falling DEFERS
(logs `OPEN_STOP_DEFERRED`); G1 (hard floor), G2 (grace budget), G3 (recovery off `low_mark`) intact. Every
cut names its trigger `[2-poll|G1_HARD|G2_BUDGET|DECISION_SWEEP]`.

**Part 4 — instrument truth.** The boot preview called `size_order` without `lane=` (read-rule TRUE,
`boot.py:28`) — it previewed the generic Kelly path, not the notional paths F/FLIP trade; now it prints real
per-lane sizes + the book-derived rate-halt bound. `scoring.py:207` is REPORTING-ONLY (the scoreboard's
`lots@book`, not entry-path EV — the entry path `_score_and_size` already passes `lane=`); made lane-aware
for display truth. Cell stats are now PER-CONTRACT (`cell_outcomes.contracts` migration; avg_win/avg_loss
divide by contracts) so an 18-lot era can't blend with the 1-lot era and fake Gate A. Part 6: boot FATALs
`DIAL_OVER_WALL` if any dial ≥ its wall.

**Sibling greps (#7):** `FLIP_SIZE_CAP` — retired from `sizing`/`shadow_runner`, kept as halt fallback +
revert; boot banner reference updated. `ONE_LOT_MAX_LOSS_CENTS` — `gateway:530` is the basis fallback;
`ops.py:203` (daily kill-clamp) is a separate bound, inspected and LEFT unchanged. `size_order` callers —
the entry path (`shadow_runner:499`) already passes `lane=`; only the reporting scoreboard didn't.

**Acceptance:** #1 lane-scoped wall + basis pricing + 40% page (`test_flip_exposure_does_not_count_against_
f_wall`, `…held_leg_prices_at_basis`, `…event_total_over_forty_percent_pages`); #2 FLIP notional-or-depth +
book-derived halt (`test_flip_scales_with_book_no_fixed_cap`, `…rate_halt_drawdown_scales`); #3 sighted stop
live + trigger names (`test_sighted_stop_live_defers_a_recovery`, `…names_the_2poll_trigger`, `…g1_hard…`,
revert flag); #4 boot per-lane sizes; #5 per-contract cell stats (`test_cell_stats_are_per_contract`); #6
dial<wall asserted (`test_dials_sit_under_their_walls`, `…a_dial_over_its_wall_is_caught`); #7 sibling greps
above; #8 **F byte-identical** (`lane_fh8` untouched; F's notional path unchanged — only its wall loosened).
New `test_floodgates.py` (16 tests). Suite 797 · preflight 23/23.

## WO-2026-07-24-H — ONE POSITION, ONE STORY (build 78)

Trigger (12:18 window, `1230-30`): entry pieced no@61 ×7 + ×10 (17 total, merge law fired correctly), then
EXIT ×7 @65 and the ↔ line declared "round-trip +28¢" as a concluded story while **10 contracts still rode**.
"Reconciliation of the same thing we just saw, but at the flip level" — the -G fix (positions, not fills)
applied to the story surfaces and the exit path. The unit of account is the POSITION; fills are evidence
that accrue basis; P&L and "concluded" exist only at count→0.

**H1 (read-rule TRUE, `shadow_runner.py:604-616`):** the ↔ line selected the single most recent ENTRY fill
(`ORDER BY id DESC LIMIT 1`) and printed `(exit − that_fill) × exit_count` as "round-trip" on ANY exit —
declaring a conclusion on a partial, pricing against one fill not the blended basis, and subtracting two
different objects' fields. **H2 (TRUE, `ledger.py:356`):** the cell outcome (the Gate A instrument) persisted
the same fill-pair fiction per exit fill. **H3 (TRUE, race-armed):** the merge correctly cancels+re-proposes
the take at the merged size, but a resting take can outlive a partial exit OVERSIZED — a resting EXIT larger
than the position, if lifted, sells what we don't hold and auto-net OPENS the opposite side (the unguarded
twin of the self-net entry wall).

**P1 — the ↔ line reads the POSITION.** The gateway (from -G) owns per-position basis + count, and
`gateway.on_fill` runs before the narration, so the post-fill state is the truth: a new `pos_story` accrual
(basis, entry/exit counts, exit proceeds, fees) snapshots into `last_concluded` at count→0. The line prints
`PARTIAL x7 @65¢ (basis 61¢) +28¢ — 10 riding` while it rides, and only at flat `ROUND-TRIP CLOSED x17 basis
61¢ → avg exit 65¢, net +68¢`.

**P2 — the cell outcome books ONCE, at conclusion.** Removed from `record_fill` (per-exit-fill); booked at
the position's conclusion in `gateway.on_fill` (regular exits) and `custodian.execute_cut` (custodian cuts —
which bypass `on_fill`; that path is their conclusion point and knows the position basis+count), both
idempotent by `(market, lane, kind)`. Blended basis, total count, position net — never a fill pair.

**P3 — the take re-sizes on a partial + a standing assert.** `note_exit`: when a partial leaves `count > 0`
and a resting take's `take_count > count`, cancel it and let custody re-propose at the remainder (the merge's
cancel+re-propose, reused). `gateway.check_exit_oversize()` runs each cycle: no resting EXIT may exceed its
position's live count → pages `EXIT_OVERSIZE` (the belt if a cancel-reject race ever leaves one; never on
honest tape).

**Sibling greps (ENGINEER, acceptance #3):** the `fills … ORDER BY id DESC LIMIT 1` pattern on
story/economics surfaces is retired — the ↔ line (H1), the cell outcome (H2), and the pack's scratch /
FLIP-R6 P&L (`ops.py`, now reads the position-level `cell_outcomes`, `gross = net + fees`). The remaining
consumers are position **reconstruction**, not P&L stories: `lane_flip.py:1859` (reboot-orphan basis+fill_ts
recovery) and `reconcile.py:131` (unsettled-position lane/side/basis recovery) — both cited as position-safe,
left unchanged.

**P4 — today's remainder (analysis):** the 1230-30's riding 10 — with P3 live, a take oversized by a later
partial is cancelled the instant the partial books and re-proposed at the remainder; the belt would have
paged `EXIT_OVERSIZE` had the cancel not landed. No oversized ×17 can sit resting unmeasured.

**Acceptance:** #1 pieced-entry PARTIAL then CLOSED, cell row only at conclusion with position totals
(`test_pieced_entry_partial_then_closed`, `test_cell_outcome_books_at_conclusion_with_totals`); #2 take
re-sizes on partial, `EXIT_OVERSIZE` fireable in test / silent on honest tape (`test_partial_exit_cancels_
the_oversized_take`, `test_exit_oversize_assert_is_fireable`); #3 sibling greps above; #4 **F byte-identical**
(`lane_fh8` untouched; no entry-gate/target/One-Shot change). New `test_one_position_one_story.py` (5 tests).
Suite 802 · preflight 23/23.

## WO-2026-07-24-I — BLIND IS NOT BARE (build 79)

Trigger (1345-45, 1:32 PM): entry no@62 ×17 → `FLIP_UNCOVERED_LEG "held 17 > covered 0"` → `EXIT_OVERSIZE
"resting EXIT x34 > held 17"` → `FLIP_UNCOVERED_FLATTENED ×17 @51` (−247¢ on a green day). The leg was never
bare — it was **DOUBLE-covered**. Two instruments read the same position as 0 and 34 in the same minute, and
the destructive close fired on the reading that said zero. **The law: a destructive close requires POSITIVE
knowledge of bareness from broker truth; blindness pauses, it never fires** (this morning's -H lesson —
positions, not fills — applied to the engine's own orders).

**Read-rule (against build 78, all TRUE):** I1 `lane_flip.py:1695` — the flatten deadline fires on
`esc == 2` (failure to CONFIRM), not on positive bareness. I2 `covered` was summed from in-memory custody
buckets while the -H `EXIT_OVERSIZE` reads the gateway resting registry — 0 and 34 for the same leg. I3
`gateway.py:386` `cancel_tristate` returns CANCELED|ALREADY_TERMINAL|UNKNOWN and on UNKNOWN **puts the order
back in `resting`** — but the FLIP merge called the boolean `cancel()` and unconditionally cleared `take_oid`
+ reposted, so an in-flight cancel read as gone and the repost doubled the cover (the x34). I4 escalation is
cycle-counted.

**P1 — one coverage authority.** The heal ladder's `covered` now reads the broker-truth resting **registry**
(`gateway.resting_exits(market, side)` — the same read `EXIT_OVERSIZE` uses). The registry is the *primary*
authority (it drives surplus detection + cancellation, the only actionable truth on 1345); a bucket
`take_oid`/`take_count`/`flatten_oid` counts as a **fallback only where the registry does not already hold
that order** (a cover in flight, or a reboot before the registry rehydrates) — one number, never double-
counted. **A resting exit IS cover — a bounded position is not panic-reversed.**

**P2 — flatten requires positive bareness.** `covered > held` (the 1345 x34) → cancel the SURPLUS resting
exits newest-first, keep the original take, `FLIP_COVER_SURPLUS`, no flatten/fee. `0 < covered < held` → top
up the gap with a resting **MAKER** exit at the take (`FLIP_COVER_TOPUP`), never a market cross of a bounded
leg. `covered == 0` after grace + reconcile → positively bare → the flatten fires exactly as before (the
doctrine survives). Registry unreadable → `HEAL_BLIND`, hold escalation, no orders.

**P4 — the x34 root.** The merge (`note_fill`) now respects `cancel_tristate`: on UNKNOWN it keeps the old
take resting and does NOT repost (the heal ladder tops the gap from broker truth); only CANCELED/
ALREADY_TERMINAL clears `take_oid` and arms the repost. One take identity per record.

**P5** — the phantom ×8 narrations are structurally impossible: the fills loop books each fill exactly once
(`booked_fills` fill-id dedup → one narration per fill), and P4 removes the double-order root, so one
economic event prints one line. No redundant narration dedup added.

**Sibling greps (ENGINEER):** the merge was the one cancel-then-**repost** site (fixed by P4). The other
`gateway.cancel()` sites (`_cancel_resting` before a CUT/hold, the HUNT take cancels, the reconcile stale-
cancel) are cancel-before-**cut**, not repost — an UNKNOWN leftover there is a *surplus* caught by P1's
registry surplus-cancel, never a double-cover; cited, left unchanged. `covered`/`heal_covered` has a single
consumer (`_check_uncovered`), now registry-first.

**Acceptance:** #1 replay (17/34) → surplus cancelled, cover 17 retained, zero flatten, zero taker fee
(`test_replay_17_held_34_resting_cancels_surplus_no_flatten`); partial (17/7) → maker top-up; #2 replay
(17/0 confirmed) → flatten survives; #3 replay (read-fail) → `HEAL_BLIND`, no escalation, no orders; #4 merge
UNKNOWN keeps one take, CONFIRMED reposts (`test_merge_unknown_cancel_keeps_one_take`); #5 one event → one
line (fills-loop dedup + P4); #6 **F byte-identical** (`lane_fh8` untouched; stop/take/One-Shot unchanged).
New `test_blind_is_not_bare.py` (7 tests); `test_uncovered_flatten`/`test_flip_count1` re-anchored to the
registry doctrine (a resting sell is cover). Suite 809 · preflight 23/23.

## WO-2026-07-24-J — POINT THE HUNTER FORWARD (build 80)

The banked Divergence Engine (the WO-F design) goes LIVE as HUNT's first scope — **HUNT is not turned
off; it is pointed forward.** The hunt was buying the wrong number: the touch-lag (`fair = 100 ×
p_survive`) is P(BTC ever *touches* the strike), but the KXBTC15M contract settles on where the window
**CLOSES**. So the hunt paid for touches and the market paid for closes, and the gap between them was
the bleed. WO-J builds the missing surface, gates on it, exits on it, narrates it, and reads it back.

**Read-rule (against build 79, all TRUE):** J1 `spotlead.py:37,73` — the needle's `fair_cents` is
`100 × p_side` and `p_side` derives from `delta.p_survive` (= 1 − p_cross, the TOUCH surface); the name
"fair" names no question — TRUE, renamed. J2 `lane_flip.py:1191` — gate B was `gap = sl.fair_cents −
join >= HUNT_GAP_CENTS`, a touch-lag gate — TRUE, replaced by the settle-edge gate. J3
`lane_flip.py:1300` — the `mark <= entry − 2` two-tick level bail — TRUE, retired. J4 the delta corpus
(`delta_builder.py`) already walks every 15-min window's candles for the touch max-excursion; the
**close-at-window-end** is the same walk's free byproduct — TRUE, one new counter, no second corpus.

**P1 — the SETTLE surface, question-tagged.** `delta_builder.compute_delta_table` now emits a second
column family from the SAME 180-day Coinbase corpus: `p_end(d,t)` = fraction of windows whose **net**
close displacement `|end_close − start_close| × sub_minute_scale ≥ d`, with `p_end_n` and
`p_end_wilson_lb` (the Wilson **LOWER** bound on `effective_n` distinct windows — the hunt gates at the
conservative bound, never the point). The accessors `delta.p_end` / `delta.p_end_wilson_lb` /
`delta.settle_loaded` return **None/False** on a legacy touch-only tape → HUNT stays **BLIND**, exactly
the touch table's own absence discipline. **The physics (Adversary ii):** a window that closes beyond
`d` necessarily *touched* `d`, so `p_end ≤ p_cross` **everywhere** — the validator gates it (`A1: p_end
<= p_cross (settle ⊆ touch)`), and the two surfaces are provably DIFFERENT numbers, not one mislabeled.
CSV schema, A1–A5, session-hash parity, synthetic/non-Coinbase/SHA refusal all extend to the new family.

**P2 — the forward gate.** HUNT's gate B is now `edge = wilson_LB(p_end(d,t)) × 100 − join`, entry
requires `edge ≥ HUNT_EDGE_MIN_C` (**DREW-DEFAULT 6c** — floored at a PROBE round-trip taker cost ~4c +
2c headroom, re-derivable from the fill ledger). The needle **demotes to ATTENTION**: gate A (ΔP ≥ N)
still decides *when the hunt looks*, never *whether it buys* — the settle edge decides that. Absent the
settle surface the gate is BLIND and sits out (`HUNT_BLIND`, paged once). Every ENTRY carries the EV tag
`ev_c = p_end(point) × 100 − join`. Gate C (sustained convergence) unchanged; One-Shot, HUNT seniority,
HUNT_REFUSE_LOWER, direction-lock all preserved.

**P3 — the exit watches the thesis.** The two-tick level bail retires. `_hunt_custody` re-reads the
settle LB at the **current** geometry (`_hunt_settle_lb`: spot→strike distance now, secs now) and bails
when `wilson_LB(p_end) × 100 ≤ cost` sustained `HUNT_EDGE_GONE_POLLS` (3) polls — the edge that bought
the hunt is gone — logging **both numbers** (`hunt exit — edge gone (settle X% ≤ cost Y¢)`). Blindness
never fires it (no strike / no surface → the check abstains; the named guardrails — breakeven-not-out-
in-R, TIME-BOX, CURFEW — still fire).

**P4 — every probability names its question.** The casefile is ONE format (`lane_flip.py:_hunt_casefile`):
`HUNT ↑ spot $56 off strike, T-9:05 · settle 22% (LB 18) · book 15¢ · edge +3 · touch 70% (info) · EV +7`.
Distances carry `$`; the gated quantity is **edge** (the Wilson-LB cents the gate used); the touch
probability is INFO-only, marked `(info)`; the EV tag rides. **"fair" is banned** — `Needle.casefile`
relabels its touch number `touch NN% (info)`. The touch surface itself is **untouched**, only labeled.

**P5 — the DIVERGENCE read-back.** The daily bundle gains a HUNT-scoped section (`ops.hunt_divergence_
lines`): settled HUNT-eligible windows bucketed by **edge** answer three calibration questions —
(1) REALIZED settle rate (HUNT-side wins vs `window_outcomes.settled_yes`), (2) the table's **p_end**,
(3) the market's **book**-implied. The full tag set `{p_end, p_end_LB, p_touch, book, edge, ev_c}` is
parsed straight from the entry casefile. HUNT is **not promoted here** — it stays PROBE until a bucket's
realized Wilson LB clears the book (reported per bucket); this section measures, it does not size.

**Sibling greps (acceptance #7 — every touch-probability consumer cited):** HUNT's gate B is the ONLY
migration to `p_end`. `spotlead.needle` (`p_survive`) stays touch — it is now the ATTENTION signal, relabeled.
`lanes.py:284` (F/H8 swing-gate floor), `custodian.py:317` (salvage needle), `lane_flip.py:1032-1094`
(OPEN two-barrier + shadow proxy), `shadow_runner.py:1114` all stay **touch** (their questions are touch
questions), cited unchanged. `lane_fh8.py:354` (F's `|TBL_…p_cross|` info line) stays **byte-identical**.

**Acceptance:** #1 settle table builds with per-cell {p_end,n,wilson_lb}, question-tagged, `p_end ≤
p_cross` proven, touch untouched (`test_settle_surface_built_question_tagged_and_bounded`,
`test_accessors_blind_on_legacy_table`); #2 entries fire only on `edge ≥ floor` from the Wilson-LB, needle
as attention, EV tag logged (`test_confirmed_needle_hunts_with_casefile`, `test_edge_under_floor_never_
hunts`, `test_blind_settle_surface_never_hunts`); #3 two-tick bail gone, `edge gone` exit with both numbers
(`test_job_b_thesis_exit_when_edge_gone`, `test_thesis_exit_blind_never_fires`); #4 casefile/exit match the
P4 format, "fair" banished (`test_casefile_names_its_questions_and_banishes_fair`); #5 DIVERGENCE with three
computable questions + full tag set (`test_divergence_computes_realized_vs_p_end_vs_book`,
`test_divergence_parses_full_tag_set`); #6 **F byte-identical** (`lane_fh8` untouched), FLIP desk / One-Shot /
seniority / gate C preserved; #7 sibling greps above. New `test_point_the_hunter_forward.py` (12 tests);
`test_p18_detective` re-anchored to the forward gate; `tape_grade`/`SEMANTICS.md` registry updated to the
new casefile vocabulary. Suite 823 · preflight 23/23.

## WO-2026-07-25-K — THE DESK EARNS ITS SIZE (build 81)

The honest diagnosis: the +4 OPEN desk is **6-of-11 since the target change (55% conversion) against a
~73% breakeven** — win +4¢/contract, the four Saturday stops averaged −14.25¢, and at 3.5:1 loss-to-win
55% loses (Saturday priced it: OPEN −$12.35 against F's +$18.95). The hard finding: **no recorded
feature separates the winners from the losers at n=11** — ratio, skew, growth, trend-agree, into-seconds,
time-band, and the Wilson cell margin (negative on *every* entry, printed "info" while trading full size)
all passed on all four losses. High confidence cannot be a threshold on the existing instruments — they
measure the pile's shape, not whether the open is fragile. All four losses were the same shape: a 56–62¢
favorite with spot pinned to the strike, where one $10–30 drift flips the book. The instrument that reads
*that* is the one already banked — the -J settle table.

**Read-rule (against build 80):** K1 `config.py:377` — `FLIP_NOTIONAL_PCT = 0.14` — TRUE, demoted to
0.04. K2 `config.py:539` `rate_halt_drawdown_c(book)` computes `lots = book·FLIP_NOTIONAL_PCT/ref` — TRUE,
it re-derives from the dial, so the halt shrank with the demotion automatically. K3 the FLIP_SWING records
`lane_flip.py:2174` writes at conclusion (`gross_cents`, `took_swing`) and the pack reads them
(`ops.flip_fill_rate_by_price`, `fill_economics`) — TRUE. K4 `lane_flip.py:889` the cell margin prints
`(info)` on every OPEN card — TRUE. K5 **honest divergence** — the WO's "worst-day boot banner re-derives
from the dial" is imprecise: `ops.worst_day_bound_line` derives from the at-risk **WALL** (`AT_RISK_CAP_
CENTS`), which the sizing-dial change does NOT touch (correct — the wall is unchanged; a voluntary smaller
size doesn't loosen the ceiling). The **SIZING boot banner** (`boot.sizing_line`) IS dial-derived and
re-derives the FLIP lots + halt; that is where "prints both" is satisfied. Reported, not papered over.

**P1 — tuition size.** `FLIP_NOTIONAL_PCT: 0.14 → 0.04` (≈5–6 lots at the ~$90 book). The desk keeps every
piece of armor (sighted stop, -I healing, floor counting) and keeps buying cells, at ~−35¢/day expected
worst instead of −$12 days. F is untouched at its earned 0.20. The rate-halt re-derives from the tuition
dial (a stop budget of 4 stop-outs at the *current* size, not a frozen constant); the boot SIZING banner
prints the tuition lots, the full target, the ladder bars, and the re-derived halt on one line.

**P2 — the confidence instrument.** The -J settle table gains its desk duty. `spotlead.settle_fair_favored`
prices the favored side's SETTLE-fair from `p_end`: a zero-drift 15-min walk is symmetric, so the favored
side loses only if the net ADVERSE move crosses back over the strike, which is half the directionless
`p_end` mass — hence **settle_fair = (1 − p_end(d, t)/2) × 100**, tending to 50 as spot pins to the strike
(d → 0, p_end → 1). New OPEN gate (the last of the all-of chain, `lane_flip.py`): **favored-side
settle-fair ≥ join + FLIP_CONF_MIN_C** (DREW-DEFAULT 4). All four Saturday losses (settle-fair ≈ 54 vs a
58–60¢ join → conf ≈ −6) are REFUSED (`OPEN_CONF_REFUSED`, reason `conf_fragile`); a genuine edge (spot
$300 off the strike → settle-fair ≈ 75, conf ≈ +15) passes and the card carries `settle-fair N vs join M →
conf +K ✓`. Absent the settle surface the gate ABSTAINS (BLIND) and the desk trades on its pile gates, tuition
bounding the unread risk — the seal's ordering (tuition pays while the instrument is built). Phase-two vol
axis (`p_end(d, t, vol)`) stays banked. Point estimate, not a bound — the +4 headroom is the conservatism;
a vol-conditioned upper-bound version is the phase-two refinement.

**P3 — promotion by conversion.** `flip_ladder.py` makes the size ladder mechanical: `trailing_conversion`
reads the last `FLIP_CONV_WINDOW` (20) FLIP_SWING round-trips (a trip CONVERTS when `gross_cents > 0`);
`evaluate_size_tier` promotes tuition→full at conversion **≥ 0.75** over a full window and demotes
full→tuition **instantly** under **0.65** (hysteretic band holds between; promote slowly, demote
instantly), paging ONCE per flip with the number that moved it (`DESK_SIZE_CHANGE` + Telegram). The
sizing chokepoint `_score_and_size` reads `active_notional_pct(ledger, cell_margin)` and passes it to
`size_order(..., notional_pct=)`; the **margin tiebreaker** rides on top — full size only when the desk is
promoted AND the entry cell's Wilson margin ≥ 0, so a lucky streak can never up-size a structurally-losing
cell (ADVERSARY's gaming check). Conversion is size-independent, so tuition trips keep earning promotion
(ADVERSARY's deadlock check). The ladder line surfaces in the hourly and the daily pack.

**Sibling greps:** `size_order`'s FLIP branch is the one notional consumer; it gained an optional
`notional_pct` (default → `FLIP_NOTIONAL_PCT` tuition, so bare callers/boot preview read tuition). F's
branch (`F_NOTIONAL_PCT`) is untouched. `rate_halt_drawdown_c` and `DIAL_OF_LANE`/`dial_wall_violations`
were re-pointed at the correct dial (the wall now guards the FULL ceiling 0.14 < 0.18). The `_score_and_size`
FLIP-pct block is wrapped so sizing never blocks on a partial ledger (tuition is the safe floor).

**Acceptance:** #1 tuition sizes ~5–6 lots, halt re-derived, boot prints both (`test_tuition_sizes_five_or_
six_lots_at_the_live_book`, `test_rate_halt_re_derives_from_the_tuition_dial`, `test_boot_banner_prints_both_
tuition_and_the_halt`); #2 conversion visible in hourly + pack, promotion/demotion mechanical with the
number (`test_promotion_and_demotion_fire_mechanically`, `test_ladder_line_reports_the_number`,
`test_hysteresis_band_holds_the_tier`); #3 settle-fair + conf logged, refusals named, the four Saturday
losses replay REFUSED (`test_saturday_losses_replay_as_refused`, `test_genuine_edge_passes_the_conf_gate`,
`test_blind_settle_surface_proceeds_at_tuition`); #4 **F byte-identical** (`lane_fh8` untouched;
`test_f_sizing_untouched`). New `test_desk_earns_its_size.py` (15 tests); `test_morning_four_build2` /
`test_size_test` / `test_size_test_data` re-anchored (full-size mechanism tests now exercise the promoted
dial explicitly; tuition is the default). Suite 838 · preflight 23/23.

## WO-2026-07-25-L — F GETS THE BOOK; EVERYTHING ELSE EARNS IT (build 82)

An outside review read the tape and Drew ruled: **move anything not-F to shadow; F gets as much as
possible; everything else has to earn it.** Supersedes -K's tuition sizing (shadow is the stronger form
of the same demotion); -K's settle-fair conf gate (P2) and conversion ladder (P3) survive as the
earn-back criteria. **Review reconciled item-by-item:** #1 leave-F-alone → adopted, amended to F-maximum
(P2); #2 FLIP-down → superseded by full shadow; #3 uncovered-leg/oversized-exit → **already closed** (-H/-I,
verified live 07/25 14:33 `FLIP_COVER_SURPLUS … no flatten, no fee`; the review read the pre-deploy tape —
cited so it isn't re-opened); #4 recalibrate be → P4; #5 suppress worst cells → superseded by shadow-all;
#6 named reversible experiments → the standing PROMOTION PROTOCOL (P3).

**Read-rule (against build 81, all TRUE):** L1 `config.py:14` `RUN_MODE` defaults SHADOW; `config.py:691`
`live_submit_enabled()` = `RUN_MODE==LIVE and phrase` — the born-shadow global kill. L2 `gateway.py:328`
the submit branch gated on the **global** `live_submit_enabled()` — TRUE, made per-lane. L3 (survey) the
treasury math — `ledger.book_cents`/`lifetime_pnl_cents` sum `settlements`(divergent=0)+`cash_movements`,
`deployed_cents` sums unsettled `fills`; **none read `cell_outcomes`** — so shadow isolation = shadow
lanes never write `fills`/`settlements`/`cash`, and `cell_outcomes` gains a tag. L4 `book_snapshots` stores
raw frames (no trade-print); through-price is derived from best-bid crossings. L5 `F_NOTIONAL_PCT=0.20`
(`config.py:371`), `AT_RISK_PCT["F"]=0.25` — TRUE, raised. **Honest divergence:** the WO's `LANE_MODE`
dict names only F as LIVE and omits **H8**; under "everything else earns it" I applied the default SHADOW
to H8 (the seal's "one lane"). F's *logic* is byte-identical (`lane_fh8` untouched); only its size params
and H8's placement mode change.

**P1 — per-lane run mode + the pessimistic fill model.** `config.LANE_MODE` (F LIVE, the rest SHADOW,
env-overridable) + `lane_is_live(lane)` = `live_submit_enabled() and mode=="LIVE"` (per-lane only restricts
below the global). `gateway.submit` takes the live door only when `lane_is_live`; every shadow lane gets a
`SHADOW-` oid and **zero broker traffic** even inside a LIVE run (F places; the desk rehearses). The
rest-back/rest-forward re-pricings re-gate on `lane_is_live` too. `shadow_fill.py` is the **pessimistic**
model (Scientist owns it, its statement prints in the pack): a maker fills only when the book trades AT or
THROUGH its price after ≥1 poll of rest (`buy` fills when the opposing bid reaches 100−price; `sell` when
its own bid reaches price); at-the-touch is not a fill; unfilled by window-end = expired. `ShadowEngine.
simulate_shadow_fills` sweeps resting `SHADOW-` orders each poll and books fills through the **same**
`gateway.on_fill` → `_on_fill_booked` path a live fill uses (👻-marked), so shadow custody is real; no
`fills`/`settlement`/`cash` row is ever written.

**P1b — two ledgers, one table, clearly labeled.** `cell_outcomes` gains a `shadow` column (legacy rows
default live); `record_cell_outcome(..., shadow=)` is tagged at every booking site by
`config.lane_books_shadow(lane)` (= live-run AND non-live lane; **False in a global-shadow run**, so the
born-state one-paper-ledger behavior and every existing test are untouched). LIVE sizing/promotion authority
(`cell_stats`, `realized_loss_avg`, `score`) reads `shadow=0` only — a rehearsed cell never drives live
size. The scoreboard renders LIVE and SHADOW in **separate labeled sections** (never a shared row). **The
isolation rail (ADVERSARY ii):** `record_fill` FATALs `SHADOW_ROW_TO_TRADEABLE_CAPITAL` if a shadow lane
tries to write a live fill in a LIVE run; a boot self-test asserts the treasury queries contain no
`cell_outcomes`/`shadow` reference (`SHADOW_LEAK_INTO_TREASURY` FATAL else). Simulated P&L provably cannot
reach real capital.

**P2 — F maximum.** `F_NOTIONAL_PCT 0.20 → 0.24`, `AT_RISK_PCT["F"] 0.25 → 0.30` (dial stays 80% of wall;
`dial_wall_violations` still empty, boot won't FATAL). The boot banner prints per-lane modes, the new
dial/wall, and **the single-loss bound**: one full unsalvaged F loss ≈ dial × book ≈ 24% of book (the
stated, accepted ceiling), with the note that further raises gate on salvage shipping (at ~40–50¢ salvaged
losses the same math supports dials past 0.30). F takes the desk's freed risk budget through this raise
alone — walls are lane-scoped, nothing transfers.

**P3 — the earn-back protocol.** A lane leaves shadow ONLY as a named experiment with promotion evidence,
a stated live size (tuition first), a pre-stated mechanical revert, and Drew's sign-off. `flip_ladder.
promotion_distance_lines` prints each shadow lane's **distance to promotion** in the daily pack (desk:
trailing-20 shadow conversion vs 75%; HUNT: shadow-entry count vs 30) so an all-shadow future is never a
stuck mood — the door is numbers, printed daily. (This reports the evidence bar; the experiment + sign-off
are still required to actually go live.)

**P4 — break-even recalibration + THIN.** `CELL_THIN_MIN_N=10`: a cell with fewer than 10 **realized**
outcomes is THIN and carries **no gate authority** — `score()["thin"]` nulls its margin in the WO-K ladder
tiebreaker (`_score_and_size`), `cell_has_authority` is False, and the scoreboard greys it with its n (the
red-margin ⚠ still shows — a warning is a warning — but marked `·THIN, no authority`). A cell leaves THIN
only by realized n, never modeled numbers. The displayed be stays the honest realized-derived
`breakeven_honest` (the review's item #4).

**Sibling greps:** the two live-only re-pricings (`gateway.py:296,313`) and the live door (`:328`) all now
gate on `lane_is_live`. Every `record_cell_outcome` caller (gateway on_fill, custodian cut, settle sweep)
passes the shadow tag. Every treasury/tradeable read was audited (survey) — none touch `cell_outcomes`.
`cell_stats`/`realized_loss_avg`/`score` gained a `shadow` param defaulting to live authority.

**Acceptance:** #1 boot prints per-lane modes + F dial/wall + single-loss bound + worst-day (`test_boot_
banner...`, rendered); #2 F places live, desk lane is a `SHADOW-` ghost with zero broker traffic (`test_f_
places_live_desk_lane_is_a_ghost`); #3 shadow & live cells never share a row, treasury reads live-only by
construction + FATAL rail (`test_shadow_cell_is_tagged...`, `test_shadow_lane_fill_is_fatal_refused_in_
live`, `test_treasury_reads_live_only_by_construction`); #4 pack gains the fill-model statement,
distance-to-promotion, THIN tags, recalibrated be (`test_promotion_distance...`, `test_scoreboard_separates_
live_and_shadow_and_marks_thin`); #5 the 07/25 lesson — the pessimistic sim never fills a price the book
missed, and books a real shadow round-trip when it does (`test_pessimistic_sim_never_fills_a_price_the_book_
missed`, `test_shadow_sim_roundtrip_books_a_shadow_cell`); #6 **F byte-identical in logic** (`lane_fh8`
untouched; `test_f_sizing_logic_untouched`). Honest scope: the shadow fill simulator's numerical
calibration against the real 07/25 tape awaits that tape on the live box (test env has none — the model +
sim are proven on synthetic books, the -J synthetic-corpus pattern). New `shadow_fill.py`,
`test_f_gets_the_book.py` (18 tests); `test_scale_f`/`test_walls`/`test_p16`/`test_p22`/`test_verify_
lossterm1`/`test_maker_rest_back` re-anchored to the F raise + per-lane gate. Suite 856 · preflight 23/23.

## WO-2026-07-26-N — THE OVERNIGHT DOCTRINE (build 83)

One document, one deploy, the whole 07/25→26 overnight run audited and ruled. Consolidates the deploy
questions of -L (shadow), -M (salvage), and the incident. **What happened, verdicts final:** boot #70 shipped
-K/-L sizing healthy; then untuned salvage (`F_SALVAGE_SLIP_POINTS=40`, level-only, no confirms) fired
**twice at maximum pain** — `SALVAGE_SLIP` at 50¢ (F no@97 ×18) and 52¢ (F yes@95 ×11), both on windows that
**settled as winners** (≈ −$14.30 realized for $0 dodged, 0-for-2); the custodian's direct ledger write +
the fills-poller **double-booked the same cut** (→ `EXIT_OVERSIZE` ×80, `SETTLE_UNMATCHED_LEG` ×2, the
−2389¢ lifetime line); and **every alarm rang true** (cash sentinel, oversize assert, F rate halt at
−750¢/8). The lane itself was never broken — Sunday it clipped 34 clean windows.

**Read-rule (independently verified at source, all TRUE):** N1 **baton lifecycle** `custodian.execute_cut`
(the agent confirmed lines ~636–708): tri-state cancel before cutting, `resweep`+`ledger_remaining`
re-derive, `UNKNOWN`→`BATON_VIOLATION` FATAL, `FLAT_RACE` skip, cut sells only `remaining` — the execution
double-cut class is dead. N2 **unified fill dedup** `fills.py`: `booked_fills(fill_id TEXT PRIMARY KEY)`,
`_already_booked` pre-check, "no path is privileged" verbatim — a re-delivered fill books at most once. N3
**lifetime from settlements** `ledger.lifetime_pnl_cents` (`SUM(pnl_cents) FROM settlements WHERE
divergent=0`) — TRUE. **Honest divergences, reported:** (a) **no restatement method / `RESTATED` concept
existed** — P4.4 is net-new, and because lifetime already reads settlements-only (which the phantom cuts
never touched), the "restatement" is a *reporting acknowledgment*, not a data rebuild; the honest lifetime
was correct throughout. (b) The salvage **"would-have-fired" telemetry was net-new** — the existing
`_note_salvage_gag` rows record *why salvage did NOT fire*, not a counterfactual fire.

**P4.1 — the ruled shadow modes.** `LANE_MODE` already defaults FLIP/OPEN/HUNT/H8 → SHADOW, F → LIVE (the
-L build); the boot banner prints them. Enforcement is env (`LANE_MODE_*`); acceptance is a 👻 on the next
desk signal + venue silence on its oid (test_f_gets_the_book, extended here).

**P4.2 — salvage GAGGED + the -M re-arm.** `SALVAGE_GAGGED` (default true): both the price-slip and the
needle-collapse triggers route through `_fire_or_gag_salvage`, which — when gagged — writes a
`SALVAGE_WOULD_FIRE` counterfactual row (👻 alert, the tuning data the re-arm review reads) and **takes no
cut, rides to the bell**. The **-M re-arm path** (the disciplined salvage that runs un-gagged) rebuilt the
level-only slip into: **confirms-symmetric** (`SALVAGE_SLIP_CONFIRMS=2` sustained ticks, reset on recovery —
a single-tick dip that recovers, the exact Saturday shape, never fires); **worth-floor**
(`SALVAGE_WORTH_FLOOR_C` — nothing worth a fee below it); **maker-first** (rest at the held mark, stage-2
crossfire after R — never the overnight's immediate crossfire); **rarity assert**
(`SALVAGE_RARITY_MAX_PER_DAY`, pages if salvage runs hot). Review after `SALVAGE_REARM_REVIEW_N=50` gag
summaries (the Gate A unlock is named, not left to rot).

**P4.3 — the two build-6 fixes, replayed.** `test_duplicate_cut_books_once_and_sells_only_ledger_remaining`
(a second cut on a concluded position `FLAT_RACE`-skips — one `CUSTODIAN_EXIT`, never two);
`test_duplicate_venue_fill_books_once` (the same `fill_id` swept twice → `booked=1, duplicate=1`, one `fills`
row). The exact overnight classes, green.

**P4.4 — the restatement.** `ops.restated_money_lines`: the daily pack MONEY section carries a **`RESTATED`**
tag, publishing lifetime rebuilt from the settlements ledger alone with the line-item delta — the phantom
double-booked cuts corrupted cell/window REPORTING only (settlements written once at bell, never a cut), so
the lifetime delta from the corruption is 0c; the `/confirm_cash` re-baselines are in `cash_movements`
(book). `test_lifetime_is_settlements_only_unhurt_by_phantom_cells` proves a phantom cell never moves
lifetime.

**P4.5 — the cash-sentinel doctrine (banked).** Boot prints it: a `CASH DELTA` that fires within 30 min of
ANY anomaly page → **/deny_cash + investigate**, never /confirm — a sentinel next to an alarm is *evidence*,
and confirming it launders the error into the books. **P4.6 — no dial changes:** F stays 24/30 (asserted);
the next size conversation happens on a restated, trusted lifetime.

**FLIP promotion verdict (data, not mood): STAYS IN SHADOW.** 6-of-11 (55%) vs 73% breakeven; FLIP/OPEN/HUNT
lifetime ≈ −$26; every entry cell Wilson-negative. The path back is -L P3 (shadow trailing-20 ≥ 75% under the
pessimistic fill + the -J conf gate live + cell margin ≥ 0), printed daily as distance-to-promotion.

**Acceptance:** #1 boot prints modes + salvage GAGGED + RESTATED + no-dial (`test_boot_prints_the_doctrine`);
#2 next desk signal is a ghost / F single-booked (-L tests + N2 dedup); #3 replay green — both Saturday
salvages NO-FIRE (`test_saturday_slip_no_fire_when_gagged`, `test_second_saturday_slip_also_no_fire`),
duplicate-cut + duplicate-fill book once; #4 pack RESTATED + distance-to-promotion + gag summaries
(`test_daily_pack_carries_the_restated_tag`); #5 F logic byte-identical (`lane_fh8` untouched;
`test_f_sizing_logic_untouched`). New `test_overnight_doctrine.py` (12 tests); the salvage-mechanics suites
(`test_p19_salvage`, `test_salv1/2`, `test_flip_both_lanes`, `test_p24`) re-anchored to the -M re-arm path
(run un-gagged) with the slip now confirms-symmetric + maker-first. Suite 869 · preflight 23/23.

## WO-2026-07-26-M + WO-2026-07-26-O — SALVAGE EARNS ITS CUT + THE SCRAPE (build 84, one deploy)

Two promises kept on the restated meter (the -N P4 lifetime restatement is Step 0 — it already shipped, and
the scrape's high-water seeds from it): the machine learns exactly when to surrender, and learns to pay its
operator five dollars of every true ten. **Read-rule:** the cited full-spec files (`WO_2026-07-26-M…md`,
`WO_2026-07-26-O…md`) are **absent** from the repo — built from this handoff's S1–S7 / O1–O4 summaries,
reported here. The overnight fixes (baton lifecycle, fill dedup) and the -N gag were independently verified
in the prior build. A real collision surfaced and was resolved (below).

**STEP 1 · WO-M — the sighted, confirmed, floored salvage.** The -N gag ends; `SALVAGE_GAGGED` now defaults
**off** and salvage is LIVE, but only as discipline. **S1 confirmation** (`custodian._salvage_tick`): a slip
fires only after `SALVAGE_CONFIRM_POLLS=3` consecutive polls where ALL of {deep-against (mark ≤ entry−slip),
pinned-at-lows (mark ≤ `low_mark`+ε — a new per-position low-water tracked each `tick`), spot-confirm (spot
on the losing side; spot-blind-proof — an unseen spot never blocks)} hold — a single-tick dip that recovers
never fires (the exact overnight regret). **S2 worth-floor** = `SALVAGE_WORTH_FLOOR_C=30`¢ residual. **S3
rarity + auto-gag**: more than `SALVAGE_RARITY_MAX_PER_DAY=6` fires in a rolling day sets `_salvage_auto_
gagged` (`SALVAGE_OVERACTIVE` page) and the machine holds — `salvage_disarmed()` = manual gag OR auto-gag.
**S4 maker-first** (`_fire_or_gag_salvage`): rest at the held mark, stage-2 crossfire after R — never the
overnight's immediate crossfire. **S5** the regret ledger (`SALVAGE_SUMMARY`/`SALVAGE_WOULD_FIRE`) stays on.
**S6 middle-band** invariant (`SALVAGE_WORTH_FLOOR_C ≤ mark ≤ SALVAGE_BAND_MAX_C`) on both the slip and
needle paths; a position that went deep yet concluded a big loss without firing pages `SALVAGE_MISSED_WINDOW`.
**S7** all cut paths fold under one discipline. **F_EVENT_TRIPWIRE's day-long suppression is RETIRED** — the
money rate halt governs a run; a single loss only pages (`F_LARGE_LOSS`).

**STEP 2 · WO-O — the scrape.** `ledger.owed_cents` / `tradeable_cents` (`ledger.py`): `trading_equity =
book − Σ(non-BASELINE cash)` = baseline + Σ settlements, so a **deposit never mints** (raises book and cash
equally) and a **loss never un-owes** (the high-water `high_water_cents` is a pure `max(persisted, live)` —
only `bank_scrape` persists the advance). **owed** = `$5 per full $10` of high-water above the seed, less
`Σ CONFIRMED_WITHDRAWAL` (a **withdrawal decrements**, no extra wiring). **`tradeable = book − owed`**.
**§O2 — substituted at EVERY sizing base** (the grep artifact): sizing + portfolio cap (`shadow_runner._
score_and_size:book_c`), the at-risk wall + event backstop (`gateway._wall_net_risk_and_at_risk`), the
pct-of-book budget (`gateway._wall_pct_of_book`, now live not snapshot), the rate-halt (`window_econ` —
passed-value − owed, so the account-value param is preserved), the worst-day rail (`ops.worst_day_bound_
line`), `ledger.drawdown_breached`, the scoreboard preview (`scoring`), and `boot.sizing_line`. Treasury /
reconcile / invariant keep reading raw `book_cents` (the owed money is earmarked, still in the account).
**§O1/Step-0** seeds the high-water at the restated equity at boot with a 💰 announcement. **§O3** `bank_
scrape_and_watch` banks silently, announces 💰 only on a milestone crossing, and prints owed on hourly/boot
(`sizing_line` now leads `book / owed / tradeable`)/daily (`ops.owed_line`). **§O4** `/owed` command,
withdrawal auto-decrement, and `OWED_UNDERWATER` (halt entries if tradeable < one F lot, auto-clears on
recovery). **Wiring assert:** no salvage event can increment owed — a salvage realizes a loss, the
high-water is monotonic (`test_salvage_loss_never_increments_owed`).

**Honest divergence / collision resolved:** the engine carried a grep-guard (`test_epoch2_grep`) that
**forbade the tokens `scrape` and `owed`** as retired *waterfall*-era profit-split terms. WO-O (Drew's
ruling) revives `scrape`/`owed` as the operator-earn — a *different* thing — so the guard's `FORBIDDEN` list
was narrowed to keep `waterfall`/`mark_paid` dead while releasing `scrape`/`owed` as ruled terms; cited in
the test. The venue-CSV cross-check (restated lifetime reconciles to the 07-26 CSV within $1) is a
boot-time artifact — the test env has no CSV, so it's noted, not automated (the restatement itself is
tested).

**Acceptance:** #1 restatement RESTATED tag prints (`test_daily_pack_carries_the_restated_tag`, -N); #2
salvage replays — sustained-pinned SHOULD-FIRE maker-first, spot-recovered NO-FIRE, worth-floor NO-FIRE,
overactive auto-gag (`test_52230_should_fire…`, `test_52345_no_fire…`, `test_worth_floor…`, `test_overactive_
auto_gags`); #3 scrape walk — +$10→owed $5, drawdown/recovery owes nothing new, +$20→$10, deposit mints
nothing, withdrawal decrements, salvage-loss never increments, `/owed` correct (`test_scrape_and_salvage`);
#4 sizing truth — worst-day/entry-path/wall/boot read tradeable (`test_worst_day_bound_reads_tradeable`,
`test_size_order_base_is_tradeable…`, `test_gateway_wall_and_boot_read_tradeable`); #5 **F entry/hold logic
byte-identical** (`lane_fh8` untouched; `test_f_sizing_logic_untouched`); #6 boot prints salvage LIVE +
book/owed/tradeable + `OWED_UNDERWATER` guard. New `test_scrape_and_salvage.py` (17); salvage-mechanics +
grep-guard + sizing-line suites re-anchored (3-poll confirm, tradeable base, released tokens, /owed
whitelist). Suite 886 · preflight 23/23.

## WO-2026-07-26-P — THE WHY BAKE & THE ONE-LOT TRACE (build 85, one deploy)

**Read-rule.** The one-lot chain verified TRUE at source: `book.visible_depth(side, price)`
returns contracts at the EXACT level (0 when F prices a fresh tier it is CREATING) →
`depth or 0` (former shadow_runner sizing) coerced the honest 0 → `min(notional, 0×0.25)=0`
→ `max(1, contracts)=1`. Two silent fallbacks turned an honest 0 into a 1-lot bet. Sibling
sweep (B3): the ONLY true sizing fabrications were those two; `sizing.py:77` (`max(1,
depth_max)` under `if visible_depth >= 1`) is RULING-3 — DEPTH-driven and HONEST (a real
book with ≥1 visible lot admits 1 lot); the REST-touch qty `or 1` proves ≥1 from a reported
touch (same logic); `max(1, price−w)`/`max(1, price_cents)` are price-clamp/div-guards. Each
cited HONEST or FIXED.

**Part B — the trace fixed.** #B1 two-question depth API in `book.py`: `joining_depth(side,
price)` (contracts AT the level an order joins; **None** on a blind book — the honest "I
don't know", never a fabricated 0) and `band_depth(side, lo, hi)` (total resting in a bounded
band). Every lane that creates a level in front of a band sizes its depth ref to
`max(joining, band × BAND_DEPTH_FRACTION)` inside a ±`SIZING_BAND_HALFWIDTH_C`¢ band, and the
size row's why names **joining, band, band_ref, and used** (`test_b1_fresh_level_sizes_to_band…`).
#B2 both silent fallbacks killed: a blind book (`joining_depth` None) → `DEPTH_BLIND` defer,
count=0; a real book sizing to 0 → `SIZE_ZERO_DEFER` with `{tradeable, joining, band,
band_ref, depth_used, sizing}`, count=0 — **never a fabricated 1**; the submit loop skips any
count≤0 proposal (`test_b2_blind_book_defers…`, `test_b2_zero_size_defers_not_one`,
`test_the_two_one_lot_windows_replayed`). A 1-lot may only exist because the math said 1.

**Part A — the Why Law, baked.** #A1 the shared surface writer (`Surface._insert`, the ONE
path to `surface_rows`) REFUSES a row with no why — insert-before-record, so a refused write
leaves no state to dedup against (`test_a1_surface_row_without_why_is_refused`). #A2
`tests/test_no_silent_fallbacks.py` greps the money modules (sizing/book/scoring/shadow_runner)
for the fallback family (`depth/mark … or <n>`, `max(1, <size/depth>)`), FAILS on a new one,
and is PROVEN to catch a planted `or 0`; audited honest floors carry an in-source
`fallback-audited:` marker (RULING-3, the REST-touch guess). #A3 `registry.py` — every surface
declares `{surface, question, validates_or_invalidates, consumer}`; 6 standing questions seeded;
boot asserts writers registered (WARN until 2026-07-27, then FATAL); the daily pack prints the
registry + each surface's last-read; a surface unread >14d pages `DATA_WITHOUT_QUESTION`
(`test_a3_*`). #A4 every capital constant is tagged in-source RULED(date)/DERIVED(source)/
DREW-DEFAULT(pending); boot prints the constants that CHANGED this deploy with their tag (WO-P
changes exactly the two band constants, no dial moves); a DERIVED with no source fails loud
(`test_a4_*`). #6 **F entry/hold byte-identical** — `lane_fh8.py` untouched (kill condition
honored); the two-question API lives in `book.py` + the shadow_runner chokepoint, never inside
F. New suites `test_why_law.py` (16) + `test_no_silent_fallbacks.py` (3); `test_verify_lossterm1`
+ `test_attribution` re-anchored to the defer/why law. Suite 903 · preflight 23/23.

## WO-2026-07-26-Q — DELETE GUARD (B): THE LAST SILENT GOVERNOR (build 86)

**Read-rule.** The surviving governor verified TRUE at source: WO-M retired the F day-long
tripwire only at the custodian (custodian.py:490); the ORIGINAL pair from WO-2026-07-23-B lived
on in shadow_runner — producer `_trip_f_event` (474-494, set the ledger flag + paged on any F
loss > `F_EVENT_TRIPWIRE_C`=60¢/contract), consumer `_f_suppressed_today` (469-472) at the entry
choke (620-632, the `F_SUPPRESSED … refused until tomorrow` log), backed by ledger
`set_f_tripwire`/`f_suppressed` and the persisted `f_tripwire_day` state key. Today's 1:13 PM
loss set the flag; every F proposal since sized full and was refused for ~5h.

**The change — deletion, not modification.** #1 `_trip_f_event` deleted; its held-to-settlement
call site now pages `_page_f_big_loss` (F_BIG_LOSS, information only). #2 `_f_suppressed_today`
and the `F_SUPPRESSED` entry-choke branch deleted. #3 ledger `set_f_tripwire`/`f_suppressed`
(and their orphaned `_day_key` helper) deleted; a one-time boot migration
`clear_f_tripwire_migration` drops any live `f_tripwire_day` flag so the deploy resumes F (the
resume-tonight test). #4 `F_EVENT_TRIPWIRE_C` retired from gating — it survives ONLY as the
F_BIG_LOSS page threshold, retagged **RULED(2026-07-26)** in the WO-P constant table. #5 sibling
sweep: zero surviving governor consumers (grep-guard test); the custodian's page unified to the
one name F_BIG_LOSS; boot banner guard-(b) line + PROFILE build 86 announce the deletion.

**Why (the Why Law, one paragraph).** The machine already has a ruled, money-denominated,
self-scaling governor for this risk: the F rate halt (a RUN of stop-equivalents, pages loudly,
resumable). Guard (b) duplicated that judgment with a cruder rule and no resume lever — one risk,
two governors, jointly unaccountable. One risk, one governor, one why: the rate halt stays, the
duplicate dies.

**Acceptance.** #1 a set `f_tripwire_day` flag is cleared on boot and the next F window sizes
full (`test_q_boot_migration_clears_a_live_tripwire_flag`, `test_q_big_f_loss_pages_but_never_
suppresses`). #2 `F_SUPPRESSED` gone from the tree; F_BIG_LOSS pages at >60¢/contract with zero
entry effect. #3 sibling grep artifact — zero surviving flag-family consumers
(`test_q_no_suppression_consumer_survives`); boot banner updated. #4 **F selection/sizing/walls/
salvage/scrape byte-identical** — `lane_fh8` untouched; this order deletes, it does not tune.
`test_scale_f` tripwire tests re-anchored to the deletion; `test_swing_gate_event` mock cleaned.
Suite 903 · preflight 23/23.

## WO-2026-07-26-R — THE WATCH ASKS ITS OWN QUESTION (build 87)

**Read-rule.** Verified at source: the post-entry watch arms per entry (shadow_runner.py:715,
`divergence_watches[market] = {until, strikes}`) and its check (1471-1503) halted on
`abs(ours − rec) > 3` sustained x3 → `ORIENTATION_DIVERGENCE`, where `rec` is a FRESH
market-record read from a different endpoint than the orderbook. The correct inversion detector
already exists: `_mirror_signature` (shadow_runner.py:1300-1302, `abs(ours−rec) > 10 and
abs(ours−(100−rec)) ≤ 3` — the WO cited 1309-1311; honest line divergence, logic matches). On
quiet books the summary endpoint lags the orderbook by a spread routinely → Sunday's two halts
(y30-vs-y26, a 4¢ offset) were freshness noise wearing an orientation alarm. Why-Law class: right
sentinel, wrong question.

**The change.** The halt condition is now `_mirror_signature(ours, rec)` (inversion) OR a gross
non-mirror gap (`≥ ORIENTATION_GROSS_DIVERGENCE_C`=15¢, DREW-DEFAULT), each sustained x3 — the
cases that actually mean our read can't be trusted. A small sub-gross, non-mirror offset
(`> BOOK_STALE_OFFSET_C`=3¢) demotes to `BOOK_STALE`: both values logged (Article 1), a feed
resync request (`feed.resync_needed.add`), counted by UTC hour in the daily pack
(`ops.book_stale_by_hour`) — the registry question (`registry.py` BOOK_STALE: endpoint-lag by
hour) that makes the tolerance derivable — and the halt strikes RESET (a stale read is affirmative
evidence the book is not inverted). Auto-recovery (the `≤3¢`-agreement resume), the
ORIENTATION_HALT_STUCK page, and /reset_halt are untouched. Both thresholds tagged NEW in the
WO-P constant table.

**Acceptance.** #1 Sunday's y30-vs-y26 replays as BOOK_STALE + resync across three checks, no
halt, entries continue (`test_tonights_y30_vs_y26_is_book_stale_not_a_halt`). #2 synthetic
inversion (ours 30, record 70 — exact mirror) → HALT x3, ceiling armed, auto-recovery on a clean
fresh read intact (`test_synthetic_inversion_halts_x3_and_arms_recovery`,
`test_inversion_auto_recovers_on_a_clean_fresh_read`). #3 gross non-mirror gap (ours 30, record
50 = 20¢) → HALT — the unknown-unknown catch (`test_gross_non_mirror_divergence_halts`). #4 pack
counts BOOK_STALE by hour, both constants tagged NEW, the check reuses `_mirror_signature`
(`test_pack_counts_book_stale_by_hour`, `test_both_constants_tagged_and_new_this_deploy`,
`test_the_watch_reuses_the_existing_mirror_detector`). **F path byte-identical** — `lane_fh8`
untouched; the inversion protection is unchanged, narrowed to its disease. New suite
`test_watch_asks_its_question.py` (9); `test_p13_narration` divergence test re-anchored to an
inversion (a 7¢ freshness offset no longer halts). Suite 913 · preflight 23/23.

## WO-2026-07-26-S — THE SECOND ROOM (build 88, four stages, one branch)

Multi-series F: the proven gates, more markets, one book. F is expanded to more SERIES, not
more features — coverage is an ensemble property, caution is a lane property. Shipped in four
green, pushed stages (Drew's ruling: incremental commits, each green).

**Part 1c — the cold read** (`docs/S_COLD_READ.md`). On receipt of the archived multi-asset
repo, every old-era mechanism tagged SALVAGE / SUPERSEDED / DEAD. **Headline finding (honest
divergence from the order's premise):** the cross-asset ensemble cap and correlated-loss
detector NEVER existed in the crypto era — the four bots were byte-identical separate processes,
each risking 20% off the full balance with zero coordination (the blind-twins pathology §1b
rejects). The correlated-tail governor is built FRESH; the blueprint is the weather-era
`d_worker/budget.py` reservation ledger. Portable salvage: the counterparty-liquidity gate
(already live in F's R2 ladder) and the XRP mechanics (KXXRP15M, 1¢ tick, maker-only, $0 fee to
be OBSERVED, 15-min hold-to-settle, no TWAP).

**Stage 1 — series as a dimension.** Every lane key becomes (series, lane); no schema migration
because series is a COMPUTED key (`series_of(market)` = ticker prefix). config: `SERIES` roster,
per-series `SERIES_MODE`/`series_is_live`, per-series F dial `f_notional_pct_of` (BTC 24% earned,
new room born 20% RULED). Sibling sweep: `lane_fh8` wall-1 gate parameterized to
`F_SERIES_ALLOWED` (default `{KXBTC15M}` → golden tape byte-identical); the dial threads into
`sizing.size_order` via `notional_pct`; `venue.list_open_markets` iterates the roster; `lane_d`
stays explicitly BTC-scoped (cited). `test_second_room.py` (9).

**Stage 2 — the ensemble governor.** The existing guard (a) reframed as the ENSEMBLE CAP
(`ENSEMBLE_AT_RISK_PCT` RULED 50%): total simultaneous at-risk across ALL rooms ≤ 50% of
TRADEABLE, one summed check above the lane walls, defers with the why on the row. Per-series
halts: `halt_scope(series, lane)` — the rate halt keys on (series, lane); with one room the scope
is the bare lane (byte-identical, zero migration), `{series}:{lane}` with more. Correlated-loss
rule: `combined_correlated_loss` / `record_correlated_window` — ≥2 rooms losing one wall-clock
window counted ONCE at combined size, paged + a CORRELATED_LOSS registry question (the measured
datum that derives the cap). `test_ensemble_governor.py` (7).

**Stage 3 — the XRP room.** `/series <asset> on|off|live|shadow` (the start command, no second
deploy) → `engine._cmd_series`: opens/parks a room, widens F's family, migrates the halt keys the
moment a second room joins (`migrate_halt_keys_to_series` — a live BTC halt survives the roster
growing, real-money safety). BTC cannot be turned off from here; the global kill governs all.
Discovery iterates `f_enabled_series()` (OFF rooms polled by nothing). Pack: `series_chapter_lines`
— one block per room with the FIRST-DAY MECHANICS WATCHLIST for a new room (maker $0 observed on
its own tape, settlement attribution, tick/strike — pages, never pre-blocks). The counterparty
gate (Part 3 item 2) is already live in F's R2 ladder. `test_second_room_open.py` (8); the
command-whitelist tests updated for `/series`.

**Acceptance (`test_second_room_acceptance.py`, 7).** #1 boot banner carries per-room modes/dials,
the ensemble cap, and the build-88 PROFILE; XRP's chapter renders once the room opens. #2 (series,
lane) is a computed key — no row migration; hardcoded-BTC sites parameterized (F gate) or
explicitly BTC-scoped (D). #3 ensemble cap defers-with-why; correlated-window losses count once at
combined size. #4 (SUPERSEDED by Drew's live-day-one ruling — the ghost/tuition graduation is
replaced by the first-day mechanics watchlist + the room's own halt) the watchlist is present and
never pre-blocks. #5 **BTC's F path byte-identical** — the BTC room dial equals the earned
constant, the golden tape is green, `F_SERIES_ALLOWED` defaults BTC-only. #6 scrape/salvage/
sentinels shared — one hwm, one owed; `owed_cents`/`tradeable_cents` take no series argument (the
BOOK is the unit of stewardship). Suite 944 · preflight 23/23. **XRP opens via `/series xrp on`
(or `SERIES_LIST=KXBTC15M,KXXRP15M`) — the default roster stays BTC-only so the printing room
keeps printing until Drew opens the second.**

## WO-2026-07-26-T — THE TWO GUARDS (open the room clean, build 89)

Two protections named by the -S build's own review, landed BEFORE KXXRP15M's first live
window. Both are no-ops on BTC (the printing room stays byte-identical); both bound a trap the
per-series halt can't price.

**Guard 1 — the counterparty-liquidity gate into F's entry path.** The finding: F's entry
sequence had no counterparty check (only H8's inherited ladder R2 reset, lane_fh8:428-437). A
maker buy fills against the OPPOSITE side; on XRP's thin book an empty opposite side means the
order rests forever (cheap) or — worse — fills into a vanishing book with NO exit liquidity
(salvage's maker-first rest has nobody to rest against; the bound widens from salvageable to
TOTAL). In `_score_and_size`, after DEPTH_BLIND and before sizing, F entries read the opposite
side's best bid from the book already in hand: `opp_bid is None or depth == 0` → `NO_COUNTERPARTY`
defer (re-eligible next poll, the why on the row), counted once per window in `failures` by
series/hour. Existence, not a threshold (constant-free). Applies to all series (Drew: "all — it's
free and BTC never triggers it"). Registered data-question `NO_COUNTERPARTY` + pack section
`no_counterparty_by_series_hour` (the room's liquidity map). `test_two_guards.py` G1 (5): empty
opposite refuses; a bid next poll re-proposes; BTC deep book never triggers; counted once not
every poll.

**Guard 2 — the table speaks its series or says BLIND.** The finding: the delta table is
BTC-trained (Coinbase BTC-USD, 180d) and no consultation was series-scoped — the -S Adversary
lens's exact named leak. `delta._TABLE_SERIES = "KXBTC15M"`; `_lookup`/`p_cross`/`p_survive`/
`p_end`/`p_end_wilson_lb`/`distance_for_p` gain `series=None` and return None (BLIND) for a
foreign series. The F card composer (lanes.py) prints `surv n/a (no KXXRP15M table)` instead of
borrowed BTC physics — the Why Law's Article 1 (a row's numbers answer for themselves). **Sibling
sweep** (every delta value/query consumer, cited): lanes.py:295 (F card, series-passed);
spotlead.py:82/129 `needle`/`settle_fair_favored` (series param, callers pass it);
custodian.py:327 (salvage anchor, pos.market's series); shadow_runner.py:1375 (F entry-proof
card, market's series); lane_flip.py:1087/1091/1092/1125/1233/1287/1313 (FLIP swing + HUNT
gates, ctx["_series"] stamped at evaluate). Every consumer was already None-safe (built for
blind/absent table); the series scoping makes a foreign lookup None, which propagates safely with
no fabricated number (Adversary ii; the planted-None test proves it). `test_two_guards.py` G2 (4):
BTC answers / foreign is BLIND; the XRP card prints surv n/a; spotlead needle+settle BLIND for a
foreign series; a planted foreign None fabricates nothing downstream.

**BTC F path byte-identical** — both guards are no-ops on BTC (deep two-sided book, own-series
table); every new signature defaults `series=None` (legacy behavior). The golden-tape F/H8
regression is green. Test fallout: the many tests that monkeypatch delta functions had their stub
signatures widened to accept the new kwarg (`**_kw`). Suite 953 · preflight 23/23. **XRP's
SERIES_MODE flips LIVE only in the deploy carrying both guards green (this one).**

## WO-2026-07-27-V — THE SLEEPING SENTINEL (build 90)

A live-money state-integrity defect: Drew withdrew ~$45 mid-session (venue $20.70 cash) and the
machine never noticed — no CASH DELTA page, sizing on a phantom $97 book, BTC bouncing
insufficient-funds silently for hours while XRP's ~$2 depth-bound entries still fit real cash.

**Root cause (traced line-level).** T2 ruled out: `bank_scrape_and_watch` (O4, shadow_runner:764)
never touches `book_cents` — the withdrawal isn't consumed there. T3 ruled out: `account_value`
(shadow_runner:1062) reads a *fresh* venue balance each attempt, not cached. **T1 confirmed:**
`cash.reconcile` defers whenever `in_flight_orders != 0 or unsettled_fills != 0` (ledger.py:748 —
the quiescence window), and `standing_reconcile` treated that quiescence-DEFERRED as **benign** —
it did NOT advance the stall streak (shadow_runner:1276-1278), so `_recon_note_deferred` /
RECON_STALLED never fired. A two-room engine that never goes quiet therefore reconciled NEVER,
silently, and the book stayed a phantom. This is the U6 class (RECON_STALLED not counting
quiescence holds), promoted from cosmetic to causal and shipped here.

**The fix (T1).** Quiescence starvation is not benign: `_recon_check_starved` pages `RECON_STARVED`
when the book has gone unverified against the venue for `RECON_MAX_QUIET_S` (30 min, DREW-DEFAULT),
for ANY reason including a busy quiescence hold — once per episode, re-armed by the next clean
reconcile. The sentinel's pulse (clean reconciles per UTC hour) is persisted (`recon_cycles` table,
`record_recon_cycle`) and rides the pack (`recon_cadence_lines`) — a run of 0-clean hours is
visible starvation. `account_value` now stamps the last CONFIRMED venue *cash* + age on the ledger
for the belts.

**The two belts (ship regardless of trace).** **B1** — `gateway._is_balance_error` classifies a
venue rejection whose error EXPLICITLY names insufficient funds/balance → `BALANCE_REJECTED
{lane, series, cost, last_confirmed_cash}` once per window (the market ticker rolls each window →
natural dedup); a transient 500/429 never pages it. A lane dying silently is its own Article-2
violation — the February live-balance law's missing SCREAM half. **B2** — `_cash_sanity_clamp`,
pre-submit: an entry's cash cost over the last CONFIRMED venue cash (the venue number, not the
ledger's belief) DEFERS `CASH_SANITY`; a confirmation older than `CASH_CONFIRM_MAX_AGE_S` (30 min)
defers everything and pages `CASH_STALE` — blind is not solvent. A phantom book can never spend
money the venue already said isn't there.

**Acceptance (`test_sleeping_sentinel.py`, 11).** T1: quiescence starvation pages RECON_STARVED
once per episode; a fresh reconcile does not; the cadence is recorded and packed. B1:
insufficient-balance pages once per window, a transient 500 does not, the classifier keys on the
insufficient-funds language only. B2: cost > venue cash defers, within-cash is a no-op, a stale
confirmation defers + pages CASH_STALE. Data-questions `RECON_CADENCE`/`BALANCE_REJECTED`
registered; both new constants tagged NEW. **F/XRP entry, salvage, scrape byte-identical** — all
three fixes are LIVE-gated (no-op in shadow/on a healthy book); the golden tape is green.
Suite 964 · preflight 23/23. Operational note until deploy: withdraw, then restart (or treat an
absent CASH DELTA page as the alarm).

## WO-2026-07-27-W — THE 24-HOUR RULINGS · P1 (broker cash is the only sizing truth) + P4 (the cash race)

Twenty-four hours of three-room live operation produced four rulings. F's entry/hold/exit logic is
**byte-identical** throughout — every ruling touches the surroundings, never the edge (golden tape
F/H8 stays green). This section covers the first increment: **P1** and **P4**, both LIVE-gated so
shadow and tests size off the paper book unchanged.

**P1 — the sizing base is the venue's live CASH, never the book, never the portfolio.** Drew:
"broker data is truth 100% of the time." `Ledger.tradeable_cents()` is the ONE base every sizing
site, gateway wall, worst-day, ensemble and halt-geometry read — a single chokepoint. In LIVE it
returns `last_venue_cash_cents − owed` (the venue's spendable cash, stamped by every reconcile /
balance read per WO-V); the ledger's **book** demotes to reporting / P&L narrative, and the
**portfolio** balance (cash + marked positions — an estimate that stays ~constant and
double-counts deployed money) is read **nowhere** in sizing (grep artifact, on the executable code
with the docstring stripped). Because the base is *spendable cash*, it mechanically shrinks as
rooms deploy, making over-commitment structurally impossible. Pre-first-read (no venue cash stamped
yet) it falls back to the book so boot has a base until the first reconcile runs. SHADOW ignores any
stamped cash and stays the paper book — byte-identical.

**P4 — the cash race is self-limiting; the ensemble ceiling reads total capital.** Deployed cash
leaves the balance, so a later proposal in the same window races only for what *remains* — bounded
by arithmetic, no lock. The ensemble cap needs the whole tail, so `ensemble_base_cents()` returns
cash + current at-risk (`deployed_cents`) in the cash regime — venue CASH already EXCLUDES deployed
money, so the at-risk is added back to recover total capital; in the book regime the book already
reflects deployment, so the base is just `tradeable_cents`. `shadow_runner._score_and_size` reads
`ensemble_base_cents()` for the `ENSEMBLE_AT_RISK_PCT` (50%) ceiling and names it on the row
(`capital=…c`).

**Acceptance (`test_cash_truth.py`, 7).** SHADOW tradeable is the paper book even with a stale
venue-cash stamped; LIVE tradeable = venue cash − owed (2070, not the 9700 phantom book) while the
book survives as reporting; LIVE falls back to book before the first venue read; ensemble base =
cash + at-risk in the cash regime and = tradeable in the book regime (no double-add); the cash race
(a later proposal sizes off less as venue cash drops); the grep artifact (executable code of
`tradeable_cents` reads `last_venue_cash_cents`, never `portfolio`/`pv`). Ensemble-governor and
FLIP-cap tests updated for the `capital=`/`ensemble_base_cents` base. **F entry/hold/exit
byte-identical** (LIVE-gated; golden tape green). Suite 971 · preflight 23/23.

## WO-2026-07-27-W — THE 24-HOUR RULINGS · P2 (the spent loss: per-lane halt geometry + the spent ledger)

**Read-rule (verified at source).** `config.py:621-670` — TRUE: `rate_halt_drawdown_c` is the desk's
stop-derived bound (`4 × lots × OPEN_MOMENTUM_STOP_C`, lots from `FLIP_NOTIONAL_PCT`), i.e. F was
being halted on the *desk's* geometry. `window_econ.py:360-370` — TRUE: the per-lane trailing sum
`sum(o["pnl"] for o in outcomes)` compared against a single `drawdown_bound`. Drew's diagnosis
confirmed: F's loss is one large tail per ~25 wins while the bound was ~3% of book, so a single
ordinary −$10–19 tail instantly exceeded it AND poisoned the trailing-8 sum for hours — the
overnight died on one event.

**W2a — F's halt bound speaks F's own loss units.** `config.f_halt_bound_c(tradeable)` =
`F_HALT_TAIL_MULT (1.5) × one full-size F loss at the dial`; one full F loss at a ~97¢ favorite ≈
`F_NOTIONAL_PCT × tradeable` (the clip cost is the whole stake), so the bound scales with cash-truth
(P1) exactly as the desk bound does. `config.lane_halt_bound_c(lane, tradeable)` dispatches: the F
family (`TAIL_HALT_LANES = {F, H8}` — the cheap-favorite, one-large-tail, non-desk lanes) speaks
tail units; every desk lane (FLIP/OPEN/HUNT/D/P) keeps `rate_halt_drawdown_c` unchanged.
`_apply_streak_per_lane` now asks the bound per lane inside the loop. A **single** ordinary tail
(1.0×) sits under the 1.5× bound → no halt (it still pages F_BIG_LOSS, which already exists); a
**cluster** — a second tail, or tail-plus-bleed, inside the trailing window (≥1.5×) — halts that room
only. The F-family halt writes a `TAIL_CLUSTER` surface datum (`n_tails`, drawdown, bound) and the
page reads "tail cluster: N F-size tails"; the desk page keeps "4 stop-outs at book $X".

**W2b — the spent loss.** When any halt clears (`reset_halt`), the triggering losses in each halted
lane's trailing window are summed as SPENT, the window restarts clean (`[]`), and both the
`HALT_RESET` surface row and the reply record what was spent (Article 1). A halt is the punishment
served — the same loss can never convict twice, so a stale tail can no longer re-halt a room it
already answered for. Nothing is spent when the cleared window held no loss.

**Registry + tag.** `TAIL_CLUSTER` joins `SEED_SURFACES` (the standing question: how often does a
room take ≥2 F-size tails in one window — the cluster the halt is meant to catch; measured frequency
turns `F_HALT_TAIL_MULT` from a DREW-DEFAULT into a DERIVED number). `F_HALT_TAIL_MULT` is tagged
DREW-DEFAULT, NEW this deploy, source "pending derivation from tail-cluster frequency per room".

**Acceptance (`test_spent_loss.py`, 8).** F/H8 bound = tail units and desk = stop units; the tail
bound is >4× the desk bound (the bug quantified); a single full F tail does NOT halt; a two-tail
cluster halts F, pages "tail cluster", writes the `TAIL_CLUSTER` datum; the desk (FLIP) still halts
at its stop bound and says "stop-outs"; at reset the losses are SPENT, the window restarts clean, and
the row/reply record it; a cleared win-only window spends nothing; `TAIL_CLUSTER` registered;
`F_HALT_TAIL_MULT` tagged. The p17 retroactive-halt test updated to a genuine full-clip two-tail
cluster (24-lot tails) — ordinary small losses correctly no longer trip F. **F entry/hold/exit
byte-identical** (the halt is the surrounding governor; golden tape green). Suite 979 · preflight
23/23.

## WO-2026-07-27-W — THE 24-HOUR RULINGS · P3 (three rooms, live by default + roster persistence U1)

Drew: "I should not have to confirm via Telegram; unlock ETH — XRP, BTC, ETH tonight."

**The default roster ships in code.** `config.SERIES` defaults to `KXBTC15M,KXXRP15M,KXETH15M` (was
BTC-only) and `SERIES_MODE` carries ETH at LIVE — three rooms open with no `/series`, no env, no
confirmation. SOL stays OFF (banked next). The `SERIES_LIST` env is still the emergency lever;
`/series` is still the override.

**ETH enters exactly as XRP did.** `f_notional_pct_of("KXETH15M")` = `NEW_SERIES_F_DIAL` (0.20 — the
born-at dial until its own record argues); its halt is its own room (`halt_scope` keys on
`KXETH15M:F`, series-scoped now that the roster is >1); the counterparty gate is live for all series
(WO-T Guard 1, a no-op on BTC's deep book); and its F card reads `surv n/a` because the BTC-trained
delta table returns None for any non-BTC series (WO-T Guard 2). BTC keeps its earned 24% dial — F-BTC
sizing byte-identical.

**Roster persistence (U1).** `config.roster_state()` serializes `{series: mode}` for every KNOWN
room; `config.apply_roster()` installs a loaded roster onto the module globals and stamps
`ROSTER_SOURCE`. `shadow_runner._persist_roster` saves the roster to the ledger on every `/series`
change; `_restore_roster` loads it at boot **before** `_apply_series_roster`, so a Drew /series change
wins over the code default and survives a restart. A fresh DB (no persisted roster) boots the
three-room default.

**The banner prints each room with its source.** The boot banner now lists every KNOWN room —
`BTC[LIVE] F@24% · XRP[LIVE] F@20% · SOL[OFF] · ETH[LIVE] F@20%` — with the roster's provenance
(`source=persisted > env > default`); SOL[OFF] is shown, not forgotten.

**The multi-room consequence, honestly handled.** Three rooms make `halt_scope` series-scoped by
default (`{series}:{lane}`), which is the production reality. The per-LANE halt-mechanic tests (lane
attribution, money-halt, spent-loss, persistence — `test_per_lane_halt`, `test_spent_loss`,
`test_aplayer`, `test_p8_golive`, `test_p9_real_numbers`, `test_p27_governor`, `test_morning_four_
build1`, `test_halt_orphan`, `test_p17_show_up`) pin a single-room roster via an autouse fixture: they
verify the series-agnostic mechanic in the byte-identical bare-key path, and series-scoping is covered
by `test_ensemble_governor` and `test_three_rooms`. A `conftest._roster_isolation` autouse fixture
snapshots and restores `config.SERIES`/`SERIES_MODE`/`lane_fh8.F_SERIES_ALLOWED` around every test so
a full-engine boot never leaks its roster into a later test — the suite stays order-independent.

**Acceptance (`test_three_rooms.py`, 8; plus updated roster/banner tests).** The default roster is
three rooms LIVE + SOL OFF; the banner shows each room with `source=default`; ETH is born at 20% like
XRP; ETH's halt is series-scoped (`KXETH15M:F`); ETH's card is `surv n/a` (delta None for KXETH15M);
`roster_state`/`apply_roster` round-trip; a `/series eth off` persists and survives a reboot (U1); a
fresh DB boots the three-room default. **F entry/hold/exit byte-identical** — the golden tape (BTC)
runs green under the three-room default. Suite 987 · preflight 23/23.

## WO-2026-07-28-X — THE VENUE SPEAKS LAST · cold read + X4 (account truth = venue reads)

**The cold read (all findings verified TRUE at source).**
- **X1 — the root (`window_econ.py:250`):** `window_pnl = account_value_cents − br.open_value_cents − cash_moves` attributes the ACCOUNT's total movement across a market's window to that market alone. Correct in the one-room era; with three rooms settling on the same bell, market A's bracket delta absorbs its siblings' simultaneous settlements — the divergence alarm fires by construction on every shared bell.
- **X2 — the inversion (`window_econ.py:259-271`):** on divergence the P-CASH-FATAL-1 §4.6 stopgap quarantines the settlement and **re-books at fills-truth** (`quarantine_divergent_settlements`) — the arithmetic made the authority, the opposite of Drew's law. A one-room fix (the 190945 phantom, where the broker read was the liar) that inverts under three rooms, where X1 pollutes the broker number.
- **X3 — the float spray (`book.py:16-22`):** `to_yes_terms` and `fills.price_cents` are REAL, carrying the venue's 0.1c ticks as floats → the `8.999999999999986c` spray; money math in floats violates the surface's integrity.
- **X7 P0 — the hwm reads the derived book (`ledger.py:257,282`):** `high_water_cents()` → `trading_equity_cents()` = `book_cents() − ext`. The scrape's high-water — the owed ratchet — is computed off the DERIVED book, the very number X1/X2 drift. **Confirmed P0.**

**X4 — TWO JOBS, TWO TRUTHS (the account-truth reporting surface).** The system asked one number to do two jobs; separated permanently. **ACCOUNT TRUTH** (what is the account worth) = **venue reads only**: `account_value` now stamps `last_venue_value_cents`/`_ts` (cash + portfolio value = the Kalshi-app number) alongside the WO-V cash stamp; `ledger.account_value_display(now)` returns `(cents, source, age_s)` — venue value + age in LIVE, the paper book in SHADOW, a book fallback before the first read; `ops.account_headline` formats it age-stamped. Every headline routes through it — `/owed` (`owed_line`), pack MONEY, the hourly Telegram, the settle 📊 line, the halt-clear reply, the RESTATED money section. **ATTRIBUTION TRUTH** (who earned it) stays fills/settlement math: `book_cents` is demoted to internal attribution (lifetime, cell stats, the book-composition arithmetic) and prints as an account value nowhere — the boot SIZING preview relabels its figure "internal sizing-base (not the account value)". Drew's law completed at the last surface it hadn't reached.

**Acceptance (`test_venue_speaks_last.py`, 7).** SHADOW account value = paper book; LIVE = the venue value (3291, not the 9700 phantom book) age-stamped; LIVE falls back to book pre-first-read; `account_headline` formats each source (venue+age / paper / pre-read); `owed_line` prints the venue account, not `book $`; the grep artifact — `owed_line`/`restated_money_lines`/`account_headline` carry no `book $`/`book=` account display, and `account_headline`'s body reads `account_value_display` not `book_cents`; the pack MONEY and hourly route through `account_headline`. Settle/halt/sizing-line tests updated to the venue-headline labels. **F/XRP/ETH entry-exit, salvage, sizing (-W cash base) byte-identical** (X4 is display-only; golden tape green). Suite 994 · preflight 23/23.

*Remaining in this WO (sequenced next, each its own green increment): X6 (integer tenth-cent money math, kill the float spray), X5 (bell-group brackets, retire the quarantine/re-book, BELL_ECON_DIVERGENCE + DISPUTED), X7 (hwm P0 re-seed from venue truth + the one-time drift restatement).*

## HARD STOP honored

Chunks 5 (demo verification), 6 (shadow-lane promotion), 7 (cutover) NOT built — separate
orders at Drew's word. Standing input requests: ~~B1~~ **DELIVERED** (gate 5 green),
**B2** (legacy CSVs → unblocks §E), **charter + lens verbatim texts** (→ closes the
gate-1 placeholder). Remaining open action: deploy the shadow to Render (or any
Kalshi-reachable environment) to start gate 7's 24-hour tape.
