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
from .lib.marketutil import parse_best_yes_no


def _fill_price(fill: Dict[str, Any], side: str) -> Optional[int]:
    for k in (("yes_price",) if side == "yes" else ("no_price",)) + ("price",):
        v = fill.get(k)
        if isinstance(v, (int, float)):
            return int(v)
    return None


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
        self._err_seen: Dict[tuple, float] = {}
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def current_market(self) -> Optional[str]:
        return self._cur_market

    # ---- loud-tape law: no error reaches the loop without a tagged row + a phone line ----
    def _stage_err(self, win: Optional["W.Window"], stage: str, e: Exception,
                   throttle: float = float("inf")) -> None:
        """Record a STAGE_ERR evidence row and page Drew. Per-window stages report once
        (window_id keys are unique); desk-level stages (discovery) throttle by time so a
        dead feed pages periodically, not every second."""
        wid = win.window_id if win else "-"
        key = (wid, stage)
        now = time.time()
        last = self._err_seen.get(key)
        if last is not None and (now - last) < throttle:
            return
        self._err_seen[key] = now
        try:
            self.ledger.record_transition(wid, None, "STAGE_ERR", {"stage": stage, "error": str(e)})
        except Exception:
            pass
        if self.notifier:
            wtag = f"W{win.tag()}" if win else "desk"
            self.notifier.send(f"⚠ {wtag} — {stage} error: {e}")

    # ---- fill polling (match our order ids on the fills tape) ----
    def _poll_fills(self, win: "W.Window", order_ids: Dict[str, Optional[str]],
                    action: str) -> Dict[str, int]:
        found: Dict[str, int] = {}
        try:
            fills = self.client.get_fills(win.market_ticker)
        except Exception as e:
            self._stage_err(self._cur_win, "fills_poll", e)
            return found
        want = {v: s for s, v in order_ids.items() if v}
        for f in fills:
            if str(f.get("action", "")).lower() != action:
                continue
            oid = str(f.get("order_id") or "")
            side = want.get(oid)
            if side is None:
                side = str(f.get("side", "")).lower() if f.get("side") in ("yes", "no") else None
            if side in ("yes", "no") and side not in found:
                p = _fill_price(f, side)
                if p is not None:
                    found[side] = p
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
            fills.update(self._poll_fills(win, {"yes": win.yes_entry_oid, "no": win.no_entry_oid}, "buy"))
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
        try:
            disc = self.client.discover_market(self.cfg.series_ticker)
        except Exception as e:
            self._stage_err(None, "discovery", e, throttle=60.0)
            return None
        if not disc:
            return None
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

    def _idle_nap(self, now: Optional[float] = None) -> float:
        """How long to sleep when idle. After a window is DONE, the next open is a KNOWN
        time (the just-handled window's close_ts) — nap toward it in shrinking steps so we
        re-discover within ~1s of the bell, capped at 30s so we never oversleep a gap and
        floored at 0.25s so we don't spin."""
        now = time.time() if now is None else now
        if self._last_close_ts:
            return max(0.25, min(self._last_close_ts + 0.5 - now, 30.0))
        return 1.0

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
