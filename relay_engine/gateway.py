"""Gateway — THE single order path (C.3 / BUILD_SEQUENCE 3.2).

THIS IS A SINGLE-THREADED SUBMIT PATH. One thread, one queue, one venue session.
The first submit pins the thread; any submit from another thread is a FATAL
integrity violation. There is no second path to the venue anywhere in this tree.

Walls run IN ORDER (canon):
  1. band + lane-scoped single entry
  2. NET-RISK cross-lane cap (<=3) + $-at-risk cap per settlement event
     — risk-reducing orders EXEMPT, classified at the canonical layer here
  3. REJECT_WRONG_WAY_TICK — sign derived per order, never hardcoded
  4. %-of-book budgets (caps snapshotted at boot)
  5. sizing-tier authorization
  6. fee tripwire (multiplier AND maker-fee designation list)
Exits/cancels SKIP walls. Maker-only/post-only on every payload.
Rate governor: token bucket; the printed number IS the enforced number.

RUN_MODE=SHADOW (born state): every accepted order is recorded, none is placed.
The live-submit path below is compiled in but hard-disabled behind
config.live_submit_enabled() (the I_UNDERSTAND_LIVE pattern, §B3).

Win/loss path symmetry: walls gate entries only — the risk they admit; exits are
never gated, so the loss path is never slower than the win path.
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import config
from .book import OrderBook
from .errors import FatalIntegrityError, WallRejection

log = logging.getLogger("relay.gateway")


@dataclass
class Order:
    lane: str
    event: str            # settlement event ticker (net-risk scope)
    market: str
    side: str             # "yes" | "no"
    action: str           # "buy" | "sell"
    price_cents: int      # in the order's own side terms
    count: int
    size_tier: str
    purpose: str          # "ENTRY" | "EXIT" | "CUT"
    improve_from: Optional[int] = None  # prior price (side terms) this order improves on
    band: Optional[Tuple[int, int]] = None  # lane band (min,max) in side-terms cents


@dataclass
class SubmitResult:
    order_id: str
    shadow: bool
    payload: dict


class FeeTripwire:
    """Watches BOTH the fee multiplier and the maker-fee designation list (C.3)."""

    def __init__(self):
        self.observed_multiplier = config.EXPECTED_FEE_MULTIPLIER
        self.observed_maker_series = frozenset(config.EXPECTED_MAKER_FEE_SERIES)
        self.tripped_reason: Optional[str] = None

    def observe(self, multiplier: Optional[float] = None, maker_series=None) -> None:
        if multiplier is not None and multiplier != config.EXPECTED_FEE_MULTIPLIER:
            self.tripped_reason = (
                f"fee multiplier changed: expected {config.EXPECTED_FEE_MULTIPLIER}, saw {multiplier}")
        if maker_series is not None and frozenset(maker_series) != frozenset(config.EXPECTED_MAKER_FEE_SERIES):
            self.tripped_reason = (
                f"maker-fee designation list changed: expected "
                f"{sorted(config.EXPECTED_MAKER_FEE_SERIES)}, saw {sorted(maker_series)}")

    def ok(self) -> bool:
        return self.tripped_reason is None


class RateGovernor:
    """Token bucket. RATE_BUCKET_CAPACITY / RATE_REFILL_PER_SECOND are the printed
    numbers on the boot tape and the enforced numbers here — same constants.
    Rejection alerts are throttled WITH COUNTS (nothing suppressed silently)."""

    def __init__(self):
        self.capacity = config.RATE_BUCKET_CAPACITY
        self.tokens = float(config.RATE_BUCKET_CAPACITY)
        self.refill = config.RATE_REFILL_PER_SECOND
        self.last = time.monotonic()
        self.rejected_since_alert = 0

    def take(self, now: Optional[float] = None) -> bool:
        now = time.monotonic() if now is None else now
        self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.refill)
        self.last = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        self.rejected_since_alert += 1
        return False

    def alert_line(self) -> str:
        n, self.rejected_since_alert = self.rejected_since_alert, 0
        return f"rate-governed: {n} submissions rejected since last alert"


class Gateway:
    def __init__(self, ledger, surface, sizing_authorized_tier=None):
        self.ledger = ledger
        self.surface = surface
        # sizing_authorized_tier: callable (lane, market) -> tier the ladder authorizes
        self.sizing_authorized_tier = sizing_authorized_tier or (lambda lane, market: None)
        self.tripwire = FeeTripwire()
        self.governor = RateGovernor()
        self._submit_thread: Optional[int] = None
        self._shadow_seq = 0
        self.shadow_orders: List[Order] = []
        # open positions in YES-terms signed contracts: (event, market, lane) -> net
        self.positions: Dict[Tuple[str, str, str], int] = {}
        # resting (or shadow-resting) orders by id
        self.resting: Dict[str, Order] = {}
        self.entries_halted_reasons: set = set()

    # ------------------------------------------------------------------
    # Canonical layer: risk-reducing classification happens HERE, once,
    # from the position registry — not in lanes, not in the custodian.
    # ------------------------------------------------------------------
    def _signed_yes_delta(self, order: Order) -> int:
        """The order's effect on net YES-terms position if filled."""
        long_yes = (order.side == "yes") == (order.action == "buy")
        return order.count if long_yes else -order.count

    def is_risk_reducing(self, order: Order) -> bool:
        net = self.positions.get((order.event, order.market, order.lane), 0)
        after = net + self._signed_yes_delta(order)
        return abs(after) < abs(net)

    # ------------------------------------------------------------------
    # THE submit path (single-threaded, stated above)
    # ------------------------------------------------------------------
    def submit(self, order: Order, book: OrderBook) -> SubmitResult:
        tid = threading.get_ident()
        if self._submit_thread is None:
            self._submit_thread = tid
        elif tid != self._submit_thread:
            raise FatalIntegrityError(
                f"gateway submit from thread {tid}; the single submit path is pinned to "
                f"{self._submit_thread}")

        risk_reducing = self.is_risk_reducing(order) or order.purpose in ("EXIT", "CUT")

        if not risk_reducing:
            if self.entries_halted_reasons:
                raise WallRejection("ENTRIES_HALTED", ",".join(sorted(self.entries_halted_reasons)))
            self._wall_band_and_single_entry(order)
            self._wall_net_risk_and_at_risk(order)
            self._wall_wrong_way_tick(order)
            self._wall_pct_of_book(order)
            self._wall_sizing_tier(order)
            self._wall_fee_tripwire(order)

        # Rate governor: entries need a token; risk reduction is always allowed
        # (it may overdraw the bucket, loudly).
        if not self.governor.take():
            if risk_reducing:
                log.warning("governor overdraw for risk-reducing order (%s)",
                            self.governor.alert_line())
            else:
                raise WallRejection("RATE_GOVERNED", self.governor.alert_line())

        payload = self._payload(order)
        if config.live_submit_enabled():
            # LIVE PATH — hard-disabled in this tree's born state. Chunks 5-7 are
            # separate orders at Drew's word; nothing here flips RUN_MODE.
            raise FatalIntegrityError(
                "live_submit_enabled() returned True in the paper-shadow build; "
                "this tree must not place orders before the cutover ruling")
        self._shadow_seq += 1
        oid = f"SHADOW-{self._shadow_seq}"
        self.shadow_orders.append(order)
        self.resting[oid] = order
        return SubmitResult(order_id=oid, shadow=True, payload=payload)

    def cancel(self, order_id: str) -> bool:
        """Cancels skip walls."""
        return self.resting.pop(order_id, None) is not None

    def on_fill(self, order_id: str) -> None:
        order = self.resting.pop(order_id, None)
        if order is None:
            raise FatalIntegrityError(f"fill for unknown order {order_id}")
        key = (order.event, order.market, order.lane)
        self.positions[key] = self.positions.get(key, 0) + self._signed_yes_delta(order)

    def halt_entries(self, reason: str) -> None:
        self.entries_halted_reasons.add(reason)

    def resume_entries(self, reason: str) -> None:
        self.entries_halted_reasons.discard(reason)

    # ------------------------------------------------------------------
    # Walls, in canon order
    # ------------------------------------------------------------------
    def _wall_band_and_single_entry(self, order: Order) -> None:
        if order.band is not None:
            lo, hi = order.band
            if not (lo <= order.price_cents <= hi):
                raise WallRejection("BAND", f"{order.price_cents}c outside [{lo},{hi}]")
        for o in self.resting.values():
            if o.lane == order.lane and o.market == order.market and o.purpose == "ENTRY":
                raise WallRejection("SINGLE_ENTRY", f"lane {order.lane} already resting on {order.market}")
        if self.positions.get((order.event, order.market, order.lane), 0) != 0:
            raise WallRejection("SINGLE_ENTRY", f"lane {order.lane} already positioned on {order.market}")

    def _event_exposure(self, event: str) -> Tuple[int, int]:
        """(net contracts at risk, cents at risk) across lanes for one settlement
        event: open positions + resting ENTRY orders. Resting EXITs never count."""
        contracts = 0
        cents = 0
        for (ev, market, lane), net in self.positions.items():
            if ev == event and net != 0:
                contracts += abs(net)
                cents += abs(net) * config.ONE_LOT_MAX_LOSS_CENTS
        for o in self.resting.values():
            if o.event == event and o.purpose == "ENTRY":
                contracts += o.count
                # side-terms basis = max loss per contract, both sides
                cents += o.count * o.price_cents
        return contracts, cents

    def _wall_net_risk_and_at_risk(self, order: Order) -> None:
        contracts, cents = self._event_exposure(order.event)
        side_basis = order.price_cents  # side-terms basis = max loss per contract
        if contracts + order.count > config.NET_RISK_CROSS_LANE_CAP:
            raise WallRejection(
                "NET_RISK_CAP",
                f"event {order.event}: {contracts}+{order.count} > {config.NET_RISK_CROSS_LANE_CAP}")
        if cents + order.count * side_basis > config.AT_RISK_CAP_CENTS:
            raise WallRejection(
                "AT_RISK_CAP",
                f"event {order.event}: {cents}+{order.count * side_basis}c > {config.AT_RISK_CAP_CENTS}c")

    def _wall_wrong_way_tick(self, order: Order) -> None:
        if order.improve_from is None:
            return
        # Sign derived PER ORDER from its own action, never hardcoded:
        # improving a BUY moves +1 toward the ask; improving a SELL moves -1 toward the bid.
        expected = 1 if order.action == "buy" else -1
        diff = order.price_cents - order.improve_from
        if diff == 0 or (diff > 0) != (expected > 0):
            raise WallRejection(
                "REJECT_WRONG_WAY_TICK",
                f"{order.action} improvement {order.improve_from}->{order.price_cents} "
                f"(expected sign {expected:+d})")

    def _wall_pct_of_book(self, order: Order) -> None:
        caps = self.ledger.boot_caps
        if caps is None:
            raise FatalIntegrityError("boot caps not snapshotted; boot sequence violated")
        notional = order.price_cents * order.count
        if notional > caps.order_budget_cents:
            raise WallRejection(
                "PCT_OF_BOOK", f"{notional}c > boot budget {caps.order_budget_cents}c")

    def _wall_sizing_tier(self, order: Order) -> None:
        if order.size_tier == config.TIER_SUPPRESS:
            raise WallRejection("SIZING_TIER", "SUPPRESS tier proposes no entries")
        if order.count > config.TIER_MAX_CONTRACTS.get(order.size_tier, 0):
            raise WallRejection(
                "SIZING_TIER",
                f"count {order.count} exceeds {order.size_tier} max "
                f"{config.TIER_MAX_CONTRACTS.get(order.size_tier, 0)}")
        authorized = self.sizing_authorized_tier(order.lane, order.market)
        if authorized is not None and order.size_tier != authorized:
            raise WallRejection(
                "SIZING_TIER", f"tier {order.size_tier} not the authorized {authorized}")

    def _wall_fee_tripwire(self, order: Order) -> None:
        if not self.tripwire.ok():
            raise WallRejection("FEE_TRIPWIRE", self.tripwire.tripped_reason or "")

    # ------------------------------------------------------------------
    @staticmethod
    def _payload(order: Order) -> dict:
        body = {
            "ticker": order.market,
            "action": order.action,
            "side": order.side,
            "type": "limit",
            "count": int(order.count),
            "post_only": True,  # maker-only/post-only EVERYWHERE — no exceptions
        }
        if order.side == "yes":
            body["yes_price"] = int(order.price_cents)
        else:
            body["no_price"] = int(order.price_cents)
        return body
