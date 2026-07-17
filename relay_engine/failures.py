"""The Failure Ledger — R5, banked as law (WO-P6 §3).

    "A failure that didn't write why/how/what/when before raising is
     itself a failure."

ONE funnel: `fail(tag, what, fatal=..., **how)` →
  1. writes the failures row (why_tag, what, how_json, where, run_mode, boot_id)
  2. alerts Telegram — FATAL always; WARN-class throttled WITH COUNTS
  3. THEN raises FatalIntegrityError for fatal-class

Every raise of FatalIntegrityError in the tree routes through here
(grep-enforced by tests/test_p6_failures.py). Failures raised before the
funnel is configured are BUFFERED and flushed at configure — early-boot
evidence is not lost.

Adversary knob: the funnel itself must never fail the engine — ledger-write
and Telegram-send errors log-and-continue (a broken pager can't crash the
patient).

Win/loss path symmetry: the funnel doesn't know or care whether the failure
happened on a winning or losing position — the row is the row.
"""

import inspect
import json
import logging
import time
from typing import Optional

from .errors import FatalIntegrityError

log = logging.getLogger("relay.failures")

FAILURES_SCHEMA = """
CREATE TABLE IF NOT EXISTS failures (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    why_tag TEXT NOT NULL,
    what TEXT NOT NULL,
    how_json TEXT NOT NULL DEFAULT '{}',
    where_src TEXT NOT NULL DEFAULT '',
    run_mode TEXT NOT NULL DEFAULT '',
    boot_id INTEGER NOT NULL DEFAULT 0
);
"""

WARN_THROTTLE_S = 300.0  # WARN-class alerts at most once per tag per 5 min, with counts

_ledger = None
_alert_fn = None
_run_mode = ""
_boot_id = 0
_buffer: list = []
_warn_last: dict = {}   # tag -> (last_alert_ts, suppressed_count)


def configure(ledger, alert_fn=None, run_mode: str = "", boot_id: int = 0) -> None:
    """Wire the funnel. Buffered pre-boot failures flush into the table now."""
    global _ledger, _alert_fn, _run_mode, _boot_id
    _ledger = ledger
    _alert_fn = alert_fn
    _run_mode = run_mode
    _boot_id = boot_id
    try:
        ledger.db.executescript(FAILURES_SCHEMA)
        ledger.db.commit()
        for row in _buffer:
            _write(*row)
        _buffer.clear()
    except Exception as e:  # the funnel never fails the engine
        log.error("failure-ledger configure error (continuing): %s", e)


def _write(ts, tag, what, how_json, where_src):
    _ledger.db.execute(
        "INSERT INTO failures (ts, why_tag, what, how_json, where_src, run_mode, boot_id)"
        " VALUES (?,?,?,?,?,?,?)",
        (ts, tag, what, how_json, where_src, _run_mode, _boot_id))
    _ledger.db.commit()


def fail(tag: str, what: str, fatal: bool = False, **how) -> None:
    """THE funnel. Writes the row, alerts, then raises iff fatal-class."""
    ts = time.time()
    caller = inspect.stack()[1]
    where_src = f"{caller.filename.rsplit('/', 1)[-1]}:{caller.lineno}"
    try:
        how_json = json.dumps(how, default=str)[:4000]
    except Exception:
        how_json = "{}"

    try:
        if _ledger is not None:
            _write(ts, tag, what, how_json, where_src)
        else:
            _buffer.append((ts, tag, what, how_json, where_src))
    except Exception as e:
        log.error("failure-ledger write error (continuing): %s", e)

    try:
        if _alert_fn is not None:
            if fatal:
                _alert_fn(f"⛔ FATAL [{tag}] {what}")
            else:
                last, suppressed = _warn_last.get(tag, (0.0, 0))
                if ts - last >= WARN_THROTTLE_S:
                    extra = f" (+{suppressed} suppressed)" if suppressed else ""
                    _alert_fn(f"⚠ [{tag}] {what}{extra}")
                    _warn_last[tag] = (ts, 0)
                else:
                    _warn_last[tag] = (last, suppressed + 1)
    except Exception as e:
        log.error("failure alert error (continuing): %s", e)

    if fatal:
        raise FatalIntegrityError(f"[{tag}] {what}")
    log.warning("[%s] %s %s", tag, what, how_json)


def pack_section(ledger) -> list:
    """The daily pack's FAILURES section: count by tag, first/last seen."""
    try:
        rows = ledger.db.execute(
            "SELECT why_tag, COUNT(*), MIN(ts), MAX(ts) FROM failures"
            " GROUP BY why_tag ORDER BY COUNT(*) DESC").fetchall()
    except Exception:
        return []
    lines = []
    for tag, n, first, last_seen in rows:
        lines.append(f"  {tag}: x{n} (first {time.strftime('%H:%M', time.gmtime(first))}"
                     f" last {time.strftime('%H:%M', time.gmtime(last_seen))} UTC)")
    return lines
