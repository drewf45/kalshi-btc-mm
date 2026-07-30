"""Engine configuration. All capital-relevant numbers are NAMED CONSTANTS here.

Rulings A3/0.3 defaults carry a `# DREW-DEFAULT` tag and are printed by the boot
tape every boot until Drew rules them. Do not scatter magic numbers elsewhere.
"""

import os

# ---------------------------------------------------------------------------
# Run mode. SHADOW is the born state of this tree (§A1/§B3). The live-submit
# path exists in the gateway but is hard-disabled unless BOTH conditions hold,
# and Chunks 5-7 (which would flip them) are separate orders at Drew's word.
# ---------------------------------------------------------------------------
RUN_MODE = os.getenv("RUN_MODE", "SHADOW").upper()
I_UNDERSTAND_LIVE_PHRASE = "I_UNDERSTAND_LIVE_ORDERS_WILL_BE_PLACED"
I_UNDERSTAND_LIVE = os.getenv("I_UNDERSTAND_LIVE", "")

EPOCH = 2  # Born EPOCH 2 (§A4). Tradeable = live balance. There is no epoch 1 in this tree.

# ---------------------------------------------------------------------------
# Ratification assumptions (A3 / Chunk 0.2): ASSUMED, printed every boot
# until Drew rules otherwise.
# ---------------------------------------------------------------------------
RATIFICATIONS_ASSUMED = (
    "Loss-Asymmetry",
    "transport-walls",
    "streams-name-readers",
    "P22-custodies-new-lanes-only",
    "fast-selection/slow-calibration",
)

# ---------------------------------------------------------------------------
# RULINGS — Drew's, recorded with scope and sunset (printed every boot).
# ---------------------------------------------------------------------------
RULINGS = (
    "A3 PROVEN GROUND (Drew, 2026-07-18): the go-live transport is REST 1s at "
    "one-lot / <=3 net per event — yesterday's proven live surface. The WS "
    "dialect is SHELVED with its lessons and fixes; it returns as an upgrade, "
    "re-certified separately, and never again gates a go-live.",
)

# ---------------------------------------------------------------------------
# P11 PROVEN GROUND — transport selection + the REST feed's enforced numbers.
# WS_ENABLED=false (the default, per A3) runs the REST 1s loop; the ws path
# is import-inert under this flag.
# ---------------------------------------------------------------------------
WS_ENABLED = os.environ.get("WS_ENABLED", "false").strip().lower() in ("1", "true", "yes")
REST_BUCKET_CAPACITY = 10        # P11.1-b: token bucket around ALL REST calls
REST_REFILL_PER_SECOND = 8.0     # books 4/s + spot 0.7/s + fills 0.3/s + headroom
REST_BOOK_STALE_CUSTODY_S = 30.0  # P11.1-d: custody's grace on a failed-fetch book
FILLS_SWEEP_S = 3.0              # the proven live fills cadence (accepted 3s lag)

# ---------------------------------------------------------------------------
# P18 "THE DETECTIVE" — the hunt's DREW-DEFAULT knobs (§2/§3). The trigger is
# EQUATIONAL: needle points from the delta table, gap in cents from the book;
# hope is nowhere in it.
# ---------------------------------------------------------------------------
HUNT_NEEDLE_POINTS = 5.0   # gate A: ΔP >= N points or nothing happened
HUNT_GAP_CENTS = 4.0       # gate B: fair − cost >= G — the lag, in cents
HUNT_CONFIRM_FRAMES = 2    # gate C: sustained-confirm (lane_p's pattern)
HUNT_TAKE_CENTS = 4        # JOB A: resting take at entry + T
HUNT_BAIL_R_S = 20.0       # JOB B: not out within R of breakeven trigger -> flatten
HUNT_TIMEBOX_M_S = 60.0    # TIME-BOX: the lag pays in a minute or it never was
HUNT_BAND = (5, 95)        # no side-max, no price cap — the whole book

# ---------------------------------------------------------------------------
# WO-2026-07-24-J "POINT THE HUNTER FORWARD" — the forward (settle) gate. The
# hunt no longer buys the TOUCH lag; it buys the SETTLE edge. Gate B is now
#   edge = wilson_LB(p_end(d,t)) * 100 − join_cost   (cents of expected value)
# and entry requires edge >= HUNT_EDGE_MIN_C. The needle (touch) demotes to an
# ATTENTION signal (gate A: it decides WHEN the hunt looks, never WHETHER it
# buys). DREW-DEFAULT 6c: floored at the taker fee-band cost (~4c round-trip on
# a PROBE clip) + 2c of headroom — re-derive from the fill ledger, don't guess.
# ---------------------------------------------------------------------------
HUNT_EDGE_MIN_C = 6.0      # gate B: settle-edge (Wilson-LB EV) floor, in cents
HUNT_EDGE_GONE_POLLS = 3   # P3 thesis-exit: settle-edge <= 0 sustained N polls

# ---------------------------------------------------------------------------
# P19 "SALVAGE" — the custodian earns Lane F. Needle-collapse exits for the
# hold-to-settlement lanes; K is tuned from the DODGED_LOSS vs SALVAGE_REGRET
# curve on Saturdays, never from a bad night.
# ---------------------------------------------------------------------------
SALVAGE_K_POINTS = 15.0    # needle collapse: p_held − p_entry <= −K, 2 ticks
SALVAGE_S_CENTS = 10.0     # AND fair_held < entry − S
SALVAGE_R_S = 10.0         # maker attempt unfilled for R -> crossfire at best
SALVAGE_T_FLOOR_S = 15.0   # never salvage inside the floor (endgame is F's)
# WO-BOTH-LANES-MARKET-TRUE (build 50) — F's PRICE salvage, the catastrophic-
# tail bound. The needle-collapse salvage above needs a spot + a table cell; a
# TABLE-BLIND favorite that slips hard has no proof and rides to the −90
# catastrophic backstop (the −$2.77 3-lot dump). This is the raw, table-free
# floor: a favorite that slips >= this many points from its entry has lost the
# high confidence that bought it (a 95c favorite never slips 40pts on noise —
# that is a decisive reversal), so recover ~−40 instead of riding to −90. One
# attempt per position; NO re-entry after (the last minute is a coin-flip).
F_SALVAGE_SLIP_POINTS = 40  # DREW-DEFAULT: price slip from entry that rules the salvage-cut
# SALV-1 §2.3 (Adversary): flap guard — logged gag TRANSITIONS per position
# per window cap here; the settlement summary still counts every tick.
SALVAGE_GAG_MAX_TRANSITIONS = 12
# ---------------------------------------------------------------------------
# WO-2026-07-26-N "THE OVERNIGHT DOCTRINE" P4.2 — SALVAGE GAGGED. The 07/25→26
# overnight ran untuned salvage (level-only, no confirms) and it fired TWICE at
# maximum pain — SALVAGE_SLIP at 50¢ and 52¢, both on windows that later SETTLED
# AS WINNERS: −$14.30 realized against $0 dodged, 0-for-2. Drew's ruling: GAG
# salvage to telemetry-only (log the would-have-fired counterfactual, take NO
# cut, ride to the bell) until it re-earns its cut the same way entries earn
# their fills. Hold-to-settlement's cost is F's known ~3.8% loss rate, which it
# has always outearned. The gags + would-have-fired shadow rows buy the tuning
# data; the -M re-arm carries the Gate A unlock and gets a named review (first
# weekly pack with ≥ SALVAGE_REARM_REVIEW_N gag summaries).
# WO-2026-07-26-M "SALVAGE EARNS ITS CUT" (S1–S7) — salvage goes LIVE, but only
# as the SIGHTED, CONFIRMED, FLOORED discipline. The -N gag ends with this deploy;
# SALVAGE_GAGGED now defaults OFF (the flag survives only as a manual kill).
# Every cut path (SLIP + K-collapse + CATASTROPHIC) is folded under one discipline;
# catastrophe survives ONLY for broker-truth emergencies.
SALVAGE_GAGGED = os.environ.get("SALVAGE_GAGGED", "false").strip().lower() in ("1", "true", "yes")
SALVAGE_REARM_REVIEW_N = 50   # gag summaries banked before a weekly review of the fires
# S1 — CONFIRMATION: a salvage fires only after N consecutive polls where ALL of
# {deep-against (mark ≤ entry − slip), pinned-at-lows (mark ≤ low_mark + ε — not
# already recovering), spot-confirm (spot on the losing side, when spot is seen)}
# hold. A single-tick dip that recovers never fires (the exact overnight shape).
SALVAGE_CONFIRM_POLLS = 3     # S1: sighted-confirm polls (deep + pinned + spot)
SALVAGE_PINNED_EPS_C = 2      # S1: mark within this of the low-water = still pinned
# S2 — WORTH-FLOOR: surrender must be worth something. Cut only when the residual
# recovered (the mark we sell at) clears this floor; below it, riding costs no
# more than the fee we'd pay to salvage.
SALVAGE_WORTH_FLOOR_C = 30    # S2: min residual cents recovered, or don't bother (was 8)
# S3 — RARITY: salvage is exceptional. More than this many fires in a rolling day
# AUTO-GAGS the machine (SALVAGE_OVERACTIVE) and pages — a hot salvage is mis-tuned.
SALVAGE_RARITY_MAX_PER_DAY = 6
# S6 — MIDDLE-BAND: salvage recovers value only in the band where surrender is
# worth something. Outside [floor, entry−slip] it does not fire; a window that MET
# the confirms but concluded a big loss without firing pages SALVAGE_MISSED_WINDOW.
SALVAGE_BAND_MAX_C = 90       # S6: above this mark there is no decisive reversal to salvage
SALVAGE_MISSED_LOSS_C = 40    # S6: a conclusion worse than this that met confirms = a missed window

# ---------------------------------------------------------------------------
# P21 "THE DOCTRINE ENGINE" — Lane OPEN (A4) + the grain (A3) + patient holds
# (A5). PAIR is retired: the venue nets one account's sides; the 49/49 penny
# is real but one-sided now — which side is the herd's, and waiting IS the
# setup.
# ---------------------------------------------------------------------------
GRAIN_K = 4                       # last-K window outcomes feed the streak
# WO-FLIP-CHEAP-LIVE §2.1 (DREW-RULED 2026-07-19, was (44,56)): the band
# widens DOWN — the edge is the cheap side, admitted to ~39c. Upper bound
# kept per the ruling (a tight-spread 39/59 book stays refused by the
# 56c complement bound; only wide/uncertain books admit the 39c side).
OPEN_BAND = (39, 56)              # legacy setup band (retired as an entry gate build 49; kept for reference)
OPEN_MIN_GRAIN = 2                # streak >= 2 or pass (OPEN_NO_GRAIN)
# WO-FLIP-EVERY-MARKET-LIQUIDITY (build 49, DREW-DEFAULT): FLIP is the market's
# LIQUIDITY PROVIDER — it buys the cheap side of EVERY biased open and sells it
# back to the forced hedgers at the MIDDLE. The old "both sides in OPEN_BAND"
# gate rejected biased opens (the expensive side out of band) — backwards, it
# waited for a balanced book. Now the entry filter is only the CHEAP side being
# BUYABLE: OPEN_ENTRY_FLOOR <= cheap_bid <= OPEN_MAX_ENTRY_CENTS, plus the
# true-50/50 skip and the HUNT/needle trend-guard. (The cheap side is always
# <= 50 by book coherence, so the max just admits the near-coinflip cheap side.)
OPEN_ENTRY_FLOOR = 25            # DREW-DEFAULT: cheap-side lower bound — buy cheap, not near-worthless/decided
# WO-FULL-COLD-AUDIT Finding 4 (build 52): tightened 50 → 42. A ~50c "cheap"
# side is a coinflip with only a fee-floored +5 gouge to the 52 middle — a
# structurally-unclearable entry (the tape's losers). The profitable entries
# were the CHEAP ones (28-36c → +16/+24 to the middle). The band top now admits
# only real-gouge entries (42c → +10 to the middle).
OPEN_MAX_ENTRY_CENTS = 42         # DREW-DEFAULT (build 52, was 50): real-gouge entries only
# The take rests toward the MIDDLE, scaled by entry depth: take = clamp(
# MIDDLE_TARGET, entry+MIN, 99). Buy 39 -> rest 52 (+13, a dime); buy 49 ->
# rest 54 (+5, the fee-safe floor). The cheaper the entry, the bigger the
# gouge — the middle is where the hedgers are forced to transact.
OPEN_MIDDLE_TARGET = 52          # DREW-DEFAULT: the take target — the 50/50 middle, one tick above
# WO-2026-07-22-E — FLIP THE SIDE. The design-error correction (7-for-7 on the
# live tape): market making acquires inventory on the side that has DEMAND and
# sells it INTO that demand. FLIP was doing the inverse — buying the ABANDONED
# cheap side and resting a take on the side nobody wants (that is why
# flip_fill=38%). FLIP now buys the FAVORED (higher-priced) side and sells the
# +20 into the pile-in of buyers. 50 is a HARD floor — never buy below fair
# value, that IS the old bug. trend_usd/depth_ratio are LOGGED, never gated
# (one change, maximally attributable; the confirms earn their gate from data).
# WO-2026-07-22-G §2.1: the band is now the DELIBERATE 55-64. On a coherent book
# skew ≡ 2·join − 99 (exact on 8/8 tape books), so the retired skew-level gate
# (∈[10,30]) and the price band were ONE gate — the effective range was 55-64,
# and 50-54 / 65-70 were unreachable. Made explicit here (never below 55 = never
# below fair value + a real pile; never above 64 = never late), and the redundant
# skew-LEVEL gate is retired (skew GROWTH stays — that is the pile signal).
OPEN_ENTRY_MIN_C = 55            # DREW-DEFAULT (WO-...-G, was 50): the deliberate favored floor
OPEN_ENTRY_MAX_C = 64            # DREW-DEFAULT (WO-...-G, was 70): above this the move is priced
OPEN_ENTRY_DEADLINE_S = 90       # DREW-DEFAULT: unfilled by here → cancel, never chase (== opening window)
OPEN_GOUGE_C = 4                # DREW-RULED (WO-2026-07-24-C "+4×10", was 10): target = entry + 4, cap 90 — the ask is fees + a cent. 4c in a 15-min binary needs ONE move; 10c needs the move AND volume that far out. Break-even ≈74% must reach +4 (MFE says 85% touch); the read is touched-vs-filled. REVERT: conversion <75% OR ≤4 of 10 fill → back to +10
OPEN_MOMENTUM_STOP_C = 10        # DREW-DEFAULT: stop = entry − 10, NO hold, 2-poll sustain, maker-first
# WO-2026-07-23-B Part 2: the uncovered-leg FLATTEN used to sell at whatever the
# book showed (unbounded mark) — 15c through the stop on the night's worst leg.
# The flatten now floors at entry − stop − slip and rides ONE poll at the floor
# before it will cross below it; a cross below the floor is a counted breach.
SLIP_TOLERANCE_C = 3             # DREW-DEFAULT: flatten may cross at most stop+slip below entry
# WO-2026-07-24-E "THE SIGHTED STOP": the momentum stop is a pure LEVEL test
# (mark <= stop_px) with no trajectory term — it fires identically whether the
# book is falling to the stop or CLIMBING back through it (26JUL0845: sold at 48
# into a book that had recovered ~28pts off its low). WO-2026-07-24-G Part 3
# (DREW's cadence re-ruling — ships on proof, any day) makes the deferral LIVE:
# an adverse LEVEL is necessary-but-not-sufficient — a book measurably recovering
# toward entry is the reversion thesis WORKING, so the stop DEFERS. Guardrails
# G1 (hard floor cuts regardless), G2 (grace budget), G3 (recovery off low_mark).
# Revert lever: OPEN_SIGHTED_STOP=0 → back to the pure-level stop (shadow only).
OPEN_RECOVERY_MIN_C = 6          # DREW-DEFAULT: mark this far off the low = recovering
OPEN_RECOVERY_MAX_POLLS = 20     # DREW-DEFAULT: grace budget — deferrals before the stop rules anyway
OPEN_GRACE_HARD_C = 8            # DREW-DEFAULT: below stop−SLIP−this, cut regardless (G1 bounded worst case)
OPEN_SIGHTED_STOP = os.environ.get("OPEN_SIGHTED_STOP", "1") == "1"  # DREW-RULED live; =0 reverts to pure-level
# WO-2026-07-22-F "WAIT FOR THE PILE": entry-discipline tuning. Every logged
# entry so far fired inside the first 57s — before the pile window even opens,
# on a book that had not moved (trend $0) or already finished (skew 41). FLIP
# now WAITS for the pile: it enters ONLY in [PILE_START, PILE_END]s AND only
# when ALL conditions agree — the favored side in-band, the skew inside a
# forming range, the skew GROWING (the stampede in progress, the heart of the
# order), the tape actually moving, and trend agreeing with the favored side.
# No pile = no trade; a skipped window is a correct outcome, logged as OPEN_SKIP.
# These are PROBE thresholds shaped by n=3 — the skip log is what corrects them.
OPEN_PILE_START_S = 60          # DREW: the pile window opens at ~1 minute
OPEN_PILE_END_S = 180          # DREW: closes at ~3 minutes — after this the move is priced
OPEN_MIN_TREND_USD = 15        # no entry on a flat tape (two of three losses had trend $0)
# WO-2026-07-22-G §2.1: the skew LEVEL gate (OPEN_MIN_SKEW_C/OPEN_MAX_SKEW_C) is
# RETIRED — it was the price band in disguise (skew ≡ 2·join−99). Only the skew
# GROWTH survives: the pile is a skew that GROWS in-window, not a static level.
OPEN_SKEW_GROWTH_C = 5         # the skew must have GROWN this much across the pile window (forming NOW, not static)
# B3 the active late-window walk-down: a position that does not fill at the
# middle is walked DOWN toward scratch as the clock runs (from OPEN_WALK_START_S
# down to OPEN_FLAT_BY = T-10), re-posting the maker take lower, so it exits
# gracefully near scratch late and is NEVER caught holding into a catastrophic
# bell dump. Late-window only — the early hold is protected by the build-48
# catastrophe-illiquidity guards; this is not a reactive early cut.
OPEN_WALK_START_S = 780          # DREW-DEFAULT: secs-remaining where the take-walk begins (full middle above here, stepping to scratch by T-10)
# P-FLIP-THESIS-1 §3 (DREW-RULED 2026-07-19, was 5): the scalp target is
# the ~20c swing, not a 5c nibble — the nibble couldn't outrun fees+tails
# (2c taker fee ate 40% of the old edge). A delta-table-derived target
# (spot's reachable move in time left) is routed to measurement before it
# replaces the constant.
OPEN_TAKE_CENTS = 20
# WO-FLIP-GOAL-TAKE (build 42, DREW-RULED 2026-07-20): the FLIP resting take
# is no longer the fixed +20 swing (201430: yes@44 rode to the 20c floor,
# -27c — the +20 take was priced to a rare event, so the position never took
# and lived long enough to ride down). The take floats to the reliable
# convergence move, bounded to Drew's per-book goal:
#   goal_per_contract = ceil(WINDOW_BOOK_GOAL_CENTS / booked-held)
#   take_cents        = clamp(goal_per_contract, OPEN_TAKE_MIN, OPEN_TAKE_MAX)
# At the 1-lot cap this is entry+5 (bank the nickel); as FLIP sizes up the
# per-contract take shrinks toward MIN and volume carries the goal (CAPSTONE
# Part B: 4 contracts x 5c = 20c clears the same book goal as one +20). MIN
# clears the ~2c round-trip taker fee with margin (5c nets ~+3c). OPEN_TAKE_CENTS
# stays the entry-geometry reward ceiling and cell-scoreboard notional (§6:
# TAKE-only; the cut and entry gate are unchanged).
WINDOW_BOOK_GOAL_CENTS = 5          # DREW-RULED: per-window book profit target (bank the nickel now)
OPEN_TAKE_MIN = 5                   # DREW-DEFAULT: reachable floor, fee-safe (>2c, nets ~+3c)
OPEN_TAKE_MAX = 20                  # DREW-DEFAULT: ceiling = today's fixed take
# WO-MAKER-REST-BACK (build 48): a maker BUY entry is re-priced at placement to
# rest PASSIVELY at the live book (at/inside the held-side bid, strictly below
# the derived ask) — never a post_only order at a crossing price. FLIP rests
# this cushion BELOW the cheap bid (its liquidity doctrine: sit under the
# pile-in and get hit as it falls); F just joins the bid. A rest-back that
# would breach the lane band SKIPS the window (a maker who can't rest in-band
# waits, never chases). Deliberate taker crossing stays CUT-only.
FLIP_REST_BACK_CENTS = 2           # DREW-DEFAULT: FLIP rests this far below the cheap bid
# WO-FLIP-LIQUIDITY-HOLD (build 45, DREW-RULED 2026-07-20): the FLIP lane is
# reconceived as LIQUIDITY PROVISION, not scalp-or-cut. A low price after a
# FLIP buy is ILLIQUIDITY (the opening pile-in, no buyers on your side yet),
# NOT a loss — the job is to buy the inventory the panic is dumping, POST the
# take (entry+5, already resting), and HOLD as the liquidity provider until
# the reversion lifts it. So build-43's SCALP regime is RETIRED on the loss
# side: OPEN_SCALP_STOP_CENTS (the tight entry−6 stop) is gone, and with it
# the risk/reward geometry gate it grounded — the reactive stop sold inventory
# during the exact early illiquidity the model must hold through. What REMAINS
# is the collapse backstop (Adversary-mandated): a CONFIRMED collapse (spot
# decided sustained, or the catastrophe floor 20) still cuts even in the
# passive hold — a real move, not illiquidity noise. The endgame T-10 handoff
# is the primary loss-side exit for an unreverted position. The reversion /
# resting-take fill rate is UNPROVEN and is the empirical gate (Instrument 1)
# before any size increase — run at the 1-lot cap. HARD RAIL: no
# Kelly/cash/rate-halt/F change; F-covers-FLIP (martingale-adjacent) NOT built.
OPEN_UNDETERMINED_BAND = (35, 65)  # inside it: NO stop, NO scratch, NO box (band floor retired as an early cut too)
# WO-FULL-COLD-AUDIT Finding 1 (build 52): raised 15 → 40, a REAL-DECISION
# threshold. At 15 a 15pt spot move (BTC drifts that in seconds) armed the
# violent SPOT_DECIDED cut, which crossfire-DUMPED positions at the depressed
# bid that would otherwise have reverted — the catastrophic-loss generator. At
# 40 the cut fires only on a genuine decision; and even then it no longer
# crossfire-dumps (it routes through the walk-down toward scratch, Finding 1).
OPEN_DETERMINED_K_POINTS = 40.0   # DREW-DEFAULT (build 52, was 15): a real decision, not drift
# WO-FULL-COLD-AUDIT Finding 3 (build 52): the market-level volatility skip. A
# hard-trending OPEN (a large one-directional BTC-spot move already visible in
# the opening ticks) gives FLIP's reversion thesis no edge — the pile-in it
# trades against never comes back. When the opening spot has already run >= this
# many DOLLARS one-directionally (>= 2 ticks observed), OPEN skips its reversion
# entry (HUNT, the momentum mode, and F are unaffected — F is byte-identical per
# the audit's criterion #5). Conservative by Adversary guard (do not skip normal
# markets); the trend reading is logged on EVERY open (skip or enter) so the
# threshold is calibrated from data, not guessed (pairs with Finding 5).
OPEN_TREND_SKIP_USD = 200.0       # DREW-DEFAULT (build 52): opening $-move that skips OPEN's reversion entry
# WO-FLIP-EXIT-DOCTRINE (build 40): determined-against is a statement about
# the MARKET'S DECISION (spot moved / time ran out), NOT the contract's
# price. The band floor (35c) marks where SWINGS happen — a cheap entry is
# bought to oscillate there, so the price cut must live BELOW the swing, at
# a genuine catastrophe. A FIXED low price (P&L-blind, not basis-anchored —
# two positions with the same book state get the same decision) is the ONLY
# price backstop that acts inside patience; spot+time are the primary cut.
OPEN_CATASTROPHE_FLOOR = 20        # DREW-DEFAULT: fixed price backstop, well below the swing band
# WO-2026-07-21-FLIP-SELECTION A1 (build 53): the price floor is RELATIVE to
# entry — max(OPEN_CATASTROPHE_FLOOR, entry − OPEN_SALVAGE_BUDGET_C). An ABSOLUTE
# 20c floor under a VARIABLE entry (25-42) meant a 42c entry risked 22c and a 29c
# entry 9c — an undeclared size-by-entry-price, and it deferred the cut until the
# price was worse (the hold that guarantees a worse exit). The budget is the EV
# table's OWN assumption (2c band-floor expectation + 5c slip, rounded up) — it
# MUST equal that assumption or the EV table stays inverted (FLIP_FLOOR_BREACH).
# The relative floor exits as a MAKER; only the absolute 20c floor crossfires.
OPEN_SALVAGE_BUDGET_C = 8          # DREW-DEFAULT (build 53): max bounded loss = the EV table's 2c+5c assumption, rounded
# WO-FLIP-CATASTROPHE-ILLIQUIDITY (build 48): the catastrophe PRICE floor was
# firing on a SINGLE poll of a thin-book low bid — a fresh cheap entry's
# held-side bid sits ~19-20c because there are no buyers YET (the opening
# pile-in = the illiquidity the thesis holds through), and it was dumping the
# inventory at the bottom, the exact anti-thesis. The price branch now fires
# only on a REAL collapse: (a) sustained >= 2 polls (no single-poll dump),
# (b) real depth on the held side (not a 1-lot thin quote), and (c) past the
# opening-illiquidity window (a fresh entry's low bid is the setup, not a
# verdict). A genuine sustained move is caught by the spot-decided branch
# regardless — the price floor is the deep backstop WITH guards.
OPEN_OPENING_WINDOW_S = 90         # DREW-DEFAULT: the opening pile-in; catastrophe price-floor is gated off until held past this
OPEN_CATASTROPHE_MIN_DEPTH = 3     # DREW-DEFAULT: real held-side bid depth for a catastrophe low to count (not a 1-lot thin quote)
# OPEN_BAIL_R_S retired (P26 §3.2): evacuations cross IMMEDIATELY — the
# determined-maker grace was tonight's 31/20/33 slide. TAKE alone rests.

# ---------------------------------------------------------------------------
# Safe defaults in force (A3 / Chunk 0.3)
# ---------------------------------------------------------------------------
ONE_LOT_MAX_LOSS_CENTS = 99  # worst-case loss on a single 1-lot maker entry (price -> 0)
AT_RISK_CAP_MULT = 3  # DREW-DEFAULT: $-at-risk per settlement event = 3x one-lot max loss
AT_RISK_CAP_CENTS = AT_RISK_CAP_MULT * ONE_LOT_MAX_LOSS_CENTS  # legacy flat cap (book==0 fallback)

# WO-2026-07-23-B Part 1 (DREW RULING 2026-07-23): the flat cross-lane risk wall
# is retired for a PER-LANE, BOOK-PROPORTIONAL dollar wall — keep the gateway
# guard, don't exempt F. The wall's real job is catching a SIZING BUG, not market
# risk; a FIXED cap holds F's absolute profit flat as the book rises (compounding
# → linear). At 97c the old count cap (3) and dollar cap (297c) were the SAME
# constraint (297/97 ≈ 3.06); a correct dollar wall subsumes the count wall. The
# NET_RISK / DOLLAR_RISK reason NAMES are kept so WALL_STORM telemetry stays
# comparable across builds.
# WO-2026-07-24-C "+4×10": FLIP 0.05→0.15 — 10×60¢ = 600¢ ≈ 14% of a $42 book.
# WO-2026-07-24-G "FLOODGATES": the wall is now the FLIP backstop (the fixed cap
# is retired as a binder). Each lane's SIZING DIAL must sit STRICTLY UNDER its
# wall — a dial == wall converts every rounding edge into a WALL reject + 30s
# backoff. F: dial F_NOTIONAL_PCT=0.20 under wall 0.25 (80% of wall). FLIP: dial
# FLIP_NOTIONAL_PCT=0.14 under wall 0.18. boot asserts dial < wall (fail loud).
# REVERT LEVERS (post-experiment): FLIP dial FLIP_NOTIONAL_PCT→lower and/or
# AT_RISK_PCT["FLIP"]→0.05; OPEN_SIGHTED_STOP=0. The wall must not silently stay
# loose after the experiment ends.
AT_RISK_PCT = {"F": 0.30, "H8": 0.05, "FLIP": 0.18, "D": 0.02, "P": 0.02}  # WO-L P2: F wall 0.25→0.30 (dial 0.24 stays 80% of wall)
AT_RISK_PCT_DEFAULT = 0.02        # any unlisted lane (ORPHAN, …) — conservative
# WO-2026-07-24-G Part 1 (ADVERSARY i backstop): the per-lane wall is now scoped
# per-lane, so a lane can't be squeezed by another's exposure — but the whole
# SETTLEMENT EVENT still must not run hot. This is the cross-lane total backstop:
# an event whose summed at-risk (all lanes) exceeds this fraction of book PAGES
# (the CEO's whole-event view; the hard 50% portfolio cap in _score_and_size
# still stops deployment). Page, not reject — the per-lane walls do the stopping.
EVENT_TOTAL_AT_RISK_PCT = 0.40    # DREW-DEFAULT: summed event at-risk over this % of book pages


def at_risk_cap_cents(lane: str, book_cents: int) -> int:
    """DREW RULING: dollar-at-risk cap per settlement event = the lane's share of
    the book. Since `_market_entry_blocked` enforces one lane per market, a
    per-lane cap is a per-event cap in practice; cross-event exposure is bounded
    by guard (a), the 50%-of-book portfolio cap."""
    return int(book_cents * AT_RISK_PCT.get(lane, AT_RISK_PCT_DEFAULT))
LANE_D_FLOOR_CENTS = 60  # DREW-DEFAULT: Lane D band floor, pending Chunk 2 data (50c vs 60c open)
DEPTH_FRACTION = 0.25  # DREW-DEFAULT: per-level size <= 25% of visible depth
# WO-2026-07-26-P §B1 — the band-depth reference for the notional lanes (F/FLIP).
# When a lane CREATES a level in front of a deep band, it sizes to a fraction of
# the band it functionally trades with, not the empty level it's joining. The
# band is the price ± halfwidth (the lane's own tier band, bounded — never the
# whole side, Adversary i). DREW-DEFAULT 0.15: derive upward from fill-quality.
BAND_DEPTH_FRACTION = 0.15         # DREW-DEFAULT(pending derivation): band size the notional lanes may take
SIZING_BAND_HALFWIDTH_C = 4        # DREW-DEFAULT: the band is price ± this many cents
# WO-INFRA-HARDENING E1: a book-vs-venue gap wider than this, with 0 unsettled
# fills and 0 resting orders, is an unexplained divergence (the phantom
# signature) and is recorded to the E1 trail. Book and venue both round once,
# so with nothing pending they match exactly — a whole-cent gap is real.
RECON_AUDIT_FLOOR_CENTS = 2  # DREW-DEFAULT: E1 records an unexplained book/venue gap above this
# COLD AUDIT build 70 §2: the venue returns cash and portfolio_value (pv) on
# DIFFERENT clocks; summing them across a settlement boundary produces a total
# wrong by the position notional (819c/99c/196c/198c — always the position). The
# cash reconcile now DEFERS unless the venue's pv agrees with the engine's own
# open-position notional (`deployed_cents`) within this tolerance — it refuses to
# compute on an internally inconsistent read instead of patching where the bad
# number lands. Honest caveat: pv is mark-to-market and deployed_cents is entry
# cost, so a large-unrealized open position also defers — safe (skip + retry;
# the reconcile still runs every flat window, where both are ~0).
PV_TOLERANCE_C = 20  # DREW-DEFAULT: max venue-pv vs deployed_cents gap to trust the read
# WO-2026-07-24-D Part 3: an ORIENTATION_DIVERGENCE halt auto-recovers on the
# first CURRENTLY-OPEN market that reads clean (the halting market expires in
# minutes and its book is pruned, so pinning recovery to it can never clear —
# 161 minutes of dead time on two occasions). This is the hard ceiling: a halt
# stuck longer than this pages ORIENTATION_HALT_STUCK rather than sitting silent.
ORIENTATION_HALT_MAX_S = 900   # DREW-DEFAULT: ~11 windows — a stuck halt must page, not wait
# WO-2026-07-26-R: the post-entry watch asks the RIGHT question. It halts only on
# what actually means "our book read can't be trusted" — an INVERSION (the mirror
# signature, _mirror_signature) or a GROSS non-mirror gap. A small sub-gross
# offset is the market-summary endpoint LAGGING the orderbook by a spread on a
# quiet book (freshness noise, not orientation): it demotes to BOOK_STALE info +
# a resync request, counted by hour so the tolerance becomes derivable.
BOOK_STALE_OFFSET_C = 3            # DREW-DEFAULT(pending derivation): offset above this but sub-gross, non-mirror = endpoint lag, not a halt
ORIENTATION_GROSS_DIVERGENCE_C = 15  # DREW-DEFAULT(pending derivation): a non-mirror gap this wide means the read is untrustworthy — halt
# WO-2026-07-24-D Part 4: a run of deferred/unreadable reconciles this long with
# NO clean venue cross-check pages RECON_STALLED — "nothing pending" must be
# distinguishable from "the check has not run in an hour" (the operator had no
# way to tell a verified book from an unverified one).
RECON_STALL_STREAK = 10        # DREW-DEFAULT: consecutive un-cross-checked cycles before it pages
# WO-2026-07-27-V T1: the SLEEPING-SENTINEL backstop. RECON_STALL_STREAK only
# counts UN-cross-checked defers (unreadable / pv-boundary); a QUIESCENCE defer
# (resting/unsettled busy) was treated as benign and never advanced it — so a
# two-room engine that never goes quiet silently never reconciled (the ~$45
# withdrawal walked past every guard). This is a WALL-CLOCK backstop: the book
# has not been verified against the venue in this long, for ANY reason → page
# RECON_STARVED, so quiescence starvation can never be silent again.
RECON_MAX_QUIET_S = 1800       # DREW-DEFAULT: 30 min unverified → RECON_STARVED (quiescence or stall)
# WO-2026-07-27-V B2: the sanity clamp's staleness threshold. A live confirmed
# venue-cash read older than this is not solvent evidence — blind is not solvent,
# so it defers everything and pages rather than sizing off a stale number.
CASH_CONFIRM_MAX_AGE_S = 1800  # DREW-DEFAULT: 30 min — a confirmed venue cash older than this defers + pages

# ---------------------------------------------------------------------------
# Walls (C.3 / BUILD_SEQUENCE 3.2)
# ---------------------------------------------------------------------------
NET_RISK_CROSS_LANE_CAP = 3  # net contracts at risk per settlement event, across lanes

# ---------------------------------------------------------------------------
# WO-2026-07-26-O "THE SCRAPE" — the operator's cut. $5 owed per full $10 of NEW
# high-water trading equity; sizing works off tradeable = book − owed. §O4: if
# tradeable ever falls below one F lot the desk is OWED_UNDERWATER — it has
# earmarked more than it can trade a single favorite with — so it PAGES and halts
# entries until the operator withdraws (reconciling owed down) or equity recovers.
SCRAPE_MILESTONE_C = 1000          # $10.00 of new high-water per milestone
SCRAPE_PER_MILESTONE_C = 500       # $5.00 owed each — half of every true ten
ONE_F_LOT_COST_C = 97              # a favorite's per-lot cost — the underwater floor
# ---------------------------------------------------------------------------
# WO-2026-07-23-B Part 1 — SCALE F. F earns ~98% of the book's profit and was
# capped at NET_RISK_CROSS_LANE_CAP=3 (a fixed count, named for risk, that turns
# compound growth into linear growth as the book rises). F now sizes to a % of
# book (self-scaling), bounded only by REAL depth — not by Kelly or the count
# cap. F ALONE takes this path; every other lane keeps min(kelly, depth, cap).
# ---------------------------------------------------------------------------
# WO-2026-07-25-L "F GETS THE BOOK" P2 — F MAXIMUM. F earns ~98% of the profit
# and just had every other lane moved to shadow (P1); it takes the desk's freed
# risk budget through THIS raise (walls are lane-scoped, so nothing else needs
# transferring). Dial 0.20 → 0.24 (stays 80% of the 0.30 wall). THE CEILING,
# STATED: at ~97¢ entries one full unsalvaged F loss costs ≈ dial × book ≈ 24%
# of book — the accepted bound of this ruling. Past ~0.25 a single ordinary loss
# is a one-third-of-book event; FURTHER F RAISES ARE GATED ON SALVAGE (the
# 1.2-point Gate A order) shipping and proving on tape (at ~40–50¢ salvaged loss
# sizes the same math supports dials well past 0.30).
F_NOTIONAL_PCT = float(os.environ.get("F_NOTIONAL_PCT", "0.24"))  # DREW DIAL: F size = pct of book / price (WO-L P2; was 0.20)
# WO-2026-07-27-W W2a — THE SPENT LOSS, per-lane halt geometry. F's halt bound is
# expressed in F's OWN loss units, not the desk's stop geometry: one full-size F
# loss at the current dial (≈ F_NOTIONAL_PCT × tradeable — the whole clip at a
# ~97¢ favorite) × this multiplier. At 1.5 a SINGLE ordinary tail (1.0×) never
# halts F alone — it pages F_BIG_LOSS (that page already exists); the halt fires
# on a CLUSTER (a second tail, or tail-plus-bleed, inside the trailing window,
# ≥1.5×). The desk lanes keep their own stop-derived rate_halt_drawdown_c. Before
# W2a a single −$10–19 tail instantly exceeded the ~3%-of-book desk bound and
# poisoned the trailing-8 sum for hours — the overnight died on one event.
F_HALT_TAIL_MULT = float(os.environ.get("F_HALT_TAIL_MULT", "1.5"))  # DREW-DEFAULT (WO-W W2a): F halt bound = this × one full F loss
# WO-2026-07-24-D Part 1: FLIP self-scales by NOTIONAL too (like F), else KELLY
# caps it at ~5-6 on a $43 book (358c/60c) and FLIP_SIZE_CAP=10 never bites —
# the +4×10 test ran at half the ruled size. 10 lots × 60c = 600c ≈ 14% of a
# $43 book, so notional gives 10; the 15% at-risk wall (10 × 64c = 640c vs 645c)
# is the gateway backstop above it. REVERT with the cap/wall (≤4 of 10 fill).
# WO-2026-07-25-K "THE DESK EARNS ITS SIZE" P1 — TUITION SIZE. The +4 desk was
# 6-of-11 (55% conversion) against a ~73% breakeven; Saturday priced it at
# OPEN −$12.35. Demote instantly to tuition (the constitution's own words): the
# desk keeps its full armor and keeps buying cells, at ~−35¢/day worst instead
# of −$12 days. FLIP_NOTIONAL_PCT is the LIVE FLOOR (tuition); the desk re-earns
# FLIP_FULL_NOTIONAL_PCT by CONVERSION (P3), the tape moving the dial — no ruling
# at either edge. rate_halt_drawdown_c re-derives from the ACTIVE dial (verified).
FLIP_NOTIONAL_PCT = float(os.environ.get("FLIP_NOTIONAL_PCT", "0.04"))  # DREW DIAL: FLIP TUITION size = pct of book / price (WO-K P1; was 0.14)
FLIP_FULL_NOTIONAL_PCT = float(os.environ.get("FLIP_FULL_NOTIONAL_PCT", "0.14"))  # the EARNED full size (WO-K P3 promotion target)
# WO-K P3 — PROMOTION BY CONVERSION: the size ladder is mechanical. The desk
# re-earns full notional when trailing-N round-trip conversion clears the top
# bar; demotion back to tuition is INSTANT under the bottom bar (promote slowly,
# demote instantly). The margin tiebreaker: no full size while the ENTRY cell's
# Wilson margin is negative, regardless of streak (blocks a lucky streak from
# up-sizing a structurally-losing cell — ADVERSARY's gaming check).
FLIP_CONV_WINDOW = 20       # trailing round-trips the conversion reads (FLIP_SWING)
FLIP_PROMOTE_CONV = 0.75    # >= this over the window → promote to full (above breakeven w/ margin)
FLIP_DEMOTE_CONV = 0.65     # < this → demote to tuition, instant
# WO-K P2 — THE CONFIDENCE INSTRUMENT: the -J settle table's desk duty. At OPEN
# entry, the favored side's SETTLE-fair must exceed the join by this margin —
# the entry must buy a side the physics already favors by more than the spread
# being paid (the fragility veto: a coin balanced on its edge reads settle-fair
# ≈ 50 and is refused). DREW-DEFAULT 4c; derive properly once tagged trades exist.
FLIP_CONF_MIN_C = float(os.environ.get("FLIP_CONF_MIN_C", "4"))


# WO-2026-07-24-G Part 1 / acceptance #6: a lane's SIZING DIAL (notional pct)
# must sit STRICTLY UNDER its at-risk WALL — a dial >= wall converts every
# rounding edge into a WALL reject + 30s backoff (the Adversary's 07-24-D
# warning, now with tape). The two self-scaling lanes are F and FLIP. WO-K: the
# FLIP entry guards the FULL (promoted) size — the largest the dial can reach —
# so the wall bounds the ceiling, not just the tuition floor.
DIAL_OF_LANE = {"F": F_NOTIONAL_PCT, "FLIP": FLIP_FULL_NOTIONAL_PCT}


def dial_wall_violations() -> list:
    """Return [(lane, dial, wall), …] for every lane whose sizing dial is NOT
    strictly under its at-risk wall. Empty = healthy. Boot asserts this and
    FATALs loud on any inversion (a dial that would fight its own wall)."""
    bad = []
    for lane, dial in DIAL_OF_LANE.items():
        wall = AT_RISK_PCT.get(lane, AT_RISK_PCT_DEFAULT)
        if dial >= wall:
            bad.append((lane, dial, wall))
    return bad
# Guard (a): total deployed capital across ALL lanes never exceeds this % of
# book (F and FLIP can hold different markets at once; nothing else bounds the sum).
PORTFOLIO_DEPLOY_PCT = 0.50       # DREW-DEFAULT: sum of open notional <= 50% of book
# WO-2026-07-26-Q: guard (b) (the day-long F suppression) is DELETED — it
# duplicated the money-based rate halt, the one governor ruled for this risk.
# This constant SURVIVES ONLY as the threshold for the F_BIG_LOSS page —
# information the operator sees when F takes a rare large tail, NEVER a governor.
F_EVENT_TRIPWIRE_C = 60           # RULED(2026-07-26): page F_BIG_LOSS above one F loss > 60c/contract (WO-Q; no longer gates)

# ---------------------------------------------------------------------------
# Sizing (C.3 / Charter §8): tiers move on Wilson lower bounds only.
# Capital raises budgets, never tiers.
# ---------------------------------------------------------------------------
# A-PLAYER B4 (CEO lens): the fraction is DREW'S DIAL, never code's
# choice — size is the premise, re-derived from the book (Kelly stays the
# SHAPE; it self-scales as the book compounds). Set via the KELLY_FRACTION
# env at deploy; the default holds the last ruled value until Drew turns
# it. The boot SIZING line states the live fraction.
KELLY_FRACTION_CEILING = float(os.environ.get("KELLY_FRACTION",
                                              str(1.0 / 12.0)))
WILSON_Z = 1.96
TIER_SUPPRESS = "SUPPRESS"
TIER_PROBE = "PROBE"
TIER_LEAN = "LEAN"
TIER_CLEAR = "CLEAR"
# Wilson lower-bound thresholds for tier admission. Placeholder ladder pending
# charter §8 verbatim (CHARTER.md is a missing input); ordering + semantics per canon.
TIER_LOWER_BOUNDS = {TIER_PROBE: 0.50, TIER_LEAN: 0.65, TIER_CLEAR: 0.80}
TIER_MAX_CONTRACTS = {TIER_SUPPRESS: 0, TIER_PROBE: 1, TIER_LEAN: 2, TIER_CLEAR: 3}
THIN_BOOK_MIN_DEPTH = 5  # visible contracts below this = thin book -> back off one tier

# ---------------------------------------------------------------------------
# P22 "THE CELL SCOREBOARD" — the ladder's floor sensors. Every closed unit
# of risk writes its cell (lane × price bucket); bars price to each cell's
# OWN breakeven (the F-bar blind spot dies). PROBE stays a ruling, not a
# bar — R1/R2 stand; only LEAN/CLEAR are earned.
# ---------------------------------------------------------------------------
CELL_WIDTH_CENTS = 5              # DREW-DEFAULT: 35-39, 40-44, ... 95-99

# ---------------------------------------------------------------------------
# P26 "EVERY WHY IS A PROOF" — one brain, loaded or explained (§1), the
# proof law (§2), and P25's OPEN fixes merged (§3).
# ---------------------------------------------------------------------------
# §1.1: boot provisions the delta table — load from disk, else BUILD
# (delta_builder's own path, A1-A5 gated, hot-load on PASS). Env-off for
# air-gapped runs; tests disable via conftest.
TABLE_AUTOBUILD = os.environ.get("TABLE_AUTOBUILD", "true").lower() == "true"
# P-FLIP-THESIS-1 §3.5 (DREW-RULED, was 480/T-8): FLIP owns the scalp
# window T-15→T-10; F owns the final five minutes. No fresh scalp
# inventory once secs_left <= 600.
OPEN_ENTRY_CUTOFF = 600           # no OPEN entries once secs_left <= this
# P-FLIP-THESIS-1 §3.5 (DREW-RULED, was 360/T-6 blind YIELD_TO_F
# crossfire): at T-10 the handoff is BOOK-AWARE, per position — a winner
# (mark at/above basis) is LEFT TO F as hold-to-settle at FLIP's cheap
# basis (F stands down via shared inventory, §4); a loser is SOLD now,
# never dumped into the settlement zone; flat hands off nothing. F owns
# the final five minutes uncontested.
OPEN_FLAT_BY = 600                # T-10: the book-aware handoff boundary
# §3.4: geometry gate — determined trigger tightens to entry−drop (floored
# at the band), and entry requires risk <= TAKE+1 or pass OPEN_BAD_GEOMETRY.
OPEN_DETERMINED_DROP = 6
# P-FLIP-THESIS-1 §2 (DREW-RULED 2026-07-19): THE PATIENCE FLOOR — a fresh
# FLIP entry holds through this assessment window (measured from FIRST FILL);
# a first-minute 1-2c dip is NOISE, not a decision (the 48->41 evacuate at
# -9c was a hair-trigger, not patience). Determined-against fires only AFTER
# the window, on a real decision — and then it fires HARD (anti-ride-to-zero:
# patience is upside-only).
OPEN_PATIENCE_S = 300

# WO-BOTH-LANES-MARKET-TRUE (build 50) — the doctrine made TIME-AWARE.
# FLIP is a market maker holding through the opening pile-in with F-like
# conviction, then working the exit down to clear its inventory by the
# absolute-last decision point; F is the inventory-aware endgame authority
# (rides winners to settlement, its ΔP/table proof rules the sell of a loser).
#   0 → NO_SELL_S from entry:  HARD NO-SELL. The opening pile-in is noise, not
#       a decision — the book has not reconciled to BTC yet. NOTHING sells
#       (spot-decided cut AND catastrophe price cut both suppressed); the only
#       exit is the resting middle-take getting FILLED. 1-lot cap + a cheap
#       (<=50) entry bound the max loss through the hold.
#   NO_SELL_S → DECISION_S: ACTIVE MANAGEMENT. F's proof re-arms (a sustained
#       spot-decided collapse cuts); an unfilled position is walked DOWN toward
#       scratch (the B3 walk-down, re-based on DECISION_S) — the goal is to
#       scalp the win and leave ZERO FLIP inventory by the decision point.
#   secs_left <= DECISION_S (~minute 11 of 15): THE DECISION — a winner is left
#       to F as hold-to-settle at FLIP's cheap basis (F rides it); a loser is
#       sold. Never caught holding unfilled into the bell.
# NO_SELL_S (secs since entry) and DECISION_S (secs remaining) are the SAME
# number by construction — the two faces of the one 4-minute conviction window.
FLIP_NO_SELL_S = 240             # DREW-DEFAULT: hard no-sell horizon from entry (secs since fill)
FLIP_DECISION_S = 240            # DREW-DEFAULT: the endgame decision point (secs remaining, ~minute 11)

# ---------------------------------------------------------------------------
# A-PLAYER doctrine (banked 2026-07-19): the machine runs untouched
# overnight; only a loss-RATE stops trading; a single loss is noise.
# ---------------------------------------------------------------------------
# B2 (DREW-DEFAULT ≤5c suggested): a cash delta at or under this magnitude
# is NOISE — silently re-baselined and logged, never prompted, never
# halted (the hourly-confirmation tax killed the overnight book). Above
# it, the WO-CASH-FATAL-1 genuine-dispute path stands untouched.
CASH_SILENT_REBASE_CENTS = 5
# WO-2026-07-22 (build 55): the slip allowed on the net-vs-gross settlement
# bound before SETTLE_NOTIONAL_BREACH pages — fees/rounding, not a whole leg.
SETTLE_NOTIONAL_SLIP_C = 6
# B3 (DREW-DEFAULT): the halt is a RATE — N losing markets of the last M
# settled traded markets (per-market broker P&L is the unit). One loss
# NEVER halts; two-in-a-row was never a reliable signal, 2-of-4 is.
RATE_HALT_LOSSES = 2         # legacy count-halt constants (retired by WO-2026-07-24-C
RATE_HALT_WINDOW = 4         # Part 2; kept for reference / a persisted legacy halt)
# WO-2026-07-24-C Part 2: the per-lane rate halt counts MONEY, not negative
# windows (−8,−7,+17 nets +2¢ and must NOT halt), over a wider window, against a
# drawdown threshold DERIVED from the lane's own position size — a flat 40¢
# threshold sized for 1-lot FLIP would strangle a 10-lot lane before its first
# loss settled (the same absolute-constant error as NET_RISK/AT_RISK_CAP). At
# FLIP_SIZE_CAP=3 → 120¢; at 10 → 400¢ (~3.5 stop-outs). F is unaffected in
# practice (96.9% wins never accumulate it). WO-Q: F's old per-event tripwire is
# DELETED — the money rate halt is F's governor now (a RUN of losses, not one);
# F_EVENT_TRIPWIRE_C survives only as the F_BIG_LOSS page threshold.
RATE_HALT_WINDOW_N = 8
# RATE_HALT_DRAWDOWN_C is derived from FLIP_SIZE_CAP (defined further below), so
# it is computed right after that constant.
# WO-FLIP-CHEAP-LIVE §2.2 (DREW-DEFAULT, permissive for live-proof — we
# WANT data): the two-sided swing gate's floor. p_cross(d_strike, t) is
# P(spot touches the strike = the 50/50 swing en route to the take);
# every cut-first path is a no-touch path, so p >= this (> 0.5) implies
# P(reach take) > P(reach cut) from the SAME table cell.
OPEN_SWING_MIN_P = 0.55
# Instrument 2: a loser's cut may exceed the band-floor expectation by at
# most this slip before FLIP_LOSER_CUT flags ok=False (the EV inverts if
# losers ride past the floor — the early warning the rate-halt can't give).
FLIP_FLOOR_SLIP_CENTS = 5
# WO-SWING-GATE-EVENT (build 39): the old gate measured P(spot crosses the
# strike) — trivially ~0.89 for every cheap entry, because a cheap side is
# cheap PRECISELY when spot sits near the strike (tiny d). It measured the
# wrong event and rubber-stamped falling knives. The fix gates on the
# MEASURED took_swing rate (Instrument 1) for the entry's price band once
# enough outcomes exist; below that it is permissive but the 1-lot cap
# bounds the risk (DREW-RULED 2026-07-20). §2's two-barrier price model
# rides along as a SHADOW comparison, calibrated against the measured rate
# before it may ever drive the decision.
OPEN_SWING_MIN_SAMPLES = 20       # Adversary (a): don't gate on a thin sample
# WO-2026-07-25-L §P4 — BREAK-EVEN RECALIBRATION + THIN. A cell with fewer than
# this many REALIZED outcomes carries NO gate authority anywhere — not in
# promotion evidence, not as a margin tiebreaker; it shows greyed with its n. A
# cell leaves THIN only by realized n, NEVER by modeled numbers (the review's
# item #4: 25 of 29 cells were off ≥8% on placeholder be/loss values).
CELL_THIN_MIN_N = 10              # DREW-DEFAULT: realized-n floor for a cell to hold gate authority
FLIP_SIZE_CAP = 10                # LEGACY reference — RETIRED as a sizing binder by WO-2026-07-24-G Part 2 (FLIP now scales with the book via min(notional, depth)); kept only for the book==0 halt fallback and the revert narrative. The live binder is FLIP_NOTIONAL_PCT + the at-risk wall.
FLIP_HALT_REF_PRICE_C = 55        # DREW-DEFAULT: the mid-band FLIP price the halt sizes its lot count at


def rate_halt_drawdown_c(book_cents: int) -> int:
    """WO-2026-07-24-G Part 2: the per-lane rate-halt drawdown threshold = FOUR
    stop-outs' worth at the lane's CURRENT size. FLIP now scales with the book
    (notional, no fixed cap), so a threshold frozen at yesterday's size is the
    count-vs-money bug reborn — it must recompute with the book. Lots ≈ notional
    at the mid-band reference price; 4 × lots × OPEN_MOMENTUM_STOP_C. book<=0
    (boot/test with no book yet) falls back to the legacy cap-sized 400¢."""
    if book_cents and book_cents > 0:
        lots = int(book_cents * FLIP_NOTIONAL_PCT / FLIP_HALT_REF_PRICE_C)
    else:
        lots = FLIP_SIZE_CAP
    return max(1, 4 * max(1, lots) * OPEN_MOMENTUM_STOP_C)


# Module-level default (book==0) — the legacy display value + a test reference;
# the LIVE threshold is rate_halt_drawdown_c(book) computed from the current book
# at settle time (window_econ) and printed in the boot banner + hourly.
RATE_HALT_DRAWDOWN_C = rate_halt_drawdown_c(0)
# §2: PROBE mode runs only WHILE the cells fill — a mature cell (n >= this)
# with negative margin means the receipts argue against the lane: it sits.
OPEN_PROBE_MAX_N = 20

# ---------------------------------------------------------------------------
# DIAG-1 "THE INTERROGATOR" §2 — the fix ships blind, armed by env. v1 = the
# current comparison (survival >= price paid, ANY-TOUCH table). v2 = the
# AT-CLOSE comparison; until the at-close correction column exists in the
# table, v2 is implemented as bar = price − buffer, stamped proof=v2-buffer.
# Drew flips ONE Render env var after reading the DIAG-001 page: H-SEMANTICS
# verdict → F_PROOF_MODE=v2; H-MARKET or DISCIPLINE → touch nothing.
# ---------------------------------------------------------------------------
F_PROOF_MODE = os.environ.get("F_PROOF_MODE", "v1").strip().lower()
F_PROOF_V2_BUFFER_PTS = 4         # v2 interim: touch→close correction stand-in
TIER_BUFFER = {TIER_LEAN: 0.03, TIER_CLEAR: 0.05}   # DREW-DEFAULT bar over BE
# §3.3: the bar math must never demand the impossible, only the honest —
# bars cap below 1.0 (a 97¢ hold-cell LEAN bar lands at the cap, .98).
TIER_BAR_CAP = {TIER_LEAN: 0.98, TIER_CLEAR: 0.99}
SALVAGE_ADJ_MIN_N = 20            # DODGED curve n before salvage-adj BE applies

# ---------------------------------------------------------------------------
# Ledger / drawdown (C.2)
# ---------------------------------------------------------------------------
DRAWDOWN_ABSOLUTE_FLOOR_USD = 25.0  # absolute-floor semantics...
DRAWDOWN_FLOOR_UNTIL_BOOK_USD = 50.0  # ...until book > $50
PCT_OF_BOOK_CAP = 0.10  # %-of-book order budget, snapshotted at boot

# ---------------------------------------------------------------------------
# Cash-movement protocol (C.2 / BUILD_SEQUENCE 1.2)
# ---------------------------------------------------------------------------
CASH_DENY_TIMEOUT_SECONDS = 30 * 60  # /deny_cash or 30-min silence -> FATAL

# ---------------------------------------------------------------------------
# Feed / degrade ladder (C.3 / BUILD_SEQUENCE 3.1)
# ---------------------------------------------------------------------------
WS_RESUME_CLEAN_FRAMES = 10  # resume entries only after this many clean frames post-resync
STALENESS_LIMIT_SECONDS = 5.0  # frames older than this are stale; stale book = no entries

# ---------------------------------------------------------------------------
# Rate governor (C.3): the printed number IS the enforced number. Boot tape
# prints these exact constants; the token bucket enforces these exact constants.
# ---------------------------------------------------------------------------
RATE_BUCKET_CAPACITY = 10  # tokens
RATE_REFILL_PER_SECOND = 2.0  # tokens/second

# ---------------------------------------------------------------------------
# Fee tripwire (C.3): expected fee schedule. The tripwire watches BOTH the
# multiplier and the maker-fee designation list; any observed deviation halts entries.
# P24 §2.3: DISPLAY/ESTIMATE-ONLY — booked fees are READ from the venue's
# fill records and order responses (venue._resolve_fee, one parser, both
# entrances), never imagined from this number. It survives only in the
# tripwire's expectation and the scoreboard's breakeven ESTIMATES.
# ---------------------------------------------------------------------------
EXPECTED_FEE_MULTIPLIER = 0.07
EXPECTED_MAKER_FEE_SERIES = frozenset()  # series currently designated as maker-fee-charging

# ---------------------------------------------------------------------------
# Paper shadow: the notional bankroll the shadow books against so budget walls
# exercise realistically. Paper only — no real dollar exists until cutover.
# ---------------------------------------------------------------------------
SHADOW_PAPER_BANKROLL_USD = 100.0

# ---------------------------------------------------------------------------
# Storage — single-writer law (§A1): this engine's OWN database, never the
# live surface DB.
# ---------------------------------------------------------------------------
# P16 §1: the disk is the constant; variable names are not. Fallback chain:
# RELAY_DB_PATH → the legacy K_WORKER_DB's DIRECTORY (never its file — a
# different engine's schema; single-writer law) → the ephemeral default.
def resolve_db_path(env=None):
    """Returns (path, source) — source in {'RELAY_DB_PATH','derived','ephemeral'}."""
    env = os.environ if env is None else env
    p = env.get("RELAY_DB_PATH")
    if p:
        return p, "RELAY_DB_PATH"
    legacy = env.get("K_WORKER_DB")
    if legacy:
        return os.path.join(os.path.dirname(legacy) or ".",
                            "relay_live.db"), "derived"
    return "relay_shadow.db", "ephemeral"


DB_PATH, DB_PATH_SOURCE = resolve_db_path()

# Kalshi endpoints (read-only usage until cutover, §B3)
API_BASE = os.getenv("KALSHI_API_BASE", "https://api.elections.kalshi.com").rstrip("/")
API_PREFIX = os.getenv("KALSHI_API_PREFIX", "/trade-api/v2").rstrip("/")
WS_URL = os.getenv("KALSHI_WS_URL", "wss://api.elections.kalshi.com/trade-api/ws/v2")
SERIES_TICKER = os.getenv("SERIES", "KXBTC15M")

# ---------------------------------------------------------------------------
# WO-2026-07-26-S "THE SECOND ROOM" §1 — SERIES AS A DIMENSION, NOT A FORK.
# One repo, one process, one gateway, one ledger, one Telegram, one pack. Every
# lane key becomes (series, lane): F-BTC and F-XRP are siblings sharing ALL
# doctrine code and NONE of their records. SERIES is the ordered roster of rooms
# that discovery iterates; series_of(market) is the ONE canonical market→series
# map. Explicitly rejected (the old era's four-bots pattern): copying the repo
# per series — four ledgers was how the legacy era lost track of truth (§1b).
# ---------------------------------------------------------------------------
# The roster default is BTC-ONLY through Stage 1/2 (the dimension + the ensemble
# governor land first, BTC byte-identical); Stage 3 adds KXXRP15M when the XRP
# room's config, gate, and pack chapter are all in place. Override with SERIES_LIST.
SERIES = [s.strip() for s in os.getenv(
    "SERIES_LIST", "KXBTC15M").split(",") if s.strip()]


def series_of(market: str) -> str:
    """THE canonical market→series map: the ticker prefix before the first '-'
    (Kalshi tickers are 'KXBTC15M-25JAN...'; series is 'KXBTC15M'). The ONE
    place a market names its room — every (series, lane) key derives from here."""
    return market.split("-", 1)[0] if market else ""


# Per-series MODE (OFF | SHADOW | LIVE). The global kill (RUN_MODE /
# I_UNDERSTAND_LIVE, §B3) still rules everything; a per-series mode can only
# RESTRICT below it, never widen it (the LANE_MODE precedent, WO-L). BTC is the
# proven room; XRP is BORN LIVE at its own dial (Drew 07-26 RULING: "no ghost
# phase — one room per day, LIVE at full per-series size, each with its own
# halt") — but the global-SHADOW born state forces every room to shadow until
# Drew arms the account. OFF = a room not discovered or traded at all.
SERIES_MODE = {
    "KXBTC15M": os.getenv("SERIES_MODE_BTC", "LIVE").upper(),
    "KXXRP15M": os.getenv("SERIES_MODE_XRP", "LIVE").upper(),
}
SERIES_MODE_DEFAULT = "OFF"    # an unrostered series does nothing (conservative)


def series_mode(series: str) -> str:
    return SERIES_MODE.get(series, SERIES_MODE_DEFAULT)


def series_is_live(series: str) -> bool:
    """A room places live orders ONLY when the global kill is off AND the room's
    own mode is LIVE. Per-series can only restrict below the global."""
    return live_submit_enabled() and series_mode(series) == "LIVE"


def series_books_shadow(series: str) -> bool:
    """Does this room's outcome book to the SHADOW ledger? In a LIVE run a room
    that is not itself LIVE rehearses; in a global-SHADOW run every room is one
    paper ledger (the born state)."""
    return live_submit_enabled() and not series_is_live(series)


# Per-series F NOTIONAL DIAL. BTC keeps its earned 24%; every NEW series is born
# at 20% (RULED 2026-07-26 WO-S) — "until the record argues": the ladder may
# raise a room above 20 only on its OWN Wilson record, and demotes it instantly
# on its own tape. The dial threads into sizing.size_order via notional_pct, so
# F-BTC sizing is byte-identical (KXBTC15M → F_NOTIONAL_PCT, unchanged).
NEW_SERIES_F_DIAL = 0.20           # RULED(2026-07-26 WO-S): a new room is born here


def f_notional_pct_of(series: str) -> float:
    """The F notional dial for a room. BTC = the earned F_NOTIONAL_PCT (0.24);
    any other series = the born-at 20% until its own record argues."""
    if series == "KXBTC15M":
        return F_NOTIONAL_PCT
    return NEW_SERIES_F_DIAL


def f_enabled_series() -> list:
    """The rostered series F is allowed to trade — every SERIES whose mode is not
    OFF. The engine widens lane_fh8.F_SERIES_ALLOWED to this at boot; a room set
    OFF is discovered by nothing and traded by no lane."""
    return [s for s in SERIES if series_mode(s) != "OFF"]


# Known 15-minute crypto series, by short asset name (for the /series command).
# XRP first (Drew's pick), then SOL, then ETH (ETH last — "needs its own tuning").
KNOWN_SERIES = {"BTC": "KXBTC15M", "XRP": "KXXRP15M",
                "SOL": "KXSOL15M", "ETH": "KXETH15M"}


def resolve_series(name: str):
    """Map a /series argument to a Kalshi series ticker. Accepts the short asset
    ('xrp') or the full ticker ('KXXRP15M'). None if unknown."""
    up = name.strip().upper()
    if up in KNOWN_SERIES:
        return KNOWN_SERIES[up]
    if up in KNOWN_SERIES.values():
        return up
    return None


def halt_scope(series: str, lane: str) -> str:
    """WO-2026-07-26-S §2 — PER-SERIES HALTS (RULED 07-26: "each lane gets its own
    halt"). The money rate-halt keys on (series, lane): XRP's drawdown parks XRP
    while BTC keeps printing. With a SINGLE room the scope IS the lane — byte-
    identical to the per-lane halt, no persisted-key migration — so the machinery
    is in place now and the series dimension activates the moment a second room
    joins the roster (Stage 3, which carries the key migration)."""
    return lane if len(SERIES) <= 1 else f"{series}:{lane}"


# WO-2026-07-26-S §2 — THE ENSEMBLE CAP (the correlated-tail governor). Total
# SIMULTANEOUS at-risk across ALL rooms ≤ this % of TRADEABLE, at all times — one
# summed check ABOVE the lane walls (never replacing them). Non-correlated rooms
# could safely deploy more than one room ever could, BUT the tail is correlated:
# a cross-crypto air-pocket is the one shock that flips every room's favorite at
# once. This is the board condition — growth in rooms may never grow the one-shock
# loss past this. It reuses the existing summed-deployed check (guard a), now
# named and measured against tradeable (book − owed). RULED 2026-07-26.
ENSEMBLE_AT_RISK_PCT = PORTFOLIO_DEPLOY_PCT   # 0.50 — the ruled ensemble ceiling


def drew_defaults() -> dict:
    """The DREW-DEFAULT constants, for the boot tape (printed until ruled)."""
    return {
        "at_risk_cap_per_settlement_event": f"{AT_RISK_CAP_MULT}x one-lot max loss = {AT_RISK_CAP_CENTS}c",
        "lane_d_floor": f"{LANE_D_FLOOR_CENTS}c (pending Chunk 2 data)",
        "depth_fraction": f"{DEPTH_FRACTION:.0%}",
    }


# ---------------------------------------------------------------------------
# WO-2026-07-26-P §A4 — CONSTANT PROVENANCE TAGS. Every capital-relevant number
# is one of three things, and the tree says which IN CODE: RULED(date) (Drew
# ruled it, with the day it was ruled), DERIVED(source) (computed from a stated
# source), or DREW-DEFAULT(pending) (a placeholder standing until ruled). A
# DERIVED tag with no source is a number pretending to be reasoned — it FAILS
# LOUD (assert_constant_tags_sane). Boot prints the constants that CHANGED this
# deploy with their tag, so a size move is never silent. `baseline` is the value
# at the prior deploy (build 84); _NEW marks a constant introduced this deploy.
# ---------------------------------------------------------------------------
RULED, DERIVED, DREW_DEFAULT = "RULED", "DERIVED", "DREW-DEFAULT"
_NEW = object()  # sentinel: a constant born this deploy (no prior baseline)


class ConstantTag:
    """One capital constant's provenance, value, and prior-deploy baseline."""
    __slots__ = ("name", "kind", "provenance", "value", "baseline")

    def __init__(self, name, kind, provenance, value, baseline):
        self.name = name
        self.kind = kind                # RULED | DERIVED | DREW-DEFAULT
        self.provenance = provenance    # date / source / "pending …"
        self.value = value
        self.baseline = baseline        # prior-deploy value, or _NEW

    def is_new(self) -> bool:
        return self.baseline is _NEW

    def is_changed(self) -> bool:
        return self.is_new() or self.value != self.baseline

    def derived_without_source(self) -> bool:
        return self.kind == DERIVED and not str(self.provenance).strip()

    def tag_str(self) -> str:
        return f"{self.kind}({self.provenance})"


def constant_tags() -> list:
    """The provenance table. Every capital-relevant constant is tagged; the
    baseline is the value shipped in build 84 (so `is_changed` flags this
    deploy's moves). WO-P adds exactly the two band constants and changes no
    dial — the changed set is those two NEW entries, nothing else."""
    return [
        ConstantTag("DEPTH_FRACTION", DREW_DEFAULT, "pending derivation",
                    DEPTH_FRACTION, 0.25),
        ConstantTag("BAND_DEPTH_FRACTION", DREW_DEFAULT,
                    "pending derivation from fill-quality", BAND_DEPTH_FRACTION, _NEW),
        ConstantTag("SIZING_BAND_HALFWIDTH_C", DREW_DEFAULT, "pending derivation",
                    SIZING_BAND_HALFWIDTH_C, _NEW),
        ConstantTag("F_NOTIONAL_PCT", RULED, "2026-07-25 WO-L P2",
                    F_NOTIONAL_PCT, 0.24),
        ConstantTag("AT_RISK_PCT.F", RULED, "2026-07-25 WO-L P2",
                    AT_RISK_PCT["F"], 0.30),
        ConstantTag("FLIP_NOTIONAL_PCT", RULED, "2026-07-25 WO-K P1",
                    FLIP_NOTIONAL_PCT, 0.04),
        ConstantTag("SCRAPE_MILESTONE_C", RULED, "2026-07-26 WO-O",
                    SCRAPE_MILESTONE_C, 1000),
        ConstantTag("SCRAPE_PER_MILESTONE_C", RULED, "2026-07-26 WO-O",
                    SCRAPE_PER_MILESTONE_C, 500),
        ConstantTag("LANE_D_FLOOR_CENTS", DREW_DEFAULT, "pending Chunk 2 data",
                    LANE_D_FLOOR_CENTS, 60),
        ConstantTag("RECON_AUDIT_FLOOR_CENTS", DREW_DEFAULT, "pending derivation",
                    RECON_AUDIT_FLOOR_CENTS, 2),
        # WO-Q: retagged DREW-DEFAULT→RULED — no longer a governor, the page threshold.
        ConstantTag("F_EVENT_TRIPWIRE_C", RULED, "2026-07-26 WO-Q (F_BIG_LOSS page only)",
                    F_EVENT_TRIPWIRE_C, 60),
        # WO-R: the orientation watch's two thresholds (NEW this deploy).
        ConstantTag("BOOK_STALE_OFFSET_C", DREW_DEFAULT,
                    "pending derivation from endpoint-lag-by-hour", BOOK_STALE_OFFSET_C, _NEW),
        ConstantTag("ORIENTATION_GROSS_DIVERGENCE_C", DREW_DEFAULT,
                    "pending derivation from endpoint-lag-by-hour",
                    ORIENTATION_GROSS_DIVERGENCE_C, _NEW),
        # WO-S: a new room is born at 20% F notional until its own record argues.
        ConstantTag("NEW_SERIES_F_DIAL", RULED, "2026-07-26 WO-S (born-at, per-series)",
                    NEW_SERIES_F_DIAL, _NEW),
        # WO-V: the sleeping-sentinel backstops (NEW this deploy).
        ConstantTag("RECON_MAX_QUIET_S", DREW_DEFAULT, "pending derivation from recon cadence",
                    RECON_MAX_QUIET_S, _NEW),
        ConstantTag("CASH_CONFIRM_MAX_AGE_S", DREW_DEFAULT, "pending derivation",
                    CASH_CONFIRM_MAX_AGE_S, _NEW),
        # WO-W W2a: F's halt bound in tail units (NEW this deploy). Gets a
        # derived number once the pack has counted tail-cluster frequency/room.
        ConstantTag("F_HALT_TAIL_MULT", DREW_DEFAULT,
                    "pending derivation from tail-cluster frequency per room",
                    F_HALT_TAIL_MULT, _NEW),
    ]


def derived_without_source(tags=None) -> list:
    """DERIVED tags carrying no source — numbers pretending to be reasoned."""
    return [t for t in (tags or constant_tags()) if t.derived_without_source()]


def changed_constants(tags=None) -> list:
    """Constants whose value differs from the prior deploy (or are new)."""
    return [t for t in (tags or constant_tags()) if t.is_changed()]


def assert_constant_tags_sane(fail_fn=None, tags=None) -> None:
    """FAIL LOUD if any DERIVED constant has no source. Called at boot."""
    orphans = derived_without_source(tags)
    if orphans:
        names = ", ".join(t.name for t in orphans)
        detail = (f"DERIVED constants with no source: {names} — a DERIVED tag "
                  "must name what it was derived FROM (Why Law §A4)")
        if fail_fn is not None:
            fail_fn("DERIVED_WITHOUT_SOURCE", detail, fatal=True)
        else:
            from . import failures
            failures.fail("DERIVED_WITHOUT_SOURCE", detail, fatal=True)


def constant_tag_boot_lines() -> list:
    """Boot tape: the constants that CHANGED this deploy, with their tags."""
    changed = changed_constants()
    if not changed:
        return ["CONSTANTS (WO-P A4): no capital constant changed this deploy "
                "(all tagged RULED/DERIVED/DREW-DEFAULT in-source)"]
    lines = ["CONSTANTS CHANGED THIS DEPLOY (WO-P A4) — each tagged in-source:"]
    for t in changed:
        was = "NEW" if t.is_new() else f"was {t.baseline}"
        lines.append(f"  {t.name} = {t.value} [{t.tag_str()}] ({was})")
    return lines


def live_submit_enabled() -> bool:
    """The hard-disable pattern (§B3). Both legs must hold; SHADOW never places orders."""
    return RUN_MODE == "LIVE" and I_UNDERSTAND_LIVE == I_UNDERSTAND_LIVE_PHRASE


# ---------------------------------------------------------------------------
# WO-2026-07-25-L "F GETS THE BOOK; EVERYTHING ELSE EARNS IT" P1 — PER-LANE RUN
# MODE. The global kill (RUN_MODE/§B3) still rules everything; a per-lane mode
# can only RESTRICT below it, never widen it. One lane earned the book (F); it
# trades live. Every other lane keeps every rep — same signals, same custody,
# same swing records — but places NOTHING: SHADOW oids, simulated fills, cell
# outcomes tagged shadow. Ghosts until they graduate (the earn-back protocol, P3).
# ---------------------------------------------------------------------------
LANE_MODE = {
    # THE RULING, literal: F is the one lane that earned the book. Everything
    # else — including H8 (the WO's dict names only F) — earns it back from
    # shadow via the P3 protocol. "one lane earned the book, so it gets the book."
    "F": os.environ.get("LANE_MODE_F", "LIVE").upper(),
    "H8": os.environ.get("LANE_MODE_H8", "SHADOW").upper(),
    "FLIP": os.environ.get("LANE_MODE_FLIP", "SHADOW").upper(),
    "OPEN": os.environ.get("LANE_MODE_OPEN", "SHADOW").upper(),
    "HUNT": os.environ.get("LANE_MODE_HUNT", "SHADOW").upper(),
    "PAIR": os.environ.get("LANE_MODE_PAIR", "SHADOW").upper(),
    "D": os.environ.get("LANE_MODE_D", "SHADOW").upper(),
    "P": os.environ.get("LANE_MODE_P", "SHADOW").upper(),
}
LANE_MODE_DEFAULT = "SHADOW"   # any unlisted lane rehearses — conservative


def lane_is_live(lane: str) -> bool:
    """A lane places live orders ONLY when the global kill is off AND the lane's
    own mode is LIVE. Per-lane can only restrict below the global — a SHADOW
    global forces every lane to shadow regardless of LANE_MODE (born state)."""
    return (live_submit_enabled()
            and LANE_MODE.get(lane, LANE_MODE_DEFAULT) == "LIVE")


def lane_books_shadow(lane: str) -> bool:
    """Does this lane's outcome book to the SHADOW (rehearsal) ledger? Only
    meaningful inside a LIVE run, where F is live and the rest rehearse: a lane
    that is NOT live books shadow. In a global-SHADOW run every lane is one paper
    ledger (returns False for all) — the born-state behavior, tests untouched.
    So the shadow TAG marks the live-run mixed-mode split, never the global mode."""
    return live_submit_enabled() and not lane_is_live(lane)


def f_single_loss_bound_pct() -> float:
    """WO-L P2 — the CEO's single-loss bound: one full unsalvaged F loss at a
    ~97¢ favorite costs ≈ the dial × book (the position is ~all-of-clip at that
    price). Printed next to the worst-day math; the stated, accepted ceiling."""
    return F_NOTIONAL_PCT


# WO-2026-07-27-W W2a — the lanes whose loss geometry is ONE LARGE TAIL per many
# small wins (the F family: F and its 8-hour sibling H8). Their halt bound speaks
# tail units (f_halt_bound_c); every other lane is the desk (FLIP/OPEN/HUNT/D/P),
# whose loss is many small stops and whose bound stays rate_halt_drawdown_c.
TAIL_HALT_LANES = frozenset({"F", "H8"})


def f_halt_bound_c(tradeable_cents: int) -> int:
    """WO-2026-07-27-W W2a — F's rate-halt drawdown bound in F's OWN units:
    F_HALT_TAIL_MULT × one full-size F loss at the current dial. One full F loss
    at a ~97¢ favorite ≈ F_NOTIONAL_PCT × tradeable (the clip cost is the whole
    stake). At the 1.5 default a single tail (1.0×) sits UNDER the bound — it
    pages F_BIG_LOSS, never halts alone; a second tail (2.0×) crosses it and the
    room parks. Scales with tradeable (cash-truth, WO-W P1), so it tightens as
    the scrape banks exactly like the desk bound does."""
    return max(1, int(round(F_HALT_TAIL_MULT * F_NOTIONAL_PCT
                            * max(0, tradeable_cents))))


def lane_halt_bound_c(lane: str, tradeable_cents: int) -> int:
    """WO-2026-07-27-W W2a — the per-lane rate-halt drawdown bound. The F family
    (TAIL_HALT_LANES) speaks tail units; the desk keeps its stop-derived bound.
    One dispatch so _apply_streak_per_lane asks each lane in its own language."""
    if lane in TAIL_HALT_LANES:
        return f_halt_bound_c(tradeable_cents)
    return rate_halt_drawdown_c(tradeable_cents)
