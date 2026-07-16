# tests/fakes.py — injected doubles so the testable core runs without the crypto stack
# or the network. FGateway/Manager/Desk take their client, notifier and ledger by
# injection precisely so these fakes can stand in.

from typing import Any, Dict, List, Optional, Tuple


class FakeClient:
    """Stands in for lib.kalshi.KalshiClient. Records every order so tests can assert
    that a wall blocked BEFORE anything reached the exchange."""

    def __init__(self, avail: float = 1000.0, total: float = 1000.0,
                 orderbook: Optional[Dict[str, Any]] = None,
                 positions: Optional[List[Dict[str, Any]]] = None,
                 open_orders: Optional[List[Dict[str, Any]]] = None,
                 market: Optional[Dict[str, Any]] = None):
        self.avail = avail
        self.total = total
        self.orderbook = orderbook or {}
        self.positions = positions or []
        self.open_orders = open_orders or []
        self.market = market or {}
        self.fills: List[Dict[str, Any]] = []
        self.placed: List[Dict[str, Any]] = []
        self.canceled: List[str] = []
        self.balance_calls = 0
        self._oid = 0

    def get_balance_usd(self) -> Tuple[Optional[float], Optional[float]]:
        self.balance_calls += 1
        return self.avail, self.total

    def place_order(self, payload: Dict[str, Any]) -> str:
        self._oid += 1
        oid = f"OID-{self._oid}"
        rec = dict(payload)
        rec["order_id"] = oid
        self.placed.append(rec)
        return oid

    def cancel_order(self, order_id: str) -> str:
        self.canceled.append(order_id)
        return "canceled"

    def get_orderbook(self, market_ticker: str, depth: int = 10) -> Dict[str, Any]:
        return self.orderbook

    def get_fills(self, market_ticker: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        return list(self.fills)

    def get_positions(self) -> List[Dict[str, Any]]:
        return list(self.positions)

    def get_open_orders(self, market_ticker: Optional[str] = None) -> List[Dict[str, Any]]:
        return list(self.open_orders)

    def get_market(self, market_ticker: str) -> Dict[str, Any]:
        return dict(self.market)

    def discover_market(self, series_ticker: str):
        return None


class FakeNotifier:
    enabled = True

    def __init__(self):
        self.sent: List[str] = []
        self.alerts: List[str] = []

    def send(self, text: str) -> bool:
        self.sent.append(text)
        return True

    def alert(self, text: str) -> bool:
        self.alerts.append(text)
        return True
