"""Lanes — all five registered; every lane evaluates every market every cycle (C.4).

A Pass is a FIRST-CLASS TERMINAL ROW: a lane that declines a market says so on
the surface, with a reason, every window.

F and H8 are PORTED from the live tree (B1) — ladder + gates byte-identical,
proven by the golden-tape regression (docs/GOLDEN_TAPE_REPORT.md). They share
one evaluator (relay_engine/lane_fh8.py): the live engine computes the favorite
side once and lane membership falls out of the cost band, so LaneF and LaneH8
here read the same decision and each reports its own side of it.
D / MM / P remain SHADOW/STUB per charter §5 — registered, passing, awaiting
their chunks (D and P are Chunk 6; MM is Chunk 8).

This module holds no capital state; proposals go to the gateway, which owns the
walls. (Win/loss path symmetry lives where the capital lives.)
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from . import config, lane_fh8
from .book import OrderBook
from .gateway import Order

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
          "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


def infer_close_ts_from_ticker(ticker: str, interval_minutes: int = 15) -> Optional[int]:
    """Close-time inference from the ticker's datetime chunk (mechanic borrowed
    from the legacy parts shelf; fallback when the market record is absent)."""
    try:
        parts = str(ticker).split("-")
        if len(parts) < 2:
            return None
        dt_chunk = parts[1]
        day = int(dt_chunk[0:2])
        mon = MONTHS[dt_chunk[2:5].upper()]
        year = 2000 + int(dt_chunk[5:7])
        hh = int(dt_chunk[7:9])
        mm = int(dt_chunk[9:11])
        start_local = datetime(year, mon, day, hh, mm, tzinfo=NY)
        close_local = start_local + timedelta(minutes=int(interval_minutes))
        return int(close_local.astimezone(UTC).timestamp())
    except Exception:
        return None


def touch_book_from(ob: OrderBook) -> lane_fh8.TouchBook:
    """Adapter: relay bids-only book -> the touch view the F/H8 logic reads."""
    yb, nb = ob.best_yes_bid(), ob.best_no_bid()
    return lane_fh8.TouchBook(
        yes_bid=yb, no_bid=nb,
        yes_ask=(100 - nb) if nb is not None else None,
        no_ask=(100 - yb) if yb is not None else None,
        yes_bid_qty=ob.visible_depth("yes", yb) if yb is not None else 0,
        no_bid_qty=ob.visible_depth("no", nb) if nb is not None else 0,
    )


@dataclass
class Decision:
    lane: str
    market: str
    proposal: Optional[Order]  # None = Pass (terminal) or still-watching (interim)
    pass_reason: str = ""
    interim: bool = False      # True = not a verdict yet (ladder watching, too early)
    multi: Optional[list] = None  # multi-proposal lanes (FLIP): list[Order], submitted in order


class Lane:
    name = "?"

    def evaluate(self, market: str, ctx: dict) -> Decision:
        raise NotImplementedError


class FH8Shared:
    """One evaluation per market per cycle, shared by LaneF and LaneH8 (the live
    engine decides once; lane membership falls out of the cost band)."""

    def __init__(self, ledger=None):
        self.state = lane_fh8.FH8State()
        self.stats = (ledger_stats(ledger) if ledger is not None
                      else lane_fh8.FH8Stats())
        self.ladders: Dict[str, lane_fh8.WatchLadder] = {}
        self._cache: Dict[str, tuple] = {}  # market -> (now, outcome)

    def decide(self, market: str, ctx: dict):
        """Returns ('PASS', reason) or ('PROPOSE', EvalResult)."""
        now = ctx.get("now", 0.0)
        cached = self._cache.get(market)
        if cached is not None and cached[0] == now:
            return cached[1]
        outcome = self._decide(market, ctx)
        self._cache[market] = (now, outcome)
        return outcome

    def _decide(self, market: str, ctx: dict):
        """Returns ('PROPOSE', EvalResult) | ('PASS', reason) terminal |
        ('WAIT', reason) interim (still deciding this window)."""
        now = ctx.get("now", 0.0)
        close_ts = ctx.get("close_ts") or infer_close_ts_from_ticker(market)
        if close_ts is None:
            return ("PASS", "NO_CLOSE_TS")
        secs_left = close_ts - now
        if secs_left < lane_fh8.CANCEL_BEFORE_EXPIRY_SEC:
            return ("PASS", "EXPIRED_BEFORE_EVAL")
        if secs_left > lane_fh8.WATCH_WINDOW_SEC:
            return ("WAIT", "TOO_EARLY")
        if not ctx.get("entries_allowed", True):
            return ("WAIT", "ENTRY_HALT_DEGRADE_LADDER")

        book = touch_book_from(ctx["book"]) if ctx.get("book") is not None \
            else lane_fh8.TouchBook()
        cash = ctx.get("cash_usd", 0.0)
        spot = ctx.get("spot")
        blo, bhi = ctx.get("boundary_lo"), ctx.get("boundary_hi")

        if secs_left > lane_fh8.ENTRY_WINDOW_SEC:
            # Watch-confirm ladder territory
            ladder = self.ladders.setdefault(market, lane_fh8.WatchLadder())
            r = ladder.tick(secs_left, book)
            if r == "CONFIRMED":
                res = lane_fh8.ladder_confirm_decision(
                    ladder, market, book, secs_left, cash, spot, blo, bhi,
                    state=self.state, stats=self.stats)
                if res is not None:
                    return ("PROPOSE", res)
                return ("WAIT", "LADDER_CONFIRMED_EVAL_REJECTED")
            return ("WAIT", f"LADDER_{r}_confirms_{ladder.confirm_count}")

        # Final window (T-180): single evaluation, live semantics
        res = lane_fh8.evaluate(market, book, secs_left, cash, spot=spot,
                                boundary_lo=blo, boundary_hi=bhi,
                                state=self.state, stats=self.stats)
        if res.allowed:
            return ("PROPOSE", res)
        return ("PASS", res.reject_code or "REJECTED")

    def to_order(self, market: str, res: lane_fh8.EvalResult) -> Order:
        """Live semantics: rest post-only at the favorite's touch, flat 1 lot."""
        band = ((lane_fh8.COST_BAND_LO_INT, lane_fh8.COST_BAND_HI_INT)
                if res.lane == "F" else (int(lane_fh8.H8_COST_LO), int(lane_fh8.H8_COST_HI)))
        return Order(
            lane=res.lane, event=market.rsplit("-", 1)[0], market=market,
            side=res.side, action="buy", price_cents=res.cost_cents,
            count=1, size_tier=config.TIER_PROBE, purpose="ENTRY", band=band,
        )


def ledger_stats(ledger) -> lane_fh8.FH8Stats:
    """D4: the live store's evidence questions answered from this engine's DB."""
    import time as _t

    def lane_losses_recent(lane: str, window_sec: int) -> int:
        return int(ledger.db.execute(
            "SELECT COUNT(*) FROM settlements WHERE lane=? AND pnl_cents<0 AND ts>=?",
            (lane, _t.time() - window_sec)).fetchone()[0])

    def h8_probe_lifetime() -> dict:
        wins = int(ledger.db.execute(
            "SELECT COUNT(*) FROM settlements WHERE lane='H8' AND pnl_cents>0").fetchone()[0])
        losses = int(ledger.db.execute(
            "SELECT COUNT(*) FROM settlements WHERE lane='H8' AND pnl_cents<0").fetchone()[0])
        return {"wins": wins, "losses": losses}

    def h8_probe_daily() -> dict:
        cents = ledger.db.execute(
            "SELECT COALESCE(SUM(price_cents*count),0) FROM fills"
            " WHERE lane='H8' AND action='ENTRY' AND ts>=?",
            (_t.time() - 86400,)).fetchone()[0]
        return {"at_risk": cents / 100.0}

    return lane_fh8.FH8Stats(
        lane_losses_recent=lane_losses_recent,
        h8_probe_lifetime=h8_probe_lifetime,
        h8_probe_daily=h8_probe_daily,
    )


class _PortedLane(Lane):
    other_band_reason = "?"

    def __init__(self, shared: FH8Shared):
        self.shared = shared

    def evaluate(self, market: str, ctx: dict) -> Decision:
        kind, payload = self.shared.decide(market, ctx)
        if kind == "PROPOSE" and payload.lane == self.name:
            return Decision(self.name, market, self.shared.to_order(market, payload))
        if kind == "PROPOSE":  # the decision went to the other lane's band
            return Decision(self.name, market, None, pass_reason=self.other_band_reason)
        return Decision(self.name, market, None, pass_reason=payload,
                        interim=(kind == "WAIT"))


class LaneF(_PortedLane):
    name = "F"
    other_band_reason = "COST_IN_H8_BAND"


class LaneH8(_PortedLane):
    name = "H8"
    other_band_reason = "COST_IN_F_BAND"


class StubLane(Lane):
    """Not-yet-built lanes: registered and looking, proposing nothing yet."""

    def __init__(self, name: str, chunk: str):
        self.name = name
        self.chunk = chunk

    def evaluate(self, market: str, ctx: dict) -> Decision:
        return Decision(self.name, market, None, pass_reason=f"STUB_AWAITING_{self.chunk}")


class FlipLaneWrapper(Lane):
    """Adapter: LaneFlip proposes 0..n orders per cycle (both sides + takes)."""

    name = "FLIP"

    def __init__(self, flip):
        self.flip = flip

    def evaluate(self, market: str, ctx: dict) -> Decision:
        close_ts = ctx.get("close_ts") or infer_close_ts_from_ticker(market)
        fctx = dict(ctx)
        fctx["close_ts"] = close_ts
        if close_ts is None:
            return Decision(self.name, market, None, pass_reason="NO_CLOSE_TS")
        now = ctx.get("now", 0.0)
        secs = close_ts - now
        if self.flip.killed:
            return Decision(self.name, market, None, pass_reason="LANE_KILLED_STOP_STREAK")
        if secs < self.flip_flat_at():
            # window over: close the books for this window (stop-streak feed)
            w = self.flip.windows.get(market)
            if w is not None and not w.done:
                w.done = True
                self.flip.note_window_result(market, w.window_realized)
            return Decision(self.name, market, None, pass_reason="WINDOW_OVER")
        if not ctx.get("entries_allowed", True):
            return Decision(self.name, market, None,
                            pass_reason="ENTRY_HALT_DEGRADE_LADDER", interim=True)
        proposals = self.flip.evaluate(market, fctx)
        if proposals:
            return Decision(self.name, market, None, multi=proposals)
        return Decision(self.name, market, None, pass_reason="FLIP_WATCHING", interim=True)

    @staticmethod
    def flip_flat_at():
        from .lane_flip import FLIP_FLAT_AT
        return FLIP_FLAT_AT


class DLaneWrapper(Lane):
    """Adapter for LaneD: entry proposal, recovery-exit baton, watchdog verdicts."""

    name = "D"

    def __init__(self, laned):
        self.d = laned
        self.exit_posted: set = set()

    def evaluate(self, market: str, ctx: dict) -> Decision:
        close_ts = ctx.get("close_ts") or infer_close_ts_from_ticker(market)
        if close_ts is None:
            return Decision(self.name, market, None, pass_reason="NO_CLOSE_TS")
        fctx = dict(ctx)
        fctx["close_ts"] = close_ts
        if not ctx.get("entries_allowed", True):
            return Decision(self.name, market, None,
                            pass_reason="ENTRY_HALT_DEGRADE_LADDER", interim=True)
        # watchdog first: broken evidence on an open seed outranks new entries
        broken = self.d.watchdog_tick(market, fctx)
        if broken:
            return Decision(self.name, market, None, pass_reason=broken, interim=True)
        # recovery baton: a filled seed gets its resting recovery exit once
        if (self.d.gateway is not None and market in self.d.seeded
                and market not in self.exit_posted):
            event = market.rsplit("-", 1)[0]
            if self.d.gateway.positions.get((event, market, "D"), 0) != 0:
                self.exit_posted.add(market)
                return Decision(self.name, market, self.d.recovery_exit(market))
        proposal = self.d.evaluate(market, fctx)
        if proposal is not None:
            return Decision(self.name, market, proposal)
        v = getattr(self.d, "last_verdict", None)
        reason = v.verdict if v is not None else "SKIP_NO_VERDICT"
        interim = reason in ("SKIP_CANT_VERIFY", "SKIP_NO_BOOK", "SKIP_ALREADY_SEEDED")
        return Decision(self.name, market, None, pass_reason=reason, interim=interim)


class PLaneWrapper(Lane):
    """Adapter for LaneP: (proposal, reason) -> Decision."""

    name = "P"

    def __init__(self, lanep):
        self.p = lanep

    def evaluate(self, market: str, ctx: dict) -> Decision:
        close_ts = ctx.get("close_ts") or infer_close_ts_from_ticker(market)
        if close_ts is None:
            return Decision(self.name, market, None, pass_reason="NO_CLOSE_TS")
        if not ctx.get("entries_allowed", True):
            return Decision(self.name, market, None,
                            pass_reason="ENTRY_HALT_DEGRADE_LADDER", interim=True)
        fctx = dict(ctx)
        fctx["close_ts"] = close_ts
        proposal, reason = self.p.evaluate(market, fctx)
        if proposal is not None:
            return Decision(self.name, market, proposal)
        interim = reason.startswith("CONFIRMING") or reason in (
            "STALE_FRAME", "NO_NEW_FRAME", "WHIPSAW_RESET", "NO_BOOK")
        return Decision(self.name, market, None, pass_reason=reason, interim=interim)


def build_registry(ledger=None, gateway=None, custodian=None) -> List[Lane]:
    """All five lanes, always. ORDER IS THE ARBITRATION ORDER (P3 Broker part):
    within a cycle, custodian exits run before lanes (the runner's job), a
    FLIP take/second-leg rides inside FLIP's own multi-proposal list (takes
    first), and new entries submit in F -> H8 -> FLIP -> D -> P order."""
    from .lane_d import LaneD
    from .lane_flip import LaneFlip
    from .lane_p import LaneP
    shared = FH8Shared(ledger)
    flip = LaneFlip(gateway, custodian=custodian,
                    stats=(ledger_stats(ledger) if ledger is not None else None))
    laned = LaneD(gateway=gateway, custodian=custodian, ledger=ledger)
    lanep = LaneP(gateway=gateway, custodian=custodian)
    return [
        LaneF(shared),
        LaneH8(shared),
        FlipLaneWrapper(flip),
        DLaneWrapper(laned),
        PLaneWrapper(lanep),
    ]


LANE_D_BAND = (config.LANE_D_FLOOR_CENTS, 99)  # floor per DREW-DEFAULT, pending Chunk 2
