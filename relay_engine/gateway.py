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
    crossfire: bool = False  # deliberate cross (post_only=False) — CUT ONLY, enforced here
    rest_fp: Optional[str] = None  # exact fixed-point price string (true-touch resting)


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
    def __init__(self, ledger, surface, sizing_authorized_tier=None, venue_client=None):
        self.ledger = ledger
        self.surface = surface
        # sizing_authorized_tier: callable (lane, market) -> tier the ladder authorizes
        self.sizing_authorized_tier = sizing_authorized_tier or (lambda lane, market: None)
        self.tripwire = FeeTripwire()
        self.governor = RateGovernor()
        self.venue_client = venue_client  # required for the live branch; None in shadow
        self._submit_thread: Optional[int] = None
        self._shadow_seq = 0
        self.shadow_orders: List[Order] = []
        # open positions in YES-terms signed contracts: (event, market, lane) -> net
        self.positions: Dict[Tuple[str, str, str], int] = {}
        # resting (or shadow-resting) orders by id
        self.resting: Dict[str, Order] = {}
        # every order ever submitted, by id — the fills loop's attribution index
        self.order_index: Dict[str, Order] = {}
        self.filled_counts: Dict[str, int] = {}
        self.live_order_ids: set = set()
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

        # Crossfire is confined to CUT at the canonical layer — a deliberate
        # cross anywhere else (even a passive EXIT) is refused outright.
        if order.crossfire and order.purpose != "CUT":
            raise WallRejection("REJECT_TAKER_ENTRY",
                                f"crossfire on purpose={order.purpose}; CUT only")

        if not risk_reducing:
            if self.entries_halted_reasons:
                raise WallRejection("ENTRIES_HALTED", ",".join(sorted(self.entries_halted_reasons)))
            self._wall_band_and_single_entry(order)
            self._wall_net_risk_and_at_risk(order)
            self._wall_wrong_way_tick(order)
            self._wall_taker_entry(order, book)
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
            # LIVE PATH (P3.1). Reached ONLY when Drew set RUN_MODE=LIVE + the
            # I_UNDERSTAND_LIVE phrase — go-live is a human act, never code's.
            return self._submit_live(order, payload)
        self._shadow_seq += 1
        oid = f"SHADOW-{self._shadow_seq}"
        self.shadow_orders.append(order)
        self.resting[oid] = order
        self.order_index[oid] = order
        return SubmitResult(order_id=oid, shadow=True, payload=payload)

    def _submit_live(self, order: Order, payload: dict) -> SubmitResult:
        """The one live door. post_only=False ONLY for crossfire CUTs (already
        canonically enforced above). Re-reads live balance before every write
        (the venue module's law)."""
        from . import venue
        if self.venue_client is None:
            self.venue_client = venue.build_client()
        cash, _pv = venue.get_balance(self.venue_client)
        cost_usd = order.price_cents * order.count / 100.0
        if cash is None or cash < cost_usd:
            raise WallRejection("BALANCE_RECHECK",
                                f"live balance {cash} < cost ${cost_usd:.2f}")
        oid, resp = venue.place_order_maker(
            self.venue_client, order.market, order.side, order.price_cents,
            count=order.count, v2_price_str=order.rest_fp,
            post_only=not order.crossfire,
        )
        self.resting[oid] = order
        self.order_index[oid] = order
        self.live_order_ids.add(oid)
        log.warning("LIVE ORDER PLACED %s %s %s %d@%dc post_only=%s oid=%s",
                    order.lane, order.market, order.side, order.count,
                    order.price_cents, not order.crossfire, oid)
        return SubmitResult(order_id=oid, shadow=False, payload=payload)

    def cancel(self, order_id: str) -> bool:
        """Cancels skip walls. Live orders cancel at the venue, verified."""
        order = self.resting.pop(order_id, None)
        if order is None:
            return False
        if order_id in self.live_order_ids:
            from . import venue
            status = venue.cancel_order(self.venue_client, order_id)
            if status not in ("canceled", "not_found"):
                # un-verified cancel: put it back and say so
                self.resting[order_id] = order
                return False
        return True

    def on_fill(self, order_id: str, count: Optional[int] = None) -> Order:
        """Book a (possibly partial) fill's position effect. The order stays
        resting until its full count is filled. Returns the order."""
        order = self.order_index.get(order_id)
        if order is None:
            raise FatalIntegrityError(f"fill for unknown order {order_id}")
        cnt = order.count if count is None else int(count)
        key = (order.event, order.market, order.lane)
        sign = 1 if self._signed_yes_delta(order) > 0 else -1
        self.positions[key] = self.positions.get(key, 0) + sign * cnt
        self.filled_counts[order_id] = self.filled_counts.get(order_id, 0) + cnt
        if self.filled_counts[order_id] >= order.count:
            self.resting.pop(order_id, None)
        return order

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

    def _wall_taker_entry(self, order: Order, book: OrderBook) -> None:
        """REJECT_TAKER_ENTRY (P2/P3): an entry may never take. Crossfire on an
        entry is rejected at the canonical layer above; here the PRICE is checked
        against the derived opposite touch. STRICTLY-through prices are rejected;
        resting exactly AT the boundary is legitimate touch-joining — the venue's
        post_only enforces the exact-cross case and its rejection is a NORMAL
        reject handled by repricing (Adversary: cross-400 normalization)."""
        if order.action == "buy":
            if order.side == "yes":
                opp = book.best_no_bid()
                ask = 100 - opp if opp is not None else None
            else:
                opp = book.best_yes_bid()
                ask = 100 - opp if opp is not None else None
            if ask is not None and order.price_cents > ask:
                raise WallRejection(
                    "REJECT_TAKER_ENTRY",
                    f"{order.side} buy at {order.price_cents}c through derived ask {ask}c")
        else:
            # a sell entry (shorting the side) takes if priced through the side's bid
            bid = book.best_yes_bid() if order.side == "yes" else book.best_no_bid()
            if bid is not None and order.price_cents < bid:
                raise WallRejection(
                    "REJECT_TAKER_ENTRY",
                    f"{order.side} sell at {order.price_cents}c through bid {bid}c")

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
