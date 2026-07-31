# VENUE SEMANTICS — every parser field, its FORM, and its citation
## Dated 2026-07-18. When Kalshi changes accounting again (they will), the
## diff against this file is the audit. P13 §2c.

## The law (P13 §2a)

**FIELD → FORM is explicit. Form is NEVER inferred from magnitude.** The old
`val < 1` heuristic read a dollars `"1.00"` as 1¢ and survived only by luck;
the migration to `*_dollars`/fp forms is live this week. Enforcement:
`venue._field_to_cents` (the ONE conversion), `DOLLAR_FORM_KEYS` /
`CENT_FORM_KEYS` (the classification), and an unclassified key is never
parsed.

## Fill records (`/portfolio/fills`) — `venue.parse_fill`

| field | form | source |
|---|---|---|
| `yes_price_dollars`, `no_price_dollars` | **dollars** (×100) | migration forms, changelog June-2026; live tape 0718 |
| `yes_price`, `no_price` | **cents** (legacy integers) | legacy API; Engineer KNOB — historically integer-cents, misfiled as dollars pre-P13 |
| `yes_price_cents`, `no_price_cents`, `price_cents` | **cents** | explicit |
| `price` | **cents** (legacy sideless) | legacy API |
| `fee_cost` | **dollars** (fp string, e.g. `'0.000000'`) | live tape 0709 |
| `taker_fee_dollars`, `maker_fee_dollars` | **dollars** | migration forms |
| `fee`, `taker_fee`, `maker_fee` | **cents** (legacy) | legacy API |
| `average_fee_paid` | **dollars, PER CONTRACT** — cents = ceil(avg × count × 100) | order responses (the 21:13:44 cut's entrance), P24 §2.1 |
| `count_fp` | fp count string (`'1.00'`) — Decimal→int | live tape 0709 |
| `count`, `quantity`, `qty` | integer | legacy |

Side-correctness outranks form: our side's keys resolve FIRST; complement
keys only as fallback (audit v13: parse_fill is side-correct — the 6¢ was
narration, not orientation).

## Orderbook (`/markets/{t}/orderbook`) — `fetch_orderbook`/`fetch_orderbook_raw`

| field | form | source |
|---|---|---|
| `orderbook_fp.yes_dollars` / `.no_dollars` | arrays of `[price_dollars_fp, count_fp]`, ASCENDING, best LAST; **bids only, both sides** (reciprocal book) | June-2026 doc (truth audit); live tape 0718 |
| `orderbook.yes` / `.no` | legacy `[cents_int, count_int]` | legacy fallback |

## Market record (`/markets`) — discovery + P13 §3 orientation oracle

| field | form | source |
|---|---|---|
| `yes_bid_dollars`, `yes_ask_dollars`, `no_bid_dollars`, `no_ask_dollars` | **dollars** (×100) | live boundary-keys log 0718 (the full key list is on the tape) |
| `close_time` (ISO) / `close_ts` (epoch) | exchange truth only, never computed locally | venue module law |

## Orders (V2) — `place_order_maker` / `ORDER-V2`

| field | form | source |
|---|---|---|
| order `price` (submit) | **dollars string** (`v2_price_str` subpenny when available) | live orders 0718; true-touch law |
| `fill_count`, `remaining_count` | fp count strings (`'0.00'`, `'1.00'`) | live ORDER-V2 resp 0718 (verbatim in fixtures) |
| V2 side semantics | quotes the YES leg only: buy NO == ask YES at (100−cost) | truth audit; flip_math.exit_args |

## Fixtures

`tests/test_p13_narration.py` carries these payloads VERBATIM (live-tape
observed + doc-cited) — the tests fail the moment the venue's published
examples stop parsing.
