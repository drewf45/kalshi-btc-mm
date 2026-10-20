"""WO-END-1 (rewrite 2026-10-18) — LANE END, the end-game pile-on.

COST IS THE SIGNAL. This lane is the certified "F" end-game pile-on ported onto
the desk's multi-room orient/rotate/equal-split structure. Where Lane A (ROT)
buys near 50c early and cashes the oscillation out BEFORE the end-game, Lane END
orients within the final minutes of a 15-minute binary and piles onto every
market whose outcome is ALREADY DECIDED — the "solved" rooms — for the last
pennies, skipping the undecided and re-checking them as close approaches.

THE DOCTRINE (reference/live_k_worker — CERTIFICATION.md, engine.py, gateway.py):
  NO probability model. A side pinned at 95-99c IS proof the market is solved —
  the richest side of the book (max maker cost) is the favorite, and its cost is
  the whole signal. There is no running-average feed, no Gaussian flip model, no
  strike in the decision. Only the END and the WINNING SIDE matter.

THE SOLVED TEST — the CONFIRM LADDER (ported verbatim in shape from engine.py
  CONFIRM_LADDER): a favorite must HOLD, for a required number of consecutive
  reads, in-band (95-99c) and above the tier's floor, same side, with live
  counterparty liquidity. Deeper into the window the floor rises but the required
  confirms drop (99c→9, 98c→6, 97c→3). step() is NOT a blocking poll loop — it is
  called once per cycle — so the per-market confirm counter + tier state are kept
  ACROSS cycles on the desk (self.confirm_state). The counter RESETS on side-flip,
  below-tier-floor, out-of-band, or an empty-counterparty (one-sided) book. A
  market qualifies as a solved entry only when its confirm count reaches the
  current tier's required confirms.

THE WINDOWS: confirming may begin as early as T-END_WATCH_WINDOW_S (900 s). An
  entry is PLACED only when END_CANCEL_BEFORE_EXPIRY_S (10 s) < secs_left <=
  END_WINDOW_S (180 s; the live .env override of 240 still applies). Markets seen
  outside the entry window are SKIP_WINDOW (undecided/too-early), re-checked each
  cycle as close approaches.

THE COUNTERPARTY CHECK (R2, engine._run_watch_ladder): if the favorite's OPPOSITE
  bid is absent (a one-sided book), skip and reset the confirm counter — a solved
  market still shows a two-sided touch.

SIZING (Drew 2026-10-17): COUNT-FIRST, scaled to the bankroll, spread across the
  board — never all-in on one market. Each cycle the available CASH is EQUAL-SPLIT
  across every solved market, and each buys as many lots as its slice affords,
  with a ONE-CONTRACT FLOOR: if spendable cash covers a single lot, the bet is
  placed even when the slice is thinner than a lot. END_MAX_BANKROLL_PCT (1%) is a
  SOFT per-position target, not a hard ceiling. AT_RISK_PCT["END"]=0.50 is the
  per-market wall that stops any one room going all-in.

MAKER-PREFERRED, RIDE TO SETTLEMENT (engine: rest post-only at the favorite's
  touch; expiry = close_ts - 10; NO active per-trade exit). A filled favorite is
  held to the bell — there is no p_flip mechanics stop. Unfilled at close -> NO_FILL.

OFF BY DEFAULT: config.END_ENABLED is False => step() returns before touching any
  state. A build with Lane END wired but disabled is byte-identical to one without
  it (proven in tests/test_lane_end.py).
"""

import logging
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal
from typing import Callable, Dict, List, Optional, Tuple

from . import config
from .book import OrderBook, to_yes_terms
from .errors import WallRejection
from .gateway import Order

log = logging.getLogger("relay.end")

LANE = "END"

# ── skip / verdict codes ─────────────────────────────────────────
SKIP_DISABLED = "SKIP_DISABLED"          # END_ENABLED False (never reached in step)
SKIP_NO_SERIES = "SKIP_NO_SERIES"        # series not in END_SERIES
SKIP_WINDOW = "SKIP_WINDOW"              # not yet inside the final END_WINDOW_S
SKIP_PRICE = "SKIP_PRICE"                # favorite outside the 95-99c cost band
SKIP_NO_FAVORITE = "SKIP_NO_FAVORITE"    # no side is the decided favorite
SKIP_NO_COUNTERPARTY = "SKIP_NO_COUNTERPARTY"  # one-sided book (R2: counterparty bid absent)
SKIP_UNCONFIRMED = "SKIP_UNCONFIRMED"    # in-band but confirm count below the tier's floor
SKIP_SIZE = "SKIP_SIZE"                  # sizing could not produce a lot
SKIP_HELD = "SKIP_HELD"                  # already in this market (any lane)
SKIP_NO_BOOK = "SKIP_NO_BOOK"            # no book this read (and no usable record touch)
SKIP_STALE_RECORD = "SKIP_STALE_RECORD"  # record touch too old to trust (no live book)
SKIP_NO_QUOTE = "SKIP_NO_QUOTE"          # book has no two-sided touch
SKIP_ENTERED_BEFORE = "SKIP_ENTERED_BEFORE"  # one entry per market, ever
SKIP_NO_CASH = "SKIP_NO_CASH"            # spendable cash < cost
SKIP_WALL = "SKIP_WALL"                  # gateway refused

# exit paths
TARGET = "TARGET"        # held to settlement (the favorite won) — the design exit
SETTLE = "SETTLE"        # window closed while held
NO_FILL = "NO_FILL"      # entry expired unfilled


def _dec(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def _norm(d: Decimal):
    d = _dec(d)
    return int(d) if d == d.to_integral_value() else d


def _clamp_touch(cents: int) -> int:
    """WO-END-3: clamp a synthesized record touch into the TRADEABLE 1-99c range.
    You can't buy a 100c contract (Kalshi YES+NO=$1: 100c means the counterparty
    already holds the payout), so a 100c favorite IS a 99c tradeable bid. Sub-cent
    dollar prices that floored to 0 are raised to 1c — a real, if thin, touch."""
    return max(1, min(99, int(cents)))


def _tier_for(secs_left: float) -> Optional[dict]:
    """The confirm-ladder tier governing this read, or None if too far out.

    Ported from engine.CONFIRM_LADDER. A tier matches when lo_sec <= secs_left <
    hi_sec. INSIDE the final entry window (secs_left below every tier's lo) the
    DEEPEST tier governs — that is the solved test the entry rides on. Above the
    top tier's hi (too early to confirm) -> None."""
    ladder = config.END_CONFIRM_LADDER
    if not ladder:
        return None
    for t in ladder:
        if t["lo_sec"] <= secs_left < t["hi_sec"]:
            return t
    deepest = min(ladder, key=lambda t: t["lo_sec"])
    if secs_left < deepest["lo_sec"]:
        return deepest       # final window: the deepest tier is the solved test
    return None              # too early — above the top tier


@dataclass
class EndTrip:
    market: str
    event: str
    series: str
    underlying: str
    side: str                # the favorite side we bought (yes | no)
    cost: int                # entry price, cents (95-99)
    size: Decimal
    close_ts: float
    strike: float
    entry_oid: str
    entry_ts: float
    confirms: int            # consecutive confirms at entry (the solved proof)
    tier_confirms: int       # the tier's required confirms
    state: str = "ENTRY"     # ENTRY | HELD | CLOSED
    entry_open: bool = True
    filled: Decimal = Decimal(0)
    fill_ts: Optional[float] = None
    pending: Optional[str] = None
    shadow_only: bool = False
    reads: int = 0
    closed_path: Optional[str] = None
    closed_ts: Optional[float] = None
    why_log: List[str] = field(default_factory=list)

    @property
    def key(self):
        return (self.event, self.market, LANE)


class EndGameDesk:
    """Lane END. Pure of I/O except through the gateway it is handed; the engine
    supplies books, the listing (meta), and the clock. The spot/running-average/
    strike feeds are RETAINED in the constructor for wiring stability but are NOT
    read by the cost-is-signal decision. OFF by default — step() is a hard no-op
    while config.END_ENABLED is False (byte-identical to no Lane END)."""

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
        # spot_fn / running_avg_fn / strike_fn: RETAINED for constructor/wiring
        # stability (shadow_runner passes them) but UNUSED by the decision — cost
        # is the signal. strike is kept on the trip for the tape only.
        self.spot_fn = spot_fn or (lambda u: None)
        self.running_avg_fn = running_avg_fn or (lambda u: None)
        self.strike_fn = strike_fn or (lambda m: None)
        self.venue_held_fn = venue_held_fn or (lambda m: None)
        self.live_fn = live_fn or (lambda u: True)
        self.on_close = on_close
        self.trips: Dict[str, EndTrip] = {}
        self.closed: List[EndTrip] = []
        self.entered: Dict[str, float] = {}      # market -> close_ts (one entry ever)
        self.verdicts: Dict[str, str] = {}
        self.events: List[tuple] = []
        # The confirm ladder lives ACROSS cycles (step is called once per cycle,
        # not a blocking poll loop). market -> {side, tier_idx, count}.
        self.confirm_state: Dict[str, dict] = {}

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

    # ── WO-END-2: the record-touch book (no live WS snapshot needed) ──────
    def _record_book(self, market: str, m: dict, now: float
                     ) -> Tuple[Optional[OrderBook], Optional[str]]:
        """Synthesize a touch-only OrderBook from the market RECORD's published
        touch (rec_yes_bid / rec_yes_ask, cents, YES terms) — the SAME oracle ROT
        uses. Returns (book, None) on a usable fresh touch, (None, SKIP_code) when
        the record can't produce a two-sided touch or is stale. NO live book is
        required; this is what lets END pile onto every active room, not only the
        handful the WS feed fully snapshots. The synthesized book carries one lot
        per touched side, which is all favorite()/has_counterparty() read — the
        confirm ladder's repeated reads supply the dwell/fill reality."""
        if not config.END_USE_RECORD_TOUCH:
            return None, SKIP_NO_BOOK
        yb = m.get("rec_yes_bid")           # YES bid, cents
        ya = m.get("rec_yes_ask")           # YES ask, cents
        cap = m.get("rec_captured_ts")
        if yb is None or ya is None:
            return None, SKIP_NO_BOOK
        if cap is not None and (now - float(cap)) > config.END_RECORD_MAX_AGE_S:
            return None, SKIP_STALE_RECORD
        # WO-END-3 (Drew 2026-10-19): at the END of a market the solved favorite
        # sits at 99c with OFTEN NO counterparty ("no one will sell you the losing
        # side when everyone knows the outcome") — and the venue record publishes
        # sub-cent dollar prices (0.005 -> 0c after *100). The old filter dropped
        # both near-edge sides -> SKIP_NO_QUOTE on the MOST-solved rooms (the whole
        # point of the lane). Fix: CLAMP each side into the tradeable 1-99c range
        # (a 100c NO favorite is really a 99c tradeable bid — you can't buy 100c,
        # per Kalshi's YES+NO=$1 accounting), and KEEP the favorite side even when
        # the counterparty side is absent. A true empty record is still SKIP.
        yb = int(yb); ya = int(ya)
        nb = 100 - ya                       # NO bid = 100 - YES ask (book.py convention)
        b = OrderBook(market=market)
        yes_bids = {_clamp_touch(yb): 1} if yb > 0 else {}
        no_bids = {_clamp_touch(nb): 1} if nb > 0 else {}
        if not yes_bids and not no_bids:
            return None, SKIP_NO_QUOTE
        b.apply_snapshot(yes_bids, no_bids, float(cap or now))
        if b.poisoned:                      # an incoherent record (yes+no>101)
            return None, SKIP_NO_QUOTE
        return b, None

    # ── the favorite: the richest side of the book (COST IS THE SIGNAL) ──
    def favorite(self, book: OrderBook) -> Tuple[Optional[str], Optional[int]]:
        """(side, cost) of the decided favorite — the side whose MAKER entry cost
        is highest (nearest 100c). The maker cost per side is the ROT convention
        (lane_rot.side_costs): YES joins the YES bid; NO joins the NO bid, which
        IS 100 - the YES ask. None when there is no two-sided touch. This is the
        certified gateway._favorite_side ported onto this repo's OrderBook — the
        richest side, with NO model."""
        yb = book.best_yes_bid()
        nb = book.best_no_bid()          # the NO bid price = 100 - yes_ask
        cands = {}
        if yb is not None:
            cands["yes"] = int(round(to_yes_terms("yes", yb)))   # YES maker cost
        if nb is not None:
            cands["no"] = int(round(nb))                          # NO maker cost = NO bid
        if not cands:
            return None, None
        side = max(cands, key=lambda s: (cands[s], s == "yes"))
        return side, cands[side]

    def has_counterparty(self, book: OrderBook, side: str) -> bool:
        """R2 (engine._run_watch_ladder): a solved favorite still shows a
        two-sided touch. The favorite's OPPOSITE bid must be present — else the
        book is one-sided and the confirm counter resets."""
        if side == "yes":
            return book.best_no_bid() is not None
        return book.best_yes_bid() is not None

    # ── the cycle ─────────────────────────────────────────────────
    def step(self, now: float, books: Dict[str, OrderBook],
             meta: Dict[str, dict]) -> None:
        """One Lane END cycle: service held trips (ride to settlement), then look
        for one new end-game entry per solved market. A HARD NO-OP while disabled
        — returns before touching any state, so the desk is byte-identical to a
        build with no Lane END."""
        if not config.END_ENABLED:
            return
        self._ev("cycle", now)
        self.inside(now, books, meta)
        self.outside(now, books, meta)

    # ── held trips: ride to settlement (NO p_flip stop) ──────────
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
        """Ride the favorite to settlement. The certified engine takes NO active
        per-trade exit (no scorer, no probability model) — a filled favorite is
        held to the bell; an unfilled rest is cancelled at T-10 and logged NO_FILL."""
        t.reads += 1
        self._sync_entry(t, now)
        if now >= t.close_ts:
            self._cancel_all(t)
            if t.filled > 0:
                self._finish(t, now, SETTLE if self.held(t) > 0 else (t.pending or TARGET))
            else:
                self._finish(t, now, NO_FILL)
            return
        if t.filled == 0 and not t.entry_open:
            self._finish(t, now, NO_FILL)
            return
        # Filled favorite: HELD to the bell. No stop path — ride to settlement.

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

    def _cancel_all(self, t: EndTrip) -> bool:
        ok = True
        oids = [t.entry_oid] if t.entry_open else []
        for oid in oids:
            if oid not in self.gateway.resting:
                if oid == t.entry_oid:
                    t.entry_open = False
                continue
            st = self.gateway.cancel_tristate(oid)
            self._ev("cancel", t.market, oid, st)
            if st == "UNKNOWN":
                ok = False
            else:
                if oid == t.entry_oid:
                    t.entry_open = False
        return ok

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
        self.alert(f"🏁 END {t.closed_path} {t.market} {t.side} entry {t.cost}¢ "
                   f"x{_norm(t.filled)} (confirms {t.confirms}/{t.tier_confirms})")

    # ── the entry hunt ────────────────────────────────────────────
    def outside(self, now: float, books: Dict[str, OrderBook],
                meta: Dict[str, dict]) -> None:
        # The set we may enter THIS cycle. Empty END_SERIES => every admitted
        # 15M series (Drew 2026-10-17: "see every 15-minute market available").
        allowed = config.end_series_for(config.SERIES_INFO)

        # Pass 1 — run the gate (favorite, cost band, confirm ladder, counterparty)
        # and collect the SOLVED markets this cycle. Sizing is deferred because the
        # per-market budget is an EQUAL SPLIT of available cash across ALL solved
        # markets — we can't split before we know how many qualify.
        qualifiers: List[dict] = []
        for market, m in meta.items():
            q = self._gate(market, m, now, books.get(market), allowed)
            if q is not None:
                qualifiers.append(q)

        if not qualifiers:
            return

        # Pass 2 — equal-split the available cash across the solved markets, size
        # each (count-first, 1-contract floor), and enter. The running spendable
        # guard shrinks as fills land, so total deployment rides up to 100% of
        # cash without any single market going all-in.
        n = len(qualifiers)
        for q in qualifiers:
            spendable = self.spendable_cents()
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
                        q["confirms"], q["tier_confirms"], now, q["book"])

    def _market_wall_cents(self) -> int:
        """The END per-market at-risk wall in cents (gateway enforces it as a HARD
        reject). The sizer clamps each market's slice to this so it never
        self-inflicts a SKIP_WALL — one market never goes all-in."""
        return config.at_risk_cap_cents(LANE, self.spendable_cents())

    def _confirm_tick(self, market: str, side: str, cost: int, tier: dict) -> int:
        """Advance (or reset) the per-market confirm counter for THIS read and
        return the current consecutive-confirm count. The ladder state persists
        across cycles (step is cyclic, not a blocking poll). Resets on tier change
        or side-flip; the caller resets on below-floor / out-of-band / one-sided
        by passing nothing (handled in _gate). A read that is same-side, in-tier,
        above-floor increments the count."""
        tier_idx = config.END_CONFIRM_LADDER.index(tier)
        st = self.confirm_state.get(market)
        if (st is None or st["side"] != side or st["tier_idx"] != tier_idx):
            st = {"side": side, "tier_idx": tier_idx, "count": 0}
        st["count"] += 1
        self.confirm_state[market] = st
        return st["count"]

    def _confirm_reset(self, market: str) -> None:
        self.confirm_state.pop(market, None)

    def _gate(self, market: str, m: dict, now: float,
              book: Optional[OrderBook],
              allowed: Optional[frozenset] = None) -> Optional[dict]:
        """Run every entry gate. Return the solved market's entry parameters (a
        dict) when it passes, else record the verdict and return None. COST IS THE
        SIGNAL: favorite = richest side, in the 95-99c band, held through the
        confirm ladder, with live counterparty. No probability model.

        The confirm ladder runs whenever secs_left is inside END_WATCH_WINDOW_S;
        the ENTRY is placed only inside END_WINDOW_S (above the cancel buffer).
        Sizing/entry is deferred to the caller (equal-split across all solved)."""
        series = config.series_of(market)
        if allowed is None:                      # direct callers (tests) get live set
            allowed = config.end_series_for(config.SERIES_INFO)
        if series not in allowed:
            self.verdicts[market] = SKIP_NO_SERIES
            self._confirm_reset(market)
            return None
        info = config.SERIES_INFO.get(series)
        if info is None:
            self.verdicts[market] = SKIP_NO_SERIES
            self._confirm_reset(market)
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
            self._confirm_reset(market)
            return None
        secs_left = float(close_ts) - now

        # The confirm ladder may run as early as T-END_WATCH_WINDOW_S; above that
        # the market is too-early/undecided — SKIP_WINDOW and re-check next cycle.
        tier = _tier_for(secs_left)
        if secs_left > config.END_WATCH_WINDOW_S or tier is None:
            self.verdicts[market] = SKIP_WINDOW
            self._confirm_reset(market)
            return None

        if book is None:
            # WO-END-2: no live WS snapshot — fall back to the record touch (the
            # oracle ROT uses), so END sees every active room, not only the few
            # the feed fully snapshots. A stale/absent record is the real skip.
            book, why = self._record_book(market, m, now)
            if book is None:
                self.verdicts[market] = why or SKIP_NO_BOOK
                self._confirm_reset(market)
                return None
        side, cost = self.favorite(book)
        if side is None or cost is None:
            self.verdicts[market] = SKIP_NO_QUOTE
            self._confirm_reset(market)
            return None
        # The favorite's cost must be in the 95-99c solved band.
        if not (config.END_PRICE_FLOOR <= cost <= config.END_PRICE_CAP):
            self.verdicts[market] = SKIP_PRICE
            self._confirm_reset(market)
            return None
        # R2 — window-aware (Drew 2026-10-19): EARLY (orient tier, before the final
        # entry window) a solved market still shows a two-sided touch, so a
        # one-sided book is noise -> reset. But AT THE END a solved favorite at 99c
        # OFTEN has NO counterparty ("no one sells you the losing side when everyone
        # knows the outcome") — that absence is itself part of the solved signal, so
        # inside the final entry window we do NOT reject a one-sided book.
        in_final_window = secs_left <= config.END_WINDOW_S
        if not in_final_window and not self.has_counterparty(book, side):
            self.verdicts[market] = SKIP_NO_COUNTERPARTY
            self._confirm_reset(market)
            return None
        # The confirm ladder (the solved test). Below the tier's floor resets the
        # counter — the favorite has slipped off its pin.
        if cost < tier["floor_cents"]:
            self.verdicts[market] = SKIP_UNCONFIRMED
            self._confirm_reset(market)
            return None
        confirms = self._confirm_tick(market, side, cost, tier)
        if confirms < tier["confirms"]:
            self.verdicts[market] = SKIP_UNCONFIRMED
            self._ev("confirm", market, side, cost, confirms, tier["confirms"])
            return None

        # SOLVED. But only PLACE inside the final entry window (above the cancel
        # buffer). Confirmed-but-not-yet-in-window re-checks next cycle.
        if not (config.END_CANCEL_BEFORE_EXPIRY_S < secs_left <= config.END_WINDOW_S):
            self.verdicts[market] = SKIP_WINDOW
            return None

        strike = self.strike_fn(market)      # tape only; not a decision input
        return {
            "market": market, "series": series, "info": info,
            "side": side, "cost": cost, "close_ts": float(close_ts),
            "strike": float(strike) if isinstance(strike, (int, float)) else 0.0,
            "confirms": confirms, "tier_confirms": tier["confirms"], "book": book,
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
                           q["confirms"], q["tier_confirms"], now, q["book"])

    def _size(self, cost: int, budget_cents: int,
              spendable_cents: int) -> Tuple[Optional[Decimal], str]:
        """Count-first sizing against a per-market CASH budget (Drew 2026-10-17).

        The 1% (END_MAX_BANKROLL_PCT) is a SOFT per-position target, never a hard
        ceiling that blocks a bet. The desk spreads the available cash across the
        solved markets this cycle (equal split, computed by the caller) and buys
        as many lots as that slice affords — but with a MINIMUM-LOT FLOOR: if the
        slice can't cover even one lot yet spendable cash can, still take one lot.
        Never all-in on one market; spread across the board.

        budget_cents  = this market's equal-split slice of available cash.
        spendable_cents = total cash still available (the min-lot floor + the
                          overspend guard both read it).
        """
        if cost <= 0:
            return None, SKIP_SIZE
        step = Decimal(config.ROT_LOT_STEP)
        # WO-END-3 (Drew 2026-10-19): the FLOOR is ONE WHOLE CONTRACT — "if you are
        # able to do one contract for a market then you should be able to bet
        # that." One contract at `cost`c costs `cost` cents. The slice buys as many
        # whole contracts as it affords; if the slice is thinner than one contract
        # but spendable cash still covers one, take exactly one.
        one_contract_cents = int(Decimal(cost))     # a whole contract's cash cost
        contracts = (Decimal(max(0, budget_cents)) / Decimal(cost)
                     ).to_integral_value(rounding=ROUND_FLOOR)
        if contracts < 1:
            if spendable_cents >= one_contract_cents:
                return Decimal(1), ""               # the 1-contract floor
            return None, SKIP_NO_CASH
        return (contracts).quantize(step), ""

    def _enter(self, market, series, info, side, cost, size, close_ts, strike,
               confirms, tier_confirms, now, book) -> bool:
        shadow_only = not self.live_fn(info.underlying)
        why_s = (f"END endgame favorite {side}@{cost}c x{_norm(size)} · "
                 f"{info.underlying} · COST-IS-SIGNAL confirms {confirms}/"
                 f"{tier_confirms} in-band 95-99c · equal-split cash across solved "
                 f"markets, 1-contract floor (soft "
                 f"{config.END_MAX_BANKROLL_PCT:.0%} per-position target) · "
                 f"ride to settlement")
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
                    entry_oid=res.order_id, entry_ts=now,
                    confirms=int(confirms), tier_confirms=int(tier_confirms),
                    shadow_only=shadow_only)
        t.why_log.append(why_s)
        self.trips[market] = t
        self.entered[market] = float(close_ts)
        self.verdicts[market] = "ENTERED"
        self.confirm_state.pop(market, None)      # one entry per market; reset ladder
        self._ev("enter", market, side, cost, size, confirms)
        return True
