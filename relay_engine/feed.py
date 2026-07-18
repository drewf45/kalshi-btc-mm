"""Feed — WebSocket-first, with the degrade ladder (C.3 / BUILD_SEQUENCE 3.1).

Channels: orderbook_delta, ticker, trade, fill, market_positions, market_lifecycle.
Venue error codes are fail-loud paths; every frame stamps staleness.

DEGRADE LADDER (the walk, in order):
  WS loss -> ALL-lane entry halt -> custodian on REST (risk reduction always
  allowed) -> snapshot resync -> resume entries after 10 clean frames.

REST polling is allowed for exploration-shadow only; rows produced from it are
tagged EXPLORATION (feed-parity law: promotion cells require WS evidence).
"""

import json
import logging
import time
from decimal import Decimal
from enum import Enum
from typing import Callable, Dict, Optional


def Decimal_q(q):
    """Quantities may arrive as ints or fp strings ('5.00') — Decimal both."""
    return int(Decimal(str(q)))

from . import config, failures
from .book import OrderBook
from .errors import FatalIntegrityError

log = logging.getLogger("relay.feed")

# P6 §1: the key ladders. NO DEFAULTS — a value either came off the wire
# through one of these keys or the frame is not a price event.
PRICE_KEYS = ("price", "price_dollars", "price_fp", "yes_price", "yes_price_dollars")
DELTA_KEYS = ("delta", "delta_fp", "change")
QTY_KEYS = ("count", "count_fp", "quantity", "qty", "size")
SIDE_KEYS = ("side",)  # P10 §1: side comes off the wire or the frame is refused
MAX_CONSECUTIVE_SHAPE_FAILURES = 3


def _parse_side(raw_side) -> Optional[str]:
    """P10 §1: NO DEFAULT SIDE — the deployed `m.get("side", "yes")` silently
    landed every side-less delta on the YES book (a crossed-book factory).
    Accepts yes/no case-insensitively; anything else is a shape failure."""
    if isinstance(raw_side, str):
        s = raw_side.strip().lower()
        if s in ("yes", "no"):
            return s
    return None


def _extract(m: dict, keys) -> object:
    for k in keys:
        v = m.get(k)
        if v is not None:
            return v
    return None


class FeedState(Enum):
    WS_LIVE = "WS_LIVE"                # normal: entries allowed (subject to walls)
    WS_LOST = "WS_LOST"                # socket dead: ALL-lane entry halt, custodian -> REST
    RESYNCING = "RESYNCING"            # snapshot re-acquired, counting clean frames
    # There is no state in which risk reduction is forbidden.


class DegradeLadder:
    """The transport state machine. Lanes ask `entries_allowed`; the custodian asks
    `custodian_transport`. Risk reduction is allowed in every state."""

    def __init__(self, clean_frames_required: int = config.WS_RESUME_CLEAN_FRAMES,
                 on_transition: Optional[Callable[[str, str], None]] = None):
        self.state = FeedState.WS_LIVE
        self.clean_frames_required = clean_frames_required
        self.clean_frames = 0
        self.on_transition = on_transition or (lambda old, new: None)

    def _move(self, new: FeedState, why: str) -> None:
        old = self.state
        if old is new:
            return
        self.state = new
        log.warning("DEGRADE LADDER: %s -> %s (%s)", old.value, new.value, why)
        self.on_transition(old.value, new.value)

    # -- events --
    def ws_lost(self, why: str = "socket closed") -> None:
        self.clean_frames = 0
        self._move(FeedState.WS_LOST, why)

    def snapshot_resynced(self) -> None:
        if self.state is FeedState.WS_LOST:
            self.clean_frames = 0
            self._move(FeedState.RESYNCING, "snapshot re-acquired")

    def clean_frame(self) -> None:
        if self.state is FeedState.RESYNCING:
            self.clean_frames += 1
            if self.clean_frames >= self.clean_frames_required:
                self._move(FeedState.WS_LIVE, f"{self.clean_frames} clean frames")
        elif self.state is FeedState.WS_LIVE:
            pass  # steady state

    # -- queries --
    def entries_allowed(self) -> bool:
        return self.state is FeedState.WS_LIVE

    def custodian_transport(self) -> str:
        """WS while live; REST the moment the socket is lost (risk reduction always allowed)."""
        return "WS" if self.state is FeedState.WS_LIVE else "REST"


class Feed:
    """WS feed wrapper. The socket object is injected so the fire-drill test can
    kill it mid-shadow; production passes a real websockets connection factory."""

    CHANNELS = ("orderbook_delta", "ticker", "trade", "fill", "market_positions",
                "market_lifecycle")

    def __init__(self, ladder: DegradeLadder, recorder=None, on_poison=None):
        self.ladder = ladder
        self.recorder = recorder
        self.books: Dict[str, OrderBook] = {}
        self.last_sent_payload: Optional[str] = None  # F1d: echoed in config-error FATALs
        self.order_errors = 0                         # per-order errors routed, counted
        self.book_units: Optional[str] = None         # P5 §3: asserted on first snapshot
        self.shape_failures = 0                       # P6 §1: consecutive unparseable deltas
        self.frames_seen = 0                          # P7: storm telemetry
        # P10 §2: coherence machinery. resync_needed is the runner's work queue
        # (markets whose book needs a fresh snapshot); on_poison(market, yb, nb)
        # fires ONCE per poison episode.
        self.resync_needed: set = set()
        self.on_poison = on_poison or (lambda market, yb, nb: None)
        self._nosnap_banked: set = set()   # markets already banked for no-foundation deltas

    def note_sent(self, payload: str) -> None:
        """Runner registers each subscribe/command payload for the error autopsy."""
        self.last_sent_payload = payload

    def book(self, market: str) -> OrderBook:
        if market not in self.books:
            self.books[market] = OrderBook(market=market)
        return self.books[market]

    def drop_book(self, market: str) -> None:
        self.books.pop(market, None)

    # P5 §2 (Adversary): classification keys on the venue's NUMERIC code first.
    # Known per-order codes route; known config codes (incl. 2 unknown cmd,
    # 8 unknown channel) are fatal; UNKNOWN codes are fatal. The keyword scan
    # is a tiebreak for CODELESS frames only — a config error whose message
    # happens to contain the word "order" still dies loud. These sets grow
    # empirically; every growth is a reviewed commit.
    PER_ORDER_CODES = frozenset({25, 27})
    CONFIG_CODES = frozenset({2, 6, 8})

    def _classify_error(self, msg: dict) -> str:
        code = (msg.get("msg") or {}).get("code")
        if code is not None:
            if code in self.PER_ORDER_CODES:
                return "order"
            return "config"  # known-config and unknown codes both die loud
        text = json.dumps(msg).lower()
        if any(k in text for k in ("order", "insufficient", "self_trade", "post only")):
            return "order"
        return "config"

    def handle_frame(self, raw: str, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        self.frames_seen += 1
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as e:
            # A garbled frame is not a clean frame; treat as transport damage.
            self.ladder.ws_lost(f"undecodable frame: {e}")
            return
        # Venue error codes are fail-loud paths (C.3), not silent skips —
        # but CLASSIFIED (F1d): a per-order rejection must not halt a live
        # book with positions open.
        if msg.get("type") == "error":
            if self._classify_error(msg) == "order":
                self.order_errors += 1
                log.warning("venue per-order error frame (routed, not fatal): %s", msg)
                return
            failures.fail("WS_CONFIG_ERROR",
                          f"venue config/subscribe error: {msg} — sent payload was: "
                          f"{self.last_sent_payload}", fatal=True, frame=msg)
        mtype = msg.get("type")
        m = msg.get("msg", {})
        market = m.get("market_ticker", "")
        seq = msg.get("seq")
        if mtype == "orderbook_snapshot" and market:
            yes_levels, yes_fps = self._parse_levels(m.get("yes") or [], market, raw)
            no_levels, no_fps = self._parse_levels(m.get("no") or [], market, raw)
            book = self.book(market)
            book.apply_snapshot(yes_levels, no_levels, ts=now,
                                yes_fp=yes_fps, no_fp=no_fps)
            if seq is not None:
                book.last_seq = int(seq)
            self.shape_failures = 0
            self._nosnap_banked.discard(market)
            self.resync_needed.discard(market)
            self._check_coherence(book, market, raw)
            self.ladder.snapshot_resynced()
        elif mtype == "orderbook_delta" and market:
            # P6 §1: NO DEFAULTS. Price/delta/SIDE come off the wire through
            # the key ladders or the frame is not a price event — tagged,
            # banked with the raw frame, book dropped, resync requested,
            # engine continues.
            raw_price = _extract(m, PRICE_KEYS)
            raw_delta = _extract(m, DELTA_KEYS)
            side = _parse_side(_extract(m, SIDE_KEYS))
            if (raw_price in (None, 0, "0", "0.0") or raw_delta is None
                    or side is None):
                self.shape_failures += 1
                self.drop_book(market)
                self.ladder.ws_lost("frame shape unknown — book dropped, resync required")
                if self.shape_failures >= MAX_CONSECUTIVE_SHAPE_FAILURES:
                    failures.fail(
                        "WS_DELTA_UNPARSEABLE",
                        f"{self.shape_failures} consecutive unparseable delta frames",
                        fatal=True, last_raw_frame=raw)
                failures.fail("FRAME_SHAPE_UNKNOWN",
                              f"delta frame without price/delta/side on {market}",
                              raw_frame=raw)
                return
            # The frame PARSED — the consecutive-unparseable count resets here
            # even if the delta is refused below (foundation/seq are book-state
            # laws, not wire-dialect damage).
            self.shape_failures = 0
            # P10 §1: a delta may only land on a SNAPSHOT FOUNDATION — a book
            # rebuilt from deltas alone (post-drop) is fiction and goes crossed.
            book = self.books.get(market)
            if book is None or not book.has_snapshot:
                self.resync_needed.add(market)
                if market not in self._nosnap_banked:
                    self._nosnap_banked.add(market)
                    failures.fail("DELTA_WITHOUT_SNAPSHOT",
                                  f"delta on {market} with no snapshot foundation "
                                  f"— refused, resync requested", market=market)
                return
            # P10 §1: a seq gap means missed deltas — the book has silently
            # drifted; refuse to keep applying onto a stale foundation.
            if seq is not None and book.last_seq is not None \
                    and int(seq) != book.last_seq + 1:
                self.drop_book(market)
                self.resync_needed.add(market)
                failures.fail("BOOK_SEQ_GAP",
                              f"{market}: delta seq {seq} after {book.last_seq} "
                              f"— book dropped, resync requested",
                              market=market, seq=seq, last_seq=book.last_seq)
                return
            cents = self._parse_price(raw_price)
            fp = str(raw_price) if (self.book_units == "dollars"
                                    and isinstance(raw_price, str)) else None
            book.apply_delta(side, cents, int(Decimal(str(raw_delta))), ts=now,
                             fp=fp)
            if seq is not None:
                book.last_seq = int(seq)
            self._check_coherence(book, market, raw)
        if self.recorder is not None and market:
            self.recorder.record(market, raw, now)
        self.ladder.clean_frame()

    # ── P10 §2: the coherence invariant (P7 §1c, now actually built) ────
    def _check_coherence(self, book: OrderBook, market: str, raw: str) -> None:
        """After EVERY apply: yes+no > 101 → POISON the book, request resync,
        page once per episode. Never FATAL — one market's corrupt book must
        not stop the others; a clean snapshot clears the poison."""
        if book.coherent():
            return
        yb, nb = book.best_yes_bid(), book.best_no_bid()
        new_episode = not book.poisoned
        book.poisoned = True
        self.resync_needed.add(market)
        failures.fail("BOOK_INCOHERENT",
                      f"{market}: yes {yb} + no {nb} = {yb + nb} > 101 — book "
                      f"POISONED, snapshot resync forced",
                      market=market, yes=yb, no=nb, last_frame=raw[:1000],
                      alert=False)  # the page is the episode line below, once
        if new_episode:
            self.on_poison(market, yb, nb)

    # ── P5 §3: first-frame unit assertion (Trader verify) ──────────────
    # The REST book is fp-dollars; the WS payload's units are asserted on the
    # FIRST snapshot each connection: int 1-99 = cents, decimal-string <= 1.00
    # = dollars. Neither form = FATAL with the raw level echoed.
    def _assert_units(self, price) -> str:
        from decimal import Decimal, InvalidOperation
        if isinstance(price, int) and 1 <= price <= 99:
            return "cents"
        if isinstance(price, str):
            try:
                d = Decimal(price)
                if 0 < d <= 1:
                    return "dollars"
                if 1 <= d <= 99 and d == int(d):
                    return "cents"
            except InvalidOperation:
                pass
        if isinstance(price, float) and 0 < price <= 1:
            return "dollars"
        failures.fail("WS_UNITS_UNPARSEABLE",
                      f"WS book units unrecognized — raw level price {price!r} is "
                      f"neither int cents 1-99 nor dollar form; refusing to guess",
                      fatal=True, raw_price=repr(price))

    def _parse_price(self, price) -> int:
        from decimal import Decimal
        if self.book_units is None:
            self.book_units = self._assert_units(price)
            log.info("WS book units: %s", self.book_units)
        if self.book_units == "dollars":
            return int(Decimal(str(price)) * 100)
        return int(price)

    def _parse_levels(self, levels, market: str = "", raw: str = ""):
        """P6 §1c: level tuples AND dict forms, through the same key ladder;
        an unparseable level is a banked shape failure, never a guess.
        Returns (levels_cents, fp_strings) — the exact touch is kept when the
        wire speaks dollars (the parts' true-touch resting law)."""
        out = {}
        fps = {}
        for lv in levels:
            if isinstance(lv, dict):
                p = _extract(lv, PRICE_KEYS)
                q = _extract(lv, QTY_KEYS)
            else:
                p = lv[0] if len(lv) > 0 else None
                q = lv[1] if len(lv) > 1 else None
            if p in (None, 0, "0", "0.0") or q is None:
                failures.fail("FRAME_SHAPE_UNKNOWN",
                              f"snapshot level unparseable on {market}",
                              level=repr(lv), raw_frame=raw[:1000])
                continue
            cents = self._parse_price(p)
            out[cents] = Decimal_q(q)
            if self.book_units == "dollars" and isinstance(p, str):
                fps[cents] = p
        return out, fps

    def socket_died(self, why: str = "socket closed") -> None:
        self.book_units = None      # units re-assert per connection
        self.shape_failures = 0     # the consecutive count is per connection
        self.resync_needed.clear()  # reconnect re-snapshots every market anyway
        self._nosnap_banked.clear()
        self.ladder.ws_lost(why)
