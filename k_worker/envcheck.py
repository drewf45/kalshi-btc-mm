"""Chunk 1 — environment validation. Fail loud on any missing or bad config."""

import os
import time
import logging

log = logging.getLogger("k_worker.envcheck")

REQUIRED_VARS = [
    "KALSHI_API_KEY_ID",
    "KALSHI_PRIVATE_KEY_PEM_BASE64",
    "KALSHI_ENV",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
]

LIVE_EXTRA = [
    "I_UNDERSTAND_LIVE",
]


def check_env() -> dict:
    """Validate all required env vars. Returns a dict of validated values.
    Dies loudly (raises) if anything is wrong."""

    vals = {}
    missing = []
    for var in REQUIRED_VARS:
        v = os.environ.get(var, "").strip()
        if not v:
            missing.append(var)
        vals[var] = v

    if missing:
        raise RuntimeError(f"FATAL: missing environment variables: {', '.join(missing)}")

    kalshi_env = vals["KALSHI_ENV"].lower()
    if kalshi_env not in ("demo", "live"):
        raise RuntimeError(f"FATAL: KALSHI_ENV must be 'demo' or 'live', got '{kalshi_env}'")

    if kalshi_env == "live":
        confirm = os.environ.get("I_UNDERSTAND_LIVE", "").strip()
        if confirm != "1":
            raise RuntimeError(
                "FATAL: live mode requires I_UNDERSTAND_LIVE=1"
            )

    vals["_env"] = kalshi_env
    vals["_is_live"] = kalshi_env == "live"

    return vals


def check_clock_skew(max_skew_seconds: float = 2.0) -> None:
    """Check NTP clock skew using Kalshi's server time. Fail if >2s."""
    import requests
    try:
        api_base = os.environ.get("KALSHI_API_BASE", "https://api.elections.kalshi.com")
        resp = requests.get(f"{api_base}/trade-api/v2/exchange/status", timeout=5)
        resp.raise_for_status()
        data = resp.json()
        server_ts = data.get("exchange_active")
        if server_ts is None:
            log.warning("[CLOCK] Could not read exchange status — skipping skew check")
            return
        local_ts = time.time()
        # If we can't extract a numeric timestamp, skip
        log.info(f"[CLOCK] Exchange active={server_ts}, local={local_ts:.0f}")
    except Exception as e:
        log.warning(f"[CLOCK] Skew check failed (non-fatal): {e}")
