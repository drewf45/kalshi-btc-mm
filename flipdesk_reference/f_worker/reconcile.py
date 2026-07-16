# f_worker/reconcile.py
# F1.1 — BOOT RECONCILE / ORPHAN SWEEP (WALL-class).
#
# The desk's inventory lives in in-memory Window objects. A crash or deploy mid-HOLDING
# leaves resting orders and held legs on the BROKER that the restarted process knows
# nothing about — unmanaged inventory in a 0-or-1 market, the exact risk this desk was
# built never to carry. So before the desk loop starts, we reconcile against broker
# truth:
#   1. cancel every resting order on the series (nothing dangles)
#   2. read positions; any non-zero leg is an ORPHAN -> flatten it now through the
#      manager's normal exit path (SEEKING->EXITING->DONE) and page Drew
#
# Recovery FLATTENS rather than tries to flip: an orphan has an unknown/lost entry
# context and lives in a 0/1 market, so the doctrinal move is to get flat immediately.

from typing import Any, Dict, List, Optional

from . import window as W
from .config import Config
from .lib.marketutil import parse_best_yes_no


def _net_position(p: Dict[str, Any]) -> int:
    """Signed net contracts for a market position: positive = long YES, negative = long NO."""
    for k in ("position", "net_position", "net_yes_position", "yes_position"):
        if k in p:
            try:
                return int(p[k])
            except Exception:
                continue
    return 0


def _avg_price(p: Dict[str, Any]) -> Optional[int]:
    for k in ("average_price", "avg_price", "average_entry_price", "avg_entry_price"):
        if k in p:
            try:
                return int(round(float(p[k])))
            except Exception:
                continue
    return None


class Reconciler:
    def __init__(self, client: Any, gateway: Any, manager: Any, ledger: Any,
                 config: Config, notifier: Any = None):
        self.client = client
        self.gw = gateway
        self.mgr = manager
        self.ledger = ledger
        self.cfg = config
        self.notifier = notifier

    def _series_match(self, ticker: Optional[str]) -> bool:
        return bool(ticker) and str(ticker).startswith(self.cfg.series_ticker)

    def boot_reconcile(self) -> Dict[str, int]:
        """Run once at boot BEFORE the desk trades. Returns a summary dict."""
        summary = {"canceled": 0, "orphans": 0}

        # 1. cancel every resting order on the series
        try:
            for o in self.client.get_open_orders():
                tk = o.get("ticker") or o.get("market_ticker")
                if not self._series_match(tk):
                    continue
                oid = o.get("order_id") or o.get("id")
                if oid:
                    self.gw.cancel(str(oid))
                    summary["canceled"] += 1
        except Exception as e:
            print(f"[reconcile] resting-order sweep failed: {e}", flush=True)
            if self.notifier:
                self.notifier.alert(f"reconcile: resting-order sweep failed ({e})")

        # 2. flatten any held leg (orphan)
        try:
            positions = self.client.get_positions()
        except Exception as e:
            positions = []
            if self.notifier:
                self.notifier.alert(f"reconcile: positions read failed ({e})")

        for p in positions:
            tk = p.get("ticker") or p.get("market_ticker")
            if not self._series_match(tk):
                continue
            net = _net_position(p)
            if net == 0:
                continue
            self._recover_orphan(str(tk), net, _avg_price(p))
            summary["orphans"] += 1

        line = (f"🅵 🔧 boot reconcile — canceled {summary['canceled']} resting, "
                f"recovered {summary['orphans']} orphan(s)")
        print(line, flush=True)
        if self.notifier:
            self.notifier.send(line)
        return summary

    def _recover_orphan(self, market_ticker: str, net: int, avg_price: Optional[int]) -> None:
        side = "yes" if net > 0 else "no"
        count = abs(net)
        # best bid on the held side = our market-out mark; entry unknown -> use avg if the
        # broker gave one, else the mark (books the recovery as ~ -taker_fee, never overstated)
        mark = self._best_bid(market_ticker, side)
        entry = avg_price if avg_price is not None else (mark if mark is not None else 0)
        rung, lots = self.ledger.current_rung()

        win = W.Window(window_id=f"ORPHAN:{market_ticker}", market_ticker=market_ticker,
                       event_ticker=None, open_ts=None, close_ts=None,
                       rung=rung, lots=count).bind_ledger(self.ledger)
        win.mode = "lone"
        win.add_fill(side, entry, count)
        self.ledger.record_window(win.window_id, market_ticker, None, None, None, rung)
        win.transition(W.SEEKING, {"orphan": True, "net": net, "avg_price": avg_price})
        # flatten through the manager's normal exit path (SEEKING -> EXITING -> DONE)
        self.mgr.flat_out(win, marks={side: mark} if mark is not None else {},
                          reason="orphan recovery flatten")
        if self.notifier:
            self.notifier.alert(f"🚨 ORPHAN RECOVERED — {market_ticker} {side.upper()} x{count} "
                                f"flattened @ {mark}")

    def _best_bid(self, market_ticker: str, side: str) -> Optional[int]:
        try:
            ob = self.client.get_orderbook(market_ticker)
        except Exception:
            return None
        yb, ya, nb, na = parse_best_yes_no(ob)
        return yb if side == "yes" else nb
