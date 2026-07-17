"""Truth-feed adapters — NWS primary, Open-Meteo second source."""

import time
import logging
from dataclasses import dataclass
from typing import Optional

import os

import requests

log = logging.getLogger("d_worker.feeds")

_NWS_CONTACT = os.environ.get("DW_NWS_CONTACT", "kal-d-worker")
_SESSION = requests.Session()
_SESSION.headers["User-Agent"] = f"(kal-d-worker, {_NWS_CONTACT})"

NWS_BASE = "https://api.weather.gov"
OMETEO_BASE = "https://api.open-meteo.com/v1/forecast"


@dataclass
class Observation:
    value: float
    ts: float
    source: str
    raw: dict
    units: str = ""


def _nws_value(prop: dict, key: str) -> Optional[float]:
    """Extract a numeric value from NWS properties, converting C→F if needed."""
    v = prop.get(key, {})
    if not isinstance(v, dict):
        return None
    val = v.get("value")
    if val is None:
        return None
    unit = v.get("unitCode", "")
    if "degC" in unit or "celsius" in unit.lower():
        return round(val * 9 / 5 + 32, 1)
    return float(val)


def observe_nws(station_id: str, field: str = "temperature") -> Optional[Observation]:
    """Fetch latest observation from NWS for a station.
    field: 'temperature', 'maxTemperatureLast24Hours', etc."""
    try:
        url = f"{NWS_BASE}/stations/{station_id}/observations/latest"
        resp = _SESSION.get(url, timeout=10)
        if resp.status_code != 200:
            log.warning(f"[FEEDS] NWS {station_id}: HTTP {resp.status_code}")
            return None
        data = resp.json()
        props = data.get("properties", {})
        val = _nws_value(props, field)
        if val is None:
            return None
        ts_str = props.get("timestamp", "")
        obs_ts = time.time()
        if ts_str:
            from datetime import datetime
            try:
                obs_ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
            except Exception:
                pass
        return Observation(value=val, ts=obs_ts, source=f"NWS:{station_id}",
                           raw=props, units="F")
    except Exception as e:
        log.warning(f"[FEEDS] NWS {station_id} error: {e}")
        return None


def observe_open_meteo(lat: float, lon: float,
                       field: str = "temperature_2m") -> Optional[Observation]:
    """Fetch current weather from Open-Meteo."""
    try:
        params = {
            "latitude": lat, "longitude": lon,
            "current": field,
            "temperature_unit": "fahrenheit",
            "timezone": "UTC",
        }
        resp = _SESSION.get(OMETEO_BASE, params=params, timeout=10)
        if resp.status_code != 200:
            log.warning(f"[FEEDS] Open-Meteo: HTTP {resp.status_code}")
            return None
        data = resp.json()
        current = data.get("current", {})
        val = current.get(field)
        if val is None:
            return None
        ts_str = current.get("time", "")
        obs_ts = time.time()
        if ts_str:
            from datetime import datetime, timezone
            try:
                dt = datetime.fromisoformat(ts_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                obs_ts = dt.timestamp()
            except Exception:
                pass
        return Observation(value=float(val), ts=obs_ts,
                           source=f"OpenMeteo:{lat},{lon}",
                           raw=current, units="F")
    except Exception as e:
        log.warning(f"[FEEDS] Open-Meteo error: {e}")
        return None


def staleness(obs: Optional[Observation], max_age: float) -> float:
    """Seconds since observation. Returns float('inf') if obs is None."""
    if obs is None:
        return float("inf")
    return time.time() - obs.ts


def feeds_agree(obs1: Optional[Observation], obs2: Optional[Observation],
                rounding_unit: float) -> Optional[bool]:
    """True if both feeds agree within one rounding unit. None if either is missing."""
    if obs1 is None or obs2 is None:
        return None
    return abs(obs1.value - obs2.value) <= rounding_unit
