"""WO-END-1 — LANE B, the end-game pile-on (OFF by default, shadow-safe).

The desk's SECOND phase, mirroring lane_rot.RotationDesk's shape (one gateway,
one ledger, one pager; the desk never books a fill itself — the fills sweep /
shadow sweep do). Where Lane A (ROT) buys near 50c early and cashes out the
oscillation BEFORE the end-game, Lane B waits for the final seconds and piles
onto the DECIDED favorite (95-97c) for the last pennies.

THE EDGE — a live running-60-second-average gap filter (load-bearing, §1):
  A 15-minute crypto binary settles on the EQUAL-WEIGHT AVERAGE of the final 60
  one-second index prints. With N of those 60 samples already in, the running
  average R and the strike K give a gap  G = R - K  (favorite = YES when G > 0).
  For the favorite to FLIP, the remaining M = 60 - N samples must average a move
    Δ* = G * 60 / M
  against the favorite. Model each one-second step as ~N(0, σ_s²); the mean of M
  such steps has stdev
    σ_R = σ_s * sqrt((M + 1)(2M + 1) / (6M))
  (the running-average's own dispersion as more samples land). The flip z-score
  is  z = Δ* / σ_R , and  p_flip = Φ(-z) . Index step vol in POINTS is
    σ_s = index * (σ_annual / sqrt(31_536_000))
  with σ_annual a config dial (END_SIGMA_ANNUAL, ~0.45). p_flip is then PADDED
  by END_JUMP_MULT (~2.5x) for the jump risk a Gaussian under-prices, and capped
  at 1.0. ONLY enter when the padded p_flip < END_MAX_PFLIP.

THE #1 TRAP (§2): the gap is measured against the RUNNING 60s AVERAGE, never
  spot or the quote. Every catastrophic flip was spot-looked-safe while the
  running average sat near the line — a late reversal then dragged the average
  across the strike. Measure G against R, always. If the engine has no true
  60s-average feed, R is APPROXIMATED from the spot samples the engine already
  collects (spot_avg_fn below) and the trip's tape says APPROX.

SIZING (§3, Drew 2026-10-17): COUNT-FIRST, scaled to the bankroll, spread across
  the board — never all-in on one market. Each cycle the available CASH is
  EQUAL-SPLIT across every qualifying market, and each buys as many lots as its
  slice affords, with a ONE-CONTRACT FLOOR: if cash can cover a single lot, the
  bet is placed even when the slice is thinner than a lot. END_MAX_BANKROLL_PCT
  (1%) is a SOFT per-position target, NOT a hard ceiling — it never blocks a bet.
  Total deployment rides up to 100% of cash as fills land (the spendable guard
  shrinks per fill), so no single market goes all-in. ("if you have $10 BTC ETH
  SOL XRP you can use $4 across the board ... win a couple cents per ... you're
  not betting it all on one market.") The 1/20-Kelly curve (kelly_fraction, below)
  is retained for reference/telemetry but no longer gates or caps the size.

PRICE FILTER (§4): floor END_PRICE_FLOOR (95c), HARD cap END_PRICE_CAP (97c) —
  98c+ is a fee/break-even trap. Entry ONLY inside the final END_WINDOW_S.

MECHANICS STOP (§5): if the gap erodes so the padded p_flip rises above
  END_STOP_PFLIP, EXIT — do not hold a decaying favorite to a possible 0.

MAKER-PREFERRED (§6): rest at the favorite price when depth exists; allow a
  taker cross in the final seconds. Every order rides the engine's existing
  gateway.submit — SHADOW places nothing (shadow_only mirrors ROT).

OFF BY DEFAULT: config.END_ENABLED is False => step() returns before touching
  any state. A build with Lane B wired but disabled is byte-identical to one
  without it (proven in tests/test_lane_end.py).
"""

import logging
import math
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal
from statistics import NormalDist
from typing import Callable, Dict, List, Optional, Tuple

from . import config
from .book import OrderBook, to_yes_terms
from .errors import WallRejection
from .gateway import Order

log = logging.getLogger("relay.end")

LANE = "END"

SETTLE_SAMPLES = 60            # the settlement average is 60 one-second prints
SECONDS_PER_YEAR = 31_536_000  # σ_annual -> σ_per_second scaling

# ── skip / verdict codes ─────────────────────────────────────────
SKIP_DISABLED = "SKIP_DISABLED"          # END_ENABLED False (never reached in step)
SKIP_NO_SERIES = "SKIP_NO_SERIES"        # series not in END_SERIES
SKIP_WINDOW = "SKIP_WINDOW"              # not yet inside the final END_WINDOW_S
SKIP_PRICE = "SKIP_PRICE"                # favorite outside [floor, cap]
SKIP_NO_FAVORITE = "SKIP_NO_FAVORITE"    # no side is the decided favorite
SKIP_NO_AVG = "SKIP_NO_AVG"              # no running-average feed (cannot judge the gap)
SKIP_PFLIP = "SKIP_PFLIP"                # padded flip prob >= END_MAX_PFLIP
SKIP_EV = "SKIP_EV"                      # EV net of taker fee <= 0 (thin: theoretical; with authority: measured margin)
SKIP_SIZE = "SKIP_SIZE"                  # 1/20-Kelly <= 0 or below one lot
SKIP_HELD = "SKIP_HELD"                  # already in this market (any lane)
SKIP_NO_BOOK = "SKIP_NO_BOOK"            # no book this read
SKIP_NO_QUOTE = "SKIP_NO_QUOTE"          # book has no two-sided touch
SKIP_ENTERED_BEFORE = "SKIP_ENTERED_BEFORE"  # one entry per market, ever
SKIP_NO_STRIKE = "SKIP_NO_STRIKE"        # the market's strike is unknown
SKIP_NO_CASH = "SKIP_NO_CASH"            # spendable cash < cost
SKIP_WALL = "SKIP_WALL"                  # gateway refused

# exit paths
TARGET = "TARGET"        # held to settlement (the favorite won) — the design exit
STOP = "STOP"            # mechanics stop: gap eroded, p_flip above the stop line
SETTLE = "SETTLE"        # window closed while held
NO_FILL = "NO_FILL"      # entry expired unfilled


_NORMAL = NormalDist()


def phi(x: float) -> float:
    """Standard-normal CDF Φ(x). statistics.NormalDist is stdlib (no scipy)."""
    return _NORMAL.cdf(x)


def sigma_per_second_points(index: float, sigma_annual: float) -> float:
    """σ_s in PRICE POINTS: one-second index move stdev = index * σ_annual /
    sqrt(seconds per year). index/σ in the same units as the strike."""
    if index <= 0 or sigma_annual <= 0:
        return 0.0
    return index * (sigma_annual / math.sqrt(SECONDS_PER_YEAR))


def running_avg_stdev_points(sigma_s: float, m: int) -> float:
    """σ_R — the stdev of the mean of the M remaining one-second steps:
    σ_s * sqrt((M+1)(2M+1) / (6M)). M >= 1."""
    if sigma_s <= 0 or m <= 0:
        return 0.0
    return sigma_s * math.sqrt((m + 1) * (2 * m + 1) / (6.0 * m))


def flip_probability(running_avg: float, strike: float, samples_in: int,
                     index: float, sigma_annual: float, jump_mult: float
                     ) -> Tuple[float, float]:
    """(padded p_flip, raw gap G) for the CURRENT favorite.

    G = running_avg - strike (favorite = YES when G > 0; the math is symmetric
    — |G| is what matters for the flip). The remaining M = 60 - samples_in
    samples must average Δ* = |G| * 60 / M against the favorite to flip; the
    flip z = Δ* / σ_R, p_flip = Φ(-z), padded x jump_mult and capped at 1.0.

    All samples already in (M == 0) => the settlement average is locked: no flip
    is possible, p_flip = 0.0. A zero gap on the line is maximal risk (Φ(0)=0.5
    padded)."""
    g = running_avg - strike
    m = SETTLE_SAMPLES - samples_in
    if m <= 0:
        return 0.0, g                       # the 60s average is locked in
    sigma_s = sigma_per_second_points(index, sigma_annual)
    sigma_r = running_avg_stdev_points(sigma_s, m)
    if sigma_r <= 0:
        # no modellable vol: a strictly positive gap cannot flip; on the line it can
        return (0.0 if abs(g) > 0 else min(1.0, 0.5 * jump_mult)), g
    delta_star = abs(g) * SETTLE_SAMPLES / m
    z = delta_star / sigma_r
    p = phi(-z) * jump_mult
    return min(1.0, p), g


def kelly_fraction(p_flip: float, kelly_div: float, max_pct: float) -> float:
    """Flat 1/kelly_div fraction, hard-capped at max_pct, NEVER laddered.
    The WO's load-bearing formula, literal (p = p_flip, the loss probability):
      w_est  = 1 - p_flip
      w_cons = max(0, w_est - 0.015)          (a 1.5c win-prob haircut)
      b      = (1 - p) / p                     (the model's own odds)
      f_full = (b * w_cons - (1 - w_cons)) / b
      f      = min(max_pct, f_full / kelly_div)
    f <= 0 (no positive edge after the haircut) => do not trade (0.0). The edge
    is that the model's win prob exceeds the loss priced into b, so f_full > 0
    only when w_cons is meaningfully above the break-even the odds imply."""
    p = p_flip
    if p <= 0:                        # no flip risk: the max cap takes over
        return max_pct
    if p >= 1.0:
        return 0.0
    w_est = 1.0 - p_flip
    w_cons = max(0.0, w_est - 0.015)
    b = (1.0 - p) / p
    if b <= 0:
        return 0.0
    f_full = (b * w_cons - (1.0 - w_cons)) / b
    if f_full <= 0:
        return 0.0
    return min(max_pct, f_full / kelly_div)


def _dec(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def _norm(d: Decimal):
    d = _dec(d)
    return int(d) if d == d.to_integral_value() else d


@dataclass
class EndTrip:
    market: str
    event: str
    series: str
    underlying: str
    side: str                # the favorite side we bought (yes | no)
    cost: int                # entry price, cents (95-97)
    size: Decimal
    close_ts: float
    strike: float
    entry_oid: str
    entry_ts: float
    approx_avg: bool         # the running average was approximated from spot
    state: str = "ENTRY"     # ENTRY | HELD | UNWIND | CLOSED
    entry_open: bool = True
    filled: Decimal = Decimal(0)
    fill_ts: Optional[float] = None
    exit_oid: Optional[str] = None
    pending: Optional[str] = None
    shadow_only: bool = False
    sell_oids: List[str] = field(default_factory=list)
    reads: int = 0
    closed_path: Optional[str] = None
    closed_ts: Optional[float] = None
    why_log: List[str] = field(default_factory=list)

    @property
    def key(self):
        return (self.event, self.market, LANE)


class EndGameDesk:
    """Lane B. Pure of I/O except through the gateway it is handed; the engine
    supplies books, the listing (meta), the clock, the spot/running-average
    feeds, and the market strike. OFF by default — step() is a hard no-op while
    config.END_ENABLED is False (byte-identical to no Lane B)."""

    def __init__(self, gateway, ledger, alert: Callable[[str], None],
                 spot_fn: Optional[Callable[[str], Optional[float]]] = None,
                 running_avg_fn: Optional[Callable[[str], Optional[Tuple[float, int, bool]]]] = None,
                 strike_fn: Optional[Callable[[str], Optional[float]]] = None,
                 venue_held_fn: Optional[Callable[[str], Optional[Decimal]]] = None,
                 live_fn: Optional[Callable[[str], bool]] = None,
                 on_close: Optional[Callable] = None):
        self.gateway = gateway
        self.ledger = ledger
        self.alert = alert
        # spot_fn(underlying) -> current index value (points), for σ_s scaling
        self.spot_fn = spot_fn or (lambda u: None)
        # running_avg_fn(underlying) -> (running_60s_avg, samples_in, approx) or
        # None. THE edge's input — the live average of the settlement prints so
        # far this second-bucket. approx=True when derived from spot (no true
        # 60s-average feed). None => the gap cannot be judged -> SKIP_NO_AVG.
        self.running_avg_fn = running_avg_fn or (lambda u: None)
        # strike_fn(market) -> the market's numeric strike (points), or None
        self.strike_fn = strike_fn or (lambda m: None)
        self.venue_held_fn = venue_held_fn or (lambda m: None)
        self.live_fn = live_fn or (lambda u: True)
        self.on_close = on_close
        self.trips: Dict[str, EndTrip] = {}
        self.closed: List[EndTrip] = []
        self.entered: Dict[str, float] = {}      # market -> close_ts (one entry ever)
        self.verdicts: Dict[str, str] = {}
        self.events: List[tuple] = []

    # ── bookkeeping (mirrors RotationDesk) ───────────────────────
    def _ev(self, *e):
        self.events.append(e)
        if len(self.events) > 5000:
            del self.events[:1000]

    def book_cents(self) -> int:
        return int(self.ledger.ensemble_base_cents())

    def spendable_cents(self) -> int:
        return int(self.ledger.tradeable_cents())

    def held(self, t: EndTrip) -> Decimal:
        g = _dec(self.gateway.positions.get(t.key, 0))
        g = max(Decimal(0), g if t.side == "yes" else -g)
        if t.shadow_only:
            return g
        v = self.venue_held_fn(t.market)
        if v is None:
            return g
        v = _dec(v)
        return min(g, max(Decimal(0), v if t.side == "yes" else -v))

    def market_held_elsewhere(self, market: str) -> bool:
        if market in self.trips:
            return True
        for (_e, m, _l), net in self.gateway.positions.items():
            if m == market and net:
                return True
        return any(o.market == market for o in self.gateway.resting.values())

    # ── the favorite and its flip probability ────────────────────
    def favorite(self, book: OrderBook) -> Tuple[Optional[str], Optional[int]]:
        """(side, cost) of the decided favorite — the side whose MAKER entry
        cost is highest (nearest 100c). The maker cost per side is the ROT
        convention (lane_rot.side_costs): YES joins the YES bid; NO joins the NO
        bid, which IS 100 - the YES ask. None when there is no two-sided touch."""
        yb = book.best_yes_bid()
        nb = book.best_no_bid()          # the NO bid price = 100 - yes_ask
        cands = {}
        if yb is not None:
            cands["yes"] = int(round(to_yes_terms("yes", yb)))   # YES maker cost
        if nb is not None:
            # NO maker cost in NO terms is the NO bid itself; to_yes_terms("no",
            # yes_ask) is the YES-terms complement. The favorite comparison is in
            # each side's OWN buy cost, so the NO favorite cost is the NO bid.
            cands["no"] = int(round(nb))
        if not cands:
            return None, None
        side = max(cands, key=lambda s: (cands[s], s == "yes"))
        return side, cands[side]

    def gap_for_side(self, side: str, running_avg: float, strike: float) -> float:
        """Signed gap in FAVOR of the chosen side. YES favorite wants
        running_avg > strike (markets are 'index >= strike'); NO is the mirror."""
        g = running_avg - strike
        return g if side == "yes" else -g

    def flip_prob_for(self, t_side: str, running_avg: float, samples_in: int,
                      strike: float, index: float) -> float:
        """Padded p_flip for holding t_side's favorite, built on the SIGNED gap
        IN FAVOR of the side (gap_for_side). A side the average favors (favor-gap
        > 0) uses that gap's flip prob directly; a side the average NO LONGER
        favors (favor-gap <= 0 — the gap eroded across the line, the #1 trap) is
        no longer the favorite, so its loss probability is >= 0.5 — the complement
        of the (now opposite) favorite's flip prob. Reported as the high number
        so the mechanics stop fires."""
        fav_gap = self.gap_for_side(t_side, running_avg, strike)
        # the model only cares about |gap|; feed a synthetic average that sits
        # |fav_gap| on the FAVORABLE side of the strike so Δ*/σ_R is correct.
        synthetic_avg = strike + abs(fav_gap)
        p, _g = flip_probability(synthetic_avg, strike, samples_in, index,
                                 config.END_SIGMA_ANNUAL, config.END_JUMP_MULT)
        if fav_gap <= 0:
            # on or past the wrong side of the average: holding this side now
            # LOSES unless it flips BACK, so its loss prob is 1 - p.
            return min(1.0, 1.0 - p)
        return p

    # ── the cycle ─────────────────────────────────────────────────
    def step(self, now: float, books: Dict[str, OrderBook],
             meta: Dict[str, dict]) -> None:
        """One Lane B cycle: service held trips (stop / settle), then look for
        one new end-game entry per admitted market. A HARD NO-OP while disabled
        — returns before touching any state, so the desk is byte-identical to a
        build with no Lane B."""
        if not config.END_ENABLED:
            return
        self._ev("cycle", now)
        self.inside(now, books, meta)
        self.outside(now, books, meta)

    # ── held trips: mechanics stop and settlement ─────────────────
    def inside(self, now: float, books: Dict[str, OrderBook],
               meta: Dict[str, dict]) -> None:
        for market in list(self.trips):
            t = self.trips[market]
            m = meta.get(market)
            if m and m.get("close_ts") and t.close_ts != m["close_ts"]:
                t.close_ts = float(m["close_ts"])
            try:
                self.service(t, now, books.get(market))
            except WallRejection as e:
                t.why_log.append(f"{now:.0f} wall {e.wall}: {e.detail}")
                log.warning("END %s exit refused by wall %s: %s", market, e.wall, e.detail)
            if t.state == "CLOSED":
                self._close(t, now)

    def service(self, t: EndTrip, now: float, book: Optional[OrderBook]) -> None:
        t.reads += 1
        self._sync_entry(t, now)
        if now >= t.close_ts:
            self._cancel_all(t)
            if t.filled > 0:
                self._finish(t, now, SETTLE if self.held(t) > 0 else (t.pending or TARGET))
            else:
                self._finish(t, now, NO_FILL)
            return
        held = self.held(t)
        if t.filled > 0 and held <= 0 and not t.entry_open:
            self._cancel_all(t)
            self._finish(t, now, t.pending or STOP)
            return
        if t.filled == 0:
            if not t.entry_open:
                self._finish(t, now, NO_FILL)
            return
        if t.state != "HELD":
            return
        # MECHANICS STOP: re-judge the flip prob on the fresh running average
        avg = self.running_avg_fn(t.underlying)
        index = self.spot_fn(t.underlying)
        if avg is None or index is None:
            t.why_log.append(f"{now:.0f} stop check: no avg/index — holding")
            return
        running_avg, samples_in, _approx = avg
        p = self.flip_prob_for(t.side, running_avg, samples_in, t.strike, index)
        if p > config.END_STOP_PFLIP:
            t.why_log.append(f"{now:.0f} STOP: p_flip {p:.4f} > {config.END_STOP_PFLIP}")
            self._ev("stop", t.market, p)
            self._exit_now(t, now, book, STOP)

    def _sync_entry(self, t: EndTrip, now: float) -> None:
        f = _dec(self.gateway.filled_counts.get(t.entry_oid, 0))
        if f > t.filled:
            if t.filled == 0:
                t.fill_ts = now
                if t.state == "ENTRY":
                    t.state = "HELD"
            t.filled = f
            self._ev("entry_fill", t.market, f)
        if t.entry_open:
            done = t.entry_oid not in self.gateway.resting or t.filled >= t.size
            # an end-game entry rests only to the bell (final window is short)
            if done:
                t.entry_open = False

    def _exit_now(self, t: EndTrip, now: float, book: Optional[OrderBook],
                  path: str) -> None:
        """The mechanics stop: cancel the resting entry, CONFIRM, then sell the
        held count. Maker-preferred (rest one tick inside the bid) but, this
        close to the bell, a taker cross is allowed. Never oversells."""
        if not self._cancel_all(t):
            t.why_log.append(f"{now:.0f} stop cancel UNKNOWN — no sell until confirmed")
            self._ev("cancel_unconfirmed", t.market)
            t.pending = path
            t.state = "UNWIND"
            return
        held = self.held(t)
        if held <= 0:
            self._finish(t, now, path)
            return
        if book is None:
            t.pending, t.state = path, "UNWIND"
            return
        bid = book.best_yes_bid() if t.side == "yes" else book.best_no_bid()
        if bid is None:
            t.pending, t.state = path, "UNWIND"
            return
        secs_left = t.close_ts - now
        taker = secs_left <= config.END_WINDOW_S / 3.0
        px = bid if taker else max(1, bid - 1)   # cross in the last seconds
        o = Order(lane=LANE, event=t.event, market=t.market, side=t.side,
                  action="sell", price_cents=int(px), count=_norm(held),
                  size_tier=config.TIER_PROBE, purpose="CUT" if taker else "EXIT",
                  band=(1, 99), crossfire=taker, ioc=taker, reduce_only=taker,
                  shadow_only=t.shadow_only,
                  reason=f"END {path}: p_flip stop, sell {held} @ {px}c "
                         f"({'taker' if taker else 'maker'})")
        self._assert_exit_size(t, held)
        res = self.gateway.submit(o, book)
        t.sell_oids.append(res.order_id)
        if not taker:
            t.exit_oid = res.order_id
        t.pending = path
        t.state = "UNWIND"
        self._ev("exit", t.market, path, px, held)

    def _cancel_all(self, t: EndTrip) -> bool:
        ok = True
        oids = ([t.exit_oid] if t.exit_oid else [])
        if t.entry_open:
            oids.append(t.entry_oid)
        for oid in oids:
            if oid not in self.gateway.resting:
                if oid == t.exit_oid:
                    t.exit_oid = None
                if oid == t.entry_oid:
                    t.entry_open = False
                continue
            st = self.gateway.cancel_tristate(oid)
            self._ev("cancel", t.market, oid, st)
            if st == "UNKNOWN":
                ok = False
            else:
                if oid == t.exit_oid:
                    t.exit_oid = None
                if oid == t.entry_oid:
                    t.entry_open = False
        return ok

    def _sells_live(self, market: str) -> Decimal:
        tot = Decimal(0)
        for oid, o in self.gateway.resting.items():
            if o.market == market and o.action == "sell":
                tot += _dec(o.count) - _dec(self.gateway.filled_counts.get(oid, 0))
        return tot

    def _assert_exit_size(self, t: EndTrip, size: Decimal) -> None:
        held = self.held(t)
        live = self._sells_live(t.market)
        if size <= 0 or size + live > held:
            from . import failures
            failures.fail("END_EXIT_OVERSIZE",
                          f"{t.market}: exit {size} + live sells {live} > held {held}"
                          " — refused (never sell more than held)",
                          alert=True, market=t.market)
            raise WallRejection("END_EXIT_OVERSIZE", f"{size}+{live}>{held}")

    def _finish(self, t: EndTrip, now: float, path: str) -> None:
        t.state = "CLOSED"
        t.closed_path = path
        t.closed_ts = now

    def _close(self, t: EndTrip, now: float) -> None:
        if t.closed_path == SETTLE:
            self.gateway.settle_key(t.key)
        self.trips.pop(t.market, None)
        self.closed.append(t)
        self.verdicts[t.market] = f"CLOSED_{t.closed_path}"
        self._ev("closed", t.market, t.closed_path)
        if t.closed_path == NO_FILL:
            return
        if self.on_close is not None:
            try:
                self.on_close(t)
                return
            except Exception as e:  # noqa: BLE001 — the trip still closes; tape loud
                from . import failures
                failures.fail("END_TRIP_TAPE_FAILED",
                              f"{t.market} {t.closed_path}: trip row not written ({e})",
                              alert=True, market=t.market)
        approx = " (avg APPROX)" if t.approx_avg else ""
        self.alert(f"🏁 END {t.closed_path} {t.market} {t.side} entry {t.cost}¢ "
                   f"x{_norm(t.filled)}{approx}")

    # ── the entry hunt ────────────────────────────────────────────
    def outside(self, now: float, books: Dict[str, OrderBook],
                meta: Dict[str, dict]) -> None:
        # The set we may enter THIS cycle. Empty END_SERIES => every admitted
        # 15M series (Drew 2026-10-17: "see every 15-minute market available").
        # Computed once per cycle off the LIVE roster so new 15M rooms are
        # covered the instant discovery admits them — no code change.
        allowed = config.end_series_for(config.SERIES_INFO)

        # Pass 1 — run every gate up THROUGH the EV check and collect the markets
        # that qualify this cycle. Sizing is deferred because the per-market
        # budget is an EQUAL SPLIT of available cash across ALL qualifiers (Drew
        # 2026-10-17: "if you have $10 BTC ETH SOL XRP you can use $4 across the
        # board ... you're not betting it all on one market"). We can't split
        # before we know how many qualify, so gate first, then size.
        qualifiers: List[dict] = []
        for market, m in meta.items():
            q = self._gate(market, m, now, books.get(market), allowed)
            if q is not None:
                qualifiers.append(q)

        if not qualifiers:
            return

        # Pass 2 — equal-split the available cash across the qualifiers, size
        # each (count-first, 1-contract floor), and enter. The running spendable
        # guard shrinks as shadow/live fills land, so total deployment rides up
        # to 100% of cash without any single market going all-in.
        n = len(qualifiers)
        for q in qualifiers:
            spendable = self.spendable_cents()
            # equal split of available cash, clamped to the per-market wall so no
            # single market goes all-in (and the gateway never hard-rejects us).
            slice_cents = min(spendable // n if n else 0, self._market_wall_cents())
            size, why = self._size(q["cost"], slice_cents, spendable)
            if size is None:
                self.verdicts[q["market"]] = why
                continue
            new_cost = int(float(size) * q["cost"])
            if new_cost > spendable:
                self.verdicts[q["market"]] = SKIP_NO_CASH
                continue
            self._enter(q["market"], q["series"], q["info"], q["side"],
                        q["cost"], size, q["close_ts"], q["strike"],
                        q["approx"], q["p_flip"], q["running_avg"],
                        q["samples_in"], now, q["book"])

    def _market_wall_cents(self) -> int:
        """The END per-market at-risk wall in cents (gateway enforces it as a
        HARD reject). The sizer clamps each market's slice to this so it never
        self-inflicts a SKIP_WALL — one market never goes all-in."""
        return config.at_risk_cap_cents(LANE, self.spendable_cents())

    def _ev_ok(self, side: str, cost: int, p_flip: float, market: str) -> bool:
        """The fee-aware EV gate. Returns True iff this entry is worth taking.

        Thin cell (no realized END history at this price bucket): gate on the
        THEORETICAL fee-adjusted EV — a favorite that wins with prob w=(1−p_flip)
        pays (100−cost) and loses cost, less the venue taker fee at this price:
            EV_c = w·(100−cost) − (1−w)·cost − fee = w·100 − cost − fee
        Enter only when EV_c > 0 — never a known-negative price, but we DO enter
        to gather data. Once the cell has REALIZED authority, the measured gate
        (wilson_LB − breakeven > 0) governs instead — the learned edge wins."""
        from . import scoring
        cell = scoring.price_cell(cost)
        fee = scoring.taker_fee_cents(cost)
        w = max(0.0, 1.0 - p_flip)
        theo_ev = w * 100.0 - cost - fee
        has_auth = scoring.cell_has_authority(self.ledger, "END", cell,
                                               shadow=config.lane_books_shadow("END"))
        if has_auth:
            s = scoring.score(self.ledger, "END", cell,
                              shadow=config.lane_books_shadow("END"))
            ok = s["margin"] > 0
            self._ev("ev_gate", market, "measured", round(s["margin"], 4),
                     round(theo_ev, 2), ok)
            return ok
        ok = theo_ev > 0
        self._ev("ev_gate", market, "theoretical", round(theo_ev, 2), ok)
        return ok

    def _gate(self, market: str, m: dict, now: float,
              book: Optional[OrderBook],
              allowed: Optional[frozenset] = None) -> Optional[dict]:
        """Run every entry gate THROUGH the EV check. Return the qualifier's
        entry parameters (a dict) when the market passes, else record the verdict
        and return None. Sizing/entry is deferred to the caller so the available
        cash can be equal-split across all qualifiers this cycle."""
        series = config.series_of(market)
        if allowed is None:                      # direct callers (tests) get live set
            allowed = config.end_series_for(config.SERIES_INFO)
        if series not in allowed:
            self.verdicts[market] = SKIP_NO_SERIES
            return None
        info = config.SERIES_INFO.get(series)
        if info is None:
            self.verdicts[market] = SKIP_NO_SERIES
            return None
        if self.market_held_elsewhere(market):
            self.verdicts[market] = SKIP_HELD
            return None
        if market in self.entered:
            self.verdicts[market] = SKIP_ENTERED_BEFORE
            return None
        close_ts = m.get("close_ts")
        if close_ts is None:
            self.verdicts[market] = SKIP_WINDOW
            return None
        secs_left = float(close_ts) - now
        if secs_left > config.END_WINDOW_S or secs_left <= 0:
            self.verdicts[market] = SKIP_WINDOW
            return None
        if book is None:
            self.verdicts[market] = SKIP_NO_BOOK
            return None
        side, cost = self.favorite(book)
        if side is None or cost is None:
            self.verdicts[market] = SKIP_NO_QUOTE
            return None
        if not (config.END_PRICE_FLOOR <= cost <= config.END_PRICE_CAP):
            self.verdicts[market] = SKIP_PRICE
            return None
        strike = self.strike_fn(market)
        if strike is None:
            self.verdicts[market] = SKIP_NO_STRIKE
            return None
        avg = self.running_avg_fn(info.underlying)
        index = self.spot_fn(info.underlying)
        if avg is None or index is None:
            self.verdicts[market] = SKIP_NO_AVG
            return None
        running_avg, samples_in, approx = avg
        # the chosen side MUST be the running-average favorite (the #1 trap:
        # never buy a side the AVERAGE does not favor, however the quote looks)
        if self.gap_for_side(side, running_avg, strike) <= 0:
            self.verdicts[market] = SKIP_NO_FAVORITE
            return None
        p_flip = self.flip_prob_for(side, running_avg, samples_in, strike, index)
        if p_flip >= config.END_MAX_PFLIP:
            self.verdicts[market] = SKIP_PFLIP
            self._ev("skip_pflip", market, round(p_flip, 5))
            return None
        # ── fee-aware EV gate (Drew 2026-10-17: "know what's profitable before
        # you bet"). Two modes, both fee-adjusted via the venue's taker curve:
        #   • thin cell (no settled history yet) — gate on THEORETICAL EV:
        #       EV_c = w·100 − cost − taker_fee(cost),  w = 1 − p_flip
        #     so we enter only known-non-negative prices while gathering data.
        #   • cell WITH authority (enough realized outcomes) — the LEARNED gate
        #     takes over: require measured margin = wilson_LB − breakeven > 0.
        # This is the "enter on knowing, then let the measured edge govern" path.
        if not self._ev_ok(side, cost, p_flip, market):
            self.verdicts[market] = SKIP_EV
            return None
        return {
            "market": market, "series": series, "info": info,
            "side": side, "cost": cost, "close_ts": float(close_ts),
            "strike": strike, "approx": approx, "p_flip": p_flip,
            "running_avg": running_avg, "samples_in": samples_in, "book": book,
        }

    def _consider(self, market: str, m: dict, now: float,
                  book: Optional[OrderBook],
                  allowed: Optional[frozenset] = None) -> bool:
        """Single-market gate→size→enter (direct callers / tests). Sizes this one
        market against ALL available cash (it is the only qualifier in view), with
        the count-first 1-contract floor; the per-cycle equal split lives in
        outside()."""
        q = self._gate(market, m, now, book, allowed)
        if q is None:
            return False
        spendable = self.spendable_cents()
        slice_cents = min(spendable, self._market_wall_cents())
        size, why = self._size(q["cost"], slice_cents, spendable)
        if size is None:
            self.verdicts[market] = why
            return False
        new_cost = int(float(size) * q["cost"])
        if new_cost > spendable:
            self.verdicts[market] = SKIP_NO_CASH
            return False
        return self._enter(q["market"], q["series"], q["info"], q["side"],
                           q["cost"], size, q["close_ts"], q["strike"],
                           q["approx"], q["p_flip"], q["running_avg"],
                           q["samples_in"], now, q["book"])

    def _size(self, cost: int, budget_cents: int,
              spendable_cents: int) -> Tuple[Optional[Decimal], str]:
        """Count-first sizing against a per-market CASH budget (Drew 2026-10-17:
        "it's per position being the one percent ... but the max percentage of
        bankroll should not limit you ... scale to your bankroll").

        The 1% (END_MAX_BANKROLL_PCT) is a SOFT per-position target, never a hard
        ceiling that blocks a bet. The desk spreads the available cash across the
        qualifying markets this cycle (equal split, computed by the caller) and
        buys as many lots as that slice affords — but with a MINIMUM-LOT FLOOR:
        if the slice can't cover even one lot yet spendable cash can, still take
        one lot ("if you are able to do one contract for market then you should
        be able to bet that"). Never all-in on one market; spread across the
        board. A "lot" is one ROT_LOT_STEP (the smallest tradeable unit; whole
        contracts in LIVE, fractional 0.01 steps in SHADOW).

        budget_cents  = this market's equal-split slice of available cash.
        spendable_cents = total cash still available (the min-lot floor + the
                          overspend guard both read it).
        """
        if cost <= 0:
            return None, SKIP_SIZE
        step = Decimal(config.ROT_LOT_STEP)
        one_lot_cents = int((step * Decimal(cost)).to_integral_value(
            rounding="ROUND_CEILING"))      # cash a single lot actually costs
        lots = (Decimal(max(0, budget_cents)) / Decimal(cost) / step
                ).to_integral_value(rounding=ROUND_FLOOR)
        if lots < 1:
            # the slice is too thin for even one lot — honor the floor whenever
            # spendable cash can cover a single lot at this price.
            if spendable_cents >= one_lot_cents:
                return (step * 1).quantize(step), ""
            return None, SKIP_NO_CASH
        return (lots * step).quantize(step), ""

    def _enter(self, market, series, info, side, cost, size, close_ts, strike,
               approx, p_flip, running_avg, samples_in, now, book) -> bool:
        shadow_only = not self.live_fn(info.underlying)
        why_s = (f"END endgame favorite {side}@{cost}c x{_norm(size)} · "
                 f"{info.underlying} strike {strike:g} · running60s_avg "
                 f"{running_avg:g}{' APPROX' if approx else ''} ({samples_in}/60 in) · "
                 f"p_flip {p_flip:.4f} < {config.END_MAX_PFLIP} · equal-split cash "
                 f"across qualifiers, 1-contract floor (soft "
                 f"{config.END_MAX_BANKROLL_PCT:.0%} per-position target)")
        if shadow_only:
            why_s += " · SHADOW-ONLY (no working spot feed)"
        o = Order(lane=LANE, event=market.rsplit("-", 1)[0], market=market,
                  side=side, action="buy", price_cents=int(cost), count=_norm(size),
                  size_tier=config.TIER_PROBE, purpose="ENTRY",
                  band=(config.END_PRICE_FLOOR, config.END_PRICE_CAP),
                  why=why_s, shadow_only=shadow_only)
        try:
            res = self.gateway.submit(o, book)
        except WallRejection as e:
            self.verdicts[market] = f"{SKIP_WALL}:{e.wall}"
            self._ev("wall", market, e.wall)
            return False
        t = EndTrip(market=market, event=o.event, series=series,
                    underlying=info.underlying, side=side, cost=int(cost),
                    size=_dec(size), close_ts=close_ts, strike=float(strike),
                    entry_oid=res.order_id, entry_ts=now, approx_avg=bool(approx),
                    shadow_only=shadow_only)
        t.why_log.append(why_s)
        self.trips[market] = t
        self.entered[market] = float(close_ts)
        self.verdicts[market] = "ENTERED"
        self._ev("enter", market, side, cost, size, round(p_flip, 5))
        return True
