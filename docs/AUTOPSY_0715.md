# AUTOPSY — window KXBTC15M-26JUL180715-15 (the first 6¢ of live tuition)

## Evidence status (read-rule, stated first)

The autopsy tool is committed (`python -m scripts.autopsy_fills <db> 0715-15`)
but its output CANNOT be produced from this checkout: the fills rows live in
the deployed worker's database, and that worker booted with
`DB: relay_shadow.db` — **RELAY_DB_PATH was unset, the DB is EPHEMERAL**. If
the service has redeployed since the window, the rows are gone with the
container. Run the tool on the deployed box NOW (before the next deploy) and
paste its output below; if it prints the evidence-gap line instead, that gap
stands recorded here — an honest gap beats a false conviction.

**Standing fix shipped with P13:** the engine now PAGES at boot when LIVE
runs with RELAY_DB_PATH unset. Set `RELAY_DB_PATH=/var/data/relay_shadow.db`
(persistent disk) so no future window's evidence is disposable.

## The reconstruction (from the live log + the P13 verdict)

- 10:59:44 boot — LIVE, baselined at $5.50 from venue truth.
- 11:00:44 `✅ ENTRY` (then-mute page: "✅ FILL"): FLIP **buy no@46¢** on
  KXBTC15M-26JUL180715-15 — pair-post ≤49, the flipdesk opening discipline.
- 11:00:49 fill booked; take posted: **sell no@50¢** (entry+4, FLIP_X).
- ~11:01 mark fell ≤43 (entry−3 = FLIP_SCRATCH_S) → custodian scratch →
  crossfire **sell filled @42¢**.
- Round trip: (42 − 46) × 1 = **−4¢**, taker fee ≈ **2¢** → **net −6¢, flat**.

**ORIENTATION CORRECT. DOCTRINE CORRECT. NARRATION BROKEN** — the scratch
paged as `✅ FILL no@42`, indistinguishable from a second buy. Under P13 §1
the same event now pages:

```
✂️ EXIT FLIP KXBTC15M-26JUL180715-15 sell no@42¢ x1 (fee 2¢) — PER_CONTRACT_STOP
↔ KXBTC15M-26JUL180715-15 FLIP round-trip -4¢ + fee 2¢ = -6¢
```

The −6¢ was a correctly-bounded designed loss (the scratch rule executing
exactly as banked yesterday). The panic it caused was a reporting failure,
not a risk failure. P13 §1 closes the ops-parity clause.

## Deployed-DB output (paste here when run)

_(pending — run `python -m scripts.autopsy_fills /var/data/relay_shadow.db
0715-15` on the worker; expected: the reconstruction above. If it differs,
THAT is the finding.)_
