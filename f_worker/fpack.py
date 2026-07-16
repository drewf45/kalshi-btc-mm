# f_worker/fpack.py
# The hourly pack (§5) + per-window DONE lines (those are emitted by the manager at
# book time). The pack is broker-truth beside ledger-truth: divergence is an alert,
# never a silent correction. Kept to <=30 lines of output (acceptance §9).

import threading
import time
from datetime import datetime, timezone
from typing import Any, List, Optional

from .config import Config
from .ledger import Ledger


def _rss_mb() -> Optional[float]:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024.0, 1)
    except Exception:
        return None
    return None


def _day_start_ts(now: Optional[float] = None) -> float:
    now = now if now is not None else time.time()
    dt = datetime.fromtimestamp(now, tz=timezone.utc)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def render_pack(ledger: Ledger, cfg: Config, client: Any = None,
                now: Optional[float] = None) -> str:
    since = _day_start_ts(now)
    counts = ledger.state_counts(since)
    day_net = ledger.day_net_cents(since)
    rung, lots = ledger.current_rung()
    halted, hreason = ledger.halt_state()

    entered = sum(v for k, v in counts.items() if k != "sat_out")
    sat = counts.get("sat_out", 0)

    lines: List[str] = ["🅵 📦 hourly pack"]
    lines.append(f"windows: entered {entered} · sat_out {sat}")
    if counts:
        lines.append("DONE-states: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    lines.append(f"day P&L (net): {day_net:+d}¢  (${day_net/100.0:+.2f})")

    # broker-truth beside ledger-truth
    if client is not None:
        try:
            avail, total = client.get_balance_usd()
            lines.append(f"account (broker): avail=${avail} total=${total}")
            if total is None:
                lines.append("⚠️ reconcile: broker balance unreadable")
        except Exception as e:
            lines.append(f"⚠️ reconcile: broker read failed ({e})")

    # lived flip performance over a trailing week — the synthetic priors get replaced by
    # what the tape actually did (F1.5)
    lived = ledger.realized_flip_stats((now or _now()) - 7 * 86400)
    lines.append(f"lived flip rate (7d): {lived['bundle']}/{lived['entered']} entered "
                 f"= {lived['flip_rate']:.0%}")

    lines.append(f"size ladder: rung {rung} ({lots} lot/side)")
    lines.append(f"halt: {'SET — ' + hreason if halted else 'clear'}")
    lines.append(f"api budget: {cfg.req_per_min}/min cap")
    rss = _rss_mb()
    if rss is not None:
        lines.append(f"mem: {rss} MB RSS")
    return "\n".join(lines[:30])


def _now() -> float:
    return time.time()


class PackScheduler(threading.Thread):
    """Fires render_pack once an hour (§5). Also sends the boot echo on start."""

    def __init__(self, ledger: Ledger, cfg: Config, notifier: Any, client: Any = None,
                 interval_sec: float = 3600.0):
        super().__init__(name="pack", daemon=True)
        self.ledger = ledger
        self.cfg = cfg
        self.notifier = notifier
        self.client = client
        self.interval = interval_sec
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        # align to the top of the next hour, then every hour
        while not self._stop.is_set():
            if self._stop.wait(self.interval):
                break
            try:
                if self.notifier:
                    self.notifier.send(render_pack(self.ledger, self.cfg, self.client))
            except Exception as e:
                print(f"[pack] render error: {e}", flush=True)
