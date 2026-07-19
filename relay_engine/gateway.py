"""Gateway — THE single order path (C.3 / BUILD_SEQUENCE 3.2).

THIS IS A SINGLE-THREADED SUBMIT PATH. One thread, one queue, one venue session.
The first submit pins the thread; any submit from another thread is a FATAL
integrity violation. There is no second path to the venue anywhere in this tree.

Walls run IN ORDER (canon — P27: bug-walls only; the tier wall is dead,
the account halt is THE stop):
  1. band + lane-scoped single entry
  2. NET-RISK cross-lane cap (<=3) + $-at-risk cap per settlement event
     — risk-reducing orders EXEMPT, classified at the canonical layer here
  3. REJECT_WRONG_WAY_TICK — sign derived per order, never hardcoded
  4. %-of-book budgets (caps snapshotted at boot)
  5. fee tripwire (multiplier AND maker-fee designation list)
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

from . import config, failures
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
    # P13 §1: every dollar gets a sentence — the compressed thesis at submit
    # (entries) and the rule that spent the money (exits/cuts).
    why: str = ""
    reason: str = ""


@dataclass
class SubmitResult:
    order_id: str
    shadow: bool
    payload: dict
    # P24 §2.2: the venue's raw order response rides back so the booking
    # path can read the response's OWN fee (parse_response_fee) — never
    # imagined from a multiplier. None in shadow.
    resp: Optional[dict] = None


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
        # P7: venue-reject backoff (per market) + storm telemetry counters
        self.backoff_until: Dict[str, float] = {}
        self.reject_counts: Dict[str, int] = {}
        self.venue_rejects = 0
        # P10 §4: wall-reject backoff — identical ENTRY re-proposals rest 30s.
        # Keyed (lane, market) holding (tag, fingerprint, until); the
        # fingerprint includes the touch, so a moved book re-opens the door.
        self.wall_backoff: dict = {}
        self.wall_reject_times: dict = {}   # (lane, market, tag) -> [monotonic ts]
        self._storm_paged: set = set()
        self.suppressed_counts: Dict[str, int] = {}  # R5: every suppressed attempt counted
        self.alert_fn = lambda msg: None    # runner wires Telegram (WALL_STORM page)
        # P15 Fix A: gross open contracts per (event, market, lane) — net
        # yes-terms masks a filled pair; the walls read BOTH.
        self.gross_open: Dict[Tuple[str, str, str], int] = {}

    # P10 §4 knobs
    WALL_BACKOFF_S = 30.0
    WALL_STORM_N = 20          # >20 same-key rejects in 60s = a storm
    WALL_STORM_WINDOW_S = 60.0

    def _note_wall_reject(self, order: "Order", tag: str, now_mono: float) -> None:
        """§4.2: same-key reject accounting; >20/60s pages ONE WALL_STORM."""
        key = (order.lane, order.market, tag)
        times = self.wall_reject_times.setdefault(key, [])
        times.append(now_mono)
        cutoff = now_mono - self.WALL_STORM_WINDOW_S
        while times and times[0] < cutoff:
            times.pop(0)
        if len(times) > self.WALL_STORM_N:
            if key not in self._storm_paged:
                self._storm_paged.add(key)
                self.alert_fn(f"⚠ WALL_STORM [{tag}] {order.lane} "
                              f"{order.market} n={len(times)}")
                failures.fail("WALL_STORM",
                              f"{tag} {order.lane} {order.market} "
                              f"n={len(times)} in {int(self.WALL_STORM_WINDOW_S)}s",
                              lane=order.lane, market=order.market,
                              wall_tag=tag, alert=False)
        else:
            self._storm_paged.discard(key)  # storm subsided — next one pages again

    def note_backoff(self, market: str, seconds: float = 30.0) -> None:
        import time as _t
        self.backoff_until[market] = _t.monotonic() + seconds

    def in_backoff(self, market: str) -> bool:
        import time as _t
        until = self.backoff_until.get(market)
        if until is None:
            return False
        if _t.monotonic() >= until:
            del self.backoff_until[market]
            return False
        return True

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
    def submit(self, order: Order, book: OrderBook,
               now_mono: Optional[float] = None) -> SubmitResult:
        import time as _t
        now_mono = _t.monotonic() if now_mono is None else now_mono
        tid = threading.get_ident()
        if self._submit_thread is None:
            self._submit_thread = tid
        elif tid != self._submit_thread:
            failures.fail("SUBMIT_THREAD_VIOLATION",
                          f"gateway submit from thread {tid}; the single submit path is "
                          f"pinned to {self._submit_thread}", fatal=True)

        risk_reducing = self.is_risk_reducing(order) or order.purpose in ("EXIT", "CUT")

        # Crossfire is confined to CUT at the canonical layer — a deliberate
        # cross anywhere else (even a passive EXIT) is refused outright.
        if order.crossfire and order.purpose != "CUT":
            raise WallRejection("TAKER_ENTRY",
                                f"crossfire on purpose={order.purpose}; CUT only")

        if not risk_reducing:
            if self.entries_halted_reasons:
                raise WallRejection("ENTRIES_HALTED", ",".join(sorted(self.entries_halted_reasons)))
            if self.in_backoff(order.market):
                # P7: a venue-rejected market rests before it is re-asked
                raise WallRejection("REJECT_BACKOFF",
                                    f"{order.market} in post-reject backoff")
            # P10 §4.1: PRE-wall suppression — an identical rejected ENTRY
            # (same proposal AND same touch) rests 30s. Exits/cuts never reach
            # this branch (risk_reducing above). Counters still count (R5).
            fp_key = (order.side, order.price_cents, order.count,
                      book.best_yes_bid() if book else None,
                      book.best_no_bid() if book else None)
            bo = self.wall_backoff.get((order.lane, order.market))
            if bo is not None:
                tag, stored_fp, until = bo
                if now_mono < until and stored_fp == fp_key:
                    self.suppressed_counts[tag] = \
                        self.suppressed_counts.get(tag, 0) + 1
                    self._note_wall_reject(order, tag, now_mono)  # storms see it
                    raise WallRejection("WALL_BACKOFF",
                                        f"identical re-propose suppressed ({tag})")
                del self.wall_backoff[(order.lane, order.market)]
            try:
                self._wall_flip_unpaired(order)  # P13 §4: the named refusal first
                self._wall_self_net(order)       # P21 A2: netting is an exit's job
                self._wall_unproven_why(order)   # P26 §2: every why is a proof
                self._wall_band_and_single_entry(order)
                self._wall_net_risk_and_at_risk(order)
                self._wall_wrong_way_tick(order)
                self._wall_taker_entry(order, book)
                self._wall_pct_of_book(order)
                self._wall_fee_tripwire(order)
            except WallRejection as e:
                if order.purpose == "ENTRY":
                    self.wall_backoff[(order.lane, order.market)] = \
                        (e.wall, fp_key, now_mono + self.WALL_BACKOFF_S)
                    self._note_wall_reject(order, e.wall, now_mono)
                raise

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
        # The proven engine BUYS ONLY: a sell intent is the complement BUY
        # (venue nets to flat) — flip_math.exit_args is the pinned translation.
        from .flip_math import exit_args
        if order.action == "sell":
            v_side, v_price = exit_args(order.side, order.price_cents)
            v_fp = None  # fp string was the held side's touch; complement re-derives
        else:
            v_side, v_price, v_fp = order.side, order.price_cents, order.rest_fp
        try:
            oid, resp = venue.place_order_maker(
                self.venue_client, order.market, v_side, v_price,
                count=order.count, v2_price_str=v_fp,
                post_only=not order.crossfire,
            )
        except RuntimeError as e:
            # P7: a venue reject is a NORMAL reject with a rest — backoff the
            # market, bank the failure, and let the lane re-propose later.
            self.venue_rejects += 1
            self.note_backoff(order.market)
            err = str(e)
            definitive = "HTTP 4" in err and "HTTP 429" not in err
            failures.fail("VENUE_REJECTED" if definitive else "VENUE_AMBIGUOUS",
                          f"{order.lane} {order.market} {order.side}@{order.price_cents}c: {err[:200]}",
                          lane=order.lane, market=order.market)
            raise WallRejection("VENUE_REJECTED" if definitive else "VENUE_AMBIGUOUS",
                                err[:200])
        self.resting[oid] = order
        self.order_index[oid] = order
        self.live_order_ids.add(oid)
        log.warning("LIVE ORDER PLACED %s %s %s %d@%dc post_only=%s oid=%s",
                    order.lane, order.market, order.side, order.count,
                    order.price_cents, not order.crossfire, oid)
        return SubmitResult(order_id=oid, shadow=False, payload=payload,
                            resp=resp)

    def cancel_tristate(self, order_id: str) -> str:
        """P14 §1: CANCELED | ALREADY_TERMINAL | UNKNOWN. The venue's terminal
        states are TRUTH, not failure — an order that filled (or was already
        canceled) before our cancel is GONE, never a violation. Only an
        unverifiable state is UNKNOWN (the caller's fail-loud business)."""
        order = self.resting.pop(order_id, None)
        if order is None:
            # not resting with us: it filled or canceled already (7:34 race)
            return "ALREADY_TERMINAL"
        if order_id in self.live_order_ids:
            from . import venue
            try:
                status = venue.cancel_order(self.venue_client, order_id)
            except Exception as e:
                self.resting[order_id] = order  # un-verified: put it back, say so
                log.warning("cancel UNKNOWN for %s: %s", order_id, e)
                return "UNKNOWN"
            if status == "canceled":
                return "CANCELED"
            if status == "not_found":
                return "ALREADY_TERMINAL"  # the venue's word: it is gone
            self.resting[order_id] = order
            return "UNKNOWN"
        return "CANCELED"

    def cancel(self, order_id: str) -> bool:
        """Cancels skip walls. True unless the venue state is UNKNOWN."""
        return self.cancel_tristate(order_id) != "UNKNOWN"

    def on_fill(self, order_id: str, count: Optional[int] = None) -> Order:
        """Book a (possibly partial) fill's position effect. The order stays
        resting until its full count is filled. Returns the order."""
        order = self.order_index.get(order_id)
        if order is None:
            failures.fail("FILL_UNKNOWN_ORDER",
                          f"fill for unknown order {order_id}", fatal=True,
                          order_id=order_id)
        cnt = order.count if count is None else int(count)
        key = (order.event, order.market, order.lane)
        sign = 1 if self._signed_yes_delta(order) > 0 else -1
        self.positions[key] = self.positions.get(key, 0) + sign * cnt
        # P15 Fix A: GROSS open contracts — a filled yes+no PAIR nets to zero
        # in yes-terms and would vanish from every wall; gross keeps the
        # paired exposure visible (pending-exposure wall).
        if order.purpose == "ENTRY":
            self.gross_open[key] = self.gross_open.get(key, 0) + cnt
        else:
            self.gross_open[key] = max(0, self.gross_open.get(key, 0) - cnt)
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
        # Lane-scoped single entry, per SIDE: FLIP legitimately quotes BOTH
        # sides of one market (its bundle walls govern the pair); a second
        # entry on the SAME side is the violation. F/H8 additionally enforce
        # their own one-entry-per-ticker inside evaluate (byte-identical port),
        # and a post-fill second leg nets flat -> risk-reducing at the
        # canonical layer, skipping walls entirely (Broker arbitration).
        for o in self.resting.values():
            if (o.lane == order.lane and o.market == order.market
                    and o.purpose == "ENTRY" and o.side == order.side):
                raise WallRejection("SINGLE_ENTRY",
                                    f"lane {order.lane} already resting {order.side} on {order.market}")
        key = (order.event, order.market, order.lane)
        # P15 Fix A: net OR gross — a filled pair (net 0, gross 2) is still
        # a position; a fresh entry on top of it is the double the tape saw.
        if self.positions.get(key, 0) != 0 or self.gross_open.get(key, 0) != 0:
            raise WallRejection("SINGLE_ENTRY", f"lane {order.lane} already positioned on {order.market}")

    def _event_exposure(self, event: str) -> Tuple[int, int]:
        """(net contracts at risk, cents at risk) across lanes for one settlement
        event: open positions + resting ENTRY orders. Resting EXITs never count."""
        contracts = 0
        cents = 0
        # P15 Fix A: exposure = max(|net|, gross) per key — a filled pair
        # (net 0, gross 2) stays visible to the caps.
        keys = set(self.positions) | set(self.gross_open)
        for (ev, market, lane) in keys:
            if ev != event:
                continue
            eff = max(abs(self.positions.get((ev, market, lane), 0)),
                      self.gross_open.get((ev, market, lane), 0))
            if eff:
                contracts += eff
                cents += eff * config.ONE_LOT_MAX_LOSS_CENTS
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
                "NET_RISK",
                f"event {order.event}: {contracts}+{order.count} > {config.NET_RISK_CROSS_LANE_CAP}")
        if cents + order.count * side_basis > config.AT_RISK_CAP_CENTS:
            raise WallRejection(
                "DOLLAR_RISK",
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
                "WRONG_WAY",
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
                    "TAKER_ENTRY",
                    f"{order.side} buy at {order.price_cents}c through derived ask {ask}c")
        else:
            # a sell entry (shorting the side) takes if priced through the side's bid
            bid = book.best_yes_bid() if order.side == "yes" else book.best_no_bid()
            if bid is not None and order.price_cents < bid:
                raise WallRejection(
                    "TAKER_ENTRY",
                    f"{order.side} sell at {order.price_cents}c through bid {bid}c")

    def _wall_unproven_why(self, order: Order) -> None:
        """P27 §2(c) relaxed P26's per-lane proof-field demands: whys
        REPORT, doctrine gates, process gates die (Drew's ruling). The
        wall now enforces the NARRATION LAW only — every ENTRY carries a
        non-empty why string, never a threshold. The lanes still print
        their arithmetic (surv, margin, casefiles) because narration is
        how the packs learn; the wall just stopped grading it."""
        if order.purpose != "ENTRY":
            return
        if not (order.why or "").strip():
            raise WallRejection(
                "REJECT_UNPROVEN_WHY",
                f"{order.lane} ENTRY with an EMPTY why — every dollar "
                f"narrates (the one thing that still walls here)")

    def _wall_self_net(self, order: Order) -> None:
        """P21 A2: no lane BUYS the opposite side of a held market as an
        ENTRY — accidental netting is a bug; deliberate netting is an exit,
        and exits/cuts (risk-reducing) never reach this wall. Purpose
        decides. Scope: the whole ACCOUNT's net on the market — the venue
        nets across our lanes whether we like it or not."""
        if order.purpose != "ENTRY":
            return
        market_net = sum(net for (ev, mkt, ln), net in self.positions.items()
                         if mkt == order.market)
        if market_net == 0:
            return
        buy_dir = 1 if (order.side == "yes") == (order.action == "buy") else -1
        if (market_net > 0) != (buy_dir > 0):
            raise WallRejection(
                "REJECT_SELF_NET",
                f"{order.market}: entry would net down the account's held "
                f"{'yes' if market_net > 0 else 'no'} side (net {market_net:+d})"
                f" — netting is an exit's job, not an entry's")

    def _wall_flip_unpaired(self, order: Order) -> None:
        """P13 §4: FLIP pair integrity — a second SAME-side ENTRY on a market
        requires the prior leg EXITED. The lane's own guard resets per trip;
        this wall is the structural belt."""
        if order.lane != "FLIP" or order.purpose != "ENTRY":
            return
        net = self.positions.get((order.event, order.market, "FLIP"), 0)
        if net == 0:
            return
        adds_same_dir = (net > 0 and self._signed_yes_delta(order) > 0) or \
                        (net < 0 and self._signed_yes_delta(order) < 0)
        if adds_same_dir:
            raise WallRejection(
                "FLIP_UNPAIRED",
                f"prior FLIP leg not yet EXITED (net {net:+d}) — "
                f"second same-side entry refused")

    def _wall_pct_of_book(self, order: Order) -> None:
        caps = self.ledger.boot_caps
        if caps is None:
            failures.fail("BOOT_SEQUENCE_VIOLATION",
                          "boot caps not snapshotted; boot sequence violated",
                          fatal=True)
        notional = order.price_cents * order.count
        if notional > caps.order_budget_cents:
            raise WallRejection(
                "BUDGET", f"{notional}c > boot budget {caps.order_budget_cents}c")

    # _wall_sizing_tier DELETED (P27 §1a): the tier ladder no longer votes
    # anywhere on the entry path — sizing is min(kelly, depth), the tier is
    # a reporting stamp, and the account halt is THE stop.

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
