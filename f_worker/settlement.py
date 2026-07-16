# f_worker/settlement.py
# F1.2 — SETTLEMENT BROKER-TRUTH.
#
# A floor ride is booked by ARITHMETIC ("the pair pays 100") — nothing ever asks the
# broker what actually settled. That is safe while every market settles cleanly, but a
# DNF / refund / void market, or a mis-parsed leg outcome, would leave silent ledger
# drift that the balance alert only catches late and rudely. So after a window closes
# with inventory that rode, we poll the market result and reconcile model-vs-broker,
# append a settlement row (broker-truth beside ledger-truth), and alert on mismatch.

import time
from typing import Any, Callable, Dict, Optional

from . import window as W
from .manager import FLOOR
from .config import Config


_SETTLED_STATUS = {"settled", "finalized", "determined", "closed"}
_VALID_RESULTS = {"yes", "no"}


def rode_to_settlement(win: "W.Window") -> bool:
    """True if this window held real inventory into settlement (a floor ride)."""
    return any(l.exit_kind == FLOOR for l in win.legs)


class Settler:
    def __init__(self, client: Any, ledger: Any, config: Config, notifier: Any = None,
                 sleep: Optional[Callable[[float], None]] = None):
        self.client = client
        self.ledger = ledger
        self.cfg = config
        self.notifier = notifier
        self._sleep = sleep or time.sleep

    def _poll_result(self, market_ticker: str, tries: int = 6,
                     interval: float = 10.0) -> Dict[str, Any]:
        """Poll the market until it reports settled (bounded). Returns the market obj."""
        mkt: Dict[str, Any] = {}
        for _ in range(max(1, tries)):
            try:
                mkt = self.client.get_market(market_ticker)
            except Exception:
                mkt = {}
            status = str(mkt.get("status", "")).lower()
            result = str(mkt.get("result", "")).lower()
            if status in _SETTLED_STATUS or result in _VALID_RESULTS:
                return mkt
            self._sleep(interval)
        return mkt

    def verify_floor(self, win: "W.Window", booked_net: int) -> Dict[str, Any]:
        """Reconcile a floor ride against broker truth. A COMPLETE bundle pays exactly
        100 for the pair no matter which side wins, so the model equals broker truth
        UNLESS the market voided/refunded — which is exactly the case we must not miss."""
        mkt = self._poll_result(win.market_ticker)
        result = str(mkt.get("result", "")).lower()
        cost = sum(l.entry_price * l.count for l in win.legs)

        if result in _VALID_RESULTS:
            broker_net = 100 * win.lots - cost      # the pair paid out cleanly
            mismatch = broker_net != booked_net
        else:
            # no clean yes/no result => void / refund / not-yet-final: premium comes back
            broker_net = 0
            mismatch = True

        self.ledger.mark_settled(win.window_id, result or "unresolved", booked_net,
                                 broker_net, mismatch,
                                 detail={"status": mkt.get("status"), "cost": cost})
        if self.notifier:
            if mismatch:
                self.notifier.alert(f"⚠️ settlement mismatch — {win.market_ticker} "
                                    f"model {booked_net:+d}¢ vs broker {broker_net:+d}¢ "
                                    f"(result={result or 'none'})")
            else:
                self.notifier.send(f"🅵 ✅ W settled — floor ✓ (result={result}) "
                                   f"{broker_net:+d}¢")
        return {"result": result, "broker_net": broker_net, "mismatch": mismatch}
