"""Surface — per-lane attribution + aggregation policy (C.2 / BUILD_SEQUENCE 1.4, 1.5).

Attribution law: every fill carries (lane, market, side, cost-basis, size-tier).
A stacked market settles by fill mapping, split per lane. Custodian exits write
to the OPENING lane's Wilson cells — the custodian never owns outcomes.

Aggregation policy: ONE terminal row per lane per market per window, plus interim
counters; full rows are written on state changes only. A Pass is a first-class
terminal row. Regret accounting reads terminal rows.

Acceptance test (shipped, tests/test_attribution.py): a synthetic stacked market
must be reconstructible per-lane from surface rows alone.

Win/loss path symmetry: wins and losses flow through the same settle_market path
and the same terminal-row states; nothing is recorded only on the winning path.
"""

import json
import time
from collections import defaultdict
from typing import Dict, List, Optional

from .book import to_yes_terms
from .ledger import Ledger

# Terminal states (one per lane/market/window)
PASS = "PASS"
SETTLED = "SETTLED"
CUSTODIED_SETTLED = "CUSTODIED_SETTLED"
# Interim states (full row on state change only)
PROPOSED = "PROPOSED"
ENTERED = "ENTERED"
EXITED = "EXITED"

TERMINAL_STATES = {PASS, SETTLED, CUSTODIED_SETTLED}


class Surface:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger
        self._last_state: Dict[tuple, str] = {}  # (lane, market, window) -> last state
        self.interim_counters = defaultdict(int)  # (lane, state) -> count

    def write_row(self, lane: str, market: str, window_id: str, state: str,
                  transport: str = "WS", detail: str = "", ts: Optional[float] = None) -> bool:
        """Full row on state change only; repeated same-state writes bump a counter."""
        key = (lane, market, window_id)
        terminal = state in TERMINAL_STATES
        if not terminal and self._last_state.get(key) == state:
            self.interim_counters[(lane, state)] += 1
            return False
        if terminal and self._last_state.get(key) in TERMINAL_STATES:
            if self._last_state[key] == state:
                return False  # re-asserting the same terminal verdict is a no-op
            # A DIFFERENT second terminal row is an accounting bug.
            from .errors import FatalIntegrityError
            raise FatalIntegrityError(
                f"duplicate terminal row for {key}: had {self._last_state[key]}, got {state}")
        self._last_state[key] = state
        self.ledger.db.execute(
            "INSERT INTO surface_rows (ts, lane, market, window_id, state, terminal, transport, detail)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (ts if ts is not None else time.time(), lane, market, window_id, state,
             1 if terminal else 0, transport, detail),
        )
        self.ledger.db.commit()
        return True

    # ------------------------------------------------------------------
    # Settlement by fill mapping (stacked markets split per lane)
    # ------------------------------------------------------------------
    def settle_market(self, market: str, window_id: str, settled_yes: bool,
                      transport: str = "WS") -> Dict[str, int]:
        """Settle every unsettled fill on `market`, split per lane by fill mapping.

        Settlement truth anchors to the market record's outcome (floor_strike /
        expiration_value upstream of this call), never generic spot.
        Returns {lane: pnl_cents} and writes one terminal row per lane.
        """
        rows = self.ledger.db.execute(
            "SELECT lane, side, action, price_cents, count FROM fills"
            " WHERE market=? AND settled=0", (market,),
        ).fetchall()
        per_lane: Dict[str, List[tuple]] = defaultdict(list)
        for lane, side, action, price, count in rows:
            per_lane[lane].append((side, action, price, count))

        result: Dict[str, int] = {}
        for lane, fills in per_lane.items():
            pnl = 0
            custodied = False
            for side, action, price_cents, count in fills:
                yes_price = to_yes_terms(side, price_cents)
                side_payoff = (100 if settled_yes else 0) if side == "yes" else (0 if settled_yes else 100)
                side_basis = yes_price if side == "yes" else 100 - yes_price
                if action == "ENTRY":
                    # Long the entered side at its basis; collects the side's payoff.
                    pnl += (side_payoff - side_basis) * count
                elif action in ("EXIT", "CUSTODIAN_EXIT"):
                    # An exit realizes the sale price and forgoes the side's payoff —
                    # symmetric to entry, opposite sign (win/loss path symmetry).
                    if action == "CUSTODIAN_EXIT":
                        custodied = True
                    pnl += (side_basis - side_payoff) * count
            self.ledger.record_settlement(market, lane, pnl, detail=f"window={window_id}")
            state = CUSTODIED_SETTLED if custodied else SETTLED
            self.write_row(lane, market, window_id, state, transport=transport,
                           detail=json.dumps({"pnl_cents": pnl, "fills": len(fills)}))
            result[lane] = pnl
        return result

    # ------------------------------------------------------------------
    # Reconstruction (the acceptance test's read path): per-lane P&L from
    # surface rows ALONE — no ledger, no fills table.
    # ------------------------------------------------------------------
    def reconstruct_per_lane(self, market: str, window_id: str) -> Dict[str, int]:
        rows = self.ledger.db.execute(
            "SELECT lane, state, detail FROM surface_rows"
            " WHERE market=? AND window_id=? AND terminal=1", (market, window_id),
        ).fetchall()
        out: Dict[str, int] = {}
        for lane, state, detail in rows:
            if state == PASS:
                out[lane] = 0
            else:
                out[lane] = int(json.loads(detail)["pnl_cents"])
        return out
