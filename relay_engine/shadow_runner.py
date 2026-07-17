"""Shadow runner — the paper-shadow main loop (gate 7).

Connects to the live Kalshi WS feed (read-only, §B3), runs every lane over every
market every cycle, writes surface rows and book snapshots to this engine's OWN
database, and places ZERO orders (RUN_MODE=SHADOW hard-disables the live path).

Run: python -m relay_engine.shadow_runner
"""

import asyncio
import json
import logging
import signal
import time

from . import config
from .boot import print_boot_tape
from .custodian import Custodian
from .errors import FatalIntegrityError
from .feed import DegradeLadder, Feed
from .gateway import Gateway
from .errors import WallRejection
from .lanes import build_registry
from .ledger import CashProtocol, Ledger
from .ops import Recorder, Telegram, daily_pack
from .surface import PASS, PROPOSED, Surface

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("relay.shadow")

CYCLE_SECONDS = 1.0
WINDOW_ROLL_CHECK_SECONDS = 5.0


class ShadowEngine:
    def __init__(self, db_path=None):
        self.ledger = Ledger(db_path)
        self.surface = Surface(self.ledger)
        self.cash = CashProtocol(self.ledger)
        self.telegram = Telegram(self.cash)
        self.cash.alert = self.telegram.alert
        self.recorder = Recorder(self.ledger)
        self.ladder = DegradeLadder(on_transition=self._on_ladder_move)
        self.feed = Feed(self.ladder, recorder=self.recorder)
        self.gateway = Gateway(self.ledger, self.surface)
        self.custodian = Custodian(self.gateway, self.ledger, self.surface, ladder=self.ladder)
        self.lanes = build_registry(self.ledger)
        self.fh8_shared = self.lanes[0].shared  # LaneF/LaneH8 shared evaluator
        self.boot_caps = None
        self._window_of = {}  # market -> window_id (market close ts as string)

    def boot(self):
        if config.RUN_MODE == "SHADOW" and self.ledger.book_cents() == 0:
            # Paper bankroll so budget walls exercise realistically (paper only).
            self.ledger.baseline(int(config.SHADOW_PAPER_BANKROLL_USD * 100),
                                 confirmed_by="shadow_paper_boot")
        self.boot_caps = self.ledger.snapshot_caps_at_boot()
        print_boot_tape(recorder=self.recorder, boot_caps=self.boot_caps)

    def _on_ladder_move(self, old, new):
        if new == "WS_LOST":
            self.gateway.halt_entries("DEGRADE_LADDER")
            log.warning("ALL-lane entry halt (WS lost); custodian on REST; risk reduction allowed")
        elif new == "WS_LIVE":
            self.gateway.resume_entries("DEGRADE_LADDER")
            log.warning("entries resumed after clean-frame count")

    def cycle(self, markets, now=None, spot=None):
        """One evaluation cycle: EVERY lane looks at EVERY market (C.4)."""
        now = time.time() if now is None else now
        for market in markets:
            window = self._window_of.setdefault(market, f"w-{market}")
            book = self.feed.book(market)
            transport = "WS" if self.ladder.entries_allowed() else "EXPLORATION"
            ctx = {
                "book": book, "now": now, "spot": spot,
                "cash_usd": self.ledger.book_cents() / 100.0,
                "entries_allowed": self.ladder.entries_allowed(),
            }
            for lane in self.lanes:
                decision = lane.evaluate(market, ctx)
                if decision.proposal is None:
                    if decision.interim:
                        # Still deciding — interim row (state change only), never terminal
                        self.surface.write_row(lane.name, market, window, "WATCHING",
                                               transport=transport,
                                               detail=decision.pass_reason)
                        continue
                    # A Pass is a first-class terminal row (one per window; re-asserts are no-ops).
                    self.surface.write_row(lane.name, market, window, PASS,
                                           transport=transport, detail=decision.pass_reason)
                else:
                    try:
                        result = self.gateway.submit(decision.proposal, book)
                    except WallRejection as e:
                        log.warning("wall rejected %s proposal on %s: %s",
                                    lane.name, market, e)
                        continue
                    # Live submit side-effects, shadow-mirrored: lane-scoped
                    # single entry + F hourly exposure (k_worker gateway.submit).
                    self.fh8_shared.state.mark_traded(market, decision.proposal.lane)
                    if decision.proposal.lane == "F":
                        self.fh8_shared.state.add_exposure(
                            decision.proposal.price_cents / 100.0)
                    self.surface.write_row(lane.name, market, window, PROPOSED,
                                           transport=transport,
                                           detail=f"shadow_order={result.order_id} "
                                                  f"@{decision.proposal.price_cents}c")
                    log.warning("SHADOW PROPOSAL %s %s %s @%dc -> %s",
                                lane.name, market, decision.proposal.side,
                                decision.proposal.price_cents, result.order_id)

    def close_window(self, market):
        self._window_of.pop(market, None)


async def run():
    engine = ShadowEngine()
    engine.boot()

    if config.RUN_MODE != "SHADOW":
        raise FatalIntegrityError(
            f"shadow_runner requires RUN_MODE=SHADOW, got {config.RUN_MODE}")

    try:
        import websockets
    except ImportError as e:
        raise FatalIntegrityError(f"websockets required for the WS-first feed: {e}")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    markets: set = set()
    last_pack = time.time()

    while not stop.is_set():
        try:
            async with websockets.connect(config.WS_URL) as ws:
                # Public orderbook/ticker channels; auth channels (fill/positions)
                # ride the same socket once creds are wired (read-only, §B3).
                await ws.send(json.dumps({
                    "id": 1, "cmd": "subscribe",
                    "params": {"channels": ["orderbook_delta", "ticker_v2"],
                               "series_tickers": [config.SERIES_TICKER]},
                }))
                log.info("WS subscribed: %s on %s", config.SERIES_TICKER, config.WS_URL)
                engine.ladder.snapshot_resynced()
                while not stop.is_set():
                    raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
                    engine.feed.handle_frame(raw)
                    try:
                        msg = json.loads(raw)
                        mkt = (msg.get("msg") or {}).get("market_ticker")
                        if mkt:
                            markets.add(mkt)
                    except (ValueError, AttributeError):
                        pass
                    engine.cycle(sorted(markets))
                    if time.time() - last_pack > 3600:
                        print(daily_pack(engine.ledger, engine.surface, engine.cash), flush=True)
                        last_pack = time.time()
        except FatalIntegrityError:
            raise  # fail loud, stay stopped
        except Exception as e:
            engine.feed.socket_died(str(e))
            log.warning("WS down (%s); degrade ladder engaged; reconnecting in 3s", e)
            await asyncio.sleep(3.0)

    print(daily_pack(engine.ledger, engine.surface, engine.cash), flush=True)
    log.info("shadow runner stopped; zero orders placed: %s",
             len(engine.gateway.shadow_orders) == 0)


if __name__ == "__main__":
    asyncio.run(run())
