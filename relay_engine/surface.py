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
GAP_RESTART = "GAP_RESTART"   # P17 §1.4: an evidence hole, counted not papered
# Interim states (full row on state change only)
WATCHING = "WATCHING"
PROPOSED = "PROPOSED"
ENTERED = "ENTERED"
EXITED = "EXITED"

TERMINAL_STATES = {PASS, SETTLED, CUSTODIED_SETTLED, GAP_RESTART}

# P17 §1: terminal is MONOTONIC, not immutable. A stale PASS meeting a
# settlement UPGRADES (logged); a GAP_RESTART heals into a real settlement;
# only a REGRESSION (rank going down, or two conflicting settlements) is the
# accounting bug that stays FATAL.
TERMINAL_RANK = {PASS: 0, GAP_RESTART: 1, SETTLED: 2, CUSTODIED_SETTLED: 2}


class Surface:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger
        self._last_state: Dict[tuple, str] = {}  # (lane, market, window) -> last state
        self._last_detail: Dict[tuple, str] = {}  # last interim reason (finalize keeps it)
        self._terminal: Dict[tuple, str] = {}    # (lane, market, window) -> terminal verdict
        self.interim_counters = defaultdict(int)  # (lane, state) -> count
        # Scientist stamp (P3): five lanes on one book contaminate each other's
        # counterfactuals — accepted by ruling, but every row carries which
        # lanes were concurrently live on the market so analysis can condition.
        self.concurrent_provider = lambda market: ""

    def write_row(self, lane: str, market: str, window_id: str, state: str,
                  transport: str = "WS", detail: str = "",
                  ts: Optional[float] = None, final: bool = False) -> bool:
        """Full row on state change only; repeated same-state writes bump a
        counter. P17 §1.1: PASS is PROVISIONAL (interim) until the window
        closes — the terminal PASS is written only by finalize_window
        (final=True). A lane that passes at T-12 and enters at T-2 therefore
        never writes a PASS terminal at all."""
        key = (lane, market, window_id)
        terminal = state in TERMINAL_STATES and (state != PASS or final)
        if not terminal:
            if self._last_state.get(key) == state:
                self.interim_counters[(lane, state)] += 1
                return False
            # §A1: insert FIRST — a why-less row is REFUSED here (raises) with no
            # state mutation, so a refused write leaves no trace to dedup against.
            self._insert(key, state, 0, transport, detail, ts)
            self._last_state[key] = state
            self._last_detail[key] = detail
            return True
        prev = self._terminal.get(key)
        if prev is not None:
            if prev == state:
                return False  # re-asserting the same terminal verdict is a no-op
            if TERMINAL_RANK.get(state, 0) > TERMINAL_RANK.get(prev, 0):
                # §1.2: upgrades legal, logged, never fatal
                from . import failures
                failures.fail("TERMINAL_UPGRADED",
                              f"{key}: terminal {prev} -> {state}",
                              lane=lane, market=market, alert=False)
                detail = (detail + " · " if detail else "") + f"upgrade from {prev}"
            else:
                # a REGRESSION is a real accounting bug — stays FATAL
                from . import failures
                failures.fail("DUPLICATE_TERMINAL_ROW",
                              f"terminal REGRESSION for {key}: had {prev}, "
                              f"got {state}", fatal=True)
        # §A1: insert FIRST (refuses a why-less terminal before any state change).
        self._insert(key, state, 1, transport, detail, ts)
        self._terminal[key] = state
        self._last_state[key] = state
        return True

    def _insert(self, key, state, terminal, transport, detail, ts):
        # ── WO-2026-07-26-P §A1 — THE WHY LAW AT THE WRITE CHOKEPOINT ──────────
        # Every surface row is a claim about money or state, and a claim with no
        # reason is exactly the silent fabrication the one-lot bug rode in on: a
        # number with nobody standing behind it. This is the ONE shared writer —
        # every write_row branch (interim state-change AND terminal) funnels
        # through here, and there is no other path to surface_rows. So it refuses
        # a row that carries no why. (Same-state interim repeats never reach here;
        # write_row bumps a counter and returns before calling _insert.)
        if detail is None or not str(detail).strip():
            raise ValueError(
                f"WHY_REQUIRED: surface row {key} state={state!r} was written "
                "with no why/reason. Every row must carry a sentence — a number "
                "with no reason is a fabrication (Why Law §A1). Give the writer "
                "a detail= that says what this row claims and why.")
        lane, market, window_id = key
        self.ledger.db.execute(
            "INSERT INTO surface_rows (ts, lane, market, window_id, state, terminal,"
            " transport, detail, concurrent_lanes) VALUES (?,?,?,?,?,?,?,?,?)",
            (ts if ts is not None else time.time(), lane, market, window_id, state,
             terminal, transport, detail, self.concurrent_provider(market)),
        )
        self.ledger.db.commit()

    def finalize_window(self, market: str, window_id: str) -> int:
        """P17 §1.1/§6.3: at window close, every lane that watched or passed —
        and never entered — writes its first-class terminal PASS. Lanes whose
        last state is order-shaped (PROPOSED/ENTERED/EXITED) wait for
        settlement's terminal. Returns terminal rows written."""
        wrote = 0
        for (lane, mkt, win), last in list(self._last_state.items()):
            if mkt != market or win != window_id:
                continue
            if (lane, mkt, win) in self._terminal:
                continue
            if last in (WATCHING, PASS):
                detail = self._last_detail.get((lane, mkt, win)) \
                    or "window closed — no entry"
                if self.write_row(lane, market, window_id, PASS,
                                  detail=detail, final=True):
                    wrote += 1
        return wrote

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
            # WO-INFRA-HARDENING E1 — the settlement SOURCE tracer (the code's
            # long-standing "E1 traces the source" TODO): every booked cent is
            # attributed to the fill + outcome that produced it, and net_held
            # catches an unmatched leg (exits exceeding entries — a phantom
            # over-exit), so a book-vs-exchange divergence NAMES its source
            # instead of being eyeballed three dollars later.
            contribs: List[dict] = []
            net_held = {"yes": 0, "no": 0}
            for side, action, price_cents, count in fills:
                yes_price = to_yes_terms(side, price_cents)
                side_payoff = (100 if settled_yes else 0) if side == "yes" else (0 if settled_yes else 100)
                side_basis = yes_price if side == "yes" else 100 - yes_price
                if action == "ENTRY":
                    # Long the entered side at its basis; collects the side's payoff.
                    c = (side_payoff - side_basis) * count
                    net_held[side] += count
                elif action in ("EXIT", "CUSTODIAN_EXIT"):
                    # An exit realizes the sale price and forgoes the side's payoff —
                    # symmetric to entry, opposite sign (win/loss path symmetry).
                    if action == "CUSTODIAN_EXIT":
                        custodied = True
                    c = (side_basis - side_payoff) * count
                    net_held[side] -= count
                else:
                    c = 0
                pnl += c
                contribs.append({"side": side, "action": action,
                                 "price": price_cents, "count": count,
                                 "contribution_cents": c})
            self.ledger.record_settlement(market, lane, pnl, detail=f"window={window_id}")
            # E1: the provenance row — pnl, the exchange outcome, the per-fill
            # breakdown, the running book AFTER this settlement books, and the
            # unmatched-leg flag. This is what "traces the source" reads.
            unmatched = net_held["yes"] < 0 or net_held["no"] < 0
            self.write_row(lane, market, window_id, "SETTLE_AUDIT",
                           transport=transport,
                           detail=json.dumps({
                               "pnl_cents": pnl, "settled_yes": settled_yes,
                               "net_held": net_held, "unmatched": unmatched,
                               "book_after_cents": self.ledger.book_cents(),
                               "fills": contribs}))
            if unmatched:
                from . import failures
                failures.fail(
                    "SETTLE_UNMATCHED_LEG",
                    f"{market}/{lane}: exits exceed entries (net_held="
                    f"{net_held}) — settlement booked on an unmatched leg; the "
                    "E1 SETTLE_AUDIT row carries the provenance",
                    alert=False, market=market, lane=lane, pnl_cents=pnl,
                    net_held=json.dumps(net_held))
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
