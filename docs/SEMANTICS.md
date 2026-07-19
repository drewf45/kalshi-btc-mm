# SEMANTICS — What This Machine Believes (P21 B1, the Answer Registry)

One page. Every banked answer as `LAW · CODE · TAPE` — the law in words, the
code that enforces it (file:line at time of writing; the FILE is the durable
cite), and the tape_grade line that FAILS if it breaks. A KNOWN without a
tape test is a hope with confidence (Answer Ledger §4.1).

STANDING RULE — KNOWLEDGE_DRIFT (B2): a KNOWN whose tape test fails
auto-demotes to QUESTION in the daily pack, pages `⚠ KNOWLEDGE_DRIFT
[{answer}]`, and blocks nothing. Reality outranks this registry; the
registry records the demotion. The Saturday retro re-proves or amends.

THE WALL (B3): every entry below MUST name an existing tape_grade line —
`tests/test_p21_doctrine.py` greps this file and fails the suite on any
entry that doesn't. The registry cannot rot silently.

Boot cites this page; `relay_engine/semantics.py` parses it; the daily pack
re-runs every TAPE line here forever (registry tests never retire).

## KNOWN

### 1. The book cannot lie
- LAW: an incoherent book is POISONED and untradeable until a clean
  snapshot cures it; deltas over an empty book build fiction (poison/resync
  tape, 10:00 PM).
- CODE: `relay_engine/book.py:44`
- TAPE: `zero unexplained orientation pages`

### 2. The money cannot lie
- LAW: every traded window opens and closes an account-truth bracket
  sourced from the venue — no paper numbers in live (deposit tapes
  +980/+57/+60).
- CODE: `relay_engine/window_econ.py:178`
- TAPE: `closed brackets source=venue`

### 3. The ear lives, the leash holds
- LAW: the Telegram listener is a supervised task whose death is tagged;
  the halt persists across restarts and only /reset_halt lifts it (11:15).
- CODE: `relay_engine/shadow_runner.py:1016`
- TAPE: `TASK_STUCK bounded: zero or one with its story (§3)`

### 4. Every dollar narrates
- LAW: every fill books once, attributed by order index, and tells its
  story on the surface — a window is never left mid-story (P13, all-day
  tape).
- CODE: `relay_engine/fills.py:85`
- TAPE: `every closed window concluded: receipt or PASS (§6.3)`

### 5. Orientation is measured
- LAW: book orientation is never assumed — boot self-test, settlement
  cross-check, and sentinels measure it continuously (green since boot).
- CODE: `relay_engine/shadow_runner.py:600`
- TAPE: `zero unexplained orientation pages`

### 6. A pass is not final until the window is
- LAW: terminal rows are a monotonic lattice PASS < GAP_RESTART < SETTLED;
  upgrades are legal, regressions are the dead FATAL class.
- CODE: `relay_engine/surface.py:43`
- TAPE: `zero terminal regressions; upgrades legal (§1)`

### 7. Cut only what you hold
- LAW: before every cut: tri-state cancel of the resting exit, re-derive
  the position from the ledger, sell exactly what the ledger proves (the
  BATON class is dead).
- CODE: `relay_engine/custodian.py:503`
- TAPE: `zero BATON_VIOLATION FATALs (P14)`

### 8. The needle is the trigger
- LAW: HUNT entries fire only on a measured spot-displacement needle with
  a complete casefile — ΔP, d, fair, gap, convergence (live hunt
  receipts).
- CODE: `relay_engine/spotlead.py:58`
- TAPE: `every HUNT casefile complete: ΔP, d, fair, gap, converge`

### 9. The custodian earned F
- LAW: salvage (needle-collapse maker-first exit, K15/S10, 👑 11:06) is the
  custodian's earned privilege on F/H8; every salvage row carries its
  needle and save-estimate and gets a settlement counterfactual.
- CODE: `relay_engine/custodian.py:298`
- TAPE: `salvage rows complete: needle + est-save (§2)`

### 10. Showing up is mandatory; trading is earned
- LAW: every window gets rows — arrive, arm, evaluate, conclude; a rowless
  window is a contract breach that pages.
- CODE: `relay_engine/shadow_runner.py:885`
- TAPE: `zero silent windows (§6 contract)`

### 11. Failures file their own reports
- LAW: every incident goes through the one funnel with a specific name —
  ambiguous tags are banned; the phone reads the tag, not a guess.
- CODE: `relay_engine/failures.py:82`
- TAPE: `storm pages carry specific wall tags (§4)`

### 12. The logs are always right
- LAW: doctrine is machine-written from the tape (RETRO_2026-07-18.md,
  in-tree) and this registry binds every belief to the tape line that
  would disprove it.
- CODE: `relay_engine/semantics.py:1`
- TAPE: `registry green: every KNOWN names its tape line (B1/B3)`

### 13. The venue nets; our books net
- LAW: an opposite-side BUY on a held market IS a net-down — booked as an
  EXIT of the held side at 100−price (A1), and refused as an ENTRY by the
  wall (A2). Kalshi closed that loophole; the 10:45/11:30 divergence was
  our stale hedge-model, not the broker's error.
- CODE: `relay_engine/fills.py:120`
- TAPE: `WINDOW_ECON_DIVERGENCE silent (A1 netting model)`

### 14. The herd's screen is readable
- LAW: the app shows gamblers the last windows and they bet continuation —
  our own settled windows are that screen (grain = last-K streak); at
  49/49 with the last three down, you buy no (A3).
- CODE: `relay_engine/grain.py:18`
- TAPE: `OPEN whys stamped: grain, join, band (A4)`

### 15. Intent defines the exit
- LAW: HUNT exits fast (its Job-B bails), OPEN holds patient (no stop, no
  scratch, no time-box inside the undetermined band — exits are exactly
  TAKE/DETERMINED/CURFEW), F holds to settlement, salvage exits collapse.
  The −11¢ (10:30) and −12¢ (11:17) round-trips were OPEN-intent positions
  killed by fast-intent stops — the last of their kind (A5).
- CODE: `relay_engine/lane_flip.py:137`
- TAPE: `OPEN exits only TAKE/DETERMINED/CURFEW (A5)`

### 16. The spot-lead law
- LAW: our spot feed is the market's present; the order book is the
  crowd's past — spot leads, the book follows, and the needle equation
  measures the lead in probability points (P18 §0, banked in spotlead.py's
  docstring).
- CODE: `relay_engine/spotlead.py:1`
- TAPE: `zero HUNT entries with ΔP < N (gate A graded)`

### 17. The score decides the size
- LAW: every closed unit of risk writes its cell (lane × 5¢ entry
  bucket — round-trips at exit booking, held positions at settlement,
  opening-lane attributed); the Wilson lower bound against the cell's
  OWN fee-adjusted breakeven earns LEAN/CLEAR (PROBE stays a ruling,
  not a bar — R1/R2 stand); promotion pages with its math; demotion
  applies at the next proposal, no grace (P22 — the graveyard audit's
  answer: measure the asymmetry, never assume it).
- CODE: `relay_engine/scoring.py:1`
- TAPE: `tier changes earned: Wilson math on every page (§4.2)`

### 18. Fees are read, never imagined
- LAW: booked fees come from the venue's OWN records — fill records and
  order responses through ONE parser (the (key, per_contract) table;
  average_fee_paid is per-contract dollars, booked ceil(avg × count));
  EXPECTED_FEE_MULTIPLIER is display/estimate-only (P24 §2 — the
  divergence alarm's meaning restored).
- CODE: `relay_engine/venue.py:481`
- TAPE: `crossfire receipts show the venue's fee (§2)`

### 19. The anchor never goes missing
- LAW: every adopted position carries a salvage anchor — the delta
  table's p at entry, or the ENTRY PRICE itself as the probability
  (WARN-tagged with the organ that missed: spot|strike|close|table);
  shieldless is an impossible class that pages. The anchor's p is also
  the reversal doctrine's entry probability — a placeholder zero can
  never mean "infinite profit" (P24 §1/§3).
- CODE: `relay_engine/fills.py:160`
- TAPE: `anchor misses name their cause (§1.2)`

### 20. Every why is a proof
- LAW: every ENTRY's why contains a computable edge from a named source,
  or the lane passes (Drew, ruling, 0718 night — the order's law #17,
  twentieth in this registry's sequence). HUNT prints its needle
  casefile; F/H8 print tier + table survival vs the price paid (the
  non-reversal proof, salvage at its back); D prints its table verdict;
  P prints its displacement arithmetic; OPEN prints its OWN cell margin
  or explicit PROBE while its cells fill. The gateway rejects anything
  else: REJECT_UNPROVEN_WHY. No lane, present or future, trades on
  vibes — and the brain that powers the proofs is loaded at boot or its
  absence is explained on every line (P26 §1/§2).
- CODE: `relay_engine/gateway.py:515`
- TAPE: `zero UNPROVEN entries: the wall stands (§2)`

### 21. The phone is the console
- LAW: the builder cannot see the live DB — Claude Code writes, GitHub
  carries, Render runs, and TELEGRAM IS THE ONLY WAY ANSWERS COME BACK.
  So every question ships as code (a diagnostics registry entry) that
  runs itself once on the next boot, pages its answer in chunks, and
  closes itself; every fix ships in the SAME deploy behind an env flag
  so acting on the answer is one variable, no second deploy. The daily
  DIAGNOSTICS section answers "why didn't we trade" before it's asked
  (DIAG-1 §1-§4 — the standing pattern, never re-invented).
- CODE: `relay_engine/diagnostics.py:1`
- TAPE: `DIAGNOSTICS section ships in the pack (§3)`

## QUESTION (articulated ignorance — collectors named, running)

- **Lane margins** — which lanes clear fees at what hit-rate? Collector:
  daily packs · window brackets · Wilson bounds (running).
- **The grain hypothesis** — does the herd actually bet continuation, and
  does grain-side entry beat coin-flip? Collector: A3's chart — OPEN whys
  vs settlements (ships now).
- **Needle-size curve** — how does hunt edge scale with ΔP? Collector:
  hunt casefile rows vs outcomes (running).
- **Salvage K** — is K15 the right collapse threshold? Collector:
  DODGED_LOSS vs SALVAGE_REGRET counterfactuals (running).
- **F's clip economics** — does hold-to-settlement clear the fee curve at
  probe size? Collector: live fills, accumulating.
- **Fee drag by book size** — where does the fee tripwire actually bind?
  Collector: window brackets by depth (running).
