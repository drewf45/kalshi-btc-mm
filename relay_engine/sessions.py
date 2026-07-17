"""Shared session windows — canonical source for all session tagging."""
import hashlib
from datetime import datetime
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")

SESSION_WINDOWS = [
    ("ASIA",        20, 0,  2, 29),
    ("LONDON_OPEN",  2, 30, 4, 0),
    ("EU",           4, 0,  8, 0),
    ("NY_PRE",       8, 0,  9, 29),
    ("NY_OPEN",      9, 30, 10, 30),
    ("NY",          10, 30, 15, 29),
    ("NY_CLOSE",    15, 30, 16, 30),
    ("EVENING",     16, 30, 20, 0),
]


def session_tag(ts: float) -> str:
    """Map a Unix timestamp to its session tag (ET-aware, handles DST)."""
    dt = datetime.fromtimestamp(ts, tz=NY)
    t = dt.hour * 60 + dt.minute
    for name, sh, sm, eh, em in SESSION_WINDOWS:
        start = sh * 60 + sm
        end = eh * 60 + em
        if start <= end:
            if start <= t < end:
                return name
        else:
            if t >= start or t < end:
                return name
    return "UNKNOWN"


def session_hash() -> str:
    """Deterministic hash of the session windows table for A5 parity check."""
    raw = repr(SESSION_WINDOWS).encode()
    return hashlib.sha256(raw).hexdigest()[:16]
