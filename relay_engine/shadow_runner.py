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
from .ops import Recorder, Telegram, daily_pack, flip_fill_rate_hourly
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
        # WO-2026-07-26-O §O4: /owed — the operator's read-only scrape look.
        from .ops import owed_line as _owed_line
        self.telegram.owed_fn = lambda: _owed_line(self.ledger)
        # WO-2026-07-22-K: /daily — the read-only day export (one .xlsx, every
        # table, bounded to the day). Built in /tmp, sent, deleted.
        self.telegram.daily_fn = self._build_and_send_daily
        # WO-2026-07-26-S §1: /series — the room start command (no second deploy).
        self.telegram.series_fn = self._cmd_series
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
        self._shadow_rested = {}        # WO-L §P1: SHADOW oid -> polls rested (fill sim)
        self._orientation_checked = False
        # WO-HALT-ORPHAN §1.3: the market that tripped ORIENTATION_DIVERGENCE
        # — re-checked with a FRESH record each cycle; a passing recheck
        # auto-resumes (a transient staleness halt self-heals, no key).
        self._orientation_halt_market = None
        # WO-2026-07-24-D Part 3: when the halt fired, so the hard ceiling can
        # page ORIENTATION_HALT_STUCK instead of sitting silent forever once the
        # halting market expires and its book is pruned (recovery can't pin to a
        # dead market). None = no live orientation halt.
        self._orientation_halt_ts = None
        # WO-2026-07-24-D Part 4: reconcile health — the operator must be able to
        # tell a verified book from an unverified one. _recon_last_ok_ts is the
        # last CLEAN venue cross-check; the streak counts consecutive cycles that
        # deferred or could not read (a stall pages RECON_STALLED).
        self._recon_last_ok_ts = None
        self._recon_deferred_streak = 0
        # WO-2026-07-24-D Part 5: latch the closed-gate reject storm — a halted
        # lane re-proposing every poll must count/log ONCE per (lane, market)
        # until the gate opens, not ~5/min of noise that hides a real reject.
        self._halt_reject_latched = set()
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

    # ── WO-2026-07-26-S §1: the rooms — roster + the /series start command ──
    def _apply_series_roster(self) -> None:
        """Widen F's series family to the enabled roster and migrate the halt
        keys the moment a second room joins (bare-lane → series-scoped). Called
        at boot and after every /series change — idempotent."""
        from . import lane_fh8
        lane_fh8.F_SERIES_ALLOWED = set(config.f_enabled_series()) or {"KXBTC15M"}
        if len(config.SERIES) > 1:
            self.econ.migrate_halt_keys_to_series("KXBTC15M")

    def _cmd_series(self, text: str) -> str:
        """`/series <asset> <on|off|live|shadow>` — open or park a room without a
        second deploy. on/live add the room to the roster at LIVE; shadow adds it
        rehearsing; off removes it. BTC (the proven room) can never be turned
        off from here. The global kill still governs every room."""
        parts = text.strip().split()
        if len(parts) < 3:
            return ("usage: /series <asset> <on|off|live|shadow> — rooms: "
                    + ", ".join(sorted(config.KNOWN_SERIES)) + "; live now: "
                    + ", ".join(config.f_enabled_series()))
        series = config.resolve_series(parts[1])
        action = parts[2].strip().lower()
        if series is None:
            return f"unknown room {parts[1]!r}; known: {', '.join(sorted(config.KNOWN_SERIES))}"
        if series == "KXBTC15M" and action in ("off",):
            return "BTC is the proven room — it cannot be turned off from here"
        mode = {"on": "LIVE", "live": "LIVE", "shadow": "SHADOW",
                "off": "OFF"}.get(action)
        if mode is None:
            return f"unknown action {action!r}; use on|off|live|shadow"
        config.SERIES_MODE[series] = mode
        if mode == "OFF":
            config.SERIES[:] = [s for s in config.SERIES if s != series]
        elif series not in config.SERIES:
            config.SERIES.append(series)
        self._apply_series_roster()
        return (f"room {series} → {mode}. roster now: "
                f"{', '.join(config.SERIES)}; F trades: "
                f"{', '.join(config.f_enabled_series())} "
                f"(global {'LIVE' if config.live_submit_enabled() else 'SHADOW'} "
                "governs all)")

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
        # fallback-audited: a REST poll that REPORTS a touch (rb.yes_bid is not
        # None) proves >=1 lot rests there — the level's existence is the venue's
        # own claim. When the poll omits qty, `or 1` records the minimum the
        # touch already guarantees, never a wall out of nothing (same RULING-3
        # logic as sizing: a real level admits >=1 lot). This REST book is the
        # standing arbiter (book_check), not the sizing read.
        if rb.yes_bid is not None:
            ob.yes_bids[rb.yes_bid] = rb.yes_bid_qty or 1  # fallback-audited: touch proves >=1
        if rb.no_bid is not None:
            ob.no_bids[rb.no_bid] = rb.no_bid_qty or 1  # fallback-audited: touch proves >=1
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

    def _page_f_big_loss(self, per_contract_loss_c: float, market: str,
                         detail: str) -> None:
        """WO-2026-07-26-Q — the LAST silent governor is DELETED. A big F loss no
        longer suppresses F for the day (the money-based rate halt is the ruled
        governor for a RUN of losses; a single loss is noise). It PAGES only —
        information, never a governor — above F_EVENT_TRIPWIRE_C/contract, so the
        operator sees the tail and the loss-clustering datum keeps flowing."""
        if per_contract_loss_c <= config.F_EVENT_TRIPWIRE_C:
            return
        from . import failures
        failures.fail(
            "F_BIG_LOSS",
            f"{market}: F loss {per_contract_loss_c:.0f}c/contract > "
            f"{config.F_EVENT_TRIPWIRE_C}c ({detail}) — noted; the rate halt "
            "governs a RUN, not this single loss (WO-Q: tripwire deleted)",
            fatal=False, alert=True, market=market,
            per_contract_c=round(per_contract_loss_c, 1))

    def _defer_entry(self, proposal, reason: str, ctx: dict) -> bool:
        """WO-2026-07-26-P §B2 — the honest defer. A depth/size read that cannot
        produce a chosen number does NOT fabricate one: the entry sits out ONE
        cycle with a named row ({reason, terms}), and re-proposes next cycle when
        the book may have formed. count=0 is the signal the submit loop skips on.
        Returns True the FIRST time this (market, lane, side, price, reason) is
        deferred in the window (so callers can count once, not every poll)."""
        proposal.count = 0
        key = (proposal.market, proposal.lane, proposal.side,
               proposal.price_cents, reason)
        if key not in self._size_zero_logged:
            self._size_zero_logged.add(key)
            log.info("%s %s %s %s@%dc — deferring one cycle: %s", reason,
                     proposal.lane, proposal.market, proposal.side,
                     proposal.price_cents, ctx)
            try:
                self.surface.write_row(
                    proposal.lane, proposal.market,
                    self._window_of.get(proposal.market, f"w-{proposal.market}"),
                    "WATCHING", detail=json.dumps({"defer": reason, **ctx}))
            except Exception:
                pass
            return True
        return False

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
        # WO-2026-07-26-P §B1/B2 — the TWO-QUESTION depth, no silent fallback.
        # `joining` is contracts AT the exact level (None = blind book, never a
        # fabricated 0). The notional lanes (F/FLIP) that CREATE a level in front
        # of a deep band size to the band they functionally trade, not the empty
        # level — the one-lot bug's fix.
        side, price = proposal.side, proposal.price_cents
        joining = book.joining_depth(side, price)
        if joining is None:                       # §B2: DEPTH_BLIND — defer, don't guess
            self._defer_entry(proposal, "DEPTH_BLIND",
                              {"side": side, "price": price})
            return
        # ── WO-2026-07-26-T Guard 1 — THE COUNTERPARTY-LIQUIDITY GATE ─────────
        # A maker buy fills against the OPPOSITE side. An empty opposite side means
        # the order rests forever (cheap) or — the deeper trap on a thin book —
        # fills into a vanishing book with NO exit liquidity: salvage's maker-first
        # rest (M §S4) has nobody to rest against, the worth-band cut can't execute,
        # and the position rides to settlement with its bound widened from
        # salvageable to TOTAL. Refuse at entry, re-eligible next poll (liquidity
        # returns — not a window kill). Existence, not a threshold: nothing to tune,
        # nothing to tag. Applies to ALL series (Drew: "all — it's free and BTC
        # never triggers it"); scoped to F's entry path (the guard's subject).
        if proposal.lane == "F" and proposal.action == "buy":
            opp_side = "no" if side == "yes" else "yes"
            opp_bid = book.best_no_bid() if side == "yes" else book.best_yes_bid()
            opp_depth = book.joining_depth(opp_side, opp_bid) if opp_bid is not None else 0
            if opp_bid is None or not opp_depth:
                first = self._defer_entry(proposal, "NO_COUNTERPARTY", {
                    "side": side, "opp_side": opp_side, "opp_bid": opp_bid,
                    "yb": book.best_yes_bid(), "nb": book.best_no_bid()})
                if first:   # count once per window — the room's liquidity map
                    from . import failures
                    failures.fail(
                        "NO_COUNTERPARTY",
                        f"{proposal.market}: buy {side} but the opposite ({opp_side}) "
                        f"side is empty (yb={book.best_yes_bid()} nb="
                        f"{book.best_no_bid()}) — no one to fill or EXIT against; "
                        "waiting for liquidity (WO-T Guard 1)",
                        alert=False, series=config.series_of(proposal.market),
                        side=side, hour=int(time.strftime("%H", time.gmtime())))
                return
        # §B1: EVERY lane that creates a level in front of a band sizes to the
        # band it functionally trades, not the empty level. `band` bounded to the
        # price's own tier band (Adversary i); the reference is the larger of the
        # honest joining depth and the band fraction. Both print on the size row.
        w = config.SIZING_BAND_HALFWIDTH_C
        band = book.band_depth(side, max(1, price - w), min(99, price + w))
        band = band if band is not None else 0
        band_ref = int(band * config.BAND_DEPTH_FRACTION)
        depth = max(joining, band_ref)
        # WO-2026-07-26-O §O2: SIZE and the portfolio cap work off TRADEABLE
        # (book − owed), never raw book — the operator's scrape is earmarked and
        # never sized against.
        book_c = self.ledger.tradeable_cents()
        # WO-2026-07-25-K §P3: the FLIP notional is the DESK LADDER's active dial
        # — full only when the desk is conversion-promoted AND this entry's cell
        # margin is non-negative, else tuition. The tier flip pages once (mechanical
        # promotion/demotion, no ruling). Only FLIP self-scales by notional.
        flip_pct = None
        if proposal.lane == "FLIP":
            try:
                from . import flip_ladder
                _s = scoring.score(self.ledger, lane, proposal.price_cents)
                # WO-2026-07-25-L §P4: a THIN cell (n < CELL_THIN_MIN_N) carries
                # NO gate authority — its margin can neither authorize full size
                # nor block it; treat it as absent so the ladder holds tuition.
                cell_margin = None if _s.get("thin") else _s.get("margin")
                flip_pct = flip_ladder.active_notional_pct(
                    self.ledger, cell_margin, now=None,
                    alert_fn=self.telegram.alert)
            except Exception:
                flip_pct = None    # sizing never blocks — tuition is the safe floor
        # WO-2026-07-26-S: F sizes to its ROOM's dial. BTC keeps its earned 24%
        # (f_notional_pct_of("KXBTC15M") == F_NOTIONAL_PCT → byte-identical); a new
        # room is born at 20% until its own record argues. FLIP keeps its ladder pct.
        if proposal.lane == "F":
            size_pct = config.f_notional_pct_of(config.series_of(proposal.market))
        else:
            size_pct = flip_pct
        dec = size_order(book_c, proposal.price_cents, depth,
                         lane=proposal.lane, notional_pct=size_pct)
        proposal.size_tier = tier   # reporting + custody scaling, never a cap
        # WO-2026-07-26-P §B2: a 0 does NOT silently become a 1. If the math
        # produced no size, the proposal DEFERS with the full term set — a 1-lot
        # order may only ever exist because the math said 1.
        if dec.contracts <= 0:
            self._defer_entry(proposal, "SIZE_ZERO_DEFER", {
                "tradeable": book_c, "joining": joining, "band": band,
                "band_ref": band_ref, "depth_used": depth, "sizing": dec.reason})
            return
        proposal.count = dec.contracts
        # §B1 / Article 1: both depth terms + the chosen reference ride the size
        # row, so "why this size?" is answered on the entry card, including N=1.
        proposal.why = ((proposal.why + " · ") if proposal.why else "") + (
            f"size {dec.contracts} [joining={joining}"
            + (f" band={band}→ref={band_ref}" if band is not None else "")
            + f" used={depth}; {dec.reason}]")
        # WO-2026-07-24-G Part 2: the FLIP_SIZE_CAP re-cap that used to sit here
        # is RETIRED — FLIP now scales with the book via sizing.size_order
        # (min(notional, depth)), and the book-proportional at-risk WALL is the
        # backstop. A fixed count here would re-introduce the very governor Part
        # 2 removes (compound growth clamped to linear).
        # WO-2026-07-23-B Part 1 guard (d): F's sizing terms are logged once
        # per (market, price) — kelly_max, depth_max, notional_max, and which
        # bound applied — so "is depth ever real" is answered from the tape,
        # never argued (the reason string carries all three).
        if proposal.lane == "F":
            key = (proposal.market, proposal.price_cents)
            if key not in self._size_zero_logged:
                self._size_zero_logged.add(key)
                log.info("F_SIZE %s @%dc book=%dc → %s at_risk_cap=%dc "
                         "(count=%d)", proposal.market, proposal.price_cents,
                         book_c, dec.reason,
                         config.at_risk_cap_cents("F", book_c), proposal.count)
        # WO-2026-07-24-C Part 4 #7: the size test's DATA. FLIP is lane-aware
        # now (min(kelly, depth, cap)) — log the BINDING term on every entry so
        # the experiment can read whether the 10-cap ever bit or depth was the
        # ceiling the whole time. Same once-per-(market, price) guard as F_SIZE;
        # dec.reason already names the bound ("→ cap/depth/kelly bound").
        if proposal.lane == "FLIP" and proposal.action == "buy":
            key = (proposal.market, proposal.price_cents, "flip_size")
            if key not in self._size_zero_logged:
                self._size_zero_logged.add(key)
                log.info("FLIP_SIZE %s @%dc book=%dc → %s at_risk_cap=%dc "
                         "(count=%d)", proposal.market, proposal.price_cents,
                         book_c, dec.reason,
                         config.at_risk_cap_cents("FLIP", book_c),
                         proposal.count)
        # WO-2026-07-26-Q — guard (b) (F's per-event day-long suppression) is
        # DELETED. It duplicated the money-based rate halt with a cruder rule
        # (one event, calendar-scoped, self-clearing at midnight, no resume
        # lever) and was the last governor nobody could name until it fired. One
        # risk, one governor: the rate halt stays; the duplicate is gone. A big
        # F loss now PAGES (F_BIG_LOSS) and never refuses the next window.
        # WO-2026-07-23-B guard (a) → WO-2026-07-26-S §2: THE ENSEMBLE CAP, the
        # correlated-tail governor. Total SIMULTANEOUS at-risk across ALL rooms
        # (deployed_cents sums every market of every series) may never exceed
        # ENSEMBLE_AT_RISK_PCT of TRADEABLE (book_c is tradeable = book − owed).
        # One summed check ABOVE the lane walls, never replacing them (Adversary
        # iii). An entry is clamped to the room that remains (0 = defers with the
        # why); F-BTC and F-XRP holding different rooms can no longer sum past
        # the one-shock ceiling. Cross-crypto air-pockets flip every favorite at
        # once — this is the board condition that the growth in rooms respects.
        if proposal.action == "buy" and proposal.count > 0 and book_c > 0:
            deployed = self.ledger.deployed_cents()
            room = int(book_c * config.ENSEMBLE_AT_RISK_PCT) - deployed
            max_by_ensemble = room // max(1, proposal.price_cents)
            if max_by_ensemble < proposal.count:
                clamped = max(0, max_by_ensemble)
                log.warning("ENSEMBLE_CAP %s %s: %d→%d lots — deployed %dc across "
                            "all rooms + this would exceed %d%% of tradeable %dc",
                            proposal.lane, proposal.market, proposal.count,
                            clamped, deployed,
                            int(config.ENSEMBLE_AT_RISK_PCT * 100), book_c)
                # §A1 why-on-row: the ensemble ceiling names itself on the size row.
                proposal.why = ((proposal.why + " · ") if proposal.why else "") + (
                    f"ensemble-cap {proposal.count}→{clamped} "
                    f"[deployed={deployed}c ≤{int(config.ENSEMBLE_AT_RISK_PCT*100)}%"
                    f" tradeable={book_c}c]")
                proposal.count = clamped
        # ── WO-2026-07-27-V B2 — THE SANITY CLAMP ─────────────────────────────
        # A phantom book can never spend money the venue already said isn't there.
        # Before submit, an ENTRY's cash cost is checked against the LAST CONFIRMED
        # VENUE CASH (the venue number, not the ledger's belief) with its age. Cost
        # over that cash → DEFER (CASH_SANITY) — the ~$45-withdrawal phantom could
        # never have sized $19 onto $20.70 of real cash without this catching a
        # deeper phantom. A confirmed cash older than CASH_CONFIRM_MAX_AGE_S is not
        # solvent evidence: blind is not solvent — defer everything and page. LIVE
        # only (shadow has no venue cash); a no-op whenever the book agrees with
        # the venue, so BTC/XRP entries are byte-identical on a healthy book.
        if (config.live_submit_enabled() and proposal.action == "buy"
                and proposal.count > 0):
            self._cash_sanity_clamp(proposal)
        # WO-VERIFY-LOSSTERM-1 B4 → WO-2026-07-26-P §B2: the "0 → count=1, let the
        # walls refuse" path is RETIRED. A computed-0 now DEFERS at sizing
        # (_defer_entry, above) with the full term set — the size is never
        # fabricated up to 1 for the walls to catch. This block is unreachable
        # (contracts<=0 returned above); kept as a tombstone for the read-rule.

    def _cash_sanity_clamp(self, proposal) -> None:
        """WO-2026-07-27-V B2: refuse to spend past the last CONFIRMED venue cash.
        Cost > venue cash → CASH_SANITY defer; a stale confirmation (age beyond
        threshold) defers everything and pages (blind is not solvent)."""
        conf = self.ledger.get_state("last_venue_cash_cents")
        ts = self.ledger.get_state("last_venue_cash_ts")
        if conf is None or ts is None:
            return   # no venue read yet (pre-first-reconcile); the boot reconcile owns this
        venue_cash = int(conf)
        age_s = int(time.time() - float(ts))
        cost = proposal.count * proposal.price_cents
        if age_s > config.CASH_CONFIRM_MAX_AGE_S:
            from . import failures
            if self._defer_entry(proposal, "CASH_SANITY", {
                    "reason": "stale_confirmation", "cost": cost,
                    "venue_cash": venue_cash, "age_s": age_s}):
                failures.fail(
                    "CASH_STALE",
                    f"the last confirmed venue cash is {age_s}s old (> "
                    f"{config.CASH_CONFIRM_MAX_AGE_S}s) — blind is not solvent; "
                    "deferring every entry until a fresh reconcile (WO-V B2)",
                    alert=True, age_s=age_s)
            return
        if cost > venue_cash:
            self._defer_entry(proposal, "CASH_SANITY", {
                "reason": "cost_over_venue_cash", "cost": cost,
                "venue_cash": venue_cash, "age_s": age_s})

    def simulate_shadow_fills(self, market, book, now) -> int:
        """WO-2026-07-25-L §P1 — the pessimistic shadow fill sweep. For each
        resting SHADOW order on this market, if it has rested ≥1 poll AND the
        book has traded AT or THROUGH its price (shadow_fill.would_fill), book it
        exactly as a live fill would: gateway.on_fill (which books the cell
        outcome tagged shadow, since the lane is not LIVE) then _on_fill_booked
        (👻 narration + lane custody). NO fills/settlements/cash row is ever
        written — treasury stays live-only. Returns the count booked."""
        from . import shadow_fill
        rested = self._shadow_rested
        booked = 0
        # snapshot: on_fill mutates gateway.resting, so iterate a copy
        for oid, order in list(self.gateway.resting.items()):
            if not oid.startswith("SHADOW-") or order.market != market:
                continue
            rested[oid] = rested.get(oid, 0) + 1
            if not shadow_fill.would_fill(order, book, rested[oid]):
                continue
            remaining = order.count - self.gateway.filled_counts.get(oid, 0)
            if remaining <= 0:
                continue
            filled = self.gateway.on_fill(oid, count=remaining)
            if filled is None:
                continue
            action = "ENTRY" if order.purpose == "ENTRY" else (
                "CUSTODIAN_EXIT" if order.purpose == "CUT" else "EXIT")
            self._on_fill_booked(order, action, order.price_cents, remaining,
                                 now, shadow=True)
            rested.pop(oid, None)
            booked += 1
        return booked

    def bank_scrape_and_watch(self) -> int:
        """WO-2026-07-26-O §O3/O4 — bank the high-water scrape (silent unless a
        milestone crosses → 💰), then guard OWED_UNDERWATER: if tradeable ever
        falls below one F lot, the desk has earmarked more than it can trade with
        → page + halt entries until it recovers or the operator withdraws (which
        reconciles owed down). Returns cents minted this bank."""
        minted = self.ledger.bank_scrape()
        if minted > 0:
            owed = self.ledger.owed_cents()
            self.telegram.alert(
                f"💰 MILESTONE — banked ${minted / 100:.2f} for you (new "
                f"high-water). Owed now ${owed / 100:.2f}; tradeable "
                f"${self.ledger.tradeable_cents() / 100:.2f}. Silent and patient "
                "until you reach for it (/owed).")
        tradeable = self.ledger.tradeable_cents()
        if tradeable < config.ONE_F_LOT_COST_C:
            if "OWED_UNDERWATER" not in self.gateway.entries_halted_reasons:
                from . import failures
                self.gateway.halt_entries("OWED_UNDERWATER")
                failures.fail(
                    "OWED_UNDERWATER",
                    f"tradeable {tradeable}c < one F lot "
                    f"({config.ONE_F_LOT_COST_C}c) — owed {self.ledger.owed_cents()}c "
                    "earmarks more than the desk can trade a favorite with; "
                    "entries HALTED until equity recovers or you withdraw (/owed)",
                    fatal=False, alert=True)
        else:
            self.gateway.resume_entries("OWED_UNDERWATER")
        return minted

    def _on_fill_booked(self, order, action, price_cents, count, now,
                        fee_cents=0, shadow=False):
        """P13 §1: fill pages say WHAT and WHY. A scratch-sell must never
        read like a second buy — on live money, mute narration is
        indistinguishable from inversion. WO-2026-07-25-L §P1: a SHADOW
        (rehearsed) fill is marked 👻 on every line — a rehearsal is never
        mistaken for money."""
        gh = "👻 " if shadow else ""
        if action == "ENTRY":
            why = f" — why: {order.why}" if order.why else ""
            self.telegram.alert(
                f"{gh}✅ ENTRY {order.lane} {order.market} "
                f"{order.action} {order.side}@{price_cents}¢ x{count}{why}")
            # §3 (P12 §4): 30s post-entry divergence watch, armed per entry
            self.divergence_watches[order.market] = {
                "until": now + 30.0, "strikes": 0}
        else:
            reason = order.reason or ("custodian cut"
                                      if action == "CUSTODIAN_EXIT" else "exit")
            fee_s = f" (fee {fee_cents}¢)" if fee_cents else ""
            self.telegram.alert(
                f"{gh}✂️ EXIT {order.lane} {order.market} "
                f"{order.action} {order.side}@{price_cents}¢ x{count}{fee_s}"
                f" — {reason}")
            # WO-2026-07-24-H "ONE POSITION, ONE STORY": the ↔ line reads the
            # POSITION, not the fills table. gateway.on_fill has already booked
            # this exit's effect, so the position state below is post-fill: if it
            # is now FLAT, the whole story concluded (CLOSED with blended math);
            # if contracts remain, this was a PARTIAL and the line says how many
            # ride. (The old line selected ONE entry fill and declared a
            # round-trip on any partial — 10/17 riding read as "concluded".)
            key = (order.event, order.market, order.lane)
            flat = (self.gateway.positions.get(key, 0) == 0
                    and self.gateway.gross_open.get(key, 0) == 0)

            def s(v):
                return f"{'+' if v > 0 else ''}{int(round(v))}"
            if not flat:
                # a partial: GOOD news, honestly narrated — x banked, R riding
                basis = self.gateway.pos_basis.get(key)
                riding = self.gateway.gross_open.get(key, 0)
                b_s = f" (basis {basis}¢)" if basis is not None else ""
                this = (price_cents - basis) * count if basis is not None else None
                pnl_s = f" {s(this)}¢" if this is not None else ""
                self.telegram.alert(
                    f"{gh}↔ {order.market} {order.lane} PARTIAL x{count} @{price_cents}¢"
                    f"{b_s}{pnl_s} — {riding} riding")
            else:
                # concluded: the position's accrued totals ARE the story
                c = self.gateway.last_concluded.get(key)
                if c is not None:
                    # the position's accrued totals ARE the story (the cell
                    # outcome was booked at conclusion inside gateway.on_fill —
                    # P2; this surface just tells it).
                    self.telegram.alert(
                        f"{gh}↔ {order.market} {order.lane} ROUND-TRIP CLOSED "
                        f"x{c['count']} basis {c['basis']}¢ → avg exit "
                        f"{c['avg_exit']}¢, net {s(c['net_cents'])}¢ "
                        f"(+ fee {c['fees_cents']}¢)")
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
        # WO-2026-07-26-Q: the last silent governor's migration. Guard (b) (the
        # day-long F suppression) is deleted; a live `f_tripwire_day` flag left in
        # the DB would otherwise keep refusing F until local midnight — the bug
        # surviving its own funeral. Clear it on boot so this deploy resumes F.
        if self.ledger.clear_f_tripwire_migration():
            print("MIGRATION (WO-Q): cleared a live f_tripwire_day flag — the "
                  "deleted day-long F suppression can no longer refuse F; the "
                  "money rate halt is the ruled governor", flush=True)
        # WO-2026-07-26-S §1: apply the rooms — widen F's family to the enabled
        # roster and migrate the halt keys if a second room boots in.
        self._apply_series_roster()
        # P8 §2.3: restarts and redeploys do NOT clear the two-strike halt
        self.econ.restore_halt_on_boot()
        # P17 §1.3/§1.4: heal across restarts — reload open brackets (so the
        # settle sweep retries stuck windows) and mark evidence holes.
        self.econ.restore_open_brackets_on_boot()
        # P-CASH-FATAL-1 §4.5 (defense in depth): every durable stop the DB
        # knows must be honored on the wall before the first cycle — or
        # FATAL loud rather than trade.
        print(self.audit_durable_stops(), flush=True)
        # WO-2026-07-24-G acceptance #6: every self-scaling lane's dial must sit
        # STRICTLY under its wall — a dial >= wall fights its own wall (every
        # rounding edge → WALL reject + backoff). FATAL loud before the first
        # cycle rather than trade into a self-throttling misconfiguration.
        violations = config.dial_wall_violations()
        if violations:
            named = "; ".join(f"{ln} dial {d:.0%} >= wall {w:.0%}"
                              for ln, d, w in violations)
            failures.fail(
                "DIAL_OVER_WALL",
                f"sizing dial not under its at-risk wall ({named}) — the dial "
                "would fight its own wall, converting every rounding edge into a "
                "WALL reject; refusing to trade a self-throttling config",
                fatal=True)
        else:
            print("DIAL<WALL SELF-TEST: "
                  + " · ".join(f"{ln} {d:.0%}<{config.AT_RISK_PCT[ln]:.0%}"
                               for ln, d in config.DIAL_OF_LANE.items())
                  + " — every dial under its wall", flush=True)
        # WO-2026-07-25-L §P1b (ADVERSARY ii) — SHADOW ISOLATION, asserted at
        # boot. Treasury (book_cents/lifetime = settlements+cash) and tradeable
        # capital (deployed_cents = unsettled fills) NEVER read cell_outcomes, and
        # a SHADOW lane's fill is FATAL-refused at record_fill — so simulated
        # money cannot reach real capital. FATAL loud if the treasury SUMs ever
        # grow a cell_outcomes/shadow reference (a leak), else name the rail.
        import inspect as _inspect
        treasury_src = (_inspect.getsource(self.ledger.book_cents.__func__)
                        + _inspect.getsource(self.ledger.lifetime_pnl_cents.__func__)
                        + _inspect.getsource(self.ledger.deployed_cents.__func__))
        if "cell_outcomes" in treasury_src or "shadow" in treasury_src:
            failures.fail(
                "SHADOW_LEAK_INTO_TREASURY",
                "a treasury/tradeable-capital query references cell_outcomes or a "
                "shadow row — simulated P&L must never touch real capital",
                fatal=True)
        else:
            _shadow_lanes = [ln for ln, m in config.LANE_MODE.items()
                             if m != "LIVE"]
            print("SHADOW-ISOLATION SELF-TEST: treasury reads settlements+cash "
                  "only, deployed reads live fills only; a shadow-lane fill is "
                  f"FATAL-refused (rail armed). Rehearsing: {','.join(_shadow_lanes)}"
                  " — their P&L is cell_outcomes(shadow=1), never tradeable",
                  flush=True)
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
        # WO-2026-07-26-O §O1/Step-0: SEED THE SCRAPE at the RESTATED trading
        # equity (owed starts at $0), ONCE, with a Telegram announcement — from
        # here $5 of every true $10 of new high-water is banked for the operator.
        seed_c = self.ledger.seed_scrape()
        self.page_once(
            "page_scrape_seeded",
            f"💰 SCRAPE seeded at ${seed_c / 100:.2f} trading equity (RESTATED) — "
            f"owed $0.00; from here ${config.SCRAPE_PER_MILESTONE_C / 100:.0f} of "
            f"every ${config.SCRAPE_MILESTONE_C / 100:.0f} of new high-water banks "
            "for you. Silent, patient, until you reach for it (/owed).")
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
            # COLD AUDIT build 70 §2: keep the pv component (the summed total
            # threw it away — the phantom's origin). standing_reconcile reads it
            # to refuse a reconcile taken across a settlement boundary.
            self._last_venue_pv_cents = int(round((pv or 0.0) * 100))
            # WO-2026-07-27-V B2: stamp the last CONFIRMED venue CASH (not the
            # total) with its age on the ledger, so the pre-submit sanity clamp
            # and the balance-rejected page can refuse to spend money the venue
            # already said isn't there — a phantom book can never outrun this.
            cash_c = int(round(cash * 100))
            self.ledger.set_state("last_venue_cash_cents", str(cash_c))
            self.ledger.set_state("last_venue_cash_ts", str(now))
            return int(round((cash + (pv or 0.0)) * 100)), "venue"
        self._av_fail_streak += 1
        # WO-2026-07-24-D Part 4: a FAILED read must INVALIDATE the pv, not
        # preserve it. `_last_venue_pv_cents` refreshed only on success, so a
        # value read while holding a position stayed non-zero after the position
        # cleared — pinning the reconcile's pv-consistency gate on a stale number
        # so it deferred forever. None means "no trustworthy pv" (the reconcile
        # then defers as RECON_NO_PV, distinguishable from a real boundary).
        self._last_venue_pv_cents = None
        failures.fail("ACCOUNT_VALUE_UNREADABLE",
                      f"live account value read failed "
                      f"(attempt {self._av_fail_streak}/{self.AV_PAGE_AT_STREAK})",
                      attempt=self._av_fail_streak)
        if self._av_fail_streak == self.AV_PAGE_AT_STREAK:
            self.telegram.alert(
                "💸 ACCOUNT VALUE UNREADABLE x3 over 15s — brackets DEFER until "
                "the venue answers; no paper number will substitute")
        return None, "venue"

    def bracket_book(self, now=None):
        """WO-2026-07-23 COLD READ (build 67): the window-econ bracket value is
        the LEDGER book, not the venue account read.

        The bug: `account_value()` sums venue `cash + position_value`, and those
        two settle on DIFFERENT clocks — at ENTRY the cash is already debited
        while the position isn't yet reflected (reads LOW by the entry notional),
        at SETTLEMENT the cash is credited while the position isn't yet cleared
        (reads HIGH by the same notional). Differencing an OPEN read against a
        CLOSE read put that notional into `window_pnl` twice, same sign — the
        +819c-vs-true-+24c phantom. The ledger book is computed from fills that
        already reconcile (cash_movements + settlements), so it is internally
        consistent at every instant. The venue read stays — for `standing_
        reconcile` only (P9 §3, a cadence AWAY from fills/settlements), which is
        what it is actually good for. Source is "ledger": a REAL reconciled
        number, never a shadow "paper" fabrication (the live invariant still
        forbids "paper")."""
        return self.ledger.book_cents(), "ledger"

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
        econ = getattr(self, "econ", None)
        if econ is not None:
            for lane in sorted(econ.halted_lanes()):
                stops.append((f"{lane} rate-halt",
                              f"{HALT_REASON}:{lane}" in wall))
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
        from . import failures
        now = time.time() if now is None else now
        val, src = self.account_value(now)
        if val is None or src != "venue":
            # WO-2026-07-24-D Part 4: an unreadable venue is an un-cross-checked
            # cycle — it counts toward the stall, same as a defer. Silence here
            # is exactly the failure this WO is closing.
            return self._recon_note_deferred("UNREADABLE", now)
        # WO-INFRA-HARDENING E1 — the reconcile-side source trail: when the
        # book disagrees with the venue and there is NOTHING pending to explain
        # it (no unsettled fills, no resting orders), that is the phantom
        # signature. Record it durably (alert=False — the cash protocol below
        # still owns the page/halt) so the divergence is timestamped against
        # the SETTLE_AUDIT trail instead of eyeballed later.
        unsettled = self.ledger.unsettled_fill_count()
        resting = len(self.gateway.resting)
        book = self.ledger.book_cents()
        # COLD AUDIT build 70 §2 — THE SOURCE FIX (was: 4 builds patching where
        # the bad number LANDS). The venue reads cash and pv on different clocks;
        # a read taken across a settlement boundary is wrong by the position
        # notional. Refuse to reconcile on it: the venue's pv must agree with the
        # engine's own open-position notional (`deployed_cents` — already in the
        # ledger, never called by the cash path until now). This makes it
        # IMPOSSIBLE to compute the delta on an internally inconsistent read, not
        # merely unlikely. Deferring is safe — the reconcile retries next cycle
        # and runs cleanly every flat window (both sides ~0).
        pv_cents = getattr(self, "_last_venue_pv_cents", 0)
        deployed = self.ledger.deployed_cents()
        # WO-2026-07-24-D Part 4: a None pv is a FAILED read invalidating itself
        # (account_value cleared it) — defer as RECON_NO_PV, distinguishable from
        # a real settlement boundary and never an `abs(None - deployed)` crash.
        if pv_cents is None:
            log.info("RECON_DEFERRED live: venue pv unavailable (last read "
                     "failed) — deferring as RECON_NO_PV until a clean read")
            return self._recon_note_deferred("RECON_NO_PV", now)
        if abs(pv_cents - deployed) > config.PV_TOLERANCE_C:
            log.info("RECON_DEFERRED %s: venue pv %dc vs deployed %dc "
                     "(gap %+dc > %dc) — cash/pv read across a settlement "
                     "boundary; reconcile waits for a consistent read",
                     "live", pv_cents, deployed, pv_cents - deployed,
                     config.PV_TOLERANCE_C)
            return self._recon_note_deferred("PV_BOUNDARY", now)
        if (unsettled == 0 and resting == 0
                and abs(book - val) > config.RECON_AUDIT_FLOOR_CENTS):
            failures.fail(
                "RECON_BOOK_VENUE_DELTA",
                f"book {book}c vs venue {val}c delta {book - val:+d}c with 0 "
                "unsettled / 0 resting — an unexplained divergence; the E1 "
                "SETTLE_AUDIT trail names which settlement moved the book",
                alert=False, book_cents=book, venue_cents=val,
                delta_cents=book - val)
        result = self.cash.reconcile(
            venue_balance_cents=val,
            in_flight_orders=resting,
            unsettled_fills=unsettled,
            now=now)
        # a completed cross-check (OK / PROMPTED / REBASED) verifies the book
        # against the venue — reset the stall. A cash-protocol DEFERRED here is
        # a benign quiescence hold (in-flight/unsettled), not an un-cross-checked
        # cycle: the venue read WAS clean+consistent, so it does not advance the
        # stall streak, but it is not a completed reconcile either.
        if result != "DEFERRED":
            self._recon_last_ok_ts = now
            self._recon_deferred_streak = 0
            self._recon_starved_paged = False   # re-arm the backstop on a clean check
        else:
            # WO-2026-07-27-V T1 — THE SLEEPING-SENTINEL BACKSTOP. A cash-protocol
            # DEFERRED here is a QUIESCENCE hold (resting/unsettled busy). The old
            # code called it benign and never advanced the stall — so a two-room
            # engine that never goes quiet reconciled NEVER, silently, and a ~$45
            # withdrawal walked past the sentinel. Quiescence starvation is NOT
            # benign: a book unverified against the venue for RECON_MAX_QUIET_S,
            # for ANY reason, PAGES. The venue read WAS clean here (we got past
            # the defers above), so this is purely "too busy to ever check".
            self._recon_check_starved(now)
        return result

    def record_recon_cycle(self, result: str, now=None) -> None:
        """WO-2026-07-27-V T1 — the sentinel-cadence measurement (acceptance #4).
        Persist each reconcile cycle's result so the pack can plot clean reads per
        hour: if the sentinel has been part-time (quiescence starvation) for
        longer than tonight, the cadence line shows it. Bounded — pruned to 2 days."""
        now = time.time() if now is None else now
        try:
            db = self.ledger.db
            db.execute("CREATE TABLE IF NOT EXISTS recon_cycles ("
                       "ts REAL NOT NULL, result TEXT NOT NULL)")
            db.execute("INSERT INTO recon_cycles (ts, result) VALUES (?,?)",
                       (now, result))
            db.execute("DELETE FROM recon_cycles WHERE ts < ?", (now - 2 * 86400,))
            db.commit()
        except Exception:
            pass   # the measurement never blocks the reconcile

    def _recon_check_starved(self, now: float) -> None:
        """WO-2026-07-27-V T1: page RECON_STARVED when the book has not been
        verified against the venue for RECON_MAX_QUIET_S — covering the
        quiescence starvation the stall-streak never counted. Once per episode
        (re-armed by the next completed reconcile)."""
        last_ok = self._recon_last_ok_ts
        quiet_for = (now - last_ok) if last_ok is not None else None
        if quiet_for is not None and quiet_for > config.RECON_MAX_QUIET_S \
                and not getattr(self, "_recon_starved_paged", False):
            self._recon_starved_paged = True
            from . import failures
            failures.fail(
                "RECON_STARVED",
                f"the book has not been verified against the venue in "
                f"{int(quiet_for)}s (> {config.RECON_MAX_QUIET_S}s) — the rooms "
                "have stayed too busy to ever reach a quiescent reconcile; the "
                "cash sentinel is effectively asleep. Sizing may be running on a "
                "stale book — restart to force a boot reconcile (WO-V T1)",
                fatal=False, alert=True, quiet_s=int(quiet_for))

    def _recon_note_deferred(self, reason: str, now: float) -> str:
        """WO-2026-07-24-D Part 4: a deferred/unreadable cycle is one where the
        book was NOT cross-checked against the venue. Count the run; a run this
        long PAGES (RECON_STALLED) — "nothing pending" must never be silently
        indistinguishable from "not checked in an hour". Returns the reason so
        the caller's status is preserved."""
        from . import failures
        self._recon_deferred_streak += 1
        if self._recon_deferred_streak == config.RECON_STALL_STREAK:
            failures.fail(
                "RECON_STALLED",
                f"no clean venue cross-check for {self._recon_deferred_streak} "
                f"consecutive reconcile cycles (latest: {reason}) — the book's "
                "only check against the venue has stopped running; it may have "
                "been unverified for a while",
                fatal=False, alert=True, reason=reason,
                streak=self._recon_deferred_streak)
        return reason if reason in ("UNREADABLE",) else "DEFERRED"

    def recon_status_line(self, now=None) -> str:
        """WO-2026-07-24-D Part 4: the hourly's reconcile-health field — a stale
        cross-check is visible without a log grep. recon_ok is seconds since the
        last CLEAN reconcile (— = never yet); recon_deferred is the current
        consecutive un-cross-checked run."""
        now = time.time() if now is None else now
        if self._recon_last_ok_ts is None:
            ago = "—"
        else:
            ago = f"{int(now - self._recon_last_ok_ts)}s"
        return f"recon_ok={ago} recon_deferred={self._recon_deferred_streak}"

    def listener_status(self) -> str:
        """P9 §1c: the hourly line's `listener:` field."""
        n = self.task_restarts.get("listener", 0)
        if self.task_alive.get("listener", True):
            return "ok" if n == 0 else f"ok({n} restarts)"
        return f"down({n} restarts)"

    def _build_and_send_daily(self, text: str = "") -> str:
        """WO-2026-07-22-K: build the day's read-only .xlsx bundle in /tmp, send
        it to Telegram as a document, and delete it. Off the trading path — a
        `mode=ro` connection to the ledger DB (never locks the settle path).
        `/daily N` pulls N days back; default today. Never raises."""
        import os
        from . import daily_bundle, scoring
        parts = text.split()
        days_back = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        fname = daily_bundle.daily_filename(days_back=days_back)
        out = os.path.join("/tmp", fname)
        try:
            sb = scoring.scoreboard_lines(self.ledger)
            res = daily_bundle.build_daily_workbook(
                self.ledger.db_path, sb, out, days_back=days_back)
            size_mb = os.path.getsize(out) / 1e6
            caption = (f"{fname} — {res['sheets']} sheets, {res['rows']} rows, "
                       f"{size_mb:.1f}MB{res['note']}")
            sent = self.telegram.send_document(out, caption)
            return caption if sent else f"{fname} built ({size_mb:.1f}MB) but " \
                "the send failed — see the log"
        except Exception as e:
            log.warning("[DAILY] export failed: %s", e)
            return f"daily export failed: {e}"
        finally:
            try:
                os.remove(out)
            except OSError:
                pass

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
        # WO-2026-07-26-T Guard 2: this market's series' physics or BLIND.
        ps = delta.p_survive(d, t_rem, series=config.series_of(market))
        if ps is None:
            return "table"     # delta-table cell miss (or a foreign-series BLIND)
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
        # WO-2026-07-24-D Part 3: the halt is about OUR BOOK's orientation, not
        # one market's. Recovery was PINNED to the halting market — but that
        # market expires within minutes and `feed.books.get(mkt)` then yields
        # nothing, so `ours` is None and the clean-read condition can NEVER be
        # satisfied again (161 minutes of dead time, twice, each cleared only by
        # the operator's key). Recover on the FIRST currently-open market that
        # reads clean; and a hard ceiling pages if the halt is genuinely stuck.
        if "ORIENTATION_DIVERGENCE" in self.gateway.entries_halted_reasons:
            recovered_on = None
            # the halting market first (still cheap if it is live), then any
            # other currently-tracked (i.e. not-yet-pruned) live market.
            candidates = [self._orientation_halt_market] if \
                self._orientation_halt_market is not None else []
            candidates += [m for m in sorted(self.feed.books)
                           if m != self._orientation_halt_market]
            for mkt in candidates:
                book = self.feed.books.get(mkt)
                ours = book.best_yes_bid() if book is not None else None
                if ours is None:
                    continue
                fresh = self._fresh_record_touches(mkt)
                fbid = fresh[0] if fresh is not None else None
                if fbid is not None and abs(ours - fbid) <= 3:
                    recovered_on = (mkt, ours, fbid)
                    break
            if recovered_on is not None:
                mkt, ours, fbid = recovered_on
                self.gateway.resume_entries("ORIENTATION_DIVERGENCE")
                self._orientation_halt_market = None
                self._orientation_halt_ts = None
                self.telegram.alert(
                    f"✅ ORIENTATION recovered on {mkt}: fresh record y{fbid}¢ "
                    f"agrees with ours y{ours}¢ (≤3¢) — entries re-enabled "
                    "automatically (WO-2026-07-24-D Part 3: any live market, "
                    "not the expired one that tripped it)")
                log.warning("ORIENTATION_DIVERGENCE auto-cleared on %s: "
                            "fresh y%s vs ours y%s", mkt, fbid, ours)
            elif (self._orientation_halt_ts is not None
                  and now - self._orientation_halt_ts
                  > config.ORIENTATION_HALT_MAX_S):
                # the promise (auto-recovery) could not be kept within the
                # ceiling — page LOUD rather than sit silent behind a dead
                # market, and re-arm the ceiling so it does not spam every cycle.
                self._orientation_halt_ts = now
                failures.fail(
                    "ORIENTATION_HALT_STUCK",
                    f"entries have been ORIENTATION_DIVERGENCE-halted for "
                    f">{int(config.ORIENTATION_HALT_MAX_S)}s with no live market "
                    "reading clean — the halting market likely expired; needs "
                    "/reset_halt or a look",
                    fatal=False, alert=True,
                    halt_market=self._orientation_halt_market,
                    ceiling_s=config.ORIENTATION_HALT_MAX_S)
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
            # WO-2026-07-26-R: the watch asks its OWN question. It halts ONLY on
            # what actually means "our book read can't be trusted": an INVERSION
            # (the mirror signature the codebase already owns) or a GROSS non-
            # mirror gap. A small sub-gross offset is the market-summary endpoint
            # LAGGING the orderbook by a spread on a quiet book — freshness noise
            # wearing an orientation alarm (tonight's two halts). That demotes to
            # BOOK_STALE info + a resync request, counted by hour, NEVER a halt.
            offset = abs(ours - rec)
            inversion = self._mirror_signature(ours, rec)
            gross = (not inversion
                     and offset >= config.ORIENTATION_GROSS_DIVERGENCE_C)
            if inversion or gross:
                w["strikes"] += 1
                kind = ("INVERSION (mirror signature)" if inversion
                        else f"GROSS ≥{config.ORIENTATION_GROSS_DIVERGENCE_C}¢ "
                             "non-mirror")
                if w["strikes"] >= 3:
                    del self.divergence_watches[market]
                    self.gateway.halt_entries("ORIENTATION_DIVERGENCE")
                    self._orientation_halt_market = market
                    self._orientation_halt_ts = now   # Part 3: arm the ceiling
                    self.telegram.alert(
                        f"⛔ ORIENTATION_DIVERGENCE {market}: ours y{ours}¢ vs "
                        f"FRESH record y{rec}¢ — {kind} x3 — entries HALTED "
                        "(auto-recovers on the first live market that reads "
                        "clean; pages if stuck)")
                    failures.fail("ORIENTATION_DIVERGENCE",
                                  f"{market}: ours {ours}¢ vs FRESH record "
                                  f"{rec}¢ — {kind} on 3 consecutive checks "
                                  f"post-entry",
                                  market=market, ours=ours, record=rec,
                                  kind=kind, offset=offset, alert=False)
            elif offset > config.BOOK_STALE_OFFSET_C:
                # Endpoint lag, not inversion. A stale read is AFFIRMATIVE
                # evidence this is not an inverted book — reset the halt strikes,
                # log both values (Article 1), request a resync, count for the
                # pack. A 4¢ offset can never be an inverted book (Adversary).
                w["strikes"] = 0
                self.feed.resync_needed.add(market)
                failures.fail(
                    "BOOK_STALE",
                    f"{market}: ours y{ours}¢ vs FRESH record y{rec}¢ (offset "
                    f"{offset}¢, sub-gross, non-mirror) — the summary endpoint "
                    "lags the orderbook by a spread on a quiet book; resync "
                    "requested, entries continue (WO-R: not a halt)",
                    market=market, ours=ours, record=rec, offset=offset,
                    hour=int(time.strftime("%H", time.gmtime(now))),
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

        # WO-2026-07-24-H P3: the standing assert — no resting EXIT may exceed
        # its position's live count (a take oversized by a later partial fill).
        self.gateway.check_exit_oversize()

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
            # WO-2026-07-25-L §P1: the pessimistic SHADOW fill sweep — every
            # resting SHADOW order on this market is filled ONLY when the healthy
            # book trades through its price after rest. Runs here (post book-health
            # guard, pre-evaluation) so a rehearsed fill feeds custody the same
            # cycle a live fill would. Zero broker traffic; treasury untouched.
            try:
                self.simulate_shadow_fills(market, book, now)
            except Exception as e:
                log.warning("shadow fill sweep error on %s: %s", market, e)
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
                    sl = _sl.needle(anchor, spot, strike, close_for - now,
                                    series=config.series_of(market))  # WO-T Guard 2
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
                        # WO-2026-07-26-P §B2: a deferred entry (DEPTH_BLIND /
                        # SIZE_ZERO_DEFER) carries count=0 — it does NOT go out at
                        # a fabricated 1. It sits this cycle and re-proposes next.
                        if proposal.count <= 0:
                            continue
                    # WO-HALT-ORPHAN §2B: an ENTRY the budget wall already
                    # refused this window is not re-submitted — the wall
                    # can't change within a window (byte-identical book).
                    br_key = (market, lane.name, proposal.side,
                              proposal.price_cents)
                    if (proposal.purpose == "ENTRY"
                            and br_key in self._budget_rejected):
                        continue
                    # WO-2026-07-24-D Part 5: the moment a lane's gate is open,
                    # release its closed-gate latch so the NEXT halt counts fresh.
                    _scope = config.halt_scope(config.series_of(market), lane.name)
                    if not self.gateway.entries_halted_for(_scope):
                        self._halt_reject_latched.discard((lane.name, market))
                    try:
                        result = self.gateway.submit(proposal, book)
                    except WallRejection as e:
                        # WO-2026-07-24-D Part 5: a closed-gate refusal (entries
                        # halted) is not a wall bug — a halted lane re-proposes
                        # every poll (~5/min). Count and log it ONCE per (lane,
                        # market) until the gate opens, so hundreds of polls of
                        # noise cannot bury a real reject in the `rejects=` field.
                        if e.wall == "ENTRIES_HALTED":
                            lk = (lane.name, market)
                            if lk in self._halt_reject_latched:
                                continue          # already noted; wait for open
                            self._halt_reject_latched.add(lk)
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
                        # market. COLD READ (build 67): the bracket value is the
                        # LEDGER book — consistent at entry, where the venue read
                        # is low by the entry notional (cash debited, position not
                        # yet reflected).
                        val, src = self.bracket_book(now)
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
                    market=market, kind="settle", now=now_eff,
                    contracts=s["net"],   # WO-2026-07-24-G Part 4: per-contract
                    shadow=config.lane_books_shadow(lane))  # WO-L: live-fills only, tagged for safety
                # WO-2026-07-26-Q: an F position that rode to settlement and LOST
                # paid its full entry per contract. The day-long tripwire this
                # once fed is DELETED — the loss PAGES (F_BIG_LOSS, information
                # only) and never suppresses the next window.
                if lane == "F" and not won:
                    self._page_f_big_loss(float(s["entry"]), market,
                                          "held to settlement (unsalvaged)")
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
        # COLD READ (build 67): close on the LEDGER book, not the venue read —
        # at settlement the venue reads HIGH by the entry notional (cash
        # credited, position not yet cleared). The ledger book already carries
        # this window's settlement (recorded above), so the close − open delta
        # IS the realized window P&L, phantom-free, and the reported "book" is
        # finally the real book.
        val, src = self.bracket_book(now)
        self.econ.close_bracket(
            market, val, fills_pnl,
            lanes_active=",".join(sorted(per_lane)), fills_count=fills_count,
            now=now, source=src, late=late, per_lane=per_lane)

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
        f"{sizing_line(engine.ledger.book_cents(), engine.ledger.owed_cents())}\n"
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
            # WO-INSTRUMENTATION-AND-FLIP-TIMING (build 51): the full pack must
            # fire EVERY day — the old `hour == 9` was a 1-hour window a deploy
            # or restart after 9am missed entirely (why it didn't fire today).
            # `hour >= 9`, once per calendar day, on the first hourly tick at or
            # after 9am ET: a post-9am restart still delivers the day's pack.
            # It reads the DB (post-reconcile truth), never a deploy grade.
            if now_et.hour >= 9 and last_full_day != now_et.date():
                last_full_day = now_et.date()
                engine.telegram.alert(pack)  # the daily full pack, on the phone
            else:
                from . import delta as _delta   # P26 §1.2: brain on the line
                # WO-2026-07-25-K §P3: the desk-size ladder ticks every hour too
                # (a demotion/promotion fires even in a quiet hour), and the
                # conversion + live dial ride the hourly line.
                from . import flip_ladder
                flip_ladder.evaluate_size_tier(
                    engine.ledger, alert_fn=engine.telegram.alert)
                # WO-2026-07-26-O §O3: bank the scrape silently; announce 💰 only
                # when a milestone crosses; the owed/tradeable ride the line.
                engine.bank_scrape_and_watch()
                engine.telegram.alert(
                    f"📗 hourly: book={engine.ledger.book_cents()}c "
                    f"owed={engine.ledger.owed_cents()}c "
                    f"tradeable={engine.ledger.tradeable_cents()}c "
                    f"windows={engine.windows_seen} "
                    f"markets={len(engine.market_meta)} "
                    f"orders={len(engine.gateway.order_index)} "
                    f"foreign={engine.fills.foreign_seen} "
                    f"streak={engine.econ.streak} "
                    f"frames={engine.feed.frames_seen} "
                    f"rejects={sum(engine.gateway.reject_counts.values())} "
                    f"venue_rejects={engine.gateway.venue_rejects} "
                    f"{engine.recon_status_line()} "
                    f"listener={engine.listener_status()} "
                    f"brain={'ok' if _delta.is_loaded() else 'absent'} "
                    f"book✓ {engine.book_checks_total} "
                    f"fetch_fail={getattr(engine.feed, 'failed_fetches', 0)} "
                    f"failures={engine.ledger.db.execute('SELECT COUNT(*) FROM failures').fetchone()[0]} "
                    + flip_fill_rate_hourly(engine.ledger)
                    + " · " + flip_ladder.ladder_line(engine.ledger))

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
                _r = await asyncio.to_thread(engine.standing_reconcile)
                engine.record_recon_cycle(_r)   # WO-V T1: cadence measurement

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
                        + f"\n{sizing_line(engine.ledger.book_cents(), engine.ledger.owed_cents())}\n"
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
