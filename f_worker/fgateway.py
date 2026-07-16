# f_worker/fgateway.py
# THE ONLY ORDER PATH. Every contract this desk ever buys or sells passes through one
# private choke point: _submit(). Grep-proof (BUILD ORDER §9): there is exactly ONE
# call to client.place_order in the whole package, and it lives here.
#
# The LAWS are WALLS, not configs (§1). Each wall is a hard-coded check on the order
# path with a test that tries to violate it and proves it can't:
#   W1  combined bundle cost <= DW_ENTRY_LINE (99c)
#   W2  a lone leg may only be HELD if its fill price <= DW_SINGLE_LEG_MAX (49c)
#   W3  1 lot/side until the ladder says otherwise; lone legs are ALWAYS 1 lot
#   W4  flat by T-90: no NEW risk inside the flat zone (exits are always allowed)
#   W5  one entry phase per window: a second entry pair is refused
#   W6  live-balance re-read before every order (the Feb-22 law)
#   W7  flip_halt set => the desk adds no risk (exits still allowed to flatten)
#   W8  fee arithmetic uses exact roundup; a crossing exit prices its taker fee FIRST
#
# A wall that fires on a risk-adding request is a BUG (the state machine should never
# ask): it raises WallViolation AND emits a BUG alert (§5). W2/W4 are also exposed as
# predicates so the state machine can branch without ever tripping the wall.

from typing import Any, Dict, Optional, Tuple

from . import feemath
from .config import Config
from .ledger import (Ledger, OK_ENTRY_BID, OK_FLIP_ASK, OK_MARKET_OUT, OK_SALVAGE)


class WallViolation(RuntimeError):
    """A hard law was asked to bend. Never caught to continue trading — it halts the
    offending action and pages Drew as a BUG."""

    def __init__(self, wall: str, detail: str):
        super().__init__(f"{wall}: {detail}")
        self.wall = wall
        self.detail = detail


def _build_payload(market_ticker: str, action: str, side: str, price_cents: int,
                   count: int, post_only: bool, order_type: str = "limit") -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "ticker": market_ticker, "action": action, "side": side,
        "type": order_type, "count": int(count),
    }
    if order_type == "limit":
        body["yes_price" if side == "yes" else "no_price"] = int(price_cents)
        if post_only:
            body["post_only"] = True
    return body


class FGateway:
    def __init__(self, client: Any, ledger: Ledger, config: Config, notifier: Any = None):
        self.client = client
        self.ledger = ledger
        self.cfg = config
        self.notifier = notifier
        self._entry_posted: set = set()   # window_ids that already ran their one entry phase (W5)

    # ---------- wall helpers (predicates the state machine consults) ----------
    def may_hold_lone(self, fill_price: int) -> bool:
        """W2 predicate. True if a lone leg filled at `fill_price` may legally be held."""
        return feemath.single_leg_ok(fill_price, self.cfg.single_leg_max)

    def in_flat_zone(self, seconds_to_close: Optional[int]) -> bool:
        """W4 predicate. True once we are at/inside T-90 — no new risk beyond here."""
        return seconds_to_close is not None and seconds_to_close <= self.cfg.flat_at_t

    def is_halted(self) -> bool:
        return self.ledger.halt_state()[0]

    def current_lots(self, is_lone: bool) -> int:
        """W3: bundle legs take the ladder's current lots; lone legs are ALWAYS 1."""
        if is_lone:
            return 1
        _, lots = self.ledger.current_rung()
        return max(1, int(lots))

    def _bug(self, wall: str, detail: str) -> "WallViolation":
        if self.notifier is not None:
            self.notifier.alert(f"BUG — wall {wall} violation attempt: {detail}")
        return WallViolation(wall, detail)

    # ---------- the single choke point ----------
    def _submit(self, window_id: str, kind: str, market_ticker: str, action: str, side: str,
                price_cents: int, count: int, post_only: bool, order_type: str,
                detail: Dict[str, Any]) -> Optional[str]:
        """The ONLY place that talks to the exchange's create-order endpoint.

        Always re-reads the live balance first (W6). DRY_RUN logs + records intent but
        never submits."""
        avail, total = self.client.get_balance_usd()  # W6: live re-read, EVERY order
        detail = dict(detail)
        detail["bal_avail_usd"] = avail
        detail["bal_total_usd"] = total

        # W6 sufficiency gate applies only to risk-adding buys.
        if action == "buy":
            need_usd = (int(price_cents) * int(count)) / 100.0
            if avail is not None and avail + 1e-9 < need_usd:
                raise self._bug("W6", f"insufficient balance {avail} < need {need_usd} for {kind}")

        payload = _build_payload(market_ticker, action, side, price_cents, count, post_only, order_type)

        if self.cfg.dry_run:
            oid = f"DRYRUN-{kind}-{side}-{price_cents}"
            print(f"[DRY_RUN] would submit {payload}", flush=True)
        else:
            oid = self.client.place_order(payload)

        self.ledger.record_order(window_id, kind, side, action, price_cents, count, oid, detail)
        return oid

    # ---------- entry (SEEKING) ----------
    def post_entry_pair(self, window_id: str, market_ticker: str, yes_price: int, no_price: int,
                        seconds_to_close: Optional[int]) -> Tuple[Optional[str], Optional[str]]:
        """Post the one entry phase for a window: a resting YES bid + NO bid.

        Walls fired here: W5 (once per window), W7 (no risk while halted), W4 (no entry
        in flat zone), W1 (bundle cost <= line), W3 (lots), W6 (inside _submit)."""
        if window_id in self._entry_posted:
            raise self._bug("W5", f"second entry phase attempted for {window_id}")
        if self.is_halted():
            raise self._bug("W7", "entry attempted while flip_halt is set")
        if self.in_flat_zone(seconds_to_close):
            raise self._bug("W4", f"entry attempted in flat zone (T-{seconds_to_close}s)")
        if not feemath.entry_ok(yes_price, no_price, self.cfg.entry_line):
            raise self._bug("W1", f"bundle {yes_price}+{no_price}={yes_price + no_price} > "
                                  f"line {self.cfg.entry_line}")
        lots = self.current_lots(is_lone=False)

        # Mark the phase spent BEFORE submitting so a partial failure can never re-arm it.
        self._entry_posted.add(window_id)
        yes_oid = self._submit(window_id, OK_ENTRY_BID, market_ticker, "buy", "yes",
                               yes_price, lots, post_only=True, order_type="limit",
                               detail={"phase": "entry", "leg": "yes"})
        no_oid = self._submit(window_id, OK_ENTRY_BID, market_ticker, "buy", "no",
                              no_price, lots, post_only=True, order_type="limit",
                              detail={"phase": "entry", "leg": "no"})
        return yes_oid, no_oid

    # ---------- flip (HOLDING) ----------
    def post_flip_ask(self, window_id: str, market_ticker: str, side: str, entry_price: int,
                      ask_price: int, is_lone: bool, seconds_to_close: Optional[int]) -> Optional[str]:
        """Post a resting flip ask (sell the held side) at ask_price. Risk-REDUCING, so
        allowed during halt. W2: a lone leg above single_leg_max must never have been
        held, so posting a flip for it is a bug. W4: past T-90 use market_out, not this."""
        if is_lone and not self.may_hold_lone(entry_price):
            raise self._bug("W2", f"lone leg entry {entry_price} > max {self.cfg.single_leg_max}")
        if self.in_flat_zone(seconds_to_close):
            raise self._bug("W4", "flip ask attempted in flat zone; use market_out")
        count = self.current_lots(is_lone)
        # Selling a held YES = sell yes; selling held NO = sell no. Maker, post_only.
        return self._submit(window_id, OK_FLIP_ASK, market_ticker, "sell", side,
                            ask_price, count, post_only=True, order_type="limit",
                            detail={"phase": "flip", "entry_price": entry_price, "lone": is_lone})

    # ---------- salvage (crossing exit) ----------
    def salvage_cross(self, window_id: str, market_ticker: str, side: str, entry_price: int,
                      exit_price: int, is_lone: bool) -> Tuple[Optional[str], int]:
        """Cross the spread to recover capital on a leg. W8: the taker fee is priced by
        feemath and attached to the record BEFORE the order goes out. Returns (oid, fee)."""
        count = self.current_lots(is_lone)
        fee = feemath.fee_cents(exit_price, count)             # W8: exact roundup, priced first
        net = feemath.net_after_taker_exit(entry_price, exit_price, count)
        oid = self._submit(window_id, OK_SALVAGE, market_ticker, "sell", side,
                           exit_price, count, post_only=False, order_type="limit",
                           detail={"phase": "salvage", "entry_price": entry_price,
                                   "taker_fee_cents": fee, "net_cents": net, "lone": is_lone})
        return oid, fee

    # ---------- T-90 flat (EXITING) ----------
    def market_out(self, window_id: str, market_ticker: str, side: str, is_lone: bool,
                   entry_price: Optional[int] = None, mark_price: Optional[int] = None) -> Optional[str]:
        """W4 executor: market-out a leg at/after T-90. Always allowed (risk-reducing),
        even in halt. W8: if a mark is known, its taker fee is estimated and recorded."""
        count = self.current_lots(is_lone)
        detail: Dict[str, Any] = {"phase": "flat_T90", "entry_price": entry_price, "lone": is_lone}
        if mark_price is not None:
            detail["taker_fee_cents"] = feemath.fee_cents(mark_price, count)
        return self._submit(window_id, OK_MARKET_OUT, market_ticker, "sell", side,
                            mark_price or 1, count, post_only=False, order_type="market",
                            detail=detail)

    # ---------- cancels ----------
    def cancel(self, order_id: str) -> str:
        if self.cfg.dry_run or not order_id or str(order_id).startswith("DRYRUN"):
            return "canceled"
        return self.client.cancel_order(order_id)
