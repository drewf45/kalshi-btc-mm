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
KELLY_FRACTION_CEILING = 1.0 / 12.0
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
