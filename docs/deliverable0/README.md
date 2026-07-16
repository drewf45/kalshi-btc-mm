# Deliverable 0 — Crossing Study (outputs)

These files are the crossing-study output shipped alongside the PR (BUILD ORDER §6).

**The checked-in `crossing_study_summary.md` / `.csv` / `flipdesk_gate.json` were
generated from SYNTHETIC candle data** (`--synthetic`, a deterministic walk) so the
deliverable renders without market data in CI. The header of the summary states its
source. Re-run against real candle data before trusting the numbers or seeding the gate:

```bash
# real 1-minute BTC candles from a CSV (columns: time, open, high, low, close)
python tools/crossing_study.py --csv path/to/btc_1m.csv --out docs/deliverable0

# or pull recent candles live from Coinbase
python tools/crossing_study.py --coinbase --out docs/deliverable0
```

The generated `flipdesk_gate.json` is what pricebrain loads at boot to seed the D4 gate
(place it in the working directory of the Render service, or point `PriceBrain(gate_path=...)`
at it). Absent the file, pricebrain uses the baked-in boot defaults.
