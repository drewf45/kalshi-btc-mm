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
        self.sids: dict = {}       # canonical -> venue subscription id (P10 resync)
        self.resync_ids: set = set()  # cmd ids of in-flight resync cmds (tolerant path)

    def resync_cmds(self, market: str) -> list:
        """P10 §2.1: force a fresh snapshot for ONE market. With the essential
        channel's sid known, drop + re-add the market on that subscription (the
        venue re-sends the snapshot on add); otherwise fall back to a plain
        re-subscribe. Replies to these ids are TOLERANT (banked, never fatal —
        the boot-subscribe fatal path is not for resyncs)."""
        out = []
        sid = self.sids.get(ESSENTIAL_CHANNEL)
        if sid is not None:
            for action in ("delete", "add"):
                self.seq += 1
                self.resync_ids.add(self.seq)
                out.append({"id": self.seq, "cmd": "update_subscription",
                            "params": {"sids": [sid], "action": action,
                                       "market_tickers": [market]}})
        else:
            self.seq += 1
            self.resync_ids.add(self.seq)
            out.append(subscribe_cmd(
                self.seq, [market],
                self.accepted.get(ESSENTIAL_CHANNEL, ESSENTIAL_CHANNEL)))
        return out

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
            sid = (msg.get("msg") or {}).get("sid")
            if sid is not None:
                self.sids[canonical] = sid  # P10: the resync path's handle
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
            from . import failures
            failures.fail("WS_ESSENTIAL_CHANNEL_REJECTED",
                          f"essential channel {canonical!r} rejected by venue "
                          f"(code {code}, tried {tried!r}) — no book, no engine",
                          fatal=True, msg=msg)
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
        # P11.1-a: RestFeed IS a Feed (the FeedLike drop-in) — the WS path and
        # the REST path share one object; the runner never rewires.
        from .rest_feed import RestFeed
        self.feed = RestFeed(self.ladder, recorder=self.recorder)
        self.gateway = Gateway(self.ledger, self.surface)
        # P-CASH-FATAL-1: the cash protocol's stops reach the GATEWAY WALL —
        # pre-fix its entries_halted/fatal flags were read by no one.
        self.cash.gateway = self.gateway
        self.custodian = Custodian(self.gateway, self.ledger, self.surface, ladder=self.ladder)
        self.lanes = build_registry(self.ledger, gateway=self.gateway,
                                    custodian=self.custodian)
        self.fh8_shared = self.lanes[0].shared  # LaneF/LaneH8 shared evaluator
        self.flip = self.lanes[2].flip          # LaneFlip (arbitration slot 3)
        # P-FLIP-THESIS-1 §4: ONE shared inventory — F consults FLIP's held
        # state before buying a decided side (F_STANDS_DOWN). Atomic per
        # cycle: F evaluates first in registry order; fills book between
        # cycles, so the read is the cycle-start snapshot (Adversary b).
        self.fh8_shared.flip_inventory = self.flip.held
        from .fills import FillBooker
        self.fills = FillBooker(self.gateway, self.ledger, self.surface,
                                custodian=self.custodian,
                                alert_fn=self.telegram.alert,
                                on_booked=self._on_fill_booked)
        from .window_econ import WindowEcon
        self.econ = WindowEcon(self.ledger, self.gateway, self.surface, self.telegram)
        self.telegram.reset_halt_fn = self.econ.reset_halt  # Drew's key, entries only
        # P22 §5: /scoreboard — read-only; the one command the whitelist grows by
        from . import scoring as _scoring
        self.telegram.scoreboard_fn = (
            lambda: "\n".join(_scoring.scoreboard_lines(self.ledger)))
        # P22 §1.2: first-boot backfill — today's clips and round-trips are
        # not lost history (guarded + idempotent inside; never boot-fatal)
        try:
            n_backfilled = self.ledger.backfill_cell_outcomes()
            if n_backfilled:
                log.warning("CELL SCOREBOARD backfilled %d closed units of "
                            "risk from fills+settlements", n_backfilled)
        except Exception as e:
            log.error("cell backfill error (continuing): %s", e)
        self.boot_caps = None
        self._window_of = {}  # market -> window_id (market close ts as string)
        self.surface.concurrent_provider = self._concurrent_lanes
        # F1/F2: exchange-truth market metadata + the spot tape
        self.market_meta = {}   # market -> {close_ts, boundary_lo, boundary_hi}
        self.spot = None
        self.spot_ts = 0.0
        self.spot_ticks = []    # rolling tape for FLIP/P features
        # P9 §1b/c: supervised-task bookkeeping (the hourly line reads these)
        self.task_restarts = {}  # name -> restarts
        self.task_alive = {}     # name -> bool (False while in backoff)
        # P9 §2: live account-value retry state (3 attempts spaced >= 5s = the
        # retry-x3-over-15s law, paced across cycles so the loop never blocks)
        self._av_fail_streak = 0
        self._av_last_attempt_ts = 0.0
        # P26 §1.2: UNPROVEN tagged once per (market, window) while unloaded
        self._unproven_tagged = set()
        # B4: SIZE_ZERO_BY_KELLY logged once per (market, price); rollover
        # cleanup in on_market_closed
        self._size_zero_logged = set()
        # P10 §2/§3: book-truth machinery
        self.quarantined = {}       # market -> until_close_ts (A5 poison ceiling)
        self.poison_episodes = {}   # market -> episodes this window
        self.book_checks_total = 0  # §3.4: the `book✓ n` heartbeat
        self._divergence_pending = {}  # market -> True after one diverged check
        self._wall_log_counts = {}     # §4.3: (lane, market, wall) -> count
        # WO-HALT-ORPHAN §2B: a lane/side/price that failed the BUDGET wall
        # this window is not re-proposed — the wall decision can't change
        # within a window (book is byte-identical until a confirmed
        # movement), so 21 retries were pure spam. Keys (market, lane,
        # side, price); cleared at rollover.
        self._budget_rejected = set()
        self.feed.on_poison = self.note_poison_episode
        self.gateway.alert_fn = self.telegram.alert  # WALL_STORM page path
        # P13 §3: orientation sentinels + §4 sit-out page wiring
        self.divergence_watches = {}   # market -> {until, strikes}
        self._orientation_checked = False
        # WO-HALT-ORPHAN §1.3: the market that tripped ORIENTATION_DIVERGENCE
        # — re-checked with a FRESH record each cycle; a passing recheck
        # auto-resumes (a transient staleness halt self-heals, no key).
        self._orientation_halt_market = None
        self.windows_seen = 0          # P17 §6.1: the show-up counter
        # P18: the displacement-event anchor per market — ONE hunt per event;
        # re-anchored on each hunt submit (a new hunt needs a NEW needle).
        self.hunt_anchor = {}
        # P19 §2: the custodian earns F/H8 — salvage armed at wiring
        from .custodian import salvage_params
        self.custodian.set_lane_params("F", salvage_params())
        self.custodian.set_lane_params("H8", salvage_params())
        self.fills.anchor_fn = self._salvage_anchor
        self.flip.alert_fn = self.telegram.alert
        # P14 §2.1: the cut boundary's on-demand fills sweep
        self.custodian.resweep = self._resweep_market

    # ── F1: market lifecycle (discovery + rollover) ─────────────────────
    def on_market_discovered(self, ticker: str, market_obj: dict) -> None:
        from . import venue
        if ticker not in self.market_meta:
            self.windows_seen += 1   # P17 §6.1: showing up, counted
        close_ts = venue.resolve_close_ts(market_obj, ticker)
        blo, bhi = venue.extract_boundaries(market_obj)
        # P13 §3: the market record's own touches (explicit form: *_dollars
        # ×100) — the free orientation oracle the venue publishes.
        def _rec_cents(key):
            v = market_obj.get(key)
            if v is None:
                return None
            try:
                from decimal import Decimal
                return int(Decimal(str(v)) * 100)
            except Exception:
                return None
        self.market_meta[ticker] = {"close_ts": close_ts,
                                    "boundary_lo": blo, "boundary_hi": bhi,
                                    "rec_yes_bid": _rec_cents("yes_bid_dollars"),
                                    "rec_yes_ask": _rec_cents("yes_ask_dollars"),
                                    # ORIENT-1 §1.4: the record's age is part
                                    # of every orientation verdict — a 60s-
                                    # stale record forged today's mirror.
                                    "rec_captured_ts": time.time()}

    def on_market_closed(self, ticker: str) -> None:
        """F4: settled/closed markets are pruned everywhere, not swept forever."""
        self.close_window(ticker)
        self.market_meta.pop(ticker, None)
        self.feed.drop_book(ticker)
        self.flip.windows.pop(ticker, None)
        # P10 §2.4: quarantine + episode counts clear at rollover
        self.quarantined.pop(ticker, None)
        self.poison_episodes.pop(ticker, None)
        self._divergence_pending.pop(ticker, None)
        self._size_zero_logged = {k for k in self._size_zero_logged
                                  if k[0] != ticker}
        self._budget_rejected = {k for k in self._budget_rejected
                                 if k[0] != ticker}   # §2B: per-window reset
        self.feed.resync_needed.discard(ticker)
        # P15 Fix A: settled window — gross exposure for the market is over
        for key in [k for k in self.gateway.gross_open if k[1] == ticker]:
            del self.gateway.gross_open[key]
        self.hunt_anchor.pop(ticker, None)   # P18: anchors die with the window
        # P19 §3.2: per-market lane state prunes at rollover — hours-safe
        # today, weeks-safe after (memory is a slow leak's favorite door).
        self.lanes[4].p.states.pop(ticker, None)
        self.fh8_shared.ladders.pop(ticker, None)
        self.fh8_shared._cache.pop(ticker, None)

    # ── P10 §2: poison episodes, quarantine, REST marks ─────────────────
    def note_poison_episode(self, market: str, yb, nb) -> None:
        """§2.2: one page per episode. §2.4 (A5): 3 episodes in one window →
        QUARANTINE — entries off for ALL lanes, custody continues on REST
        marks, exactly one page naming the self-heal time."""
        n = self.poison_episodes[market] = self.poison_episodes.get(market, 0) + 1
        self.telegram.alert(f"⚠ BOOK_INCOHERENT {market} y{yb}+n{nb} — resyncing")
        if n >= 3 and market not in self.quarantined:
            from . import failures
            from .lanes import infer_close_ts_from_ticker
            close_ts = (self._meta(market).get("close_ts")
                        or infer_close_ts_from_ticker(market)
                        or (time.time() + 900))
            self.quarantined[market] = close_ts
            when = time.strftime("%H:%M", time.gmtime(close_ts)) + " UTC"
            self.telegram.alert(
                f"⛔ QUARANTINE {market} until {when} — 3x incoherent")
            failures.fail("QUARANTINE",
                          f"{market} quarantined until {when} — "
                          f"{n} poison episodes in one window (A5 ceiling)",
                          market=market, episodes=n, alert=False)

    def _rest_book(self, market: str):
        """§2.3 (A6): REST truth for custodian marks when the WS book is
        poisoned — a corrupted feed may pause NEW risk; it may never pause
        the management of EXISTING risk."""
        from . import venue
        from .book import OrderBook
        try:
            if self.gateway.venue_client is None:
                self.gateway.venue_client = venue.build_client()
            rb = venue.fetch_orderbook(self.gateway.venue_client, market)
        except Exception as e:
            log.warning("REST book fetch failed for %s: %s", market, e)
            return None
        if rb.yes_bid is None and rb.no_bid is None:
            return None
        ob = OrderBook(market=market, transport="REST")
        if rb.yes_bid is not None:
            ob.yes_bids[rb.yes_bid] = rb.yes_bid_qty or 1
        if rb.no_bid is not None:
            ob.no_bids[rb.no_bid] = rb.no_bid_qty or 1
        ob.has_snapshot = True
        ob.last_update_ts = time.time()
        return ob

    # ── P10 §3: the venue REST book is the standing arbiter ─────────────
    def book_check(self, now=None) -> int:
        """Every 60s per subscribed market: REST touches vs the WS book.
        §3.2 (A2): >2c divergence → BOOK_DIVERGENCE row EVERY trip (R5) +
        silent resync on the FIRST; the PHONE only on the second consecutive
        trip (a REST snapshot races a fast WS book — honest mid-move trips
        resync quietly)."""
        from . import failures, venue
        now = time.time() if now is None else now
        try:
            if self.gateway.venue_client is None:
                self.gateway.venue_client = venue.build_client()
        except Exception as e:
            log.warning("book_check: no venue client (%s)", e)
            return 0
        clean = 0

        def side_div(ws_b, r_b):
            if r_b is None:
                return 0
            return 100 if ws_b is None else abs(ws_b - r_b)

        for market, ws in sorted(self.feed.books.items()):
            if not ws.has_snapshot:
                continue
            try:
                rest = venue.fetch_orderbook(self.gateway.venue_client, market)
            except Exception:
                continue
            if rest.yes_bid is None and rest.no_bid is None:
                continue  # REST unreadable — no verdict on the WS book
            div = max(side_div(ws.best_yes_bid(), rest.yes_bid),
                      side_div(ws.best_no_bid(), rest.no_bid))
            if div > 2:
                failures.fail(
                    "BOOK_DIVERGENCE",
                    f"{market}: ws y{ws.best_yes_bid()}/n{ws.best_no_bid()} vs "
                    f"rest y{rest.yes_bid}/n{rest.no_bid} (max {div}c)",
                    market=market, divergence=div, alert=False)
                self.feed.resync_needed.add(market)
                if self._divergence_pending.get(market):
                    self.telegram.alert(
                        f"⚠ BOOK_DIVERGENCE {market} persists across two checks: "
                        f"ws y{ws.best_yes_bid()}/n{ws.best_no_bid()} vs "
                        f"rest y{rest.yes_bid}/n{rest.no_bid}")
                self._divergence_pending[market] = True
            else:
                self._divergence_pending.pop(market, None)
                clean += 1
        self.book_checks_total += clean
        return clean

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

    def _score_and_size(self, proposal, book) -> None:
        """P27 §1 — SIZING = FULL KELLY: contracts = min(kelly, depth); the
        Wilson ladder still scores every cell (tier_for — the REPORTING
        stamp, its pages, and custody scaling read it) but it no longer
        votes on size. A zero from sizing keeps count=1 and lets the walls
        refuse BY NAME (the walls are the refusal organ)."""
        from . import scoring
        from .sizing import size_order
        lane = scoring.cell_lane(proposal.lane, proposal.why)
        tier = scoring.tier_for(self.ledger, lane, proposal.price_cents,
                                alert_fn=self.telegram.alert)
        depth = book.visible_depth(proposal.side, proposal.price_cents) or 0
        dec = size_order(self.ledger.book_cents(),
                         proposal.price_cents, depth)
        proposal.size_tier = tier   # reporting + custody scaling, never a cap
        proposal.count = max(1, dec.contracts)
        # WO-SWING-GATE-EVENT §4.2 (DREW-RULED 2026-07-20): FLIP's swing
        # gate measured the wrong event and rubber-stamped falling knives —
        # cap FLIP entries at 1 lot until the gate tracks measured
        # took_swing. F is untouched (its survival gate is correct). This
        # bounds the 3-lot bleed while Instrument 1 keeps calibrating.
        if proposal.lane == "FLIP":
            proposal.count = min(proposal.count, config.FLIP_SIZE_CAP)
        # WO-VERIFY-LOSSTERM-1 B4 (pure logging): when Kelly is the term
        # that zeroed a favorite, say so BY NAME once per (market, price) —
        # "0 @98c" must be self-explaining arithmetic, never a mystery bug.
        if dec.contracts == 0:
            book_c = self.ledger.book_cents()
            kelly_budget = int(book_c * config.KELLY_FRACTION_CEILING)
            if kelly_budget // max(1, proposal.price_cents) == 0:
                key = (proposal.market, proposal.price_cents)
                if key not in self._size_zero_logged:
                    self._size_zero_logged.add(key)
                    log.info(
                        "SIZE_ZERO_BY_KELLY %s price=%dc book=%dc "
                        "kelly_budget=%dc — throttle is book size, not a "
                        "wall (count=1 proposed; the walls refuse by name)",
                        proposal.market, proposal.price_cents, book_c,
                        kelly_budget)

    def _on_fill_booked(self, order, action, price_cents, count, now,
                        fee_cents=0):
        """P13 §1: fill pages say WHAT and WHY. A scratch-sell must never
        read like a second buy — on live money, mute narration is
        indistinguishable from inversion."""
        if action == "ENTRY":
            why = f" — why: {order.why}" if order.why else ""
            self.telegram.alert(
                f"✅ ENTRY {order.lane} {order.market} "
                f"{order.action} {order.side}@{price_cents}¢ x{count}{why}")
            # §3 (P12 §4): 30s post-entry divergence watch, armed per entry
            self.divergence_watches[order.market] = {
                "until": now + 30.0, "strikes": 0}
        else:
            reason = order.reason or ("custodian cut"
                                      if action == "CUSTODIAN_EXIT" else "exit")
            fee_s = f" (fee {fee_cents}¢)" if fee_cents else ""
            self.telegram.alert(
                f"✂️ EXIT {order.lane} {order.market} "
                f"{order.action} {order.side}@{price_cents}¢ x{count}{fee_s}"
                f" — {reason}")
            # the desk's unit of thought: one glance, one verdict per pair
            row = self.ledger.db.execute(
                "SELECT price_cents FROM fills WHERE market=? AND lane=?"
                " AND action='ENTRY' ORDER BY id DESC LIMIT 1",
                (order.market, order.lane)).fetchone()
            if row is not None:
                rt = (price_cents - row[0]) * count
                net = rt - fee_cents

                def s(v):
                    return f"{'+' if v > 0 else ''}{v}"
                self.telegram.alert(
                    f"↔ {order.market} {order.lane} round-trip {s(rt)}¢ "
                    f"+ fee {fee_cents}¢ = {s(net)}¢")
        if order.lane == "FLIP":
            # FLIP-COUNT-1: the fill's COUNT rides into custody — a second
            # same-side fill merges, an exit realizes ×count and decrements.
            if action == "ENTRY":
                self.flip.note_fill(order.market, order.side, price_cents,
                                    now, count=count)
            else:
                self.flip.note_exit(order.market, order.side, price_cents,
                                    now, count=count)
        elif order.lane == "D":
            laned = self.lanes[3].d
            if action == "ENTRY":
                laned.budget.convert(order.market)  # reservation -> at_risk
            else:
                laned.budget.release(order.market, f"{action} filled @{price_cents}c")
                laned.seeded.pop(order.market, None)
                self.lanes[3].exit_posted.discard(order.market)

    def page_once(self, key: str, msg: str) -> bool:
        """P26 §3.5: a deploy-scoped page — sent once per ledger lifetime
        (restarts re-run boot; the phone should not hear it twice)."""
        if self.ledger.get_state(key):
            return False
        self.ledger.set_state(key, "1")
        self.telegram.alert(msg)
        return True

    def _table_build_page(self, msg: str) -> None:
        """§1.1: the builder's verdict, tagged for the phone."""
        bad = "FAILED" in msg or "ERROR" in msg
        tag = "⛔ TABLE: FAILED — proven lanes mute" if bad else "🧠 TABLE"
        self.telegram.alert(f"{tag}\n{msg[:600]}")

    def boot(self, auth_line=None):
        # P-CASH-FATAL-1 §4.3 (Engineer: this ORDER is load-bearing): the
        # operator's durable cash stops restore BEFORE any baseline — the
        # deny-reboot breach was exactly an amnesiac re-baseline running
        # ahead of a stop nobody had persisted.
        self.cash.restore_on_boot()
        if (config.RUN_MODE == "SHADOW" and self.ledger.book_cents() == 0
                and not self.cash.fatal):
            # Paper bankroll so budget walls exercise realistically (paper only).
            self.ledger.baseline(int(config.SHADOW_PAPER_BANKROLL_USD * 100),
                                 confirmed_by="shadow_paper_boot")
        self.boot_caps = self.ledger.snapshot_caps_at_boot()
        print_boot_tape(recorder=self.recorder, boot_caps=self.boot_caps,
                        auth_line=auth_line)
        # P5 §4 (CEO knob): a crash-loop tells the sleeping operator its story
        # in one tagged line, with the last FATAL attached.
        boots_last_hour = self.ledger.record_boot()
        # R5: the failure funnel gets its ledger, pager, and boot id here
        boot_id = int(self.ledger.db.execute("SELECT COUNT(*) FROM boots").fetchone()[0])
        from . import failures
        failures.configure(self.ledger, alert_fn=self.telegram.alert,
                           run_mode=config.RUN_MODE, boot_id=boot_id)
        if boots_last_hour > 10:
            last_fatal = self.ledger.get_state("last_fatal") or "(no FATAL recorded)"
            self.telegram.alert(
                f"BOOT_LOOP: {boots_last_hour} boots in the last hour — "
                f"last FATAL: {last_fatal}")
        self.boot_id = boot_id
        # P8 §2.3: restarts and redeploys do NOT clear the two-strike halt
        self.econ.restore_halt_on_boot()
        # P17 §1.3/§1.4: heal across restarts — reload open brackets (so the
        # settle sweep retries stuck windows) and mark evidence holes.
        self.econ.restore_open_brackets_on_boot()
        # P-CASH-FATAL-1 §4.5 (defense in depth): every durable stop the DB
        # knows must be honored on the wall before the first cycle — or
        # FATAL loud rather than trade.
        print(self.audit_durable_stops(), flush=True)
        # WO-VERIFY-LOSSTERM-1 B1: the salvage registration path must be
        # provably reachable before the first cycle — ARMED or
        # DISABLED_TAGGED, never silence on a held position.
        print(self.salvage_selftest(), flush=True)
        self.gap_restart_scan()
        # P19 §2.5: the promotion — P26 §3.5 fold: paged ONCE per DEPLOY
        # (ledger-keyed dedup; the 6:22 restart double-page was cosmetic,
        # the ledger held).
        self.page_once(
            "page_salvage_promoted",
            f"👑 custodian promoted: Lane F salvage armed "
            f"(K={config.SALVAGE_K_POINTS:.0f} S={config.SALVAGE_S_CENTS:.0f} "
            f"R={config.SALVAGE_R_S:.0f} floor={config.SALVAGE_T_FLOOR_S:.0f}s)")
        # P26 §1 — ONE BRAIN, LOADED OR EXPLAINED: boot provisions the delta
        # table. Load from disk (manifest+SHA gated); else BUILD via
        # delta_builder's own A1-A5-gated path and hot-load on PASS. The
        # boot page carries the verdict either way; while unloaded the
        # proven lanes stay evidence-born mute but the SILENCE IS EXPLAINED
        # (hourly `brain:` field + UNPROVEN tags).
        from . import delta
        delta.set_alert_fn(self.telegram.alert)
        if delta.load():
            st = delta.table_status()
            self.telegram.alert(
                f"🧠 TABLE: loaded · cells={len(delta._TABLE)} · "
                f"{st.get('detail', '')}")
        elif config.TABLE_AUTOBUILD:
            from . import delta_builder
            self.telegram.alert(
                f"🧠 TABLE: building (~10 min; 180d Coinbase pull) — "
                f"disk load refused: {delta.refusal_reason() or 'absent'}. "
                f"Proven lanes mute until PASS, and saying so.")
            delta_builder.start_background_build(
                notify_fn=self._table_build_page)
        else:
            self.telegram.alert(
                f"⛔ TABLE: absent and autobuild disabled "
                f"({delta.refusal_reason() or 'no table on disk'}) — proven "
                f"lanes mute; the brain line will say so hourly.")
        # Tape 0718: the deployed worker booted with DB=relay_shadow.db (no
        # RELAY_DB_PATH) — an EPHEMERAL database. Halt persistence, booking
        # dedup, and every autopsy depend on the disk surviving a redeploy.
        # P16 §1: the fallback chain means only a truly EPHEMERAL resolution
        # (no RELAY_DB_PATH, no legacy K_WORKER_DB dir to derive) warns.
        if config.live_submit_enabled() and config.DB_PATH_SOURCE == "ephemeral":
            self.telegram.alert(
                "⚠ RELAY_DB_PATH unset in LIVE — the DB is EPHEMERAL: the "
                "two-strike halt, fill dedup, and autopsy evidence will NOT "
                "survive a redeploy. Set RELAY_DB_PATH to a persistent disk "
                "path (e.g. /var/data/relay_live.db).")
        return boots_last_hour

    AV_RETRY_SPACING_S = 5.0   # P9 §2: 3 attempts >= 5s apart = retry x3 over 15s
    AV_PAGE_AT_STREAK = 3

    def account_value(self, now=None):
        """P9 §2: account truth, split by mode — NO PAPER NUMBERS IN LIVE, EVER.

        SHADOW: (paper book, "paper") — papers the money, never the market.
        LIVE:   (venue cash+pv, "venue") on a successful read; (None, "venue")
        while the read fails — the caller DEFERS. A failed live read NEVER
        substitutes the ledger book; it banks ACCOUNT_VALUE_UNREADABLE per
        attempt and pages at attempt 3."""
        if not config.live_submit_enabled():
            return self.ledger.book_cents(), "paper"
        now = time.time() if now is None else now
        if (self._av_fail_streak > 0
                and now - self._av_last_attempt_ts < self.AV_RETRY_SPACING_S):
            return None, "venue"   # between retries — still deferring
        self._av_last_attempt_ts = now
        from . import failures, venue
        cash = pv = None
        try:
            if self.gateway.venue_client is None:
                self.gateway.venue_client = venue.build_client()
            cash, pv = venue.get_balance(self.gateway.venue_client)
        except Exception as e:
            log.warning("live account value read raised: %s", e)
        if cash is not None:
            self._av_fail_streak = 0
            return int(round((cash + (pv or 0.0)) * 100)), "venue"
        self._av_fail_streak += 1
        failures.fail("ACCOUNT_VALUE_UNREADABLE",
                      f"live account value read failed "
                      f"(attempt {self._av_fail_streak}/{self.AV_PAGE_AT_STREAK})",
                      attempt=self._av_fail_streak)
        if self._av_fail_streak == self.AV_PAGE_AT_STREAK:
            self.telegram.alert(
                "💸 ACCOUNT VALUE UNREADABLE x3 over 15s — brackets DEFER until "
                "the venue answers; no paper number will substitute")
        return None, "venue"

    def flush_deferred_econ(self, now=None) -> int:
        """P9 §2: deferred brackets complete on the next successful read."""
        if not (self.econ.pending_opens or self.econ.pending_closes):
            return 0
        val, src = self.account_value(now)
        if val is None:
            return 0
        return self.econ.flush_deferred(val, src, now=now)

    def audit_durable_stops(self) -> str:
        """P-CASH-FATAL-1 §4.5: ONE boot assertion that every durable stop
        persisted in the DB is loaded AND honored on the gateway wall
        before the first cycle. A stop the DB knows and the wall doesn't
        is the deny-reboot breach shape — FATAL loud rather than trade.
        Covers the whole class (two-strike, cash-fatal, pending prompt),
        not just the stop that failed this time."""
        from . import failures
        from .ledger import (CASH_FATAL_KEY, CASH_FATAL_REASON,
                             CASH_PENDING_KEY, CASH_PROMPT_REASON)
        from .window_econ import HALT_KEY, HALT_REASON
        wall = self.gateway.entries_halted_reasons
        stops = []
        if self.ledger.get_state(HALT_KEY) == "1":
            stops.append(("two-strike", HALT_REASON in wall))
        if self.ledger.get_state(CASH_FATAL_KEY) is not None:
            stops.append(("cash-fatal",
                          self.cash.fatal and CASH_FATAL_REASON in wall))
        if self.ledger.get_state(CASH_PENDING_KEY) is not None:
            stops.append(("cash-pending", self.cash.pending is not None
                          and CASH_PROMPT_REASON in wall))
        for name, honored in stops:
            if not honored:
                failures.fail(
                    "STOP_AUDIT_FAILED",
                    f"durable stop '{name}' present in DB but NOT honored "
                    "on the gateway wall — refusing to trade",
                    fatal=True, stop=name, wall=sorted(wall))
        if stops:
            return ("STOP AUDIT: "
                    + " · ".join(f"{n}=HONORED" for n, _ in stops)
                    + " — restored stops hold before the first cycle")
        return "STOP AUDIT: no durable stops in DB — clean boot"

    def salvage_selftest(self) -> str:
        """WO-VERIFY-LOSSTERM-1 B1: prove at boot that adoption REGISTERS
        the loss-term — a held position lands SALVAGE_ARMED (with its
        anchor values) or SALVAGE_DISABLED_TAGGED (with its reason), never
        silence — and that the catastrophic backstop stays reachable with
        NO anchor. Runs against a throwaway in-memory ledger; the live
        surface sees nothing. An unreachable path is FATAL loud at boot:
        silence on a dying position is recklessness by omission (the
        doctrine's loss-term must always be computed)."""
        from . import failures
        from .custodian import Custodian, OpenPosition, salvage_params
        from .gateway import Gateway
        from .ledger import Ledger
        from .surface import Surface
        led = Ledger(":memory:")
        surf = Surface(led)
        cust = Custodian(Gateway(led, surf), led, surf, ladder=DegradeLadder())
        cust.set_lane_params("F", salvage_params())
        mkt = "SELFTEST-B1"
        cust.adopt(OpenPosition(
            event="SELFTEST", market=mkt, lane="F", side="yes", count=1,
            entry_price_cents=95, entry_p_win=0.95,
            size_tier=config.TIER_PROBE, entry_time=0.0,
            d_entry=200.0, t_entry=500.0, p_entry=0.93))
        armed = led.db.execute(
            "SELECT detail FROM surface_rows WHERE state='SALVAGE_ARMED'"
            " AND market=?", (mkt,)).fetchone()
        blind = OpenPosition(
            event="SELFTEST", market=mkt + "-D", lane="F", side="yes",
            count=1, entry_price_cents=95, entry_p_win=0.95,
            size_tier=config.TIER_PROBE, entry_time=0.0, p_entry=None)
        cust.adopt(blind, disabled_reason="table")
        tagged = led.db.execute(
            "SELECT detail FROM surface_rows WHERE"
            " state='SALVAGE_DISABLED_TAGGED' AND market=?",
            (mkt + "-D",)).fetchone()
        backstop = cust.should_cut(
            blind, now=1.0, secs_remaining=400, p_win=0.04, exit_bid_cents=4,
            spot=None, boundary_lo=None, boundary_hi=None, balance_usd=100.0)
        ok_armed = armed is not None and "p_entry" in armed[0]
        ok_tagged = tagged is not None and "table" in tagged[0]
        ok_backstop = backstop == "CATASTROPHIC"
        if not (ok_armed and ok_tagged and ok_backstop):
            failures.fail(
                "SALVAGE_SELFTEST_FAILED",
                f"salvage registration unreachable: armed={ok_armed} "
                f"tagged={ok_tagged} backstop={ok_backstop} — the loss-term "
                "would go silent on a held position; refusing to run",
                fatal=True, armed=ok_armed, tagged=ok_tagged,
                backstop=ok_backstop)
        return ("SALVAGE SELF-TEST: ARMED fires · DISABLED_TAGGED names its "
                "reason · catastrophic backstop reads no anchor — loss-term "
                "wired (B1)")

    def standing_reconcile(self, now=None) -> str:
        """P9 §3: every 60s in live — venue truth vs ledger expectation, routed
        to the EXISTING cash protocol (quiescence window, halt + breakdown page,
        /confirm_cash | /deny_cash). Drift pages between brackets, not at them."""
        val, src = self.account_value(now)
        if val is None or src != "venue":
            return "UNREADABLE"
        # WO-INFRA-HARDENING E1 — the reconcile-side source trail: when the
        # book disagrees with the venue and there is NOTHING pending to explain
        # it (no unsettled fills, no resting orders), that is the phantom
        # signature. Record it durably (alert=False — the cash protocol below
        # still owns the page/halt) so the divergence is timestamped against
        # the SETTLE_AUDIT trail instead of eyeballed later.
        unsettled = self.ledger.unsettled_fill_count()
        resting = len(self.gateway.resting)
        book = self.ledger.book_cents()
        if (unsettled == 0 and resting == 0
                and abs(book - val) > config.RECON_AUDIT_FLOOR_CENTS):
            from . import failures
            failures.fail(
                "RECON_BOOK_VENUE_DELTA",
                f"book {book}c vs venue {val}c delta {book - val:+d}c with 0 "
                "unsettled / 0 resting — an unexplained divergence; the E1 "
                "SETTLE_AUDIT trail names which settlement moved the book",
                alert=False, book_cents=book, venue_cents=val,
                delta_cents=book - val)
        return self.cash.reconcile(
            venue_balance_cents=val,
            in_flight_orders=resting,
            unsettled_fills=unsettled,
            now=now)

    def listener_status(self) -> str:
        """P9 §1c: the hourly line's `listener:` field."""
        n = self.task_restarts.get("listener", 0)
        if self.task_alive.get("listener", True):
            return "ok" if n == 0 else f"ok({n} restarts)"
        return f"down({n} restarts)"

    def _salvage_anchor(self, market: str, side: str):
        """P19 §2.1: (d_entry, t_entry, p_entry) at custody registration —
        the delta table via spotlead's semantics, at the entry instant.
        P24 §1.2: a miss returns its CAUSE as a string (spot | strike |
        close | table) — four distinct organs, no longer indistinguishable;
        the 1715 class gets a named cause instead of a shrug."""
        from . import delta, spotlead as _sl
        spot = self.fresh_spot(time.time())
        if spot is None:
            return "spot"      # spot stale/absent
        meta = self._meta(market)
        strike = _sl.pick_strike(spot, meta.get("boundary_lo"),
                                 meta.get("boundary_hi"))
        if strike is None:
            return "strike"    # no boundary metadata
        close_ts = meta.get("close_ts")
        if close_ts is None:
            from .lanes import infer_close_ts_from_ticker
            close_ts = infer_close_ts_from_ticker(market)
        if close_ts is None:
            return "close"     # no close timestamp anywhere
        t_rem = close_ts - time.time()
        if t_rem <= 0:
            return "close"     # window already over by the clock
        d = abs(spot - strike)
        ps = delta.p_survive(d, t_rem)
        if ps is None:
            return "table"     # delta-table cell miss
        on_side = "yes" if spot >= strike else "no"
        p_entry = ps if on_side == side else 1.0 - ps
        return (d, t_rem, p_entry)

    def _resweep_market(self, market: str) -> None:
        """P14 §2.1: the 3s fills cadence's on-demand version, at the cut
        boundary — so the cut count is derived from fills booked THIS instant.
        Failure degrades to the current ledger (the cut still re-derives)."""
        if not config.live_submit_enabled() or self.gateway.venue_client is None:
            return
        from . import venue
        try:
            self.fills.sweep(venue.get_fills(self.gateway.venue_client, market))
        except Exception as e:
            log.warning("on-demand fills sweep failed for %s "
                        "(cut proceeds on current ledger): %s", market, e)

    # ── P13 §3: engine-side orientation sentinels (keyless) ─────────────
    @staticmethod
    def _mirror_signature(ours: int, rec: int) -> bool:
        """The inversion signature: no direct match, tight mirror match."""
        return abs(ours - rec) > 10 and abs(ours - (100 - rec)) <= 3

    def _fresh_record_touches(self, market: str):
        """ORIENT-1 §1.1-1.3: the SECOND witness — a fresh market record via
        venue.get_market (the same call the divergence watch uses). One
        retry; two failures -> None (the caller FATALs UNVERIFIABLE:
        cannot prove innocence -> refuse, fail-loud unchanged). A success
        also refreshes market_meta's record + captured_ts."""
        from decimal import Decimal

        from . import venue
        for attempt in (1, 2):
            try:
                client = self.gateway.venue_client
                if client is None:
                    client = self.gateway.venue_client = venue.build_client()
                rec_obj = venue.get_market(client, market)

                def cents(key):
                    v = rec_obj.get(key)
                    if v is None:
                        return None
                    return int(Decimal(str(v)) * 100)

                fbid, fask = cents("yes_bid_dollars"), cents("yes_ask_dollars")
                meta = self.market_meta.get(market)
                if meta is not None:
                    meta.update(rec_yes_bid=fbid, rec_yes_ask=fask,
                                rec_captured_ts=time.time())
                return fbid, fask
            except Exception as e:
                log.warning("fresh orientation record pull %d/2 failed for "
                            "%s: %s", attempt, market, e)
        return None

    def orientation_selftest(self, market: str, book) -> None:
        """Boot self-test, once per boot: our book vs the venue market
        record's own touches. ORIENT-1 — TWO-WITNESS: the discovery record
        can be up to DISCOVERY_SWEEP_S (60s) stale, so a symmetric cross of
        50¢ inside the gap FORGES a mirror (tape 12:22:59Z: FATAL rec=37/
        ours=66; 7s later the fresh record read 66 — staleness, not
        inversion). On a mirror signature we do NOT fail: we pull a FRESH
        record and FATAL only if the signature holds on the fresh record on
        BOTH touches (bid AND ask; bid alone governs when the record lacks
        an ask — stated in the line). An unpullable fresh record is
        ORIENTATION_UNVERIFIABLE — still FATAL, still loud."""
        if self._orientation_checked:
            return
        meta = self._meta(market)
        rec = meta.get("rec_yes_bid")
        yb = book.best_yes_bid()
        if rec is None or yb is None:
            return
        self._orientation_checked = True
        from . import failures
        age = time.time() - meta.get("rec_captured_ts", time.time())
        if not self._mirror_signature(yb, rec):
            log.info("ORIENTATION SELF-TEST OK: %s ours y%d vs record y%d "
                     "(record age %.0fs)", market, yb, rec, age)
            return
        # WITNESS 2: the stale record accuses; only a fresh record convicts.
        fresh = self._fresh_record_touches(market)
        if fresh is None:
            failures.fail(
                "ORIENTATION_UNVERIFIABLE",
                f"{market}: mirror signature (ours {yb}¢ vs record {rec}¢, "
                f"record age {age:.0f}s) and the fresh record pull failed "
                f"twice — cannot prove innocence, refusing to trade",
                fatal=True, market=market, ours=yb, record=rec,
                record_age_s=round(age, 1))
            return
        fbid, fask = fresh
        if fbid is None:
            failures.fail(
                "ORIENTATION_UNVERIFIABLE",
                f"{market}: mirror signature and the fresh record carries "
                f"no yes_bid — cannot prove innocence, refusing to trade",
                fatal=True, market=market, ours=yb, record=rec)
            return
        bid_mirror = self._mirror_signature(yb, fbid)
        if fask is not None:
            mirrored = bid_mirror and self._mirror_signature(yb, fask)
            mode = "BOTH touches (bid+ask)"
        else:
            mirrored = bid_mirror
            mode = "single-touch fallback (fresh record lacks an ask)"
        if mirrored:
            failures.fail(
                "ORIENTATION_MIRROR",
                f"{market}: our yes_bid {yb}¢ mirrored by the FRESH record "
                f"(bid {fbid}¢, ask {fask}¢) on {mode} — book orientation "
                f"INVERTED, refusing to trade (stale record was {rec}¢, "
                f"age {age:.0f}s)",
                fatal=True, market=market, ours=yb, fresh_bid=fbid,
                fresh_ask=fask, stale_record=rec)
            return
        log.warning("ORIENTATION SELF-TEST OK: %s — stale-record mirror "
                    "CLEARED by fresh record (stale y%d age %.0fs → fresh "
                    "y%s/ask %s; ours y%d; %s)", market, rec, age,
                    fbid, fask, yb, mode)

    def process_divergence_watches(self, client, now=None) -> None:
        """§3: 30s post-entry watch — ours vs the venue market record >3¢
        for 3 consecutive checks → entries halt + page.

        WO-HALT-ORPHAN §2C: a strike counts ONLY against a FRESH record
        (re-pulled, age-stamped) — a stale discovery record can no longer
        cast a strike (the 7¢ movement-lag false halt: ours y23 vs a stale
        y30). §1.3: a live ORIENTATION_DIVERGENCE halt auto-recovers the
        moment a fresh recheck of the halting market reads clean — a
        transient staleness halt self-heals; /reset_halt is the backstop,
        not the only door."""
        from . import failures
        now = time.time() if now is None else now
        # §1.3 AUTO-RECOVER: a live orientation halt re-checks itself with a
        # FRESH record; a clean fresh read resumes entries (no key needed).
        if ("ORIENTATION_DIVERGENCE" in self.gateway.entries_halted_reasons
                and self._orientation_halt_market is not None):
            mkt = self._orientation_halt_market
            book = self.feed.books.get(mkt)
            ours = book.best_yes_bid() if book is not None else None
            fresh = self._fresh_record_touches(mkt)
            fbid = fresh[0] if fresh is not None else None
            if ours is not None and fbid is not None and abs(ours - fbid) <= 3:
                self.gateway.resume_entries("ORIENTATION_DIVERGENCE")
                self._orientation_halt_market = None
                self.telegram.alert(
                    f"✅ ORIENTATION recovered {mkt}: fresh record y{fbid}¢ "
                    f"agrees with ours y{ours}¢ (≤3¢) — entries re-enabled "
                    "automatically (WO-HALT-ORPHAN §1.3)")
                log.warning("ORIENTATION_DIVERGENCE auto-cleared on %s: "
                            "fresh y%s vs ours y%s", mkt, fbid, ours)
        for market, w in list(self.divergence_watches.items()):
            if now > w["until"]:
                del self.divergence_watches[market]
                continue
            book = self.feed.books.get(market)
            ours = book.best_yes_bid() if book is not None else None
            if ours is None:
                continue
            # §2C: the strike is cast by a FRESH record, never a stale one.
            fresh = self._fresh_record_touches(market)
            rec = fresh[0] if fresh is not None else None
            if rec is None:
                continue
            if abs(ours - rec) > 3:
                w["strikes"] += 1
                if w["strikes"] >= 3:
                    del self.divergence_watches[market]
                    self.gateway.halt_entries("ORIENTATION_DIVERGENCE")
                    self._orientation_halt_market = market
                    self.telegram.alert(
                        f"⛔ ORIENTATION_DIVERGENCE {market}: ours y{ours}¢ vs "
                        f"FRESH record y{rec}¢ >3¢ x3 — entries HALTED "
                        "(auto-recovers on a clean fresh recheck)")
                    failures.fail("ORIENTATION_DIVERGENCE",
                                  f"{market}: ours {ours}¢ vs FRESH record "
                                  f"{rec}¢ diverged 3 consecutive checks "
                                  f"post-entry",
                                  market=market, ours=ours, record=rec,
                                  alert=False)
            else:
                w["strikes"] = 0

    def transport_label(self) -> str:
        """P11.1-c: the transport stamp on surface rows and brackets — future
        evidence stays separable when WS returns as an upgrade."""
        if not config.WS_ENABLED:
            return "REST"
        return "WS" if self.ladder.entries_allowed() else "EXPLORATION"

    def rest_poll_all(self, client, now=None) -> int:
        """P11: one pass of the 1s book poll over every discovered market."""
        now = time.time() if now is None else now
        ok = 0
        for m in sorted(self.market_meta):
            if self.feed.poll_market(client, m, now=now):
                ok += 1
        return ok

    def record_fatal(self, message: str) -> None:
        self.ledger.set_state("last_fatal", message[:500])

    def _on_ladder_move(self, old, new):
        if new == "WS_LOST":
            self.gateway.halt_entries("DEGRADE_LADDER")
            log.warning("ALL-lane entry halt (WS lost); custodian on REST; risk reduction allowed")
        elif new == "WS_LIVE":
            self.gateway.resume_entries("DEGRADE_LADDER")
            log.warning("entries resumed after clean-frame count")
        # R6: transitions are ledger-of-record events — they page
        self.telegram.alert(f"🪜 DEGRADE LADDER {old} → {new}")

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

        # P9 §2: deferred brackets complete the moment the venue answers again
        self.flush_deferred_econ(now)

        # P10 §2.4: a quarantine self-heals at its window close (belt under
        # the rollover prune)
        for m, until in list(self.quarantined.items()):
            if now >= until:
                del self.quarantined[m]
                self.poison_episodes.pop(m, None)

        # 1) custodian exits outrank everything (risk reduction first).
        # P10 §2.3 (A6): a poisoned/quarantined WS book must NOT blind the
        # custodian — its marks for that market switch to the REST book.
        from .lanes import infer_close_ts_from_ticker
        cust_books = {}
        held = {p.market for p in self.custodian.positions.values()}
        for m in markets:
            b = self.feed.book(m)
            if (b.poisoned or m in self.quarantined) and m in held:
                rb = self._rest_book(m)
                if rb is not None:
                    b = rb
            # P11.1-d: a failed-fetch book serves custody only WITHIN the
            # staleness bound; beyond it custody marks DEFER (no key → the
            # custodian holds) exactly as P9 defers money. Never a stale
            # fabrication.
            if (not config.WS_ENABLED
                    and m in getattr(self.feed, "fetch_failed", ())
                    and b.has_snapshot
                    and b.is_stale(now, config.REST_BOOK_STALE_CUSTODY_S)):
                continue
            cust_books[m] = b
        cuts = self.custodian.tick(
            books=cust_books,
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
            transport = self.transport_label()
            # P10 §2.2/§2.4: lanes refuse a poisoned or quarantined book —
            # skipped this cycle, tagged; NEVER fatal (one market's corrupt
            # book must not stop the others). Custody already ran above.
            # P11.1-d: a failed REST fetch means NO book this cycle — lanes
            # skip, same shape, never a fabrication.
            if (book.poisoned or market in self.quarantined
                    or (not config.WS_ENABLED
                        and market in getattr(self.feed, "fetch_failed", ()))):
                if market in self.quarantined:
                    tag = "QUARANTINED"
                elif book.poisoned:
                    tag = "BOOK_POISONED"
                else:
                    tag = "BOOK_FETCH_FAILED"
                for lane in self.lanes:
                    self.surface.write_row(lane.name, market, window,
                                           "WATCHING", transport=transport,
                                           detail=tag)
                continue
            # P18 §4.1: shared eyes, not orders — the spot-lead signal
            # computes ONCE per cycle here; FLIP·HUNT consumes it as trigger,
            # F/H8 record it as a why-tag field, P yields the floor.
            from . import delta as _delta, spotlead as _sl
            from .lanes import infer_close_ts_from_ticker as _infer
            sl = None
            if spot is not None:
                strike = _sl.pick_strike(spot, meta.get("boundary_lo"),
                                         meta.get("boundary_hi"))
                close_for = meta.get("close_ts") or _infer(market)
                if strike is not None and close_for is not None:
                    anchor = self.hunt_anchor.setdefault(market, spot)
                    sl = _sl.needle(anchor, spot, strike, close_for - now)
            # P26 §1.2: a mute organ must say it's mute (R5, applied to the
            # brain) — while the table is unloaded, each window's proven-lane
            # silence tags itself ONCE as UNPROVEN, never a shrug.
            if (spot is not None and not _delta.is_loaded()
                    and (market, window) not in self._unproven_tagged):
                self._unproven_tagged.add((market, window))
                self.surface.write_row(
                    "FLIP", market, window, "WATCHING", transport=transport,
                    detail="UNPROVEN pass — brain absent "
                           f"({_delta.refusal_reason() or 'table not loaded'})")
            # P21 A3: the herd's compass — OUR settled windows, last-K streak.
            from . import grain as _grain
            ctx = {
                "book": book, "now": now, "spot": spot,
                "spot_ticks": self.spot_ticks,
                "close_ts": meta.get("close_ts"),
                "boundary_lo": meta.get("boundary_lo"),
                "boundary_hi": meta.get("boundary_hi"),
                "cash_usd": self.ledger.book_cents() / 100.0,
                "entries_allowed": self.ladder.entries_allowed(),
                "spotlead": sl,
                # P19 §2.4: mutual suppression — ONE mechanism, both rules.
                "needle_confirmed": _sl.is_confirmed_needle(sl),
                "salvage_active": self.custodian.salvage_in_progress(market),
                "grain": _grain.grain(self.ledger),
            }
            # P13 §3: boot orientation self-test on the first comparable book
            if not self._orientation_checked and book.has_snapshot:
                self.orientation_selftest(market, book)

            # FLIP pair-grace housekeeping: drop the unfilled opposite entry
            stale_oid = self.flip.pair_grace_expired(market, now)
            if stale_oid is not None:
                self.gateway.cancel(stale_oid)

            for lane in self.lanes:
                # P19 §3.1: P-suppression moved INTO the lane (the ctx flag is
                # the one mechanism) — the lane returns the suppression Pass
                # and the ordinary row path logs it, tape-gradable.
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
                    # P22 §4.1: the ladder gains its caller — every ENTRY is
                    # sized from its cell's score (tier from Wilson LB vs the
                    # cell's OWN bars; lots from size_order's untouched
                    # min(tier, kelly, depth)). Demotion applies HERE, at the
                    # next proposal, no grace. Exits/cuts are never resized.
                    if proposal.purpose == "ENTRY":
                        self._score_and_size(proposal, book)
                    # WO-HALT-ORPHAN §2B: an ENTRY the budget wall already
                    # refused this window is not re-submitted — the wall
                    # can't change within a window (byte-identical book).
                    br_key = (market, lane.name, proposal.side,
                              proposal.price_cents)
                    if (proposal.purpose == "ENTRY"
                            and br_key in self._budget_rejected):
                        continue
                    try:
                        result = self.gateway.submit(proposal, book)
                    except WallRejection as e:
                        self.gateway.reject_counts[e.wall] = \
                            self.gateway.reject_counts.get(e.wall, 0) + 1
                        # §2B: one budget rejection per lane/side/price/window
                        if e.wall in ("BUDGET", "DOLLAR_RISK", "NET_RISK"):
                            self._budget_rejected.add(br_key)
                        # P10 §4.3: log the first + every 10th (the 01:25 log
                        # was 60 identical lines); the count keeps fidelity.
                        k = (lane.name, market, e.wall)
                        n = self._wall_log_counts[k] = \
                            self._wall_log_counts.get(k, 0) + 1
                        if n == 1 or n % 10 == 0:
                            log.warning("wall rejected %s proposal on %s: %s (x%d)",
                                        lane.name, market, e, n)
                        continue
                    if proposal.purpose == "ENTRY":
                        # P8 §1: OPEN BRACKET at the first order submit on this
                        # market. P9 §2: a failed live read DEFERS the bracket.
                        val, src = self.account_value(now)
                        self.econ.open_bracket(market, val, now=now, source=src,
                                               transport=transport)
                        # P18: ONE hunt per displacement EVENT — re-anchor at
                        # the post-event spot; a new hunt needs a NEW needle.
                        if (proposal.lane == "FLIP"
                                and proposal.why.startswith("HUNT")
                                and spot is not None):
                            self.hunt_anchor[market] = spot
                    if proposal.lane in ("F", "H8"):
                        # Live submit side-effects, mirrored from k_worker gateway.submit:
                        # lane-scoped single entry + F hourly exposure.
                        self.fh8_shared.state.mark_traded(market, proposal.lane)
                        if proposal.lane == "F":
                            self.fh8_shared.state.add_exposure(
                                proposal.price_cents / 100.0)
                    elif proposal.lane == "FLIP":
                        self.flip.on_submitted(proposal, result.order_id, now)
                    sentence = (f" why={proposal.why}" if proposal.why else "") \
                        + (f" reason={proposal.reason}" if proposal.reason else "")
                    self.surface.write_row(lane.name, market, window, PROPOSED,
                                           transport=transport,
                                           detail=f"order={result.order_id} "
                                                  f"{proposal.purpose} "
                                                  f"@{proposal.price_cents}c"
                                                  + sentence)
                    log.warning("PROPOSAL %s %s %s %s @%dc -> %s",
                                lane.name, market, proposal.purpose, proposal.side,
                                proposal.price_cents, result.order_id)

    def close_window(self, market):
        """P17 §6.3/§6.5: at close, every watching/passing lane writes its
        first-class PASS terminal; a window with NO rows at all is a §6
        contract breach — paged, never silent."""
        window = self._window_of.pop(market, None)
        if window is None:
            return
        self._unproven_tagged.discard((market, window))  # P26 §1.2 prune
        self.surface.finalize_window(market, window)
        from . import failures
        n = self.ledger.db.execute(
            "SELECT COUNT(*) FROM surface_rows WHERE market=? AND window_id=?",
            (market, window)).fetchone()[0]
        if n == 0:
            failures.fail("SILENT_WINDOW",
                          f"{market}: window closed with NO rows at all — "
                          f"§6 contract breach (arrive/arm/evaluate missing)",
                          market=market)

    def gap_restart_scan(self) -> int:
        """P17 §1.4: on boot, any tracked window already past close with no
        terminal row writes GAP_RESTART — evidence holes are counted, never
        papered over."""
        from .lanes import infer_close_ts_from_ticker
        now = time.time()
        rows = self.ledger.db.execute(
            "SELECT lane, market, window_id, MAX(terminal) FROM surface_rows"
            " GROUP BY lane, market, window_id HAVING MAX(terminal)=0").fetchall()
        wrote = 0
        for lane, market, window_id, _ in rows:
            close_ts = infer_close_ts_from_ticker(market)
            if close_ts is not None and now > close_ts + 60:
                self.surface.write_row(lane, market, window_id,
                                       "GAP_RESTART",
                                       detail="window spanned a restart — "
                                              "evidence hole, counted")
                wrote += 1
        return wrote

    def settle_traded_market(self, market: str, settled_yes: bool,
                             now=None) -> None:
        """P8 §1: CLOSE BRACKET — settlement confirmed, fills booked, account
        truth snapshotted. Traded markets only (open brackets); untraded
        markets write no bracket."""
        # P17 §2.1: a settlement booked well after close is LATE — the streak
        # still counts it (retroactive halt capable) and the receipt says so.
        now_eff = time.time() if now is None else now
        close_ts = self._meta(market).get("close_ts")
        if close_ts is None:
            from .lanes import infer_close_ts_from_ticker
            close_ts = infer_close_ts_from_ticker(market)
        late = close_ts is not None and now_eff > close_ts + 300
        # P13 §3: settlement cross-check — the free oracle every 15 minutes.
        # The winning side must have been our book's high side near close.
        book = self.feed.books.get(market)
        yb = book.best_yes_bid() if book is not None else None
        if yb is not None and ((settled_yes and yb < 40)
                               or (not settled_yes and yb > 60)):
            from . import failures
            failures.fail("ORIENTATION_SUSPECT",
                          f"{market}: settled {'YES' if settled_yes else 'NO'} "
                          f"but our last yes_bid was {yb}¢ — book orientation "
                          f"suspect, audit this window's tape",
                          market=market, settled_yes=settled_yes, last_yes_bid=yb)
        window = self._window_of.get(market, f"w-{market}")
        # SALV-1 §2.3: positions that RODE to settlement conclude here —
        # one summary each (win pays 100−entry, loss pays −entry).
        for key in [k for k, p in self.custodian.positions.items()
                    if p.market == market]:
            pos = self.custodian.positions.pop(key)
            won = (settled_yes and pos.side == "yes") or \
                  (not settled_yes and pos.side == "no")
            realized = (100 - pos.entry_price_cents) if won \
                else -pos.entry_price_cents
            self.custodian.emit_salvage_summary(pos, "SETTLED",
                                                realized_cents=realized)
        # P21 A3: bank the outcome for the grain — the herd's screen updates
        # the moment we learn how the window went (traded windows only today;
        # that partial view is the registry's grain QUESTION).
        self.ledger.record_outcome(market, settled_yes, now=now_eff)
        # P22 §1.1(b): positions HELD to settlement close their unit of risk
        # here, attributed to the OPENING lane (the attribution law). Read
        # the unsettled fills BEFORE settle_market flips them; round-trips
        # already banked their trip rows at exit booking — held residue only.
        held_rows = self.ledger.db.execute(
            "SELECT lane, side, action, price_cents, count FROM fills"
            " WHERE market=? AND settled=0", (market,)).fetchall()
        by_lane: dict = {}
        for lane, side, action, price, count in held_rows:
            d = by_lane.setdefault(lane, {})
            s = d.setdefault(side, {"net": 0, "entry": None})
            if action == "ENTRY":
                s["net"] += count
                s["entry"] = price
            else:
                s["net"] -= count
        for lane, sides in by_lane.items():
            for side, s in sides.items():
                if s["net"] <= 0 or s["entry"] is None:
                    continue
                won = (settled_yes and side == "yes") or \
                      (not settled_yes and side == "no")
                pnl = ((100 - s["entry"]) if won else -s["entry"]) * s["net"]
                self.ledger.record_cell_outcome(
                    lane, s["entry"], won=won, pnl_cents=pnl, fees_cents=0,
                    market=market, kind="settle", now=now_eff)
        per_lane = self.surface.settle_market(market, window,
                                              settled_yes=settled_yes)
        # P19 §2.6: every salvage row gets its settlement COUNTERFACTUAL —
        # the DODGED_LOSS vs SALVAGE_REGRET curve is Saturday chart #2, and
        # K is tuned from it, never from a bad night.
        for lane, detail in self.ledger.db.execute(
                "SELECT lane, detail FROM surface_rows WHERE market=?"
                " AND state='SALVAGE'", (market,)).fetchall():
            try:
                d = json.loads(detail)
                held_wins = (settled_yes and d["side"] == "yes") or \
                            ((not settled_yes) and d["side"] == "no")
                counterfactual = (100 - d["entry"]) if held_wins else -d["entry"]
                realized = d["mark"] - d["entry"]
                dodged = realized - counterfactual
                self.surface.write_row(
                    lane, market, window, "SALVAGE_VERDICT",
                    detail=json.dumps({
                        "dodged_cents": dodged, "realized": realized,
                        "counterfactual": counterfactual,
                        "verdict": ("DODGED_LOSS" if dodged > 0
                                    else "SALVAGE_REGRET")}))
            except Exception:
                pass
        fills_pnl = sum(per_lane.values())
        fills_count = int(self.ledger.db.execute(
            "SELECT COUNT(*) FROM fills WHERE market=?", (market,)).fetchone()[0])
        val, src = self.account_value(now)
        self.econ.close_bracket(
            market, val, fills_pnl,
            lanes_active=",".join(sorted(per_lane)), fills_count=fills_count,
            now=now, source=src, late=late)

    def settlement_sweep(self, now=None) -> int:
        """Poll settlement for markets with open brackets whose close passed."""
        from . import venue
        now = time.time() if now is None else now
        settled = 0
        for market in list(self.econ.open_brackets):
            meta = self._meta(market)
            close_ts = meta.get("close_ts")
            if close_ts is not None and now < close_ts + 10:
                continue
            client = self.gateway.venue_client
            if client is None:
                from . import venue as _v
                try:
                    client = self.gateway.venue_client = _v.build_client()
                except Exception:
                    return settled
            result = venue.get_settlement_result(client, market)
            if result in ("yes", "no"):
                self.settle_traded_market(market, settled_yes=(result == "yes"),
                                          now=now)
                settled += 1
        return settled


SUPERVISOR_BACKOFF_S = 5.0

# P9 §1b: every background task's death is TAGGED — "Task exception was never
# retrieved" is a banked fail-silent class. The listener's tag is named in the
# order because it holds a safety command path (/reset_halt, /confirm_cash).
TASK_TAGS = {
    "spot": "SPOT_TASK_DOWN",
    "pack": "PACK_TASK_DOWN",
    "listener": "TG_LISTENER_DOWN",
    "settle": "SETTLE_TASK_DOWN",
    "fills": "FILLS_TASK_DOWN",
    "reconcile": "RECONCILE_TASK_DOWN",
    "book_check": "BOOK_CHECK_TASK_DOWN",
    "orientation": "ORIENTATION_TASK_DOWN",
}

BOOK_CHECK_S = 60.0  # P10 §3: the standing arbiter's cadence
# §3.3 (banked, no action): the cross-check is per-market; at 4 series it is
# ~4 req/min — inside the rate governor. Written down so nobody rediscovers it.


# P17 §2.2: a stuck safety task states its CONSEQUENCE in words on the page.
TASK_CONSEQUENCES = {
    "settle": " — streak & brackets FROZEN until healed",
    "fills": " — fill knowledge frozen (positions may lag the venue)",
    "listener": " — /reset_halt and /confirm_cash DEAF until healed",
}

TASK_STUCK_AFTER = 5       # P17 §3: same error x5 -> one page, then 60s pace
TASK_STUCK_BACKOFF_S = 60.0


async def supervise(name, factory, stop, engine, backoff_s=SUPERVISOR_BACKOFF_S):
    """P9 §1b: NO un-supervised task may hold a safety command path. Any
    exception -> fail(tag) -> outbound alert -> restart after backoff.
    P17 §3: the SAME error 5 consecutive times -> ONE `TASK_STUCK` page ->
    backoff to 60s retries (still supervised, still counted); a different
    error resets the count. A stuck loop that pages once and waits is honest;
    one that retries forever quietly is the storm class in a badge."""
    from . import failures
    tag = TASK_TAGS.get(name, "TASK_DOWN")
    last_err, same_count = None, 0
    while not stop.is_set():
        engine.task_alive[name] = True
        try:
            await factory()
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            engine.task_alive[name] = False
            n = engine.task_restarts[name] = engine.task_restarts.get(name, 0) + 1
            rep = repr(e)
            same_count = same_count + 1 if rep == last_err else 1
            last_err = rep
            try:
                failures.fail(tag,
                              f"supervised task {name!r} died: {e!r} — "
                              f"restart #{n}"
                              + TASK_CONSEQUENCES.get(name, ""),
                              alert=(same_count < TASK_STUCK_AFTER))
            except Exception:
                pass  # the supervisor itself never dies of its report
            if same_count == TASK_STUCK_AFTER:
                try:
                    engine.telegram.alert(
                        f"⚠ TASK_STUCK [{name}] same error x{same_count} — "
                        f"manual attention"
                        + TASK_CONSEQUENCES.get(name, "")
                        + f" · retrying every {TASK_STUCK_BACKOFF_S:.0f}s")
                    failures.fail("TASK_STUCK",
                                  f"{name}: same error x{same_count}: {rep[:200]}",
                                  task=name, alert=False)
                except Exception:
                    pass
            await asyncio.sleep(TASK_STUCK_BACKOFF_S
                                if same_count >= TASK_STUCK_AFTER else backoff_s)


def _send_boot_page_rest(engine) -> None:
    """P11 §C: the REST boot page — transport named, every dollar sourced."""
    from .boot import sizing_line
    from .ops import worst_day_bound_line
    engine.telegram.alert(
        f"🟢 BOOT #{getattr(engine, 'boot_id', '?')} "
        f"[{config.RUN_MODE}] EPOCH {config.EPOCH} — "
        f"{len(engine.market_meta)} market(s)\n"
        f"transport: REST 1s (A3 proven ground; WS shelved as an upgrade)\n"
        f"{sizing_line(engine.ledger.book_cents())}\n"
        f"{worst_day_bound_line(engine.ledger)}\n"
        f"listener: {engine.listener_status()}")


async def _rest_main(engine, client, stop) -> None:
    """P11 "PROVEN GROUND" — the REST 1s main loop: discovery every 60s,
    every book polled every cycle through the FeedLike drop-in, the same
    five-lane cycle. No dialect to learn; the organs already existed."""
    from . import failures, venue
    auth_strikes = 0
    booted = False
    last_discovery = -1e9
    while not stop.is_set():
        t0 = time.monotonic()
        try:
            if t0 - last_discovery >= DISCOVERY_SWEEP_S:
                last_discovery = t0
                mkts = await asyncio.to_thread(venue.list_open_markets, client)
                current = set()
                for m in mkts:
                    ticker = m.get("ticker") or m.get("market_ticker", "")
                    if not ticker:
                        continue
                    current.add(ticker)
                    engine.on_market_discovered(ticker, m)
                for gone in sorted(set(engine.market_meta) - current):
                    engine.on_market_closed(gone)
                    log.info("window closed + pruned: %s", gone)
            await asyncio.to_thread(engine.rest_poll_all, client)
            if not booted:
                booted = True
                _send_boot_page_rest(engine)
            engine.recorder.flush()  # P6 §4: the cycle gate commits the tape
            engine.cycle(sorted(engine.market_meta.keys()))
            auth_strikes = 0
        except FatalIntegrityError as fe:
            engine.record_fatal(str(fe))  # P5 §4: the BOOT_LOOP alert's evidence
            raise  # fail loud, stay stopped
        except Exception as e:
            msg = str(e)
            if "HTTP 401" in msg or "HTTP 403" in msg:
                auth_strikes += 1
                if auth_strikes >= 3:
                    failures.fail("AUTH_REJECTED",
                                  f"3 consecutive REST auth rejections: {msg[:200]}",
                                  fatal=True)
            log.warning("REST loop error (continuing): %s", e)
            await asyncio.sleep(3.0)
            continue
        elapsed = time.monotonic() - t0
        await asyncio.sleep(max(0.05, CYCLE_SECONDS - elapsed))


async def run():
    from . import auth

    # Auth boot-stop (§4 doctrine): missing/unparseable creds are FATAL here,
    # BEFORE any connect attempt — never a retry loop.
    auth_line = auth.boot_check()

    engine = ShadowEngine()
    engine.boot(auth_line=auth_line)

    from . import failures
    if config.RUN_MODE != "SHADOW" and not config.live_submit_enabled():
        failures.fail("RUN_MODE_WITHOUT_PHRASE",
                      f"RUN_MODE={config.RUN_MODE} without the I_UNDERSTAND_LIVE phrase — "
                      f"go-live is a human act (env var + phrase), refusing to run",
                      fatal=True)
    if config.live_submit_enabled() and not engine.telegram.wired():
        # R6: the pager is safety equipment — LIVE without it is a boot-stop
        failures.fail("PAGER_UNWIRED_LIVE",
                      "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID absent in LIVE mode — "
                      "the ledger of record cannot be silent", fatal=True)

    from . import venue
    client = venue.build_client()
    engine.gateway.venue_client = client  # one client, shared by every organ

    # Tape 0718: the default orders-list route 404'd on this API base — probe
    # both candidates at boot so cancel/status/foreign-resting all speak the
    # route the venue actually answers. Non-fatal: order PLACEMENT has its
    # own route; a failed probe degrades listing only.
    try:
        venue.probe_orders_route(client)
    except Exception as e:
        log.warning("orders-route probe failed (listing degraded): %s", e)

    if config.live_submit_enabled():
        # F6: LIVE boot reconcile — money truth before the first cycle
        from .reconcile import live_boot_reconcile
        live_boot_reconcile(engine, client)

    # DIAG-1 §1: THE INTERROGATOR — one-time diagnostics run here, after
    # reconcile and before the first cycle: each pages its answer once
    # ever and closes itself. Never boot-fatal; the phone is the console.
    try:
        from . import diagnostics
        diagnostics.run_boot_diagnostics(engine)
    except Exception as e:
        log.error("boot diagnostics error (continuing): %s", e)

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

    # ── F7/R6: the pack timer — hourly one-liner, 9AM ET full pack ─────────
    async def pack_task():
        from zoneinfo import ZoneInfo
        from datetime import datetime
        last_full_day = None
        while not stop.is_set():
            await asyncio.sleep(PACK_HOURLY_S)
            pack = daily_pack(engine.ledger, engine.surface, engine.cash,
                              foreign_fills=engine.fills.foreign_seen,
                              econ=engine.econ)
            print(pack, flush=True)
            now_et = datetime.now(ZoneInfo("America/New_York"))
            if now_et.hour == 9 and last_full_day != now_et.date():
                last_full_day = now_et.date()
                engine.telegram.alert(pack)  # the 9AM full pack, on the phone
            else:
                from . import delta as _delta   # P26 §1.2: brain on the line
                engine.telegram.alert(
                    f"📗 hourly: book={engine.ledger.book_cents()}c "
                    f"windows={engine.windows_seen} "
                    f"markets={len(engine.market_meta)} "
                    f"orders={len(engine.gateway.order_index)} "
                    f"foreign={engine.fills.foreign_seen} "
                    f"streak={engine.econ.streak} "
                    f"frames={engine.feed.frames_seen} "
                    f"rejects={sum(engine.gateway.reject_counts.values())} "
                    f"venue_rejects={engine.gateway.venue_rejects} "
                    f"listener={engine.listener_status()} "
                    f"brain={'ok' if _delta.is_loaded() else 'absent'} "
                    f"book✓ {engine.book_checks_total} "
                    f"fetch_fail={getattr(engine.feed, 'failed_fetches', 0)} "
                    f"failures={engine.ledger.db.execute('SELECT COUNT(*) FROM failures').fetchone()[0]}")

    # ── §2c/R6: the inbound listener — EXACTLY the accounting pair ─────────
    async def listener_task():
        while not stop.is_set():
            handled = await asyncio.to_thread(
                engine.telegram.poll_updates_once, engine.ledger)
            if handled == 0 and not engine.telegram.wired():
                return  # nothing to listen on; the log heard the refusal
            await asyncio.sleep(1.0)

    # ── P8: settlement sweep, every 30s ────────────────────────────────────
    async def settle_task():
        while not stop.is_set():
            await asyncio.sleep(30.0)
            await asyncio.to_thread(engine.settlement_sweep)

    # ── P11 (Marta's cadence): LIVE fills reconcile every 3s — the proven
    # live rhythm; fill knowledge up to 3s late is yesterday's accepted
    # reality at one-lot, stated in the pack header (not a bug).
    async def fills_task():
        while not stop.is_set():
            await asyncio.sleep(config.FILLS_SWEEP_S)
            if config.live_submit_enabled() and engine.gateway.venue_client is not None:
                await asyncio.to_thread(engine.fills.reconcile_sweep,
                                        engine.gateway.venue_client)

    # ── P9 §3: standing live reconcile — drift pages BETWEEN brackets ──────
    async def reconcile_task():
        while not stop.is_set():
            await asyncio.sleep(60.0)
            if config.live_submit_enabled():
                await asyncio.to_thread(engine.standing_reconcile)

    # ── P10 §3: the venue REST book audits the WS book every 60s.
    # In REST mode the book IS the venue's REST book — there is no second
    # truth to arbitrate, so the auditor stands down (WS-only).
    async def book_check_task():
        while not stop.is_set():
            await asyncio.sleep(BOOK_CHECK_S)
            if config.WS_ENABLED:
                await asyncio.to_thread(engine.book_check)

    # ── P13 §3: the 30s post-entry divergence watch, processed every 10s ───
    async def orientation_task():
        while not stop.is_set():
            await asyncio.sleep(10.0)
            if engine.divergence_watches:
                await asyncio.to_thread(engine.process_divergence_watches, client)

    # P9 §1b: EVERY task runs supervised — a death is banked, paged, restarted.
    asyncio.create_task(supervise("orientation", orientation_task, stop, engine))
    asyncio.create_task(supervise("spot", spot_task, stop, engine))
    asyncio.create_task(supervise("pack", pack_task, stop, engine))
    asyncio.create_task(supervise("listener", listener_task, stop, engine))
    asyncio.create_task(supervise("settle", settle_task, stop, engine))
    asyncio.create_task(supervise("fills", fills_task, stop, engine))
    asyncio.create_task(supervise("reconcile", reconcile_task, stop, engine))
    asyncio.create_task(supervise("book_check", book_check_task, stop, engine))

    if not config.WS_ENABLED:
        # ── A3 PROVEN GROUND: the REST 1s main loop. The ws path below is
        # never entered and websockets is never imported (P11.1-d).
        await _rest_main(engine, client, stop)
        stop.set()
        engine.recorder.flush()  # P6 §4: shutdown flush — no buffered frame lost
        print(daily_pack(engine.ledger, engine.surface, engine.cash,
                         foreign_fills=engine.fills.foreign_seen,
                         econ=engine.econ), flush=True)
        engine.telegram.alert("🔵 CLEAN SHUTDOWN — recorder flushed, pack printed")
        log.info("runner stopped; zero orders placed: %s",
                 len(engine.gateway.shadow_orders) == 0)
        return

    try:
        import websockets
    except ImportError as e:
        failures.fail("DEPENDENCY_MISSING",
                      f"websockets required for the WS feed: {e}", fatal=True)

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

                # P9 §4: the BOOT page waits for channel negotiation — it
                # carries the ACCEPTED list (not "negotiating"), the SIZING
                # line, the worst-day bound, boot #N, and the listener state.
                boot_page_pending = True
                connect_mono = time.monotonic()

                def send_boot_page():
                    from .boot import sizing_line
                    from .ops import worst_day_bound_line
                    engine.telegram.alert(
                        f"🟢 BOOT #{getattr(engine, 'boot_id', '?')} "
                        f"[{config.RUN_MODE}] EPOCH {config.EPOCH} — "
                        f"{len(subscribed)} market(s) subscribed\n"
                        f"channels: {sorted(subscriber.accepted.values()) or '(none accepted)'}"
                        + (f" / degraded: {subscriber.degraded}" if subscriber.degraded else "")
                        + f"\n{sizing_line(engine.ledger.book_cents())}\n"
                        f"{worst_day_bound_line(engine.ledger)}\n"
                        f"listener: {engine.listener_status()}")

                last_cycle = 0.0
                last_discovery = time.monotonic()
                resync_sent = {}   # P10 §2: market -> last resync send (5s floor)
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
                    # P10 §2: resync-cmd replies are TOLERANT — banked on
                    # error, never the boot-subscribe fatal path.
                    if pre is not None and pre.get("id") in subscriber.resync_ids:
                        subscriber.resync_ids.discard(pre.get("id"))
                        if pre.get("type") == "error":
                            failures.fail("RESYNC_REJECTED",
                                          f"book resync cmd rejected: {pre}",
                                          frame=pre)
                        continue
                    if pre is not None and pre.get("id") in subscriber.pending:
                        retry = subscriber.on_reply(pre)
                        if retry is not None:
                            payload = json.dumps(retry)
                            engine.feed.note_sent(payload)
                            await ws.send(payload)
                        if boot_page_pending and not subscriber.pending:
                            boot_page_pending = False
                            send_boot_page()  # P9 §4: negotiation done — page now
                        continue
                    if boot_page_pending and (not subscriber.pending
                                              or time.monotonic() - connect_mono > 30):
                        boot_page_pending = False
                        send_boot_page()
                    engine.feed.handle_frame(raw)
                    # P10 §2.1: poisoned/unfounded books force a snapshot
                    # resubscribe for THAT market (5s floor per market — the
                    # A5 ceiling quarantines a market that keeps tripping).
                    if engine.feed.resync_needed:
                        mono_now = time.monotonic()
                        for m in sorted(engine.feed.resync_needed):
                            if m not in subscribed:
                                continue
                            if mono_now - resync_sent.get(m, -1e9) < 5.0:
                                continue
                            resync_sent[m] = mono_now
                            for cmd in subscriber.resync_cmds(m):
                                payload = json.dumps(cmd)
                                engine.feed.note_sent(payload)
                                await ws.send(payload)
                            log.warning("BOOK RESYNC requested for %s", m)
                        engine.feed.resync_needed.clear()
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
                    # runs on the CYCLE_SECONDS gate. P6 §4: the recorder's
                    # buffer commits on the same gate.
                    if mono - last_cycle >= CYCLE_SECONDS:
                        last_cycle = mono
                        engine.recorder.flush()
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
    engine.recorder.flush()  # P6 §4: shutdown flush — no buffered frame lost
    print(daily_pack(engine.ledger, engine.surface, engine.cash,
                     foreign_fills=engine.fills.foreign_seen, econ=engine.econ),
          flush=True)
    engine.telegram.alert("🔵 CLEAN SHUTDOWN — recorder flushed, pack printed")
    log.info("runner stopped; zero orders placed: %s",
             len(engine.gateway.shadow_orders) == 0)


if __name__ == "__main__":
    asyncio.run(run())
