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
from urllib.parse import urlparse

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
        self.lanes = build_registry(self.ledger, gateway=self.gateway,
                                    custodian=self.custodian)
        self.fh8_shared = self.lanes[0].shared  # LaneF/LaneH8 shared evaluator
        self.flip = self.lanes[2].flip          # LaneFlip (arbitration slot 3)
        from .fills import FillBooker
        self.fills = FillBooker(self.gateway, self.ledger, self.surface,
                                custodian=self.custodian,
                                alert_fn=self.telegram.alert,
                                on_booked=self._on_fill_booked)
        self.boot_caps = None
        self._window_of = {}  # market -> window_id (market close ts as string)

    def _on_fill_booked(self, order, action, price_cents, count, now):
        if order.lane == "FLIP":
            if action == "ENTRY":
                self.flip.note_fill(order.market, order.side, price_cents, now)
            else:
                self.flip.note_exit(order.market, order.side, price_cents, now)
        elif order.lane == "D":
            laned = self.lanes[3].d
            if action == "ENTRY":
                laned.budget.convert(order.market)  # reservation -> at_risk
            else:
                laned.budget.release(order.market, f"{action} filled @{price_cents}c")
                laned.seeded.pop(order.market, None)
                self.lanes[3].exit_posted.discard(order.market)

    def boot(self, auth_line=None):
        if config.RUN_MODE == "SHADOW" and self.ledger.book_cents() == 0:
            # Paper bankroll so budget walls exercise realistically (paper only).
            self.ledger.baseline(int(config.SHADOW_PAPER_BANKROLL_USD * 100),
                                 confirmed_by="shadow_paper_boot")
        self.boot_caps = self.ledger.snapshot_caps_at_boot()
        print_boot_tape(recorder=self.recorder, boot_caps=self.boot_caps,
                        auth_line=auth_line)

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
            # FLIP pair-grace housekeeping: drop the unfilled opposite entry
            stale_oid = self.flip.pair_grace_expired(market, now)
            if stale_oid is not None:
                self.gateway.cancel(stale_oid)

            for lane in self.lanes:
                decision = lane.evaluate(market, ctx)
                proposals = decision.multi if decision.multi else (
                    [decision.proposal] if decision.proposal is not None else [])
                if not proposals:
                    if decision.interim:
                        # Still deciding — interim row (state change only), never terminal
                        self.surface.write_row(lane.name, market, window, "WATCHING",
                                               transport=transport,
                                               detail=decision.pass_reason)
                        continue
                    # A Pass is a first-class terminal row (one per window; re-asserts are no-ops).
                    self.surface.write_row(lane.name, market, window, PASS,
                                           transport=transport, detail=decision.pass_reason)
                    continue
                for proposal in proposals:
                    try:
                        result = self.gateway.submit(proposal, book)
                    except WallRejection as e:
                        log.warning("wall rejected %s proposal on %s: %s",
                                    lane.name, market, e)
                        continue
                    if proposal.lane in ("F", "H8"):
                        # Live submit side-effects, mirrored from k_worker gateway.submit:
                        # lane-scoped single entry + F hourly exposure.
                        self.fh8_shared.state.mark_traded(market, proposal.lane)
                        if proposal.lane == "F":
                            self.fh8_shared.state.add_exposure(
                                proposal.price_cents / 100.0)
                    elif proposal.lane == "FLIP":
                        self.flip.on_submitted(proposal, result.order_id, now)
                    self.surface.write_row(lane.name, market, window, PROPOSED,
                                           transport=transport,
                                           detail=f"order={result.order_id} "
                                                  f"{proposal.purpose} @{proposal.price_cents}c")
                    log.warning("PROPOSAL %s %s %s %s @%dc -> %s",
                                lane.name, market, proposal.purpose, proposal.side,
                                proposal.price_cents, result.order_id)

    def close_window(self, market):
        self._window_of.pop(market, None)


async def run():
    from . import auth

    # Auth boot-stop (§4 doctrine): missing/unparseable creds are FATAL here,
    # BEFORE any connect attempt — never a retry loop.
    auth_line = auth.boot_check()

    engine = ShadowEngine()
    engine.boot(auth_line=auth_line)

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
    ws_path = urlparse(config.WS_URL).path or "/trade-api/ws/v2"
    rejections = auth.HandshakeRejections(limit=3)

    while not stop.is_set():
        try:
            # Sign at connect time, never import time — fresh timestamp per attempt.
            async with websockets.connect(
                    config.WS_URL,
                    additional_headers=auth.signed_headers("GET", ws_path)) as ws:
                await ws.send(json.dumps({
                    "id": 1, "cmd": "subscribe",
                    "params": {"channels": ["orderbook_delta", "ticker_v2"],
                               "series_tickers": [config.SERIES_TICKER]},
                }))
                rejections.success()  # venue accepted the handshake
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
            # §2.4: a 401/403 handshake is an AUTH event, not transport damage —
            # three consecutive rejections escalate FATAL. Everything else is
            # the ladder's (transport) business, unchanged.
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status is not None:
                rejections.rejected(status)  # raises FatalIntegrityError on strike 3
            engine.feed.socket_died(str(e))
            log.warning("WS down (%s); degrade ladder engaged; reconnecting in 3s", e)
            await asyncio.sleep(3.0)

    print(daily_pack(engine.ledger, engine.surface, engine.cash), flush=True)
    log.info("shadow runner stopped; zero orders placed: %s",
             len(engine.gateway.shadow_orders) == 0)


if __name__ == "__main__":
    asyncio.run(run())
