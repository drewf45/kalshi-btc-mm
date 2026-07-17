# kalshi-btc-mm — RELAY ENGINE (parallel paper-shadow build)

**MODE: PARALLEL BUILD — PAPER SHADOW — ZERO CAPITAL.**

This tree is the NEW relay engine authorized by work order **WO-2026-07-17-RELAY**
(`docs/WORK_ORDER.md`, canonical). It is NOT the live trading engine:

- The **live Kal** (k_worker tree, AtDcG lineage) runs elsewhere, on Render, UNTOUCHED.
  It keeps trading until cutover is ruled by Drew.
- **This tree trades nothing.** It runs as a paper shadow against the live Kalshi feed with
  zero capital. `RUN_MODE=SHADOW` is the default and the live-submit path is hard-disabled
  behind the `I_UNDERSTAND_LIVE` pattern. Cutover happens only after the relay's F/H8
  decisions match live Kal for 5 clean days AND Drew rules the switch (§A1).
- **Single-writer law:** this engine writes its OWN database (`relay_shadow.db` by default).
  It never writes the live surface DB.
- **Born EPOCH 2** (§A4): tradeable = live balance. No waterfall-era accounting code exists
  in this tree, by construction; a grep-proof test enforces it (`tests/test_epoch2_grep.py`).

## Layout

- `docs/` — canonical documents (work order, build sequence, charter, lens read) + verify reports
- `relay_engine/` — the engine: `config` `boot` `feed` `book` `delta` `lanes` `gateway`
  `custodian` `sizing` `ledger` `surface` `ops` `shadow_runner`
- `reference/legacy_dump_bot.py` — QUARANTINED parts shelf (the old truncated monolith).
  Never imported, never run. DUMP exit *mechanics* were the Chunk 4 borrow source; its
  parameters were not borrowed (§F: mechanics yes, parameters/edge no).
- `tests/` — verify-gate tests (boot tape, cash protocol, degrade ladder, walls, attribution)

## Running the shadow

```
python -m relay_engine.shadow_runner
```

Requires read-only Kalshi API usage (§B3). Places no orders. Prints the boot tape
(EPOCH 2 header, ratification assumptions, DREW-DEFAULT constants, SHADOW mode,
recorder confirmation) on every boot.

## What is gated

- **F and H8 lane ports** are GATED ON B1 (the live k_worker zip from Drew). They are
  registered as lanes but evaluate to first-class PASS rows tagged `GATED_ON_B1`. No F/H8
  logic is ported from memory or from the monolith (§A5).
- **Chunk 2 analysis** is GATED ON B2 (legacy CSVs). `docs/analysis/` stays empty until then.
- **Chunks 5–7** (demo verification, shadow-lane promotion, cutover) are separate orders cut
  at Drew's word. HARD STOP after verify gate 7.
