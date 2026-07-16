# f_worker/config.py
# FLIPDESK configuration + FATAL-loud env validation (fenvcheck).
#
# One package, one job. Every knob here is either FATAL-checked (the desk refuses
# to boot without it) or defaulted per BUILD ORDER §8. Names keep the DW_ prefix
# from the retired weather desk on purpose: Drew's rulings are written against those
# names and the size ladder + halt patterns are borrowed verbatim.

import os
import sys
from dataclasses import dataclass, field
from typing import List, Optional


# -----------------------------
# Env helpers (borrowed style from bot.py)
# -----------------------------
def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return default
    return int(v)


def env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return default
    return float(v)


def getenv_first(keys: List[str], default: str = "") -> str:
    for k in keys:
        v = os.getenv(k)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return default


class FatalConfigError(RuntimeError):
    """Raised when the desk cannot legally boot. Loud + fatal (BUILD ORDER §2)."""


@dataclass
class Config:
    # --- Kalshi (FATAL-checked) ---
    api_base: str = "https://api.elections.kalshi.com"
    api_prefix: str = "/trade-api/v2"
    api_key_id: str = ""
    private_key_pem_b64: str = ""

    series_ticker: str = "KXBTC15M"

    # --- Telegram (soft: missing => notify is a no-op, warned at boot) ---
    tg_token: str = ""
    tg_chat_id: str = ""

    # --- THE LAWS AS NUMBERS (§8) ---
    entry_line: int = 99          # DW_ENTRY_LINE  (W1: combined bundle cost <= this)
    single_leg_max: int = 49      # DW_SINGLE_LEG_MAX (W2: lone leg only if fill <= this)
    flip_x: int = 6               # DW_FLIP_X (flip ask at entry + X per side)
    entry_window_sec: int = 60    # DW_ENTRY_WINDOW_SEC (one entry phase length)
    flat_at_t: int = 90           # DW_FLAT_AT_T (W4: flat-out at T-90s)
    stop_cents: int = 25          # DW_STOP_CENTS (a window "stopped" if it loses >= this)
    pause_after_stops: int = 2    # DW_PAUSE_AFTER_STOPS (W7: two consecutive stops => halt)
    lots: int = 1                 # DW_LOTS (ladder-owned; boot rung is 1)
    req_per_min: int = 30         # DW_REQ_PER_MIN (api budget)

    # --- Discovery / loop timing ---
    entry_start_lead_sec: int = 600   # begin seeking this many secs before close
    poll_seconds: float = 1.0
    fill_wait_seconds: int = 30       # how long a resting entry bid waits inside the phase

    # --- Halt clearance (W7) ---
    clear_halt: bool = False          # DW_CLEAR_HALT=1 boot clears a standing flip_halt

    # --- Files ---
    db_path: str = "flipdesk.db"
    lock_path: str = "/tmp/f_worker.lock"

    # --- Behaviour flags ---
    dry_run: bool = False             # DRY_RUN=1 => gateway logs orders, never submits

    # Non-fatal warnings accumulated during load (echoed at boot)
    warnings: List[str] = field(default_factory=list)

    # ---- derived ----
    @property
    def flip_target_lo(self) -> int:
        """Target flip is entry+5..7c/side (§ targets). flip_x is the boot default."""
        return 5

    @property
    def flip_target_hi(self) -> int:
        return 7

    def echo_lines(self) -> List[str]:
        """Config echo for BOOT telegram (§5)."""
        return [
            "🅵 FLIPDESK boot — one shot per window, live from the first bell",
            f"entry_line={self.entry_line}c  single_leg_max={self.single_leg_max}c  flip_x={self.flip_x}c",
            f"entry_window={self.entry_window_sec}s  flat_at_T-{self.flat_at_t}s  stop={self.stop_cents}c",
            f"lots={self.lots} (rung1)  pause_after_stops={self.pause_after_stops}  req/min={self.req_per_min}",
            f"series={self.series_ticker}  dry_run={self.dry_run}  clear_halt={self.clear_halt}",
        ]


def load_config() -> Config:
    """Build a Config from the environment. Non-fatal; validation is fenvcheck()."""
    c = Config()
    c.api_base = getenv_first(["KALSHI_API_BASE"], c.api_base).rstrip("/")
    c.api_prefix = getenv_first(["KALSHI_API_PREFIX"], c.api_prefix).rstrip("/")
    c.api_key_id = getenv_first(["KALSHI_API_KEY_ID"], "")
    c.private_key_pem_b64 = getenv_first(["KALSHI_PRIVATE_KEY_PEM_BASE64"], "")
    c.series_ticker = getenv_first(["SERIES", "KALSHI_SERIES", "KALSHI_SERIES_TICKER"], c.series_ticker)

    c.tg_token = getenv_first(["TELEGRAM_BOT_TOKEN", "TELEGRAM_TOKEN"], "")
    c.tg_chat_id = getenv_first(["TELEGRAM_CHAT_ID", "TELEGRAM_CHAT"], "")

    c.entry_line = env_int("DW_ENTRY_LINE", c.entry_line)
    c.single_leg_max = env_int("DW_SINGLE_LEG_MAX", c.single_leg_max)
    c.flip_x = env_int("DW_FLIP_X", c.flip_x)
    c.entry_window_sec = env_int("DW_ENTRY_WINDOW_SEC", c.entry_window_sec)
    c.flat_at_t = env_int("DW_FLAT_AT_T", c.flat_at_t)
    c.stop_cents = env_int("DW_STOP_CENTS", c.stop_cents)
    c.pause_after_stops = env_int("DW_PAUSE_AFTER_STOPS", c.pause_after_stops)
    c.lots = env_int("DW_LOTS", c.lots)
    c.req_per_min = env_int("DW_REQ_PER_MIN", c.req_per_min)

    c.entry_start_lead_sec = env_int("DW_ENTRY_START_LEAD_SEC", c.entry_start_lead_sec)
    c.poll_seconds = env_float("POLL_SECONDS", c.poll_seconds)
    c.fill_wait_seconds = env_int("DW_FILL_WAIT_SEC", c.fill_wait_seconds)

    c.clear_halt = env_bool("DW_CLEAR_HALT", False)
    c.db_path = getenv_first(["FLIPDESK_DB"], c.db_path)
    c.lock_path = getenv_first(["FLIPDESK_LOCK"], c.lock_path)
    c.dry_run = env_bool("DRY_RUN", False)
    return c


def fenvcheck(c: Config) -> Config:
    """FATAL-loud validation (BUILD ORDER §2 / borrow pattern).

    Prints every problem, then raises FatalConfigError if any are fatal. Soft issues
    (no Telegram) are appended to c.warnings and echoed, not raised.
    """
    fatal: List[str] = []

    if not c.api_key_id:
        fatal.append("KALSHI_API_KEY_ID is missing")
    if not c.private_key_pem_b64:
        fatal.append("KALSHI_PRIVATE_KEY_PEM_BASE64 is missing")

    # Sanity walls on the laws themselves — a mis-set env must not silently widen risk.
    if not (1 <= c.entry_line <= 100):
        fatal.append(f"DW_ENTRY_LINE={c.entry_line} out of 1..100")
    if not (1 <= c.single_leg_max <= 99):
        fatal.append(f"DW_SINGLE_LEG_MAX={c.single_leg_max} out of 1..99")
    if c.single_leg_max >= c.entry_line:
        fatal.append(
            f"DW_SINGLE_LEG_MAX ({c.single_leg_max}) must be < DW_ENTRY_LINE ({c.entry_line})"
        )
    if not (0 <= c.flip_x <= 50):
        fatal.append(f"DW_FLIP_X={c.flip_x} out of 0..50")
    if c.lots < 1:
        fatal.append(f"DW_LOTS={c.lots} must be >= 1")
    if c.flat_at_t < 0:
        fatal.append(f"DW_FLAT_AT_T={c.flat_at_t} must be >= 0")
    if c.pause_after_stops < 1:
        fatal.append(f"DW_PAUSE_AFTER_STOPS={c.pause_after_stops} must be >= 1")

    if not c.tg_token or not c.tg_chat_id:
        c.warnings.append("TELEGRAM_* not fully set — notify() will be a silent no-op")

    if c.dry_run:
        c.warnings.append("DRY_RUN=1 — gateway will log orders but never submit")

    for w in c.warnings:
        print(f"FATAL-CHECK WARN: {w}", file=sys.stderr, flush=True)

    if fatal:
        for f in fatal:
            print(f"FATAL: {f}", file=sys.stderr, flush=True)
        raise FatalConfigError("; ".join(fatal))

    return c
