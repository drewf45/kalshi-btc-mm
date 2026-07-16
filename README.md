# FLIPDESK (`f_worker`)

> One package, one job, one shot per window, one dollar until the ledger says two.
> Live from the first bell because the floor — not courage — carries the tuition.

A purpose-built desk that trades one KXBTC15M window at a time: build a cheap YES+NO
**bundle** (combined cost ≤ the entry line), try to **flip** both legs a few cents higher,
and if it can't, let the complete bundle ride its guaranteed floor to settlement. One
entry phase per window (the One-Shot Law), 1 lot at a time until the ledger's own math
earns the right to scale.

Run it: `python -m f_worker`. That is the whole box.

---

## A note on the borrow list vs. this repo

The BUILD ORDER's borrow table credits `k_worker/*` and `d_worker/*` modules (a Kalshi
client, `notify`, `feemath`, budget/halt, watchdog, delta tables, envcheck). **Those
modules do not exist in this repository** — the repo was a single, truncated `bot.py`
(no `main()`, not runnable). So "beg-borrow-steal" was honoured against the real proven
code that *did* exist: the RSA-PSS Kalshi client, 15M discovery, orderbook parsing,
spot/sigma fetchers, and the balance reader were lifted from `bot.py` into
`f_worker/lib/`. Everything the borrow list pointed at but that never existed
(Telegram, fee math, delta-table gate, halt/ladder, envcheck, watchdog) was **built** to
the order's spec. Where the doc said "borrow", the code comments say exactly what was
borrowed and from where.

`bot.py` is left untouched (it belongs to the other branch's history); nothing on this
branch imports it.

## Package layout

```
f_worker/
  __main__.py     boot: envcheck (FATAL loud) → lock → ledger init → threads
                  (desk loop, pack scheduler, halt listener) + 3-strike supervisor
  config.py       env config (§8) + fenvcheck (FATAL-loud validation)
  feemath.py      W8: exact per-order roundup fee + entry/leg predicates
  pricebrain.py   spot/vol-regime feed + delta-table gate loader + the D4 window gate
  ledger.py       flipdesk.db — APPEND-ONLY: windows, events, orders, fills, pnl,
                  size_ladder, halt
  fgateway.py     THE ONLY order path — walls W1..W8, one place calls place_order
  window.py       the state machine: IDLE→SEEKING→HOLDING→EXITING→DONE (+SAT_OUT)
  manager.py      flip asks, salvage walk, T-90 flat, knee accounting (window P&L)
  desk.py         the window loop: discover → run one window → book → next
  notify.py       Telegram (🅵 prefix); inbound is ONLY /fhalt + /fstatus
  fpack.py        hourly pack (broker-truth beside ledger-truth) + boot echo
  reconcile.py    F1.1 boot reconcile / orphan sweep (cancel resting, flatten held)
  settlement.py   F1.2 settlement broker-truth (poll result, reconcile, alert)
  feewatch.py     F1.3 fee tripwire (fingerprint the schedule; change -> halt + alert)
  lib/            borrowed-from-bot.py parts:
    kalshi.py       the ONLY module that imports cryptography (signed client)
    marketutil.py   crypto-free: orderbook parse, payload build, close-ts, discovery
    spot.py         Coinbase spot/candles/realized-sigma
tools/
  crossing_study.py   Deliverable 0 — the offline crossing study (a script)
tests/                unittest suite (walls, state machine, feemath, pnl, halt, e2e)
docs/deliverable0/    generated crossing-study outputs (see its README)
```

## The laws as walls (`fgateway.py`)

Each is a hard-coded check on the single order path, and each has a test that tries to
violate it and proves it can't (`tests/test_walls.py`):

| Wall | Rule |
|---|---|
| **W1** | combined bundle cost ≤ `DW_ENTRY_LINE` (99¢) |
| **W2** | a lone leg may only be held if its fill ≤ `DW_SINGLE_LEG_MAX` (49¢) |
| **W3** | 1 lot/side until the ladder says otherwise; lone legs are ALWAYS 1 lot |
| **W4** | flat by T-90: no NEW risk in the flat zone; exits always allowed |
| **W5** | one entry phase per window (a second entry pair is refused) |
| **W6** | live-balance re-read before every order (the Feb-22 law) |
| **W7** | `flip_halt` blocks new risk; exits stay allowed so inventory can flatten |
| **W8** | crossing exits price their exact roundup taker fee before deciding |

A wall fired by a risk-adding request is a **BUG** — it raises `WallViolation` and pages
Drew (§5). The phone can `/fhalt` (halt) and `/fstatus` (ask); it can never reach the
order path (W5).

## Config (env — §8)

`DW_ENTRY_LINE=99` · `DW_SINGLE_LEG_MAX=49` · `DW_FLIP_X=6` · `DW_ENTRY_WINDOW_SEC=60` ·
`DW_FLAT_AT_T=90` · `DW_STOP_CENTS=25` · `DW_PAUSE_AFTER_STOPS=2` · `DW_LOTS=1`
(ladder-owned) · `DW_REQ_PER_MIN=30` · `DW_CLEAR_HALT` (1 at boot clears a standing halt).
FATAL-checked: `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PEM_BASE64`. Soft:
`TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` (absent ⇒ notify is a logged no-op). `DRY_RUN=1`
records intent but never submits.

## Between-windows hardening (Work Order F1)

The walls guard the desk while it trades; F1 guards it while it crashes, restarts,
settles, and learns:

- **F1.1 Boot reconcile / orphan sweep** (`reconcile.py`) — before the desk takes any new
  risk, it cancels every resting order on the series and flattens any held leg the broker
  reports (a crash/deploy orphan) through the manager's normal exit path, paging
  `🅵 🚨 ORPHAN RECOVERED`. The reconcile line reaches the phone before the first window.
- **F1.2 Settlement broker-truth** (`settlement.py`) — a floor ride is reconciled against
  the market's actual result; a void/refund is a mismatch that alerts instead of drifting
  the ledger silently. A `settlements` row is appended beside the model P&L.
- **F1.3 Fee tripwire** (`feewatch.py`) — the series fee schedule is fingerprinted at boot
  and re-checked every 6h; any change trips `flip_halt` + alert. The economics die loudly.
- **F1.5 Real crossing study** — `tools/crossing_study.py --coinbase --days 180` paginates
  Coinbase's 300-candle cap into a multi-day pull with a manifest+validator (coverage %,
  gaps). The pack prints the **lived** 7-day flip rate so tape supersedes synthetic priors.
- **F1.6** — the hourly pack carries rung, halt, day-net beside account, mem, and the lived
  flip rate; every fill records its broker fee + taker flag.

## Loud tape (Drew's law)

Everything fails or tags *why*, loudly — no decision, skip, or error reaches the tape
without its reason + evidence attached:

- A `SAT_OUT` line says **which** kind it was, because they are different findings:
  `SAT_OUT (gate: vol too calm to cross a strike | σ=4.1 low)` (gate refused — tune the
  gate) vs `SAT_OUT (posted 49/48, 0 fills in 60s | touch 51/50)` (bids posted, nobody
  filled — a thesis finding) vs `SAT_OUT (gate: BLIND: spot/sigma feed unavailable)`
  (the feed is down — fail closed, never a crash, never a trade).
- Every desk-loop IO error (discovery, fills poll, book fetch) writes a tagged
  `STAGE_ERR` evidence row and pages a `⚠` line (rate-limited to once per window;
  desk-level stages throttle by time), instead of a silent `except: return`.

## Running the tests

```bash
python -m unittest discover -s tests    # 61 tests, no network / no crypto needed
```

The testable core is deliberately importable without the `cryptography` stack: only
`lib/kalshi.py` imports it, and it is injected everywhere else, so tests use fakes.

## Size ladder (§7)

Boot rung is 1 lot/side. Promotion is reviewed **weekly by Drew**, only if the Wilson
lower bound on net-¢-per-window is > 0 over ≥40 windows at the current rung. Demotion is
instant and automatic on `flip_halt`. Lone legs never scale past 1 lot (W3). The ledger
tracks per-rung window history (`rung_window_history`) for that review.
