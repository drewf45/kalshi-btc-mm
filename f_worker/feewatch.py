# f_worker/feewatch.py
# F1.3 — FEE TRIPWIRE (P8 port).
#
# Every flip's economics assume $0 maker fees and the 0.07 taker roundup on this series
# — true today, on tape. If Kalshi revises the series fee schedule, the desk's math
# rots SILENTLY. The tripwire denies that: it caches a fingerprint of the market's
# fee-relevant fields at boot, re-checks periodically, and on ANY change trips
# flip_halt + alerts. The desk's economics are allowed to die loudly, never quietly.

import hashlib
import json
import threading
import time
from typing import Any, Dict, Optional

from .config import Config

# Fee-relevant fields we fingerprint. Kalshi has used several names across versions;
# we hash whichever are present so a rename OR a value change trips the wire.
_FEE_KEYS = [
    "maker_fee", "taker_fee", "maker_fee_rate", "taker_fee_rate",
    "fee_rate", "trading_fee", "fee_type", "fee_multiplier", "fee_waived",
]


def fee_fingerprint(market_obj: Dict[str, Any]) -> str:
    present = {k: market_obj.get(k) for k in _FEE_KEYS if k in market_obj}
    blob = json.dumps(present, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16] + f":{len(present)}"


class FeeTripwire:
    def __init__(self, client: Any, ledger: Any, config: Config, notifier: Any = None):
        self.client = client
        self.ledger = ledger
        self.cfg = config
        self.notifier = notifier

    def capture(self, market_ticker: str) -> Optional[str]:
        """Record the current fee fingerprint as the baseline (boot)."""
        try:
            mkt = self.client.get_market(market_ticker)
        except Exception as e:
            print(f"[feewatch] capture failed: {e}", flush=True)
            return None
        fp = fee_fingerprint(mkt)
        self.ledger.record_fee_fingerprint(fp, detail={k: mkt.get(k) for k in _FEE_KEYS if k in mkt})
        print(f"[feewatch] baseline fee fingerprint {fp}", flush=True)
        return fp

    def check(self, market_ticker: str) -> bool:
        """Compare the live fingerprint to the last baseline. On change: halt + alert.
        Returns True if a change was detected (and the desk halted)."""
        baseline = self.ledger.last_fee_fingerprint()
        try:
            mkt = self.client.get_market(market_ticker)
        except Exception as e:
            # couldn't read the schedule this cycle — say so; do NOT infer "unchanged".
            print(f"[feewatch] check fetch failed ({e}); will retry next cycle", flush=True)
            if self.notifier:
                self.notifier.alert(f"fee tripwire could not read {market_ticker} fees: {e}")
            return False
        fp = fee_fingerprint(mkt)
        if baseline is None:
            self.ledger.record_fee_fingerprint(fp)
            return False
        if fp != baseline:
            self.ledger.record_fee_fingerprint(fp, detail={"changed_from": baseline})
            self.ledger.set_halt(True, "fee schedule changed (tripwire)")
            if self.notifier:
                self.notifier.alert(f"🚨 FEE SCHEDULE CHANGED on {self.cfg.series_ticker} "
                                    f"({baseline} -> {fp}). flip_halt SET — the desk's "
                                    f"math must be re-derived before it trades again.")
            return True
        return False


class FeeTripwireThread(threading.Thread):
    """Re-checks the series fee fingerprint every `interval_sec` (default 6h, F1.3)."""

    def __init__(self, tripwire: FeeTripwire, market_ticker_fn, interval_sec: float = 6 * 3600.0):
        super().__init__(name="feewatch", daemon=True)
        self.tripwire = tripwire
        self._market_ticker_fn = market_ticker_fn
        self.interval = interval_sec
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            if self._stop.wait(self.interval):
                break
            mt = self._market_ticker_fn()
            if mt:
                try:
                    self.tripwire.check(mt)
                except Exception as e:
                    print(f"[feewatch] check error: {e}", flush=True)
