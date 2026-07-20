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
# P19 "SALVAGE" — the custodian earns Lane F. Needle-collapse exits for the
# hold-to-settlement lanes; K is tuned from the DODGED_LOSS vs SALVAGE_REGRET
# curve on Saturdays, never from a bad night.
# ---------------------------------------------------------------------------
SALVAGE_K_POINTS = 15.0    # needle collapse: p_held − p_entry <= −K, 2 ticks
SALVAGE_S_CENTS = 10.0     # AND fair_held < entry − S
SALVAGE_R_S = 10.0         # maker attempt unfilled for R -> crossfire at best
SALVAGE_T_FLOOR_S = 15.0   # never salvage inside the floor (endgame is F's)
# SALV-1 §2.3 (Adversary): flap guard — logged gag TRANSITIONS per position
# per window cap here; the settlement summary still counts every tick.
SALVAGE_GAG_MAX_TRANSITIONS = 12

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
OPEN_BAND = (39, 56)              # setup: BOTH sides in the open band
OPEN_MIN_GRAIN = 2                # streak >= 2 or pass (OPEN_NO_GRAIN)
OPEN_MAX_ENTRY_CENTS = 49         # maker join grain-side <= this
# P-FLIP-THESIS-1 §3 (DREW-RULED 2026-07-19, was 5): the scalp target is
# the ~20c swing, not a 5c nibble — the nibble couldn't outrun fees+tails
# (2c taker fee ate 40% of the old edge). A delta-table-derived target
# (spot's reachable move in time left) is routed to measurement before it
# replaces the constant.
OPEN_TAKE_CENTS = 20
OPEN_UNDETERMINED_BAND = (35, 65)  # inside it: NO stop, NO scratch, NO box
OPEN_DETERMINED_K_POINTS = 15.0   # ΔP-collapse >= K sustained = math changed
# WO-FLIP-EXIT-DOCTRINE (build 40): determined-against is a statement about
# the MARKET'S DECISION (spot moved / time ran out), NOT the contract's
# price. The band floor (35c) marks where SWINGS happen — a cheap entry is
# bought to oscillate there, so the price cut must live BELOW the swing, at
# a genuine catastrophe. A FIXED low price (P&L-blind, not basis-anchored —
# two positions with the same book state get the same decision) is the ONLY
# price backstop that acts inside patience; spot+time are the primary cut.
OPEN_CATASTROPHE_FLOOR = 20        # DREW-DEFAULT: fixed price backstop, well below the swing band
# OPEN_BAIL_R_S retired (P26 §3.2): evacuations cross IMMEDIATELY — the
# determined-maker grace was tonight's 31/20/33 slide. TAKE alone rests.

# ---------------------------------------------------------------------------
# Safe defaults in force (A3 / Chunk 0.3)
# ---------------------------------------------------------------------------
ONE_LOT_MAX_LOSS_CENTS = 99  # worst-case loss on a single 1-lot maker entry (price -> 0)
AT_RISK_CAP_MULT = 3  # DREW-DEFAULT: $-at-risk per settlement event = 3x one-lot max loss
AT_RISK_CAP_CENTS = AT_RISK_CAP_MULT * ONE_LOT_MAX_LOSS_CENTS
LANE_D_FLOOR_CENTS = 60  # DREW-DEFAULT: Lane D band floor, pending Chunk 2 data (50c vs 60c open)
DEPTH_FRACTION = 0.25  # DREW-DEFAULT: per-level size <= 25% of visible depth

# ---------------------------------------------------------------------------
# Walls (C.3 / BUILD_SEQUENCE 3.2)
# ---------------------------------------------------------------------------
NET_RISK_CROSS_LANE_CAP = 3  # net contracts at risk per settlement event, across lanes

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

# ---------------------------------------------------------------------------
# A-PLAYER doctrine (banked 2026-07-19): the machine runs untouched
# overnight; only a loss-RATE stops trading; a single loss is noise.
# ---------------------------------------------------------------------------
# B2 (DREW-DEFAULT ≤5c suggested): a cash delta at or under this magnitude
# is NOISE — silently re-baselined and logged, never prompted, never
# halted (the hourly-confirmation tax killed the overnight book). Above
# it, the WO-CASH-FATAL-1 genuine-dispute path stands untouched.
CASH_SILENT_REBASE_CENTS = 5
# B3 (DREW-DEFAULT): the halt is a RATE — N losing markets of the last M
# settled traded markets (per-market broker P&L is the unit). One loss
# NEVER halts; two-in-a-row was never a reliable signal, 2-of-4 is.
RATE_HALT_LOSSES = 2
RATE_HALT_WINDOW = 4
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
FLIP_SIZE_CAP = 1                 # DREW-RULED: 1-lot cap while miscalibrated
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


def drew_defaults() -> dict:
    """The DREW-DEFAULT constants, for the boot tape (printed until ruled)."""
    return {
        "at_risk_cap_per_settlement_event": f"{AT_RISK_CAP_MULT}x one-lot max loss = {AT_RISK_CAP_CENTS}c",
        "lane_d_floor": f"{LANE_D_FLOOR_CENTS}c (pending Chunk 2 data)",
        "depth_fraction": f"{DEPTH_FRACTION:.0%}",
    }


def live_submit_enabled() -> bool:
    """The hard-disable pattern (§B3). Both legs must hold; SHADOW never places orders."""
    return RUN_MODE == "LIVE" and I_UNDERSTAND_LIVE == I_UNDERSTAND_LIVE_PHRASE
