# GOLDEN-TAPE REGRESSION REPORT — F / H8 port (gate 5)

Live side: `reference/live_k_worker/{engine,gateway,delta_table_loader}.py`
(vendored byte-identical from B1, I/O stubbed, clock faked).
New side: `relay_engine/lane_fh8.py` + `relay_engine/delta.py`.
Comparison key per frame: (passed, lane, side, cost_cents, cost_exact,
reject_code, why_tag). Free-text reject_reason excluded (declared change D1).

## Result: GREEN

- Watch-ladder tapes: 26 scenarios, 26 identical
- Final-window evaluate sweep: 2941 cases, 2941 identical
- Mismatches: 0

## Watch-ladder tape outcomes

| tape | live verdict | port verdict | identical |
|---|---|---|---|
| tier0_clean_pass | `(True, 'F', 'yes', 99, 99.0, None, 'FAV_99.0c_T-855_confirms_9/9')` | `(True, 'F', 'yes', 99, 99.0, None, 'FAV_99.0c_T-855_confirms_9/9')` | ✓ |
| tier0_floor_dropout_tier1_pass | `(True, 'F', 'yes', 98, 98.0, None, 'FAV_98.0c_T-570_confirms_6/6')` | `(True, 'F', 'yes', 98, 98.0, None, 'FAV_98.0c_T-570_confirms_6/6')` | ✓ |
| tier2_pass_97 | `(True, 'F', 'yes', 97, 97.0, None, 'FAV_97.0c_T-285_confirms_3/3')` | `(True, 'F', 'yes', 97, 97.0, None, 'FAV_97.0c_T-285_confirms_3/3')` | ✓ |
| below_band_never | Pass(no entry) | Pass(no entry) | ✓ |
| side_flip_reset | `(True, 'F', 'yes', 99, 99.0, None, 'FAV_99.0c_T-820_confirms_9/9')` | `(True, 'F', 'yes', 99, 99.0, None, 'FAV_99.0c_T-820_confirms_9/9')` | ✓ |
| r2_counterparty_vanish | `(True, 'F', 'yes', 99, 99.0, None, 'FAV_99.0c_T-800_confirms_9/9')` | `(True, 'F', 'yes', 99, 99.0, None, 'FAV_99.0c_T-800_confirms_9/9')` | ✓ |
| book_vanish | `(True, 'F', 'yes', 99, 99.0, None, 'FAV_99.0c_T-810_confirms_9/9')` | `(True, 'F', 'yes', 99, 99.0, None, 'FAV_99.0c_T-810_confirms_9/9')` | ✓ |
| fp_string_subpenny | Pass(no entry) | Pass(no entry) | ✓ |
| no_side_pass | `(True, 'F', 'no', 99, 99.0, None, 'FAV_99.0c_T-855_confirms_9/9')` | `(True, 'F', 'no', 99, 99.0, None, 'FAV_99.0c_T-855_confirms_9/9')` | ✓ |
| floor_flicker | `(True, 'F', 'yes', 99, 99.0, None, 'FAV_99.0c_T-790_confirms_9/9')` | `(True, 'F', 'yes', 99, 99.0, None, 'FAV_99.0c_T-790_confirms_9/9')` | ✓ |
| toprung_guard_fail | `(True, 'F', 'yes', 99, 99.0, None, 'FAV_99.0c_T-570_confirms_6/6')` | `(True, 'F', 'yes', 99, 99.0, None, 'FAV_99.0c_T-570_confirms_6/6')` | ✓ |
| toprung_lowtail_tag | `(True, 'F', 'yes', 99, 99.0, None, 'FAV_99.0c_T-855_LOWTAIL_confirms_9/9')` | `(True, 'F', 'yes', 99, 99.0, None, 'FAV_99.0c_T-855_LOWTAIL_confirms_9/9')` | ✓ |
| hourly_cap_blocks | Pass(no entry) | `(True, 'F', 'yes', 99, 99.0, None, 'FAV_99.0c_T-855_confirms_9/9')` | ✓ |
| lane_f_killed | Pass(no entry) | `(True, 'F', 'yes', 99, 99.0, None, 'FAV_99.0c_T-855_confirms_9/9')` | ✓ |
| single_entry_blocks | Pass(no entry) | Pass(no entry) | ✓ |
| cross_lane_cap_blocks | Pass(no entry) | Pass(no entry) | ✓ |
| tier_transition | `(True, 'F', 'yes', 99, 99.0, None, 'FAV_99.0c_T-855_confirms_9/9')` | `(True, 'F', 'yes', 99, 99.0, None, 'FAV_99.0c_T-855_confirms_9/9')` | ✓ |
| low_balance | Pass(no entry) | Pass(no entry) | ✓ |
| late_join_tier1 | `(True, 'F', 'yes', 98, 98.0, None, 'FAV_98.0c_T-375_confirms_6/6')` | `(True, 'F', 'yes', 98, 98.0, None, 'FAV_98.0c_T-375_confirms_6/6')` | ✓ |
| cost_100_oob | Pass(no entry) | Pass(no entry) | ✓ |
| floor_edge_96_tier2 | Pass(no entry) | Pass(no entry) | ✓ |
| floor_edge_95_all_tiers | Pass(no entry) | Pass(no entry) | ✓ |
| floor_edge_97_tier1 | `(True, 'F', 'yes', 97, 97.0, None, 'FAV_97.0c_T-285_confirms_3/3')` | `(True, 'F', 'yes', 97, 97.0, None, 'FAV_97.0c_T-285_confirms_3/3')` | ✓ |
| floor_edge_98_tier0 | `(True, 'F', 'yes', 98, 98.0, None, 'FAV_98.0c_T-570_confirms_6/6')` | `(True, 'F', 'yes', 98, 98.0, None, 'FAV_98.0c_T-570_confirms_6/6')` | ✓ |
| confirm_edge_tier2 | `(True, 'F', 'yes', 97, 97.0, None, 'FAV_97.0c_T-275_confirms_3/3')` | `(True, 'F', 'yes', 97, 97.0, None, 'FAV_97.0c_T-275_confirms_3/3')` | ✓ |
| confirm_edge_tier1 | `(True, 'F', 'yes', 98, 98.0, None, 'FAV_98.0c_T-545_confirms_6/6')` | `(True, 'F', 'yes', 98, 98.0, None, 'FAV_98.0c_T-545_confirms_6/6')` | ✓ |

## Evaluate sweep

2941 grid cases (books × times × spot/boundary × evidence-state × table-state). All compared on the full key.

No mismatches. Ladder + gates are decision-identical on this corpus.

