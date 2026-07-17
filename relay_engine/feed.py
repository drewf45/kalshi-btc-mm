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
from enum import Enum
from typing import Callable, Dict, Optional

from . import config
from .book import OrderBook
from .errors import FatalIntegrityError

log = logging.getLogger("relay.feed")


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

    def __init__(self, ladder: DegradeLadder, recorder=None):
        self.ladder = ladder
        self.recorder = recorder
        self.books: Dict[str, OrderBook] = {}

    def book(self, market: str) -> OrderBook:
        if market not in self.books:
            self.books[market] = OrderBook(market=market)
        return self.books[market]

    def handle_frame(self, raw: str, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as e:
            # A garbled frame is not a clean frame; treat as transport damage.
            self.ladder.ws_lost(f"undecodable frame: {e}")
            return
        # Venue error codes are fail-loud paths (C.3), not silent skips.
        if msg.get("type") == "error":
            raise FatalIntegrityError(f"venue error frame: {msg}")
        mtype = msg.get("type")
        m = msg.get("msg", {})
        market = m.get("market_ticker", "")
        if mtype == "orderbook_snapshot" and market:
            book = self.book(market)
            book.apply_snapshot(
                {p: q for p, q in (m.get("yes") or [])},
                {p: q for p, q in (m.get("no") or [])},
                ts=now,
            )
            self.ladder.snapshot_resynced()
        elif mtype == "orderbook_delta" and market:
            self.book(market).apply_delta(m.get("side", "yes"), m.get("price", 0),
                                          m.get("delta", 0), ts=now)
        if self.recorder is not None and market:
            self.recorder.record(market, raw, now)
        self.ladder.clean_frame()

    def socket_died(self, why: str = "socket closed") -> None:
        self.ladder.ws_lost(why)
