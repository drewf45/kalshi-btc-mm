# bot.py
import os
import time
import json
import base64
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ----------------------------
# Logging
# ----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("kalshi-bot")

ET = ZoneInfo("America/New_York")

# ----------------------------
# Config
# ----------------------------
@dataclass
class Config:
    api_base: str
    api_key_id: str | None
    private_key_pem_base64: str | None
    poll_seconds: int
    series_prefix: str
    enable_trading: bool

    email_enabled: bool
    smtp_host: str | None
    smtp_port: int | None
    smtp_username: str | None
    smtp_password: str | None
    smtp_tls: bool

def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")

def env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None:
        return default
    try:
        return int(v.strip())
    except Exception:
        return default

def load_config() -> Config:
    api_base = os.getenv("KALSHI_API_BASE", "https://trading-api.kalshi.com").strip()
    # normalize: strip trailing slash
    api_base = api_base.rstrip("/")

    return Config(
        api_base=api_base,
        api_key_id=os.getenv("KALSHI_API_KEY_ID"),
        private_key_pem_base64=os.getenv("KALSHI_PRIVATE_KEY_PEM_BASE64"),
        poll_seconds=env_int("POLL_SECONDS", 60),
        series_prefix=os.getenv("SERIES_PREFIX", "KXBTC15M").strip(),
        enable_trading=env_bool("ENABLE_TRADING", False),

        email_enabled=env_bool("EMAIL_ENABLED", False),
        smtp_host=os.getenv("SMTP_HOST"),
        smtp_port=env_int("SMTP_PORT", 587) if os.getenv("SMTP_PORT") else None,
        smtp_username=os.getenv("SMTP_USERNAME"),
        smtp_password=os.getenv("SMTP_PASSWORD"),
        smtp_tls=env_bool("SMTP_TLS", True),
    )

# ----------------------------
# HTTP session with retries
# ----------------------------
def make_session() -> requests.Session:
    sess = requests.Session()
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST", "DELETE"),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)
    return sess

# ----------------------------
# Time helpers
# ----------------------------
def next_15m_boundary_et(now_et: datetime) -> datetime:
    """
    Return the next quarter-hour boundary strictly AFTER now_et.
    Example: 17:28 -> 17:30, 17:30:00 -> 17:45
    """
    # floor to minute
    now_et = now_et.replace(second=0, microsecond=0)
    minute = now_et.minute
    next_min = ((minute // 15) + 1) * 15
    if next_min >= 60:
        # bump hour
        dt = (now_et.replace(minute=0) + timedelta(hours=1))
    else:
        dt = now_et.replace(minute=next_min)
    return dt

def kalshi_datecode(dt_et: datetime) -> str:
    """
    Kalshi tickers in your logs look like:
    KXBTC15M-26JAN181730-30
      ^ series  ^ DDMMMYYHHMM
    Example: Jan 18 2026 17:30 ET -> 18JAN261730? No, your log shows 26JAN18...
    That indicates format is: DDMMMYYHHMM with DD=18, MMM=JAN, YY=26, HHMM=1730 => 18JAN261730.
    """
    dd = f"{dt_et.day:02d}"
    mmm = dt_et.strftime("%b").upper()  # JAN
    yy = f"{dt_et.year % 100:02d}"      # 26
    hhmm = dt_et.strftime("%H%M")       # 1730
    return f"{dd}{mmm}{yy}{hhmm}"

def primary_candidate_tickers(series: str, dt_et: datetime) -> list[str]:
    """
    Try a few common ticker variants.
    In your logs the resolved ticker included '-30' for 17:30.
    """
    code = kalshi_datecode(dt_et)               # 18JAN261730
    mm = f"{dt_et.minute:02d}"                  # 30
    # Common patterns we've seen:
    return [
        f"{series}-{code}",                     # KXBTC15M-18JAN261730
        f"{series}-{code}-{mm}",                # KXBTC15M-18JAN261730-30
    ]

# ----------------------------
# Auth helpers (READ-ONLY if key invalid)
# ----------------------------
def decode_private_key_pem(b64: str | None) -> str | None:
    if not b64:
        return None
    # remove whitespace/newlines that commonly break decoding
    compact = "".join(b64.split())
    try:
        pem_bytes = base64.b64decode(compact)
        pem = pem_bytes.decode("utf-8", errors="replace").strip()
        # quick sanity check
        if "BEGIN" not in pem or "PRIVATE KEY" not in pem:
            return None
        return pem
    except Exception:
        return None

def build_headers(cfg: Config) -> dict:
    # Kalshi trading API commonly uses key id + signature.
    # If your signing scheme differs, this bot will still run and resolve tickers (READ-ONLY mode).
    # We keep headers minimal here; market fetching does not require signature on some endpoints,
    # but trading definitely will.
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "kalshi-btc-mm/1.0",
    }
    return headers

# ----------------------------
# Kalshi API calls
# ----------------------------
class KalshiClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.sess = make_session()
        self.headers = build_headers(cfg)
        self.base_v2 = f"{cfg.api_base}/trade-api/v2"

    def get_market(self, ticker: str) -> dict | None:
        url = f"{self.base_v2}/markets/{ticker}"
        r = self.sess.get(url, headers=self.headers, timeout=15)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 404:
            return None
        raise RuntimeError(f"HTTP {r.status_code} {r.text}")

    def list_markets_open_for_series(self, series: str, limit: int = 200) -> list[dict]:
        url = f"{self.base_v2}/markets"
        params = {"limit": limit, "status": "open", "series_ticker": series}
        r = self.sess.get(url, headers=self.headers, params=params, timeout=20)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code} {r.text}")
        data = r.json()
        # common response key is "markets"
        return data.get("markets", []) if isinstance(data, dict) else []

def resolve_market_ticker(client: KalshiClient, series: str, dt_next_et: datetime) -> str:
    """
    Resolve the actual market ticker for the next 15m boundary.
    Strategy:
      1) Try direct known formats
      2) If 404, list open markets for the series and pick the one containing our datecode
    """
    candidates = primary_candidate_tickers(series, dt_next_et)
    for t in candidates:
        try:
            m = client.get_market(t)
            if m is not None:
                return t
        except Exception as e:
            log.warning(f"Direct market fetch failed for {t}: {repr(e)}")

    # Fallback: search open markets for series and match datecode substring
    code = kalshi_datecode(dt_next_et)  # e.g. 18JAN261730
    markets = client.list_markets_open_for_series(series=series, limit=200)

    matches = []
    for m in markets:
        tick = m.get("ticker") or ""
        if code in tick:
            matches.append(tick)

    if not matches:
        raise RuntimeError(f"Could not resolve a valid ticker for next 15m boundary ({dt_next_et.isoformat()}).")

    # Prefer the shortest ticker (often the canonical), else first.
    matches.sort(key=lambda x: (len(x), x))
    return matches[0]

# ----------------------------
# Main loop
# ----------------------------
def main():
    cfg = load_config()

    # hard fail if they configured the known-wrong host
    if "api.kalshi.com" in cfg.api_base:
        log.error("KALSHI_API_BASE is set to api.kalshi.com which does NOT resolve on Render. Use https://trading-api.kalshi.com")
        raise SystemExit(1)

    pem = decode_private_key_pem(cfg.private_key_pem_base64)
    if not cfg.api_key_id or not pem:
        log.warning("Kalshi credentials missing/invalid private key -> running in READ-ONLY mode")
        cfg.enable_trading = False

    log.info("=== BOT STARTED ===")
    log.info(f"ENABLE_TRADING={cfg.enable_trading}")
    log.info(f"POLL_SECONDS={cfg.poll_seconds}")
    log.info(f"SERIES_PREFIX={cfg.series_prefix}")
    log.info(f"API_BASE={cfg.api_base}")
    log.info(f"EMAIL_ENABLED={cfg.email_enabled}")

    client = KalshiClient(cfg)

    while True:
        try:
            now_et = datetime.now(ET)
            nxt = next_15m_boundary_et(now_et)

            ticker = resolve_market_ticker(client, cfg.series_prefix, nxt)

            log.info(
                f"Heartbeat ET now={now_et.strftime('%Y-%m-%d %H:%M:%S %Z')} | "
                f"next15={nxt.strftime('%Y-%m-%d %H:%M:%S %Z')} | "
                f"ticker={ticker}"
            )

            # NOTE: Place your strategy/trading logic here.
            # This code intentionally only resolves the ticker reliably and proves connectivity.

        except Exception as e:
            log.error(f"LOOP ERROR: {repr(e)}")

        time.sleep(cfg.poll_seconds)

if __name__ == "__main__":
    main()