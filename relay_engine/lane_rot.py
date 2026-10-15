"""WO-ROT-1 Chunks 3, 3A, 4, 5 — the Rotation Desk (lane ROT).

Small maker bets in the 40-54c band across every admitted 15-minute and hourly
series; each exits by a resting cash-out (entry + ROT_TARGET_CENTS) or a
confirmed stop; freed cash rotates into the next-best market.

ONE GATEWAY. Every order this desk makes goes through gateway.submit (entries
purpose ENTRY, cash-outs and maker stop/scratch exits purpose EXIT, the hard
stop purpose CUT with crossfire + immediate_or_cancel + reduce_only). Cancels go
through gateway.cancel_tristate. Fills are read back from the gateway's own
state (filled_counts, positions) — the fills sweep (live) and the pessimistic
shadow sweep (shadow) book them there; the desk never books a fill itself.

TWO LOOPS, inside first (Chunk 3A).
  INSIDE  — the desk's own book: every trip (resting entry, held position,
            unwinding exit) is serviced every cycle from its market's fresh
            book. Exits only. Its book reads come first in the cycle's read
            order (engine.book_poll_set) and claim the budget first.
  OUTSIDE — every admitted market not held: priced from the /markets listing
            (no book read), filtered by band and time, ranked; the top
            ROT_QUEUE_SIZE are the queue, the top few (within what the read
            budget leaves) are the SHORTLIST whose full books are read so a
            freed slot is refilled by rank 1 on the same cycle. Entries only.

THE STOP RULE (Chunk 5), evaluated per read on a held position, in order:
  grace      — nothing for ROT_GRACE_S after the first fill
  hard stop  — held-side bid <= entry - ROT_HARD_STOP_CENTS on ANY read
               -> cancel the cash-out, CONFIRM, then IOC reduce_only sell at
               the bid (purpose CUT)
  soft stop  — bid <= entry - ROT_STOP_CENTS on ROT_CONFIRM_READS reads
               >= ROT_CONFIRM_SPACING_S apart AND spot moved against by
               ROT_SPOT_CONFIRM -> cancel, CONFIRM, then a maker exit one tick
               inside the bid, walked down one tick per read
  late window— final 2 min (15M) / 5 min (1H): hold to settle while the held
               side is the favorite (bid >= 50), else best maker exit
  time stop  — no target fill after ROT_TIME_STOP_S and the bid within
               ROT_TIME_STOP_NEAR_CENTS of entry -> scratch at the best maker
               price
No stop sell is ever placed while a cancel is unconfirmed (UNKNOWN). Before any
exit order: exit size <= held size (venue truth when the venue was read), and
the market's live sells plus this one never exceed it.

UNPROVEN, by name:
  * the seed band stats (CSV: 40-49 h 71% W 8.4 L 14.9; 50-54 h 75% W 7.6
    L 20.9; the 40-49 Wilson 95% lower bound ~62% sits UNDER its 64% h*)
  * "YES loses when spot falls" — true for up/above crypto markets; any other
    market shape must prove it before its soft stop is trusted
  * the exit-side depth reading: held-side bids within ROT_STOP_CENTS of the
    entry (the liquidity a cash-out or stop would sell into)
"""

import logging
import math
import time
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal
from typing import Callable, Dict, List, Optional, Tuple

from . import config
from .book import OrderBook, to_yes_terms
from .errors import WallRejection
from .gateway import Order

log = logging.getLogger("relay.rot")

LANE = "ROT"

# ── skip codes (Chunk 3 §3) — each its own code ──────────────────
SKIP_BAND = "SKIP_BAND"
SKIP_TIME = "SKIP_TIME"
SKIP_DEPTH = "SKIP_DEPTH"
SKIP_HELD = "SKIP_HELD"
SKIP_CAP_DEPLOY = "SKIP_CAP_DEPLOY"
SKIP_CAP_GROUP = "SKIP_CAP_GROUP"
SKIP_CAP_MARKET = "SKIP_CAP_MARKET"
SKIP_BAND_PAUSED = "SKIP_BAND_PAUSED"
SKIP_BELOW_ONE_LOT = "SKIP_BELOW_ONE_LOT"
# beyond the WO's nine: the other honest reasons a market is not entered
SKIP_NO_QUOTE = "SKIP_NO_QUOTE"            # listing has no two-sided touch
SKIP_NO_SERIES = "SKIP_NO_SERIES"          # market's series not admitted
SKIP_NO_CLOSE = "SKIP_NO_CLOSE"            # no close time on the record
SKIP_SCORE = "SKIP_SCORE"                  # band scores <= 0 (not entered)
SKIP_QUEUE = "SKIP_QUEUE"                  # in band, ranked below the queue
SKIP_NO_BOOK = "SKIP_NO_BOOK"              # shortlisted, book not read yet
SKIP_NO_CASH = "SKIP_NO_CASH"              # spendable cash < this entry's cost
SKIP_ENTRIES_HALTED = "SKIP_ENTRIES_HALTED"
SKIP_WALL = "SKIP_WALL"                    # the gateway refused it (wall named)
SKIP_TRADED_WINDOW = "SKIP_TRADED_WINDOW"  # this market already had a trip this window

# exit paths (rot_trips.exit_path in Chunk 7)
TARGET, SOFT_STOP, HARD_STOP, TIME_STOP, LATE, SETTLE = (
    "TARGET", "SOFT_STOP", "HARD_STOP", "TIME", "LATE", "SETTLE")
NO_FILL = "NO_FILL"   # entry expired unfilled: not a trip, nothing owed


# ── bands and ranking (Chunk 3A) ─────────────────────────────────

@dataclass(frozen=True)
class BandStats:
    h: float           # hit rate (target before stop)
    W: float           # avg win, cents/contract
    L: float           # avg loss, cents/contract (incl. taker fees on hard stops)
    hold_win_s: float
    hold_loss_s: float
    n: int
    source: str

    def ev_cents(self) -> float:
        return self.h * self.W - (1.0 - self.h) * self.L

    def hold_minutes(self) -> float:
        return (self.h * self.hold_win_s + (1.0 - self.h) * self.hold_loss_s) / 60.0

    def h_star(self) -> float:
        """Break-even hit rate L / (W + L)."""
        return self.L / (self.W + self.L) if (self.W + self.L) > 0 else 1.0


# LENS: UNPROVEN — the CSV seed (WO-ROT-1 Chunk 3A §2); winners lingered 310 s,
# losers 515 s in the same CSV. Replaced per band by its own last
# ROT_GUARD_TRIPS trips once it has that many (Chunk 7 wires rot_trips).
SEED_STATS = {
    "40-49": BandStats(0.71, 8.4, 14.9, 310.0, 515.0, 0, "CSV seed (UNPROVEN)"),
    "50-54": BandStats(0.75, 7.6, 20.9, 310.0, 515.0, 0, "CSV seed (UNPROVEN)"),
}


def band_of(cost: int) -> Optional[str]:
    if 40 <= cost <= 49:
        return "40-49"
    if 50 <= cost <= 54:
        return "50-54"
    return None


def score(stats: Optional[BandStats], cost: int) -> float:
    """Expected cents per dollar per minute: (h W - (1-h) L) / cost / hold.
    A band with no stats scores 0 (not entered)."""
    if stats is None or cost <= 0:
        return 0.0
    hold = stats.hold_minutes()
    if hold <= 0:
        return 0.0
    return stats.ev_cents() / cost * 100.0 / hold


def side_costs(yes_bid: Optional[int], yes_ask: Optional[int]) -> Dict[str, int]:
    """The maker entry cost per side at the touch. YES joins the YES bid; NO
    joins the NO bid, which IS 100 minus the YES ask — converted through the one
    YES-terms function (book.to_yes_terms), never by hand."""
    out = {}
    if yes_bid is not None:
        out["yes"] = int(round(to_yes_terms("yes", yes_bid)))
    if yes_ask is not None:
        out["no"] = int(round(to_yes_terms("no", yes_ask)))
    return out


def min_time_left_s(cadence: str) -> int:
    return (config.ROT_MIN_TIME_LEFT_15M_S if cadence == "15M"
            else config.ROT_MIN_TIME_LEFT_1H_S)


def late_window_s(cadence: str) -> int:
    return (config.ROT_LATE_WINDOW_15M_S if cadence == "15M"
            else config.ROT_LATE_WINDOW_1H_S)


def pick_side(yes_bid, yes_ask, secs_left: float, cadence: str
              ) -> Tuple[Optional[str], Optional[int], str]:
    """(side, cost, "") or (None, None, skip code). Band first, then time.
    Both sides in band -> the cheaper; a tie takes YES (deterministic)."""
    costs = side_costs(yes_bid, yes_ask)
    if not costs:
        return None, None, SKIP_NO_QUOTE
    lo, hi = config.ROT_ENTRY_BAND
    inband = {s: c for s, c in costs.items() if lo <= c <= hi}
    if not inband:
        return None, None, SKIP_BAND
    if secs_left < min_time_left_s(cadence):
        return None, None, SKIP_TIME
    side = min(inband, key=lambda s: (inband[s], s != "yes"))
    return side, inband[side], ""


def exit_side_depth(book: OrderBook, side: str, cost: int):
    """Contracts bid on the HELD side at prices >= cost - ROT_STOP_CENTS: the
    liquidity a cash-out or a stop sells into (UNPROVEN reading of the WO's
    'resting on the exit side')."""
    bids = book.yes_bids if side == "yes" else book.no_bids
    floor = cost - config.ROT_STOP_CENTS
    return sum(q for p, q in bids.items() if p >= floor)


def held_bid(book: OrderBook, side: str) -> Optional[int]:
    return book.best_yes_bid() if side == "yes" else book.best_no_bid()


def held_ask(book: OrderBook, side: str) -> Optional[int]:
    """The held side's ask = 100 - the opposite side's best bid."""
    opp = book.best_no_bid() if side == "yes" else book.best_yes_bid()
    return None if opp is None else 100 - opp


# ── candidates ───────────────────────────────────────────────────

@dataclass
class Candidate:
    market: str
    series: str
    underlying: str
    cadence: str
    side: str
    cost: int
    band: str
    score: float
    secs_left: float
    close_ts: float
    depth: Optional[float] = None     # known once the book is read
    group_used_c: float = 0.0
    rank: int = 0

    def sort_key(self):
        # score, then deeper exit-side book, then more time, then the group
        # with less of the bankroll in use (Chunk 3A §3)
        return (-self.score, -(self.depth or 0), -self.secs_left, self.group_used_c)


# ── trips (Chunk 5) ──────────────────────────────────────────────

@dataclass
class Trip:
    market: str
    event: str
    series: str
    underlying: str
    cadence: str
    side: str
    cost: int
    size: Decimal
    close_ts: float
    band: str
    rank: int
    score: float
    entry_oid: str
    entry_ts: float
    state: str = "ENTRY"        # ENTRY | HELD | UNWIND | WALK | CUT | CLOSED
    entry_open: bool = True     # the entry order may still fill
    filled: Decimal = Decimal(0)
    fill_ts: Optional[float] = None
    spot_at_fill: Optional[float] = None
    cashouts: Dict[str, Decimal] = field(default_factory=dict)  # oid -> size rested
    exit_oid: Optional[str] = None   # the current maker stop/scratch/late exit
    exit_px: Optional[int] = None
    pending: Optional[str] = None    # the exit path being executed
    soft_reads: List[Tuple[float, int]] = field(default_factory=list)
    late_hold: bool = False
    reads: int = 0
    closed_path: Optional[str] = None
    closed_ts: Optional[float] = None
    trigger_px: Optional[int] = None
    why_log: List[str] = field(default_factory=list)

    @property
    def key(self):
        return (self.event, self.market, LANE)


def _dec(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def _norm(d: Decimal):
    """int when whole (gateway/venue speak ints for whole counts)."""
    d = _dec(d)
    return int(d) if d == d.to_integral_value() else d


class RotationDesk:
    """The desk. Pure of I/O except through the gateway it is handed; the
    engine supplies books, the listing (market_meta), and the clock."""

    def __init__(self, gateway, ledger, alert: Callable[[str], None],
                 spot_fn: Optional[Callable[[str], Optional[float]]] = None,
                 spot_confirm_fn: Optional[Callable[[str], Optional[float]]] = None,
                 venue_held_fn: Optional[Callable[[str], Optional[Decimal]]] = None,
                 stats_fn: Optional[Callable[[str], Optional[BandStats]]] = None):
        self.gateway = gateway
        self.ledger = ledger
        self.alert = alert
        self.spot_fn = spot_fn or (lambda underlying: None)
        self.spot_confirm_fn = spot_confirm_fn or default_spot_confirm
        # venue_held_fn(market) -> the venue's signed YES-terms net on the
        # market, or None when the venue was not read this cycle (shadow: None)
        self.venue_held_fn = venue_held_fn or (lambda market: None)
        self.stats_fn = stats_fn or (lambda band: SEED_STATS.get(band))
        self.trips: Dict[str, Trip] = {}
        self.closed: List[Trip] = []
        # markets that already had a trip (any outcome, NO_FILL included):
        # no re-entry in the same window unless ROT_REENTER_SAME_WINDOW — an
        # expired entry is never chased (Chunk 5 §1)
        self.traded: set = set()
        self.queue: List[Candidate] = []
        self.shortlist: List[str] = []
        self.verdicts: Dict[str, str] = {}       # market -> last skip code / ENTERED
        self.paused_bands: set = set()           # Chunk 8 break-even guard
        self.events: List[tuple] = []            # ordered trace (tests + tape)
        self.replacements: List[dict] = []
        self._freed_at: Optional[float] = None
        self._freed_by: Optional[str] = None
        self.first_reuse_delay_s: Optional[float] = None

    # ── bookkeeping ──────────────────────────────────────────────
    def _ev(self, *e):
        self.events.append(e)
        if len(self.events) > 5000:
            del self.events[:1000]

    def book_cents(self) -> int:
        """The book the caps and size measure against: total capital (venue
        cash + capital at risk, minus owed — ledger.ensemble_base_cents)."""
        return int(self.ledger.ensemble_base_cents())

    def spendable_cents(self) -> int:
        """Venue cash minus owed scrape (ledger.tradeable_cents)."""
        return int(self.ledger.tradeable_cents())

    def held(self, trip: Trip) -> Decimal:
        """Held contracts on the trip's side. The gateway's booked position,
        capped by the venue's when the venue was read (Venue Speaks Last)."""
        g = _dec(self.gateway.positions.get(trip.key, 0))
        g = max(Decimal(0), g if trip.side == "yes" else -g)
        v = self.venue_held_fn(trip.market)      # signed, YES-terms net
        if v is None:
            return g
        v = _dec(v)
        return min(g, max(Decimal(0), v if trip.side == "yes" else -v))

    def exposure(self) -> Tuple[Decimal, Dict[str, Decimal], Dict[str, Decimal]]:
        """(total, by group, by market) cents at cost: held x entry cost plus
        the unfilled remainder of resting entries."""
        tot = Decimal(0)
        grp: Dict[str, Decimal] = {}
        mkt: Dict[str, Decimal] = {}
        for t in self.trips.values():
            c = self.held(t) * t.cost
            if t.entry_open:
                c += max(Decimal(0), t.size - t.filled) * t.cost
            tot += c
            grp[t.underlying] = grp.get(t.underlying, Decimal(0)) + c
            mkt[t.market] = mkt.get(t.market, Decimal(0)) + c
        return tot, grp, mkt

    def market_held_elsewhere(self, market: str) -> bool:
        """Never two positions in one market — any lane, QUARANTINE included,
        and any resting order of ours on it."""
        if market in self.trips:
            return True
        for (_e, m, _l), net in self.gateway.positions.items():
            if m == market and net:
                return True
        return any(o.market == market for o in self.gateway.resting.values())

    # ── Chunk 4: caps and size ───────────────────────────────────
    def size_for(self, cost: int) -> Tuple[Optional[Decimal], str]:
        from .sizing import rot_size_contracts
        book = self.book_cents()
        budget = int(math.floor(book * config.ROT_POSITION_PCT))
        return rot_size_contracts(max(0, budget), cost)

    def caps_check(self, underlying: str, market: str, new_cost_c: Decimal) -> str:
        """'' when the entry fits every cap AFTER this order, else the code.
        Market, then group, then deploy (narrowest first)."""
        book = Decimal(self.book_cents())
        tot, grp, mkt = self.exposure()
        if mkt.get(market, Decimal(0)) + new_cost_c > book * Decimal(str(config.ROT_MARKET_CAP_PCT)):
            return SKIP_CAP_MARKET
        if grp.get(underlying, Decimal(0)) + new_cost_c > book * Decimal(str(config.ROT_GROUP_CAP_PCT)):
            return SKIP_CAP_GROUP
        if tot + new_cost_c > book * Decimal(str(config.ROT_DEPLOY_CAP_PCT)):
            return SKIP_CAP_DEPLOY
        return ""

    # ── the cycle ────────────────────────────────────────────────
    def step(self, now: float, books: Dict[str, OrderBook], meta: Dict[str, dict],
             budget_reads: int) -> None:
        """One desk cycle: inside loop (every trip), then outside loop (rank),
        then fill freed slots from the queue — all on the same cycle."""
        self._ev("cycle", now)
        self.inside(now, books, meta)
        self.outside(now, books, meta, budget_reads - len(self.trips))
        self.fill_slots(now, books)

    # ── INSIDE loop ──────────────────────────────────────────────
    def inside(self, now: float, books: Dict[str, OrderBook],
               meta: Optional[Dict[str, dict]] = None) -> None:
        for market in list(self.trips):
            t = self.trips[market]
            self._ev("inside", market)
            m = (meta or {}).get(market)
            if m and m.get("close_ts") and t.close_ts != m["close_ts"]:
                t.close_ts = float(m["close_ts"])   # exchange truth wins
            book = books.get(market)
            try:
                self.service(t, now, book)
            except WallRejection as e:
                t.why_log.append(f"{now:.0f} wall {e.wall}: {e.detail}")
                log.warning("ROT %s exit refused by wall %s: %s", market, e.wall, e.detail)
            if t.state == "CLOSED":
                self._close(t, now)

    def service(self, t: Trip, now: float, book: Optional[OrderBook]) -> None:
        t.reads += 1
        self._sync_entry(t, now)
        # settlement: the window is over — whatever is held settles
        if now >= t.close_ts:
            self._cancel_all(t)
            if t.filled > 0:
                self._finish(t, now, SETTLE if self.held(t) > 0 else (t.pending or TARGET))
            else:
                self._finish(t, now, NO_FILL)
            return
        held = self.held(t)
        if t.filled > 0 and held <= 0 and not t.entry_open:
            # flat after a fill: the cash-out (or an exit) took us out
            self._cancel_all(t)
            self._finish(t, now, t.pending or TARGET)
            return
        if t.filled == 0:
            if not t.entry_open:
                self._finish(t, now, NO_FILL)
            return
        if book is None:
            t.why_log.append(f"{now:.0f} no book this read")
            return
        bid = held_bid(book, t.side)
        if bid is None:
            t.why_log.append(f"{now:.0f} held side has no bid")
            return

        if t.state == "HELD":
            self._grow_cashout(t, book)
            path = self._stop_rule(t, now, bid)
            if path is not None:
                t.pending = path
                t.trigger_px = bid
                t.state = "UNWIND"
                self._ev("trigger", t.market, path, bid)
        elif t.state in ("WALK", "CUT") and t.pending != HARD_STOP:
            # a hard-stop print outranks a walking soft/time/late exit
            if (now - (t.fill_ts or now) >= config.ROT_GRACE_S
                    and bid <= t.cost - config.ROT_HARD_STOP_CENTS):
                t.pending, t.trigger_px, t.state = HARD_STOP, bid, "UNWIND"
                self._ev("trigger", t.market, HARD_STOP, bid)

        if t.state == "UNWIND":
            if not self._cancel_all(t):
                t.why_log.append(f"{now:.0f} cancel UNKNOWN — no stop sell until confirmed")
                self._ev("cancel_unconfirmed", t.market)
                return
            held = self.held(t)
            if held <= 0:
                # the cash-out filled while the stop was being armed
                self._finish(t, now, TARGET)
                return
            self._act(t, now, book, bid, held)
        elif t.state == "WALK":
            self._walk(t, now, book, bid)
        elif t.state == "CUT":
            # an IOC that did not take the whole position: re-fire at the bid
            held = self.held(t)
            if held > 0:
                self._fire_cut(t, now, book, bid, held)

    # entry fills / expiry
    def _sync_entry(self, t: Trip, now: float) -> None:
        f = _dec(self.gateway.filled_counts.get(t.entry_oid, 0))
        if f > t.filled:
            if t.filled == 0:
                t.fill_ts = now
                t.spot_at_fill = self.spot_fn(t.underlying)
                if t.state == "ENTRY":
                    t.state = "HELD"
            t.filled = f
            self._ev("entry_fill", t.market, f)
        if t.entry_open:
            done = t.entry_oid not in self.gateway.resting or t.filled >= t.size
            expired = now - t.entry_ts >= config.ROT_ENTRY_REST_S
            if done:
                t.entry_open = False
            elif expired or t.state not in ("ENTRY", "HELD"):
                st = self.gateway.cancel_tristate(t.entry_oid)
                if st != "UNKNOWN":
                    t.entry_open = False
                    self._ev("entry_cancel", t.market, st)
                    # a fill that raced the cancel is booked by the sweep; read
                    # it once more so the position is never under-counted
                    f = _dec(self.gateway.filled_counts.get(t.entry_oid, 0))
                    if f > t.filled:
                        if t.filled == 0:
                            t.fill_ts = now
                            t.state = "HELD"
                        t.filled = f

    def _grow_cashout(self, t: Trip, book: OrderBook) -> None:
        """High leg: rest the cash-out at entry + target for every filled
        contract not yet covered. Partial fills grow it in deltas."""
        covered = sum(self._live_cashouts(t).values(), Decimal(0))
        held = self.held(t)
        delta = held - covered
        if delta <= 0:
            return
        px = t.cost + config.ROT_TARGET_CENTS
        bid = held_bid(book, t.side)
        if bid is not None and px <= bid:
            px = bid + 1          # a maker sell never prices into the bid
        px = min(99, px)
        oid = self._submit_exit(t, book, px, delta, "EXIT",
                                f"ROT cash-out +{config.ROT_TARGET_CENTS}c on "
                                f"entry {t.cost}c (filled {delta})")
        if oid:
            t.cashouts[oid] = delta
            self._ev("cashout", t.market, px, delta)

    def _live_cashouts(self, t: Trip) -> Dict[str, Decimal]:
        """Cash-outs still resting, sized by their unfilled remainder."""
        out = {}
        for oid, sz in list(t.cashouts.items()):
            if oid in self.gateway.resting:
                out[oid] = sz - _dec(self.gateway.filled_counts.get(oid, 0))
            else:
                t.cashouts.pop(oid, None)
        return out

    def _stop_rule(self, t: Trip, now: float, bid: int) -> Optional[str]:
        """The Record's stop rule, in order. Returns the exit path to execute."""
        since = now - (t.fill_ts or now)
        if since < config.ROT_GRACE_S:
            return None                                          # 1 grace
        if bid <= t.cost - config.ROT_HARD_STOP_CENTS:
            return HARD_STOP                                     # 3 hard (any read)
        if bid <= t.cost - config.ROT_STOP_CENTS:                # 2 soft
            if not t.soft_reads or now - t.soft_reads[-1][0] >= config.ROT_CONFIRM_SPACING_S:
                t.soft_reads.append((now, bid))
            if len(t.soft_reads) >= config.ROT_CONFIRM_READS:
                moved = self._spot_against(t)
                need = self.spot_confirm_fn(t.underlying)
                if moved is None or need is None:
                    t.why_log.append(f"{now:.0f} soft stop unconfirmed: no spot "
                                     f"for {t.underlying} (hard stop still armed)")
                    self._ev("soft_unconfirmed", t.market)
                elif moved >= need:
                    return SOFT_STOP
        else:
            t.soft_reads.clear()
        secs_left = t.close_ts - now
        if secs_left <= late_window_s(t.cadence):                 # 5 late window
            if bid >= 50:
                if not t.late_hold:
                    t.late_hold = True
                    self._ev("late_hold", t.market, bid)
                return None
            return LATE
        if (since >= config.ROT_TIME_STOP_S                       # 4 time stop
                and abs(bid - t.cost) <= config.ROT_TIME_STOP_NEAR_CENTS):
            return TIME_STOP
        return None

    def _spot_against(self, t: Trip) -> Optional[float]:
        """Spot move AGAINST the held side since fill (points; >0 = against).
        UNPROVEN mapping: YES gains when spot rises (up/above markets)."""
        if t.spot_at_fill is None:
            return None
        s = self.spot_fn(t.underlying)
        if s is None:
            return None
        move = s - t.spot_at_fill
        return -move if t.side == "yes" else move

    def _cancel_all(self, t: Trip) -> bool:
        """Cancel the entry remainder, every cash-out and the current exit.
        True only when every one is CONFIRMED gone (CANCELED or terminal)."""
        ok = True
        oids = list(t.cashouts) + ([t.exit_oid] if t.exit_oid else [])
        if t.entry_open:
            oids.append(t.entry_oid)
        for oid in oids:
            if oid not in self.gateway.resting:
                self._forget(t, oid)
                continue
            st = self.gateway.cancel_tristate(oid)
            self._ev("cancel", t.market, oid, st)
            if st == "UNKNOWN":
                ok = False
            else:
                self._forget(t, oid)
        return ok

    def _forget(self, t: Trip, oid: str) -> None:
        t.cashouts.pop(oid, None)
        if oid == t.exit_oid:
            t.exit_oid = None
        if oid == t.entry_oid:
            t.entry_open = False

    def _act(self, t: Trip, now: float, book: OrderBook, bid: int, held: Decimal) -> None:
        if t.pending == HARD_STOP:
            t.state = "CUT"
            self._fire_cut(t, now, book, bid, held)
            return
        # soft stop / time stop / late exit: a maker exit one tick inside the
        # bid (soft walks down a tick per read; time/late rest at the best
        # maker price and keep re-quoting to it)
        t.state = "WALK"
        t.exit_px = None
        self._walk(t, now, book, bid)

    def _walk(self, t: Trip, now: float, book: OrderBook, bid: int) -> None:
        held = self.held(t)
        if held <= 0:
            return
        if t.exit_oid is not None and t.exit_oid in self.gateway.resting:
            # still resting: step it (soft: one tick down per read; time/late:
            # back to the best maker price) — cancel CONFIRMED first
            target = self._walk_price(t, bid)
            if target == t.exit_px:
                return
            st = self.gateway.cancel_tristate(t.exit_oid)
            self._ev("cancel", t.market, t.exit_oid, st)
            if st == "UNKNOWN":
                t.why_log.append(f"{now:.0f} walk cancel UNKNOWN — holding")
                return
            t.exit_oid = None
            held = self.held(t)
            if held <= 0:
                return
        px = self._walk_price(t, bid)
        oid = self._submit_exit(t, book, px, held, "EXIT",
                                f"ROT {t.pending} maker exit @{px}c (bid {bid}c, "
                                f"entry {t.cost}c)")
        if oid:
            t.exit_oid, t.exit_px = oid, px
            self._ev("walk", t.market, t.pending, px, held)

    def _walk_price(self, t: Trip, bid: int) -> int:
        inside = bid + 1                              # one tick inside the bid
        if t.pending == SOFT_STOP and t.exit_px is not None:
            return max(inside, t.exit_px - 1) if t.exit_px - 1 > bid else inside
        return inside

    def _fire_cut(self, t: Trip, now: float, book: OrderBook, bid: int, held: Decimal) -> None:
        if self._sells_live(t.market) > 0:
            self._ev("cut_blocked_live_sell", t.market)
            return
        o = Order(lane=LANE, event=t.event, market=t.market, side=t.side,
                  action="sell", price_cents=bid, count=_norm(held),
                  size_tier=config.TIER_PROBE, purpose="CUT", band=(1, 99),
                  crossfire=True, ioc=True, reduce_only=True,
                  reason=f"ROT HARD_STOP bid {bid}c <= entry {t.cost}c - "
                         f"{config.ROT_HARD_STOP_CENTS}")
        self._assert_exit_size(t, held)
        res = self.gateway.submit(o, book)
        self._ev("cut", t.market, bid, held, getattr(res, "ioc_filled", None))

    def _sells_live(self, market: str) -> Decimal:
        tot = Decimal(0)
        for oid, o in self.gateway.resting.items():
            if o.market == market and o.action == "sell":
                tot += _dec(o.count) - _dec(self.gateway.filled_counts.get(oid, 0))
        return tot

    def _assert_exit_size(self, t: Trip, size: Decimal) -> None:
        """Oversize guard: this exit plus every live sell on the market must
        not exceed what is held. Refused loudly — never sold."""
        held = self.held(t)
        live = self._sells_live(t.market)
        if size <= 0 or size + live > held:
            from . import failures
            failures.fail("ROT_EXIT_OVERSIZE",
                          f"{t.market}: exit {size} + live sells {live} > held {held}"
                          " — refused (never sell more than held)",
                          alert=True, market=t.market)
            raise WallRejection("ROT_EXIT_OVERSIZE", f"{size}+{live}>{held}")

    def _submit_exit(self, t: Trip, book: OrderBook, px: int, size: Decimal,
                     purpose: str, reason: str) -> Optional[str]:
        self._assert_exit_size(t, size)
        o = Order(lane=LANE, event=t.event, market=t.market, side=t.side,
                  action="sell", price_cents=int(px), count=_norm(size),
                  size_tier=config.TIER_PROBE, purpose=purpose, band=(1, 99),
                  reason=reason)
        res = self.gateway.submit(o, book)
        return res.order_id

    def _finish(self, t: Trip, now: float, path: str) -> None:
        t.state = "CLOSED"
        t.closed_path = path
        t.closed_ts = now

    def _close(self, t: Trip, now: float) -> None:
        if t.closed_path == SETTLE:
            # the contracts settled at the venue: no position remains to cap
            self.gateway.settle_key(t.key)
        self.trips.pop(t.market, None)
        self.closed.append(t)
        self.verdicts[t.market] = f"CLOSED_{t.closed_path}"
        self._freed_at, self._freed_by = now, f"{t.market} {t.closed_path}"
        self._ev("closed", t.market, t.closed_path)
        if t.closed_path != NO_FILL:
            c = self.gateway.last_concluded.get(t.key)
            net = "" if c is None else f" net {c['net_cents']:+}¢"
            self.alert(f"🔁 ROT {t.closed_path} {t.market} {t.side} entry {t.cost}¢ "
                       f"x{_norm(t.filled)}{net} · held {now - (t.fill_ts or now):.0f}s")

    # ── OUTSIDE loop ─────────────────────────────────────────────
    def outside(self, now: float, books: Dict[str, OrderBook],
                meta: Dict[str, dict], budget_left: int) -> None:
        """Rank every admitted market not held from its listing touch. Keeps
        the top ROT_QUEUE_SIZE; the top `budget_left` (capped) are shortlisted
        for full-book reads."""
        cands: List[Candidate] = []
        _tot, grp, _m = self.exposure()
        for market, m in meta.items():
            series = config.series_of(market)
            info = config.SERIES_INFO.get(series)
            if info is None:
                self.verdicts[market] = SKIP_NO_SERIES
                continue
            if self.market_held_elsewhere(market):
                self.verdicts[market] = SKIP_HELD
                continue
            if market in self.traded and not config.ROT_REENTER_SAME_WINDOW:
                self.verdicts[market] = SKIP_TRADED_WINDOW
                continue
            close_ts = m.get("close_ts")
            if close_ts is None:
                self.verdicts[market] = SKIP_NO_CLOSE
                continue
            side, cost, why = pick_side(m.get("rec_yes_bid"), m.get("rec_yes_ask"),
                                        close_ts - now, info.cadence)
            if side is None:
                self.verdicts[market] = why
                continue
            band = band_of(cost)
            if band in self.paused_bands:
                self.verdicts[market] = SKIP_BAND_PAUSED
                continue
            sc = score(self.stats_fn(band), cost)
            if sc <= 0:
                self.verdicts[market] = SKIP_SCORE
                continue
            c = Candidate(market, series, info.underlying, info.cadence, side, cost,
                          band, sc, close_ts - now, close_ts,
                          group_used_c=float(grp.get(info.underlying, 0)))
            b = books.get(market)
            if b is not None:
                c.depth = float(exit_side_depth(b, side, cost))
            cands.append(c)
        cands.sort(key=Candidate.sort_key)
        self.queue = cands[:config.ROT_QUEUE_SIZE]
        for i, c in enumerate(self.queue, 1):
            c.rank = i
            self.verdicts[c.market] = f"QUEUED#{i}"
        for c in cands[config.ROT_QUEUE_SIZE:]:
            self.verdicts[c.market] = SKIP_QUEUE
        k = max(0, min(int(budget_left), config.ROT_SHORTLIST_MAX))
        self.shortlist = [c.market for c in self.queue[:k]]
        for m in self.shortlist:
            self._ev("outside", m)

    def forget_market(self, market: str) -> None:
        """The window closed (the engine pruned the market)."""
        self.traded.discard(market)
        self.verdicts.pop(market, None)

    def free_slots(self) -> bool:
        tot, _g, _m = self.exposure()
        return tot < Decimal(self.book_cents()) * Decimal(str(config.ROT_DEPLOY_CAP_PCT))

    def fill_slots(self, now: float, books: Dict[str, OrderBook]) -> None:
        """Rank order: the first candidate that passes book, depth, caps and
        size gets the slot — this cycle."""
        for c in list(self.queue):
            if not self.free_slots():
                return
            if c.market in self.trips or self.market_held_elsewhere(c.market):
                continue
            if self.gateway.entries_halted_for(config.halt_scope(c.series, LANE)):
                self.verdicts[c.market] = SKIP_ENTRIES_HALTED
                continue
            self._try_enter(c, now, books.get(c.market))

    def _try_enter(self, c: Candidate, now: float, book: Optional[OrderBook]) -> bool:
        if book is None:
            self.verdicts[c.market] = SKIP_NO_BOOK
            return False
        # re-pick on the fresh book (the listing touch may be stale)
        yb = book.best_yes_bid()
        nb = book.best_no_bid()
        ya = None if nb is None else 100 - nb
        side, cost, why = pick_side(yb, ya, c.close_ts - now, c.cadence)
        if side is None:
            self.verdicts[c.market] = why
            return False
        size, why = self.size_for(cost)
        if size is None:
            self.verdicts[c.market] = why
            return False
        depth = exit_side_depth(book, side, cost)
        if _dec(depth) < size * config.ROT_MIN_DEPTH_MULT:
            self.verdicts[c.market] = SKIP_DEPTH
            return False
        new_cost = size * cost
        code = self.caps_check(c.underlying, c.market, new_cost)
        if code:
            self.verdicts[c.market] = code
            return False
        if new_cost > self.spendable_cents():
            self.verdicts[c.market] = SKIP_NO_CASH
            return False
        band = band_of(cost) or c.band
        st = self.stats_fn(band)
        why_s = (f"ROT band {band} rank {c.rank} score {c.score:.3f}c/$/min "
                 f"h {st.h:.2f} W {st.W} L {st.L} ({st.source}) · {side}@{cost}c "
                 f"x{_norm(size)} · {c.cadence} {c.underlying} "
                 f"{c.close_ts - now:.0f}s left · depth {depth}")
        o = Order(lane=LANE, event=c.market.rsplit("-", 1)[0], market=c.market,
                  side=side, action="buy", price_cents=cost, count=_norm(size),
                  size_tier=config.TIER_PROBE, purpose="ENTRY",
                  band=config.ROT_ENTRY_BAND, why=why_s)
        try:
            res = self.gateway.submit(o, book)
        except WallRejection as e:
            self.verdicts[c.market] = f"{SKIP_WALL}:{e.wall}"
            self._ev("wall", c.market, e.wall)
            return False
        t = Trip(market=c.market, event=o.event, series=c.series,
                 underlying=c.underlying, cadence=c.cadence, side=side,
                 cost=o.price_cents, size=_dec(size), close_ts=c.close_ts,
                 band=band, rank=c.rank, score=c.score,
                 entry_oid=res.order_id, entry_ts=now)
        self.trips[c.market] = t
        self.traded.add(c.market)
        self.verdicts[c.market] = "ENTERED"
        self._ev("enter", c.market, c.rank, side, cost, size)
        rep = {"ts": now, "market": c.market, "rank": c.rank,
               "score": round(c.score, 4), "freed_by": self._freed_by}
        self.replacements.append(rep)
        if self._freed_at is not None:
            log.warning("ROT REPLACEMENT slot freed by %s -> filled by %s rank %d "
                        "score %.4f", self._freed_by, c.market, c.rank, c.score)
            if self.first_reuse_delay_s is None:
                # T+0 cash reuse on Kalshi is UNPROVEN until seen on tape
                self.first_reuse_delay_s = now - self._freed_at
                self.alert(f"⏱ ROT first cash reuse: slot freed by {self._freed_by} "
                           f"refilled {self.first_reuse_delay_s:.1f}s later by "
                           f"{c.market} (rank {c.rank})")
            self._freed_at = self._freed_by = None
        return True


def adopt_trip(desk: "RotationDesk", market: str, side: str, count, cost: int,
               now: float, close_ts: Optional[float]) -> Trip:
    """WO-ROT-1: re-adopt a ROT position our own fills explain, at boot. Only
    called when NOTHING of ours rests on the market (reconcile checks), so the
    desk's fresh cash-out can never sit beside an old one. The grace restarts
    at adoption (the stop rule reads the position from here); the gap is
    logged by the caller."""
    series = config.series_of(market)
    info = config.SERIES_INFO.get(series)
    from .lanes import infer_close_ts_from_ticker
    t = Trip(market=market, event=market.rsplit("-", 1)[0], series=series,
             underlying=info.underlying if info else (series or "?"),
             cadence=info.cadence if info else "15M", side=side, cost=int(cost),
             size=_dec(count), close_ts=float(close_ts or infer_close_ts_from_ticker(market)
                                             or now + 3600),
             band=band_of(int(cost)) or "?", rank=0, score=0.0,
             entry_oid=f"ADOPTED-{market}", entry_ts=now, state="HELD",
             entry_open=False, filled=_dec(count), fill_ts=now,
             spot_at_fill=desk.spot_fn(info.underlying if info else ""))
    t.why_log.append(f"{now:.0f} adopted at boot: {side} x{count} @ {cost}c")
    desk.trips[market] = t
    desk.traded.add(market)
    return t


def shutdown_cancel(desk: "RotationDesk") -> List[str]:
    """Clean shutdown: cancel every ROT resting order (entries, cash-outs,
    stop/scratch exits). Positions stay held; the next boot re-adopts them
    cleanly (nothing resting). Returns the markets whose cancel was UNKNOWN."""
    unknown = []
    for t in list(desk.trips.values()):
        if not desk._cancel_all(t):
            unknown.append(t.market)
    return unknown


def default_spot_confirm(underlying: str) -> Optional[float]:
    """ROT_SPOT_CONFIRM: BTC 40 points (the Record: a 15-point trigger caused
    every catastrophic FLIP loss). Every other underlying is None until Chunk 6
    measures 2x its median 1-minute move — so its soft stop cannot confirm and
    only the hard stop protects it (said on the tape, never assumed)."""
    if underlying == "BTC":
        return float(config.ROT_SPOT_CONFIRM_BTC_PTS)
    return None
