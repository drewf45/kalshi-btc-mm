"""Chunk 1 — environment validation. Fail loud on any missing or bad config."""

import os
import time
import logging

log = logging.getLogger("k_worker.envcheck")

REQUIRED_VARS = [
    "KALSHI_API_KEY_ID",
    "KALSHI_PRIVATE_KEY_PEM_BASE64",
    "KALSHI_ENV",
    "K_WORKER_MODE",
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

    worker_mode = vals["K_WORKER_MODE"].lower()
    if worker_mode not in ("observe", "trade"):
        raise RuntimeError(f"FATAL: K_WORKER_MODE must be 'observe' or 'trade', got '{worker_mode}'")

    vals["_env"] = kalshi_env
    vals["_is_live"] = kalshi_env == "live"
    vals["_mode"] = worker_mode

    return vals


def check_clock_skew(max_skew_seconds: float = 2.0) -> None:
    """Check clock skew using Kalshi's Date response header. Raise if >2s."""
    import requests
    from email.utils import parsedate_to_datetime
    kalshi_env = os.environ.get("KALSHI_ENV", "demo").lower()
    if kalshi_env == "live":
        api_base = os.environ.get("KALSHI_API_BASE", "https://external-api.kalshi.com")
    else:
        api_base = os.environ.get("KALSHI_API_BASE", "https://external-api.demo.kalshi.co")
    try:
        resp = requests.get(f"{api_base}/trade-api/v2/exchange/status", timeout=5)
        resp.raise_for_status()
        date_header = resp.headers.get("Date")
        if not date_header:
            log.warning("[CLOCK] No Date header in exchange response — skipping skew check")
            return
        server_dt = parsedate_to_datetime(date_header)
        server_ts = server_dt.timestamp()
        local_ts = time.time()
        skew = abs(server_ts - local_ts)
        log.info(f"[CLOCK] server={server_ts:.1f} local={local_ts:.1f} skew={skew:.2f}s")
        if skew > max_skew_seconds:
            raise RuntimeError(
                f"FATAL: Clock skew {skew:.2f}s exceeds {max_skew_seconds}s — "
                f"signatures will be rejected"
            )
    except RuntimeError:
        raise
    except Exception as e:
        log.warning(f"[CLOCK] Skew check failed (non-fatal): {e}")
