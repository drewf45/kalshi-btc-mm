# f_worker/desk.py
# The window loop: discover the next 15M market, run the state machine once, book it,
# move on. LIVE FROM BOOT (Drew's ruling): no shadow phase — the bundle floor bounds
# the tuition, so we risk $1 at a time (1-lot bundles) from the first bell.
#
# The desk does IO and clock; it makes NO trading decisions of its own — every choice
# is delegated to pricebrain (gate/prices), the manager (flips/salvage/flat/booking)
# and fgateway (the walls). That keeps the correctness in the unit-tested components.

import time
from typing import Any, Dict, Optional

from . import window as W
from .config import Config
from .lib.marketutil import (parse_best_yes_no, parse_fill, current_window_ticker,
                             next_window_ticker, throttled_print)


class TokenBucket:
    """Simple token bucket so the discovery spin lives under req/min (F3.3). Bursts up to
    `capacity` are fine (catching a bell); sustained draw is capped at `refill_per_min`.
    The exchange relationship is a position — every request path lives under a budget."""

    def __init__(self, capacity: int, refill_per_min: int, now: Optional[float] = None):
        self.capacity = float(max(1, capacity))
        self.tokens = self.capacity
        self.refill_per_sec = max(0.001, refill_per_min / 60.0)
        self._last = now if now is not None else time.time()

    def allow(self, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        self.tokens = min(self.capacity, self.tokens + (now - self._last) * self.refill_per_sec)
        self._last = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


class Desk:
    def __init__(self, client: Any, gateway: Any, manager: Any, pricebrain: Any,
                 ledger: Any, config: Config, notifier: Any = None, settler: Any = None):
        self.client = client
        self.gw = gateway
        self.mgr = manager
        self.pb = pricebrain
        self.ledger = ledger
        self.cfg = config
        self.notifier = notifier
        self.settler = settler
        self._last_window_id: Optional[str] = None
        self._last_close_ts: Optional[int] = None
        self._cur_market: Optional[str] = None
        self._cur_win: Optional["W.Window"] = None
        self._err_seen: Dict[tuple, Dict[str, float]] = {}
        self._starve: int = 0                      # consecutive empty idle ticks (F3.2)
        self._disc_bucket = TokenBucket(config.req_per_min, config.req_per_min)  # F3.3
        self._births_seen: set = set()             # tickers whose birth latency is logged
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def current_market(self) -> Optional[str]:
        return self._cur_market

    # ---- loud-tape law: no error reaches the loop without a tagged row + a phone line ----
    def _stage_err(self, win: Optional["W.Window"], stage: str, e: Exception,
                   throttle: float = float("inf"), now: Optional[float] = None) -> None:
        """Record a STAGE_ERR evidence row and page Drew. Per-window stages report once
        (window_id keys are unique); desk-level stages (discovery) throttle by time so a
        dead feed pages periodically, not every second. A throttled alert CARRIES ITS
        RATE (F3.4: `×N in Ts`) — suppression without a count is how a 4 Hz storm hides
        inside one calm ⚠."""
        wid = win.window_id if win else "-"
        key = (wid, stage)
        now = time.time() if now is None else now
        rec = self._err_seen.get(key)
        if rec is not None and (now - rec["last"]) < throttle:
            rec["supp"] += 1
            return
        supp = int(rec["supp"]) if rec else 0
        span = int(now - rec["last"]) if rec else 0
        self._err_seen[key] = {"last": now, "supp": 0}
        rate = f" (×{supp + 1} in {span}s)" if supp else ""
        try:
            self.ledger.record_transition(wid, None, "STAGE_ERR",
                                          {"stage": stage, "error": str(e), "suppressed": supp})
        except Exception:
            pass
        if self.notifier:
            wtag = f"W{win.tag()}" if win else "desk"
            self.notifier.send(f"⚠ {wtag} — {stage} error: {e}{rate}")

    # ---- fill polling (match OUR order ids on the V2 fills tape) ----
    def _poll_fills(self, win: "W.Window", order_ids: Dict[str, Optional[str]],
                    action: str = "") -> Dict[str, int]:
        """Match fills to our order_ids (robust across V2, where fills carry no plain
        buy/sell action) and read the price in OUR-side cents via the proven parse_fill.
        For entry fills that's cost; for flip fills it's the exit proceeds."""
        found: Dict[str, int] = {}
        try:
            fills = self.client.get_fills(win.market_ticker)
        except Exception as e:
            self._stage_err(self._cur_win, "fills_poll", e)
            return found
        want = {v: s for s, v in order_ids.items() if v}
        for f in fills:
            oid = str(f.get("order_id") or "")
            side = want.get(oid)
            if side in ("yes", "no") and side not in found:
                price, _fee, _cnt = parse_fill(f, side)
                if price is not None:
                    found[side] = int(round(price))
        return found

    def _seconds_to_close(self, win: "W.Window") -> Optional[int]:
        if not win.close_ts:
            return None
        return int(win.close_ts - time.time())

    def _best_bid(self, side: str) -> Optional[int]:
        try:
            ob = self.client.get_orderbook(self._cur_market)
        except Exception as e:
            self._stage_err(self._cur_win, "book_fetch", e)
            return None
        yb, ya, nb, na = parse_best_yes_no(ob)
        return yb if side == "yes" else nb

    # ---- run exactly one window to a terminal state ----
    def run_window(self, win: "W.Window") -> None:
        self._cur_market = win.market_ticker
        self._cur_win = win
        self.ledger.record_window(win.window_id, win.market_ticker, win.event_ticker,
                                  win.open_ts, win.close_ts, win.rung)

        # wait for the seek moment (T - entry_start_lead)
        while not self._stop:
            s2c = self._seconds_to_close(win)
            if s2c is None or s2c <= self.cfg.entry_start_lead_sec:
                break
            time.sleep(self.cfg.poll_seconds)

        # IDLE -> gate
        ob = self._safe_ob(win.market_ticker)
        gate = self.pb.gate_window(ob)
        self._check_stuck_gauge(gate)

        if not gate.ok:
            # gate REFUSED — carry the reason + evidence (σ/regime/BLIND) onto the tape
            win.regime = gate.regime
            win.sat_reason = gate.reason
            win.sat_evidence = dict(gate.evidence or {})
            win.transition(W.SAT_OUT, {"gate": gate.reason, "evidence": gate.evidence})
            self.mgr._book(win)
            return
        # SEEKING
        win.regime = gate.regime
        win.transition(W.SEEKING, {"gate": gate.reason, "yes": gate.yes_price,
                                   "no": gate.no_price, "regime": gate.regime})
        self.mgr.post_entry(win, gate, self._seconds_to_close(win))

        # entry phase
        deadline = time.time() + self.cfg.entry_window_sec
        fills: Dict[str, int] = {}
        while not self._stop and time.time() < deadline and len(fills) < 2:
            for side, px in self._poll_fills(
                    win, {"yes": win.yes_entry_oid, "no": win.no_entry_oid}, "buy").items():
                if side not in fills:
                    fills[side] = px
                    self._announce_fill(win, side, px)   # inventory born -> announce loud
            if len(fills) < 2:
                time.sleep(self.cfg.poll_seconds)
        # one final touch snapshot so a no-fill sat-out shows where the market was
        touch = self._touch(win.market_ticker) if len(fills) < 2 else None
        self.mgr.settle_entry_phase(win, fills, self._seconds_to_close(win), touch=touch)
        if win.is_terminal:
            return

        # HOLDING / EXITING loop
        if win.state == W.EXITING:
            self.mgr.flat_out(win, self._marks(win))
            return

        self.mgr.post_flips(win, self._seconds_to_close(win))
        while not self._stop and not win.is_terminal:
            s2c = self._seconds_to_close(win)
            if s2c is not None and s2c <= self.cfg.flat_at_t:
                # T-90 flat wall (W4)
                if win.mode == "bundle" and len(win.held_legs()) == 2:
                    self.mgr.ride_floor(win)
                    self._settle_floor(win)
                else:
                    self.mgr.flat_out(win, self._marks(win))
                break
            flip_fills = self._poll_fills(
                win, {l.side: l.order_id for l in win.held_legs()}, "sell")
            for side, px in flip_fills.items():
                if not win.is_terminal and win.leg_for(side):
                    self.mgr.on_flip_fill(win, side, px)
            if win.mode == "lone" and not win.is_terminal:
                s2c = self._seconds_to_close(win)
                if s2c is not None and s2c <= self.cfg.flat_at_t + 20:
                    self.mgr.salvage_walk(win, self._best_bid(win.open_sides()[0]) if win.open_sides() else None, s2c)
            time.sleep(self.cfg.poll_seconds)

    def _settle_floor(self, win: "W.Window") -> None:
        """After a floor ride, reconcile against broker truth (F1.2)."""
        if self.settler is None:
            return
        try:
            from .manager import compute_window_pnl
            booked = compute_window_pnl(win, self.cfg)["net_cents"]
            self.settler.verify_floor(win, booked)
        except Exception as e:
            print(f"[desk] settlement check failed: {e}", flush=True)

    def _marks(self, win: "W.Window") -> Dict[str, int]:
        ob = self._safe_ob(win.market_ticker)
        yb, ya, nb, na = parse_best_yes_no(ob)
        return {"yes": yb or 1, "no": nb or 1}

    def _safe_ob(self, market_ticker: str) -> Dict[str, Any]:
        try:
            return self.client.get_orderbook(market_ticker)
        except Exception as e:
            self._stage_err(self._cur_win, "book_fetch", e)
            return {}

    def _check_stuck_gauge(self, gate: Any) -> int:
        """STUCK-GAUGE TRIPWIRE (F2.4): a refusal reason repeating N windows is a sensor
        suspect, not a market fact — a market can be boring, but a reading that never
        varies is broken. A healthy pass reason repeating is fine; only refusals trip it.
        Returns the trailing streak."""
        streak = self.ledger.record_gate_reason(gate.reason)
        if not gate.ok and streak >= self.cfg.stuck_gauge_n and self.notifier:
            self.notifier.alert(f"gate reason repeating {streak}x — sensor suspect, not "
                                f"market fact: '{gate.reason}'")
        return streak

    def _announce_fill(self, win: "W.Window", side: str, px: int) -> None:
        """A resting entry bid filled — inventory just came into existence. Announce it
        immediately (Drew's standing ruling) so HOLDING is never mistaken for stuck."""
        if self.notifier:
            flip = self.pb.flip_ask_price(side, px)
            self.notifier.send(f"🌱 W{win.tag()} — filled {side.upper()}@{px} "
                               f"(resting flip posted @{flip})")

    def _touch(self, market_ticker: str) -> Dict[str, Any]:
        """Final best-bid snapshot at entry-phase end: where the market was while our
        bids sat, so a no-fill sat-out can prove the touch (loud-tape law)."""
        yb, ya, nb, na = parse_best_yes_no(self._safe_ob(market_ticker))
        return {"yes_bid": yb, "no_bid": nb}

    # ---- the outer forever-loop ----
    def loop_once(self) -> Optional[str]:
        """Discover + run one window. Returns the window_id handled, or None if idled."""
        halted, reason = self.ledger.halt_state()
        if halted:
            self.notifier and self.notifier.send(f"desk idle — flip_halt set ({reason}); "
                                                 f"clear with DW_CLEAR_HALT=1 boot")
            return None
        # F3.3: the discovery spin lives under the printed req/min budget. If we're out of
        # tokens (sustained draw), skip this poll and let the nap space us out — bursts to
        # catch a bell are fine, 4 Hz storms are not.
        if not self._disc_bucket.allow():
            return None
        # THE COMPUTED BELL: stop asking the list endpoint (it lists a newborn ~3-4 min
        # late); the ticker is arithmetic — compute it and knock directly. None here means
        # "knocking, not born yet" (normal) — _discover_direct handles the loud cases.
        disc = self._discover_direct(time.time())
        if not disc:
            return None
        self._starve = 0   # a live/born market came back — not starving (F3.2 backoff resets)
        event_ticker, market_ticker, mkt, close_ts = disc
        if market_ticker == self._last_window_id:
            return None
        rung, lots = self.ledger.current_rung()
        win = W.Window(window_id=market_ticker, market_ticker=market_ticker,
                       event_ticker=event_ticker, open_ts=None, close_ts=close_ts,
                       rung=rung, lots=lots).bind_ledger(self.ledger)
        self.run_window(win)
        self._last_window_id = market_ticker
        # the current window's close IS the next window's open — wake there, not on a
        # lazy poll (WAKE AT THE BELL).
        self._last_close_ts = close_ts

        # W7: two consecutive stopped windows -> flip_halt
        if self.ledger.consecutive_stops() >= self.cfg.pause_after_stops:
            self.ledger.set_halt(True, f"{self.cfg.pause_after_stops} consecutive stops")
            if self.notifier:
                self.notifier.alert(f"HALT — {self.cfg.pause_after_stops} consecutive stopped "
                                    f"windows. Desk idles until DW_CLEAR_HALT=1 boot.")
        return market_ticker

    # ---- direct-ticker discovery (THE COMPUTED BELL) ----
    def _discover_direct(self, now: float):
        """Compute the window tickers and knock get_market directly, so a newborn is found
        the moment Kalshi creates it (the list lags 3-4 min). Order: current in-progress
        window (boot mid-window), else knock the NEXT window (its 200 is the real bell).
        Returns a (event, ticker, mkt, close_ts) tuple or None (knocking / idle). Loud
        cases (errors, 404-past-open, empty fallback) page from here."""
        series = self.cfg.series_ticker
        direct_error = False

        # 1. the window currently in progress — covers boots mid-window and MUST exist
        cur_ticker, cur_close = current_window_ticker(series, now)
        cur_open = cur_close - 900
        if cur_ticker != self._last_window_id and cur_close > now + 5:
            try:
                m = self.client.get_market(cur_ticker)
                if isinstance(m, dict):
                    return self._disc_tuple(m, cur_ticker, cur_close)
                # None -> an IN-PROGRESS window returned 404. Once we're comfortably inside
                # it, it must exist, so the ticker format/tz is suspect: degrade to the list
                # path loudly instead of going dark on a construction guess.
                if now > cur_open + 30:
                    self._stage_err(None, "ticker_suspect",
                                    Exception(f"{cur_ticker} 404 while in progress"),
                                    throttle=120.0)
                    fb = self._list_fallback(now, "current window 404 — ticker format/tz suspect")
                    if fb:
                        return fb
            except Exception as e:
                direct_error = True
                self._stage_err(None, "discovery", e, throttle=60.0)

        # 2. the NEXT window — knock until Kalshi gives birth (200 = the real bell). A 404
        # here is NORMAL (it opens at cur_close); quiet knock print, no page.
        nxt_ticker, nxt_close = next_window_ticker(series, now)
        if nxt_ticker != self._last_window_id:
            try:
                m = self.client.get_market(nxt_ticker)
            except Exception as e:
                direct_error = True
                self._stage_err(None, "discovery", e, throttle=60.0)
                m = "ERR"
            if isinstance(m, dict):
                self._record_birth(nxt_ticker, nxt_close, now)
                return self._disc_tuple(m, nxt_ticker, nxt_close)
            if m is None:   # 404 — not born yet (opens at cur_close); this is expected
                throttled_print(f"[discover] knocking {nxt_ticker} (not born yet)")
                return None

        # both direct fetches errored (not 404) -> list fallback (patch §3)
        if direct_error:
            return self._list_fallback(now, "both direct fetches errored")
        return None

    def _disc_tuple(self, m: Dict[str, Any], ticker: str, close_ts: int):
        ev = m.get("event_ticker") or (m.get("event") or {}).get("ticker") or ""
        return (ev, ticker, m, close_ts)

    def _record_birth(self, ticker: str, close_ts: int, now: float) -> None:
        """Listing latency is a measured market fact — the true bell's offset. Record it
        once per ticker (the entry phase runs from BIRTH, where the chop lives)."""
        if ticker in self._births_seen:
            return
        self._births_seen.add(ticker)
        lat = int(now - (close_ts - 900))   # 15M window opened close-900s ago
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo
        wtag = datetime.fromtimestamp(close_ts, tz=timezone.utc).astimezone(
            ZoneInfo("America/New_York")).strftime("%H:%M")
        sign = "+" if lat >= 0 else ""
        throttled_print(f"[discover] W{wtag} born T{sign}{lat}s after wall open")
        try:
            self.ledger.record_transition(ticker, None, "BORN",
                                          {"listing_latency_s": lat, "close_ts": close_ts})
        except Exception:
            pass

    def _list_fallback(self, now: float, reason: str):
        """Old list-based discovery — used only when direct fetch can't answer. Loud."""
        print(f"[discover] FALLBACK to list ({reason})", flush=True)
        try:
            disc = self.client.discover_market(self.cfg.series_ticker)
        except Exception as e:
            self._stage_err(None, "discovery", e, throttle=60.0)
            return None
        if not disc:
            self._stage_err(None, "discovery_empty",
                            Exception(f"list fallback empty ({reason})"), throttle=120.0)
        return disc

    def _idle_nap(self, now: Optional[float] = None) -> float:
        """How long to sleep when idle — TWO regimes (F3.2). Approaching a known bell we
        RACE it: shrink toward close_ts, floor 0.25s so we catch the open. Once we're AT
        or PAST the bell and still empty we're STARVING (status-lag gap) — back off
        0.5→1→2→4s (cap 5s) instead of hammering the exchange at 4 Hz. Backoff resets on
        a handled window."""
        now = time.time() if now is None else now
        if self._last_close_ts and now < self._last_close_ts:
            # racing toward the bell (approach regime)
            return max(0.25, min(self._last_close_ts + 0.5 - now, 30.0))
        # starvation regime — progressive backoff so blindness never costs exchange goodwill
        self._starve += 1
        return min(5.0, 0.5 * (2 ** min(self._starve - 1, 20)))   # 0.5,1,2,4,5(cap)

    def run_forever(self) -> None:
        while not self._stop:
            try:
                handled = self.loop_once()
            except Exception as e:
                if self.notifier:
                    self.notifier.alert(f"desk loop error: {e}")
                print(f"[desk] loop error: {e}", flush=True)
                handled = None
            if handled is None:
                time.sleep(self._idle_nap())
