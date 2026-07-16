# FLIPDESK — Deliverable 0: Crossing Study

Source: **synthetic:3000m** · Windows analyzed: **200**

- Avg strike crossings / window: **1.81**
- Avg re-crosses / window: **1.11**

## Both-legs-flip rate by target X (all windows)

| X (¢/side) | both-legs-flip rate |
|---|---|
| 4 | 55.5% |
| 5 | 53.0% |
| 6 | 49.5% |
| 7 | 48.0% |
| 8 | 44.0% |

## Both-legs-flip @6¢ by vol regime

| regime | flip@6 |
|---|---|
| low | 25.0% |
| mid | 50.0% |
| high | 0.0% |

## Recommended D4 gate thresholds

```json
{
  "sigma_low": 6.0,
  "sigma_high": 18.0,
  "sigma_floor": 7.24,
  "sigma_ceil": 10.61,
  "max_build_cost": 99,
  "min_flip_headroom": 2,
  "max_leg_spread": 6,
  "flip_rate": {
    "low": 0.25,
    "mid": 0.5,
    "high": 0.0
  }
}
```

### Reading

- A window flips both legs only when spot swings past the strike by the
  cents→USD-equivalent of X on **both** sides. Low-vol windows rarely do;
  they are exactly the SAT_OUT windows the gate should refuse.
- `sigma_floor`/`sigma_ceil` are the 10th/95th percentiles of realized
  window sigma — below the floor is too calm to cross, above the ceil is
  coin-flip territory we won't feed at rung 1.
- These seed pricebrain via `flipdesk_gate.json`. The tape retunes them;
  they are boot defaults, not law (the LAWS are the walls).
