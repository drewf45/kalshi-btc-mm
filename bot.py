import os
import time
import base64
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
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

def normalize_api_base(raw: str) -> str:
    """
    Render DNS cannot resolve api.kalshi.com.
    Force correct trading API host.
    """
    s = (raw or "").strip().rstrip("/")
    if not s:
        s = "https://trading-api.kalshi.com"

    # Self-heal common wrong values
    if "api.kalshi.com" in s:
        log.warning(f"API_BASE was set to '{s}' (bad DNS on Render). Rewriting to https://trading-api.kalshi.com")
        s = "https://trading-api.kalshi.com"

    return s

def load_config() -> Config:
    raw_api_base = os.getenv("KALSHI_API_BASE", "https://trading-api.kalshi.com")
    api_base = normalize_api_base(raw_api_base)

    return Config(
        api_base=api_base,
        api_key_id=os.getenv("KALSHI_API_KEY_ID"),
        private_key_pem_base64=os.getenv("KALSHI_PRIVATE_KEY_PEM_BASE64"),
        poll_seconds=env_int("POLL_SECONDS", 60),
        series_prefix=os.getenv("SERIES_PREFIX", "KXBTC15M").strip(),
        enable_trading=env_bool("ENABLE_TRADING", False),
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
    next quarter-hour strictly after now
    """
    now_et = now_et.replace(second=0, microsecond=0)
    minute = now_et.minute
    next_min = ((minute // 15) + 1) * 15
    if next_min >= 60:
        return (now_et.replace(minute=0) + timedelta(hours=1))
    return now_et.replace(minute=next_min)

def kalshi_datecode(dt_et: datetime) -> str:
    """
    Format: DDMMMYYHHMM  (18JAN261745)
    """
    dd = f"{dt_et.day:02d}"
    mmm = dt_et.strftime("%b").upper()
    yy = f"{dt_et.year % 100:02d}"
    hhmm = dt_et.strftime("%H%M")
    return f"{dd}{mmm}{yy}{hhmm}"

def primary_candidate_tickers(series: str, dt_et: datetime) -> list[str]:
    code = kalshi_datecode(dt_et)
    mm = f"{dt_et.minute:02d}"
    return [
        f"{series}-{code}",
        f"{series}-{code}-{mm}",
    ]

# ----------------------------
# Key sanity (read-only if invalid)
# ----------------------------
def decode_private_key_pem(b64: str | None) -> str | None:
    if not b64:
        return None
    compact = "".join(b64.split())
    try:
        pem_bytes = base64.b64decode(compact)
        pem = pem_bytes.decode("utf-8", errors="replace").strip()
        if "BEGIN" not in pem or "PRIVATE KEY" not in pem:
            return None
        return pem
    except Exception:
        return None

# ----------------------------
# Kalshi API client (public endpoints)
# ----------------------------
class KalshiClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.sess = make_session()
        self.headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "kalshi-btc-mm/1.0",
        }
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
        return data.get("markets", []) if isinstance(data, dict) else []

def resolve_market_ticker(client: KalshiClient, series: str, dt_next_et: datetime) -> str:
    candidates = primary_candidate_tickers(series, dt_next_et)

    for t in candidates:
        try:
            m = client.get_market(t)
            if m is not None:
                return t
        except Exception as e:
            log.warning(f"Direct market fetch failed for {t}: {repr(e)}")

    code = kalshi_datecode(dt_next_et)
    markets = client.list_markets_open_for_series(series=series, limit=200)
    matches = []

    for m in markets:
        tick = m.get("ticker") or ""
        if code in tick:
            matches.append(tick)

    if not matches:
        raise RuntimeError(f"Could not resolve ticker for next 15m boundary ({dt_next_et.isoformat()}).")

    matches.sort(key=lambda x: (len(x), x))
    return matches[0]

# ----------------------------
# Main loop
# ----------------------------
def main():
    cfg = load_config()

    # Print ONLY safe runtime info
    log.info("=== BOT STARTED ===")
    log.info(f"ENABLE_TRADING={cfg.enable_trading}")
    log.info(f"POLL_SECONDS={cfg.poll_seconds}")
    log.info(f"SERIES_PREFIX={cfg.series_prefix}")
    log.info(f"API_BASE={cfg.api_base}")

    pem = decode_private_key_pem(cfg.private_key_pem_base64)
    if not cfg.api_key_id or not pem:
        log.warning("Kalshi credentials missing/invalid private key -> running in READ-ONLY mode")
        cfg.enable_trading = False

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

            # Strategy/trading logic goes here (kept separate intentionally)

        except Exception as e:
            log.error(f"LOOP ERROR: {repr(e)}")

        time.sleep(cfg.poll_seconds)

if __name__ == "__main__":
    main()