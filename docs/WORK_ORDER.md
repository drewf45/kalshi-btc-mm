# MASTER WORK ORDER — WO-2026-07-17-RELAY — "NEW BRANCH, NEW ENGINE"
## Authority: Drew (capital gate). Builder: Claude Code. Status: CANONICAL.
## This document IS the ruling and the order. Everything in it derives from the 0717 session artifacts.

---

# §A — CANONICAL DECLARATION + RULINGS (recorded by this order)

**A1. RULING 0.1 = (a) PARALLEL BUILD.** Drew is standing up a NEW BRANCH/REPO for the relay engine.
The live Kal (k_worker tree, deployed on Render) keeps trading UNTOUCHED. The new engine builds clean,
runs as PAPER SHADOW against the live feed with ZERO capital, and cuts over only when its F/H8 decisions
match live Kal for 5 clean days (N=5) and Drew rules the switch. **Single-writer law:** the new engine
gets its OWN database; it never writes the live surface DB.

**A2. CANONICAL DOCUMENTS.** Commit these three into the new branch at `docs/`, verbatim, first commit:
1. `docs/CHARTER.md` — KAL_RELAY_ENGINE_FULL_CHARTER_0717 (the machine)
2. `docs/BUILD_SEQUENCE.md` — BUILD_CHUNK_SEQUENCE_AMENDED_0717 (the order)
3. `docs/LENS_READ_CHARTER.md` — SEVEN_LENS_CHARTER_READ_0717 (the walls, folded in below)
Where this work order and those documents conflict, THIS WORK ORDER WINS (it post-dates them and
records rulings they marked open).

**A3. RULINGS 0.2/0.3 (recorded):** the five ratifications (Loss-Asymmetry · transport-walls ·
streams-name-readers · P22-custodies-new-lanes-only · fast-selection/slow-calibration) are ASSUMED and
the boot tape must PRINT the assumption every boot until Drew rules otherwise. Safe defaults in force:
$-at-risk per settlement event = 3× one-lot max loss · Lane D floor = 60¢ (pending Chunk 2 data) ·
depth_fraction = 25%. All three live in config as named constants with a `# DREW-DEFAULT` tag.

**A4. WATERFALL RULING, applied to parallel mode:** the new engine is **born EPOCH 2** — no waterfall,
scrape, owed, or mark_paid code EVER EXISTS in this tree. Tradeable = live balance. The live Kal's
waterfall dies with the old engine at cutover. ("Remove entirely" honored by never being born.)

**A5. WHAT THIS TREE IS AND IS NOT.** The truncated `bot.py` monolith currently in the repo is NOT the
live engine — it is a PARTS SHELF (the DUMP exit logic = Chunk 4 borrow source for P22). Move it to
`reference/legacy_dump_bot.py`, never import from it, never run it. The live engine's tree (k_worker,
AtDcG lineage) will be provided by Drew as a zip — REQUIRED INPUT for §C.4 and §D. Do not port F/H8
from memory or from the monolith; port only from the provided live tree.

# §B — REQUIRED INPUTS FROM DREW (build what you can; STOP at gates that need these)

- **B1.** The live k_worker tree zip (latest deployed, AtDcG lineage) — needed for Chunk 3 ports and
  golden-tape material. [BLOCKS §C.4]
- **B2.** Legacy activity CSVs (the 1,500+ market taker file + the full-history file) — needed for
  Chunk 2 mining. [BLOCKS §E]
- **B3.** Kalshi API creds already on Render env — the new service gets READ-ONLY usage until cutover
  (paper shadow places NO orders; the gateway's live-submit path is compiled in but hard-disabled by
  `RUN_MODE=SHADOW` + the existing `I_UNDERSTAND_LIVE` pattern).

# §C — THE BUILD (this order authorizes Chunks 1→4 of BUILD_SEQUENCE.md in the new tree)

**C.1 — Repo skeleton + canon commit.** Layout per Charter §2 (`relay_engine/` modules: feed, book,
delta, lanes, gateway, custodian, sizing, ledger, surface, ops). Commit canon docs (A2), move the
monolith to `reference/`, `README.md` states: parallel paper-shadow build; live Kal elsewhere; this
tree trades nothing until cutover ruling.

**C.2 — Instrument first (Chunk 1, born-in versions).**
- Ledger EPOCH 2 from birth (A4): %-of-book caps snapshotted at boot + confirmed cash movements;
  honest lifetime from settlements ledger; invariant check (book == balance ± in-flight); drawdown
  rail keeps absolute-floor semantics until book > $50.
- Cash-movement protocol exactly per BUILD_SEQUENCE 1.2: quiescence window (zero in-flight orders,
  zero unsettled fills, else defer); negative delta → halt entries + Telegram prompt WITH BREAKDOWN;
  `/confirm_cash` → re-baseline + CASH_MOVEMENTS table row; `/deny_cash` or 30-min silence → FATAL,
  stay stopped; positive → confirm without halt; monthly true-up line in the pack.
- Book-snapshot recorder ON from first boot (reader: replay harness + shadow verdicts).
- Attribution schema per BUILD_SEQUENCE 1.4: per-fill (lane, market, side, cost-basis, size-tier);
  custodian exits attribute to OPENING lane's Wilson cells; acceptance test = stacked market
  reconstructible per-lane from surface rows alone (ship the test).
- Surface aggregation per 1.5: one terminal row per lane per market per window + interim counters;
  full rows on state changes only.

**C.3 — Spine (Chunk 3, lens walls folded in).**
- Feed: WebSocket-first (orderbook_delta, ticker, trade, fill, market_positions, lifecycle); venue
  error codes fail-loud; staleness stamps. **Degrade ladder:** WS loss → ALL-lane entry halt →
  custodian on REST (risk reduction always allowed) → snapshot resync → resume after 10 clean frames.
  REST poll allowed for exploration-shadow only, rows tagged EXPLORATION (feed-parity law: promotion
  cells require WS evidence).
- Gateway: ONE single-threaded submit path (stated in code); ONE canonical YES-terms conversion
  function (book is bids-only reciprocal — all math from yes-bid/no-bid); walls in order per Charter
  §6 with amendments: **NET-RISK cross-lane cap ≤3 + $-at-risk cap (A3 default), risk-reducing orders
  EXEMPT and classified at the canonical layer** · `REJECT_WRONG_WAY_TICK` (improving a BUY = +1
  toward ask; improving a SELL = −1 toward bid; sign derived per order, never hardcoded) · %-of-book
  budgets · sizing-tier authorization · fee tripwire watching BOTH the multiplier and the maker-fee
  designation list. Exits/cancels skip walls. Maker-only/post-only everywhere. Rate governor: token
  bucket, printed number IS the enforced number, alert throttling carries counts.
- Sizing born-in (Chunk 3/Charter §8): P16-B ladder (SUPPRESS/PROBE/LEAN/CLEAR on Wilson lower
  bounds), ~1/12 Kelly ceiling, per-level size ≤ depth_fraction of visible depth, thin-book backoff.
  Capital raises budgets, never tiers.
- Delta table: borrow the live `delta_table.py` + candle fetcher WHOLE (from B1); settlement truth
  anchors to `floor_strike`/`expiration_value` on the market record, never generic spot; API =
  gate-input only (`p_survive`, `p_cross`), no scheduler role.

**C.4 — Port F and H8 [GATED ON B1].** Byte-identical = LADDER + GATES ONLY (v4 watch-confirm floors
99/98/97/95, dropout/side-flip/counterparty resets, R2, queue instrumentation; H8 delta-gate ≥99%),
proven by **golden-tape regression**: replay recorded markets through live-F and new-F; identical
proposals/passes required, diff report committed. Cap plumbing + custodian passthrough are declared
changes with their own verifies. All five lanes registered (D/MM/P as SHADOW/STUB per charter §5);
every lane evaluates every market every cycle; a Pass is a first-class terminal row.

**C.5 — Custodian (Chunk 4).** Recover DUMP exit logic from `reference/legacy_dump_bot.py` + the
branch `claude/trading-contract-math-2biFE` if provided; generalize to per-lane cut params scaled by
size tier. Baton lifecycle: cancel-resting-exit THEN cut, atomically verified, fail-loud on partial
(`decrease/reduce_by` available for partial reductions). Kill semantics: lane-kill halts ENTRIES only;
open positions custodied to conclusion, loudly. Lane F in PASSTHROUGH (cut-disabled-except-
catastrophic) until the custodian earns F on its own clean weeks (Drew's gate).

# §D — VERIFY GATES (in order; STOP and report at each)

1. Canon committed; monolith quarantined to `reference/`; README states the mode. [no inputs needed]
2. Boot tape on paper: prints EPOCH 2 header, ratification assumptions, DREW-DEFAULT constants,
   SHADOW mode, recorder confirmation. Synthetic ±$5 delta test passes both branches (confirm resumes /
   deny stays FATAL).
3. WS degrade ladder fire-drill: kill the socket mid-shadow; watch halt → REST custodian → resync →
   resume on tape.
4. Wrong-way-tick + net-risk walls exercised synthetically (including a resting exit NOT consuming cap).
5. [Needs B1] Golden-tape regression green for F and H8; diff report committed.
6. Attribution acceptance test green on a synthetic stacked market.
7. Shadow runs 24h against live WS feed: per-lane terminal rows census-clean, zero orders placed,
   daily pack renders with per-lane sections.
**HARD STOP after gate 7.** Chunks 5 (demo verification: `Get Total Resting Order Value` + the
`netting_enabled` first-order lock test), 6 (shadow-lane promotion evidence), and 7 (cutover) are
separate orders cut at Drew's word.

# §E — PARALLEL ANALYSIS TRACK [GATED ON B2, engine-independent]

Poster-price extraction (prices posters offered per asset, fill timing vs window clock, density by
cell) · forgone-clip decomposition by price cell × band (rules the D floor 50¢ vs 60¢) · caution-ledger
cost-vs-savings decomposition · Feb 27 whipsaw tape → Lane P negative test set · session/liquidity
priors across BTC/ETH/SOL/XRP. Five artifacts to `docs/analysis/`.

# §F — STANDING LAWS FOR THIS BUILD (from the sixteen; the ones Claude Code touches daily)

Single gateway; Telegram is accounting/alerts only (`/confirm_cash`, `/deny_cash` adjust baselines,
never orders, never tiers) · fail loud, FATAL on integrity violations · every recorded stream names
its reader or carries SHELF+date · every capital-touching module's docstring declares win/loss path
symmetry · borrow MECHANICS from anything on any branch; borrow PARAMETERS/EDGE from nothing — gates
come only from the canon docs · read-rule applies to Claude Code's own reports back (cite files/lines,
tag TRUE/FALSE/UNPROVEN).

## SEAL
This order makes the plan canonical, records the parallel ruling, and authorizes the new engine
through its first 24-hour paper shadow — instrument first, spine second, custodian third, hard stop
before anything touches money. The live Kal trades on, untouched, until the new machine earns the
book. Two inputs from Drew unlock the gated halves: the live k_worker zip (B1) and the legacy CSVs
(B2). Everything else starts now.
