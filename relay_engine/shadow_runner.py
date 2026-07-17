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

def subscribe_cmd(cmd_id: int, market_tickers, channel: str) -> dict:
    """F1b/P5 §1: ONE channel per cmd — a stranger channel name can only kill
    itself, never the book. market_tickers always; never series_tickers."""
    return {"id": int(cmd_id), "cmd": "subscribe",
            "params": {"channels": [channel],
                       "market_tickers": sorted(market_tickers)}}


# P5 §1: the venue-dialect table. Canonical name -> fallback names to try ONCE
# on code 8 ("Unknown channel name"). orderbook_delta and fill have no alias.
CHANNEL_FALLBACKS = {
    "orderbook_delta": [],
    "ticker_v2": ["ticker"],
    "market_lifecycle_v2": ["market_lifecycle"],
    "fill": [],
}
ESSENTIAL_CHANNEL = "orderbook_delta"  # no book, no engine


class ChannelSubscriber:
    """P5 §1 — per-channel subscription with empirical fallback, sans-io.
    The runner sends the cmds this produces and feeds venue replies back in.

    Classification: ESSENTIAL_CHANNEL failing every name = FATAL with echo;
    any other channel failing = WARN 'degraded' and CONTINUE (lifecycle loss is
    covered by the 60s sweep, fill loss by the REST fills sweep, ticker loss
    costs telemetry only). The accepted vocabulary is logged once (Broker knob:
    the empirical dialect is worth a line in the tape for the next port)."""

    def __init__(self):
        self.seq = 0
        self.accepted: dict = {}   # canonical -> venue name that worked
        self.degraded: list = []
        self.pending: dict = {}    # cmd_id -> (canonical, tried_name, tickers, remaining_fallbacks)
        self._vocab_logged = False

    def cmds_for(self, market_tickers, channels=None) -> list:
        """Subscribe cmds for these markets, one per channel, using the accepted
        vocabulary where already learned."""
        out = []
        for canonical in (channels or list(CHANNEL_FALLBACKS)):
            if canonical in self.degraded:
                continue
            name = self.accepted.get(canonical, canonical)
            self.seq += 1
            cmd = subscribe_cmd(self.seq, market_tickers, name)
            self.pending[self.seq] = (canonical, name, sorted(market_tickers),
                                      list(CHANNEL_FALLBACKS.get(canonical, []))
                                      if canonical not in self.accepted else [])
            out.append(cmd)
        return out

    def on_reply(self, msg: dict):
        """Feed a venue reply carrying an id we sent. Returns:
        None (not ours / handled) | a retry cmd dict (send it) — and raises
        FatalIntegrityError when the essential channel fails every name."""
        cmd_id = msg.get("id")
        if cmd_id not in self.pending:
            return None
        canonical, tried, tickers, fallbacks = self.pending.pop(cmd_id)
        mtype = msg.get("type")
        code = (msg.get("msg") or {}).get("code")
        if mtype != "error":
            self.accepted[canonical] = tried
            self._maybe_log_vocab()
            return None
        if code == 8 and fallbacks:
            # unknown channel name: try the fallback ONCE
            self.seq += 1
            retry = subscribe_cmd(self.seq, tickers, fallbacks[0])
            self.pending[self.seq] = (canonical, fallbacks[0], tickers, fallbacks[1:])
            log.warning("WS channel %r unknown — trying dialect %r", tried, fallbacks[0])
            return retry
        # no names left (or a non-vocabulary error on subscribe)
        if canonical == ESSENTIAL_CHANNEL:
            raise FatalIntegrityError(
                f"essential channel {canonical!r} rejected by venue "
                f"(code {code}, tried {tried!r}) — no book, no engine; msg={msg}")
        if canonical not in self.degraded:
            self.degraded.append(canonical)
        log.warning("WS channel %r unavailable (code %s) — degraded, continuing",
                    canonical, code)
        self._maybe_log_vocab()
        return None

    def _maybe_log_vocab(self):
        want = set(CHANNEL_FALLBACKS) - set(self.degraded)
        if not self._vocab_logged and want.issubset(self.accepted.keys() | set(self.degraded)):
            self._vocab_logged = True
            log.info("WS channels accepted: %s / degraded: %s",
                     sorted(self.accepted.values()), sorted(self.degraded))


CYCLE_SECONDS = 1.0            # F3: the cycle gate (frames update books; sweeps run at 1s)
SPOT_MAX_AGE_S = 30.0          # F2: BLIND bound — older spot serves as None
SPOT_POLL_S = 1.5              # F2: spot fetch cadence
DISCOVERY_SWEEP_S = 60.0       # F1c: rediscovery cadence (belt under lifecycle events)
PACK_HOURLY_S = 3600.0         # F7: pack timer


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
        self.surface.concurrent_provider = self._concurrent_lanes
        # F1/F2: exchange-truth market metadata + the spot tape
        self.market_meta = {}   # market -> {close_ts, boundary_lo, boundary_hi}
        self.spot = None
        self.spot_ts = 0.0
        self.spot_ticks = []    # rolling tape for FLIP/P features

    # ── F1: market lifecycle (discovery + rollover) ─────────────────────
    def on_market_discovered(self, ticker: str, market_obj: dict) -> None:
        from . import venue
        close_ts = venue.resolve_close_ts(market_obj, ticker)
        blo, bhi = venue.extract_boundaries(market_obj)
        self.market_meta[ticker] = {"close_ts": close_ts,
                                    "boundary_lo": blo, "boundary_hi": bhi}

    def on_market_closed(self, ticker: str) -> None:
        """F4: settled/closed markets are pruned everywhere, not swept forever."""
        self.close_window(ticker)
        self.market_meta.pop(ticker, None)
        self.feed.drop_book(ticker)
        self.flip.windows.pop(ticker, None)

    # ── F2: the spot tape ───────────────────────────────────────────────
    def record_spot(self, price, now) -> None:
        if price is None:
            return
        self.spot = price
        self.spot_ts = now
        self.spot_ticks.append(price)
        del self.spot_ticks[:-64]

    def fresh_spot(self, now, max_age=None):
        """BLIND behavior: a stale spot is None — lanes degrade safely."""
        limit = SPOT_MAX_AGE_S if max_age is None else max_age
        if self.spot is None or (now - self.spot_ts) > limit:
            return None
        return self.spot

    def _concurrent_lanes(self, market: str) -> str:
        """Scientist stamp: lanes with a position or resting order on this market."""
        live = set()
        for (ev, mkt, lane), net in self.gateway.positions.items():
            if mkt == market and net != 0:
                live.add(lane)
        for o in self.gateway.resting.values():
            if o.market == market:
                live.add(o.lane)
        return ",".join(sorted(live))

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
        # P5 §4 (CEO knob): a crash-loop tells the sleeping operator its story
        # in one tagged line, with the last FATAL attached.
        boots_last_hour = self.ledger.record_boot()
        if boots_last_hour > 10:
            last_fatal = self.ledger.get_state("last_fatal") or "(no FATAL recorded)"
            self.telegram.alert(
                f"BOOT_LOOP: {boots_last_hour} boots in the last hour — "
                f"last FATAL: {last_fatal}")
        return boots_last_hour

    def record_fatal(self, message: str) -> None:
        self.ledger.set_state("last_fatal", message[:500])

    def _on_ladder_move(self, old, new):
        if new == "WS_LOST":
            self.gateway.halt_entries("DEGRADE_LADDER")
            log.warning("ALL-lane entry halt (WS lost); custodian on REST; risk reduction allowed")
        elif new == "WS_LIVE":
            self.gateway.resume_entries("DEGRADE_LADDER")
            log.warning("entries resumed after clean-frame count")

    def _meta(self, market):
        return self.market_meta.get(market, {})

    def cycle(self, markets, now=None, spot=None):
        """One evaluation cycle: EVERY lane looks at EVERY market (C.4).
        ARBITRATION (P3 Broker part): custodian exits run FIRST, then lanes in
        registry order (F -> H8 -> FLIP -> D -> P); FLIP's takes lead its own
        proposal list. close_ts and boundaries come from exchange-truth
        market_meta when discovery has run (F1/F2); ticker inference is the
        fallback."""
        now = time.time() if now is None else now
        if spot is None:
            spot = self.fresh_spot(now)

        # 1) custodian exits outrank everything (risk reduction first)
        from .lanes import infer_close_ts_from_ticker
        cuts = self.custodian.tick(
            books={m: self.feed.book(m) for m in markets},
            close_ts_of=lambda m: (self._meta(m).get("close_ts")
                                   or infer_close_ts_from_ticker(m)),
            now=now, balance_usd=self.ledger.book_cents() / 100.0, spot=spot,
            boundaries={m: (self._meta(m).get("boundary_lo"),
                            self._meta(m).get("boundary_hi")) for m in markets})
        for market, lane, trigger in cuts:
            self.surface.write_row(lane, market,
                                   self._window_of.get(market, f"w-{market}"),
                                   "EXITED", detail=f"CUSTODIAN_CUT:{trigger}")

        for market in markets:
            window = self._window_of.setdefault(market, f"w-{market}")
            book = self.feed.book(market)
            meta = self._meta(market)
            transport = "WS" if self.ladder.entries_allowed() else "EXPLORATION"
            ctx = {
                "book": book, "now": now, "spot": spot,
                "spot_ticks": self.spot_ticks,
                "close_ts": meta.get("close_ts"),
                "boundary_lo": meta.get("boundary_lo"),
                "boundary_hi": meta.get("boundary_hi"),
                "cash_usd": self.ledger.book_cents() / 100.0,
                "entries_allowed": self.ladder.entries_allowed(),
            }
            # FLIP pair-grace housekeeping: drop the unfilled opposite entry
            stale_oid = self.flip.pair_grace_expired(market, now)
            if stale_oid is not None:
                self.gateway.cancel(stale_oid)

            for lane in self.lanes:
                decision = lane.evaluate(market, ctx)
                # D watchdog abandon: broken evidence -> custodian cut NOW
                if lane.name == "D" and decision.pass_reason == "EVIDENCE_BROKEN":
                    pos = self.custodian.positions.get(f"{market}:D")
                    if pos is not None:
                        mark = book.best_yes_bid() if pos.side == "yes" else book.best_no_bid()
                        if mark is not None:
                            self.custodian.execute_cut(pos, mark, book,
                                                       "EVIDENCE_BROKEN", crossfire=True)
                            lane.d.clear_broken()
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

    if config.RUN_MODE != "SHADOW" and not config.live_submit_enabled():
        raise FatalIntegrityError(
            f"RUN_MODE={config.RUN_MODE} without the I_UNDERSTAND_LIVE phrase — "
            f"go-live is a human act (env var + phrase), refusing to run")

    try:
        import websockets
    except ImportError as e:
        raise FatalIntegrityError(f"websockets required for the WS-first feed: {e}")

    from . import venue
    client = venue.build_client()

    if config.live_submit_enabled():
        # F6: LIVE boot reconcile — money truth before the first cycle
        from .reconcile import live_boot_reconcile
        live_boot_reconcile(engine, client)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    ws_path = urlparse(config.WS_URL).path or "/trade-api/ws/v2"
    rejections = auth.HandshakeRejections(limit=3)

    # ── F2: the spot task — its own cadence, staleness-bounded ─────────────
    async def spot_task():
        while not stop.is_set():
            price = await asyncio.to_thread(venue.get_btc_spot)
            engine.record_spot(price, time.time())
            await asyncio.sleep(SPOT_POLL_S)

    # ── F7: the pack timer — decoupled from market activity ────────────────
    async def pack_task():
        while not stop.is_set():
            await asyncio.sleep(PACK_HOURLY_S)
            print(daily_pack(engine.ledger, engine.surface, engine.cash,
                             foreign_fills=engine.fills.foreign_seen), flush=True)

    asyncio.create_task(spot_task())
    asyncio.create_task(pack_task())

    subscribed: set = set()

    async def sync_subscriptions(ws, subscriber):
        """F1a-c/P5 §1: signed REST discovery is the subscription's ground
        truth; each channel subscribes in its OWN cmd (a stranger name can
        only kill itself); closed windows prune everywhere."""
        mkts = await asyncio.to_thread(venue.list_open_markets, client)
        current = set()
        for m in mkts:
            ticker = m.get("ticker") or m.get("market_ticker", "")
            current.add(ticker)
            engine.on_market_discovered(ticker, m)
        new = sorted(current - subscribed)
        gone = sorted(subscribed - current)
        if new:
            for cmd in subscriber.cmds_for(new):
                payload = json.dumps(cmd)
                engine.feed.note_sent(payload)
                await ws.send(payload)
            subscribed.update(new)
            log.warning("WS subscribed: %s", ",".join(new))
        for ticker in gone:
            subscribed.discard(ticker)
            engine.on_market_closed(ticker)
            log.info("window closed + pruned: %s", ticker)
        return current

    while not stop.is_set():
        try:
            # Sign at connect time, never import time — fresh timestamp per attempt.
            async with websockets.connect(
                    config.WS_URL,
                    additional_headers=auth.signed_headers("GET", ws_path)) as ws:
                rejections.success()  # venue accepted the handshake
                subscribed.clear()
                subscriber = ChannelSubscriber()
                await sync_subscriptions(ws, subscriber)
                engine.ladder.snapshot_resynced()
                last_cycle = 0.0
                last_discovery = time.monotonic()
                while not stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
                    except asyncio.TimeoutError:
                        # F5: a quiet stretch is not death — ping; only a
                        # failed pong walks the ladder.
                        try:
                            pong = await ws.ping()
                            await asyncio.wait_for(pong, timeout=5.0)
                            continue
                        except Exception as pe:
                            raise ConnectionError(f"ping failed after quiet: {pe}")
                    # P5 §1: subscription replies route to the subscriber FIRST —
                    # a vocabulary rejection retries its fallback or degrades;
                    # only the essential channel's failure is fatal.
                    try:
                        pre = json.loads(raw)
                    except ValueError:
                        pre = None
                    if pre is not None and pre.get("id") in subscriber.pending:
                        retry = subscriber.on_reply(pre)
                        if retry is not None:
                            payload = json.dumps(retry)
                            engine.feed.note_sent(payload)
                            await ws.send(payload)
                        continue
                    engine.feed.handle_frame(raw)
                    if pre is not None:
                        m = pre.get("msg") or {}
                        mkt = m.get("market_ticker")
                        # F1c: lifecycle events drive rollover between sweeps
                        if pre.get("type") in ("market_lifecycle_v2", "market_lifecycle"):
                            await sync_subscriptions(ws, subscriber)
                        elif mkt and mkt not in subscribed:
                            subscribed.add(mkt)
                    mono = time.monotonic()
                    if mono - last_discovery >= DISCOVERY_SWEEP_S:
                        last_discovery = mono
                        await sync_subscriptions(ws, subscriber)
                    # F3: frames update books continuously; the five-lane sweep
                    # runs on the CYCLE_SECONDS gate.
                    if mono - last_cycle >= CYCLE_SECONDS:
                        last_cycle = mono
                        engine.cycle(sorted(engine.market_meta.keys() | subscribed))
        except FatalIntegrityError as fe:
            engine.record_fatal(str(fe))  # P5 §4: the BOOT_LOOP alert's evidence
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

    stop.set()
    print(daily_pack(engine.ledger, engine.surface, engine.cash,
                     foreign_fills=engine.fills.foreign_seen), flush=True)
    log.info("runner stopped; zero orders placed: %s",
             len(engine.gateway.shadow_orders) == 0)


if __name__ == "__main__":
    asyncio.run(run())
