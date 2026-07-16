# f_worker/manager.py
# The inside body (BUILD ORDER §2): flip asks, the quote-skew salvage walk, the T-90
# flat wall, and knee accounting — window P&L booked on GUARANTEED numbers.
#
# The manager ACTS through fgateway (the only order path). It never talks to the
# exchange's create-order endpoint directly. It moves the Window through its states
# and, at the end, books one window_pnl row (salvage = recovered capital, §3).

from typing import Any, Dict, List, Optional, Tuple

from . import feemath, window as W
from .config import Config
from .ledger import (Ledger, KIND_BUNDLE, KIND_FLOOR, KIND_LONE, KIND_SALVAGE, KIND_SAT_OUT)

# exit kinds recorded on a leg
FLIP = "flip"
SALVAGE = "salvage"
FLOOR = "floor"
MARKET_OUT = "market_out"

_TAKER_EXITS = {SALVAGE, MARKET_OUT}


# -----------------------------------------------------------------------------
# Knee accounting — pure P&L on guaranteed numbers (unit-tested in test_pnl.py)
# -----------------------------------------------------------------------------
def compute_window_pnl(win: "W.Window", cfg: Config) -> Dict[str, Any]:
    """Book a window on numbers that already happened. Returns a dict:
    {kind, gross_cents, fees_cents, net_cents, stopped}. Salvage/market-out legs pay a
    taker fee (W8, exact roundup); flip and floor legs are fee-free (maker / settlement)."""
    legs = win.legs
    if not legs:
        # a sat-out is a first-class finding — it carries WHY (gate refusal vs bids
        # posted with no fill vs blind feed) and its evidence onto the tape.
        return {"kind": KIND_SAT_OUT, "gross_cents": 0, "fees_cents": 0,
                "net_cents": 0, "stopped": False,
                "reason": win.sat_reason or "?", "evidence": win.sat_evidence or {}}

    gross = 0
    fees = 0
    floor_legs: List["W.Leg"] = []
    kinds_seen = set()

    for l in legs:
        if l.exit_kind == FLOOR:
            floor_legs.append(l)
            kinds_seen.add(FLOOR)
            continue
        if l.exit_price is None:
            # unresolved & not floored: it settled on its own outcome; conservatively
            # book it as a total loss of premium (should not happen — flat wall prevents it).
            gross += (0 - l.entry_price) * l.count
            kinds_seen.add("lost")
            continue
        gross += (l.exit_price - l.entry_price) * l.count
        if l.exit_kind in _TAKER_EXITS:
            fees += feemath.fee_cents(l.exit_price, l.count)
        kinds_seen.add(l.exit_kind)

    # a complete bundle that rode to settlement pays 100 for the pair
    if floor_legs:
        cost = sum(l.entry_price * l.count for l in floor_legs)
        lots = floor_legs[0].count if floor_legs else win.lots
        gross += (100 * lots) - cost

    net = gross - fees
    stopped = net <= -int(cfg.stop_cents)

    # classify by outcome priority
    if _TAKER_EXITS & kinds_seen:
        kind = KIND_SALVAGE
    elif FLOOR in kinds_seen:
        kind = KIND_FLOOR
    elif len([l for l in legs if l.exit_kind == FLIP]) >= 2:
        kind = KIND_BUNDLE
    else:
        kind = KIND_LONE
    return {"kind": kind, "gross_cents": gross, "fees_cents": fees,
            "net_cents": net, "stopped": stopped}


def window_story(win: "W.Window", pnl: Dict[str, Any]) -> str:
    """The one line at DONE/SAT_OUT (§5 window grammar), prefixed by the caller."""
    tag = _hhmm(win.close_ts)
    kind = pnl["kind"]
    net = pnl["net_cents"]
    sign = "+" if net >= 0 else ""
    if kind == KIND_SAT_OUT:
        ev = pnl.get("evidence") or {}
        if "posted_yes" in ev:
            # bids POSTED, nobody filled — a thesis finding; show where the touch was
            t = ev.get("touch") or {}
            secs = ev.get("entry_window_sec", "?")
            return (f"⏭ W{tag} — SAT_OUT (posted {ev.get('posted_yes')}/{ev.get('posted_no')}, "
                    f"0 fills in {secs}s | touch {t.get('yes_bid', '?')}/{t.get('no_bid', '?')}) "
                    f"| DONE")
        # gate REFUSED (nothing posted) — a gate-tuning finding; show σ/regime
        r = pnl.get("reason", "?")
        sig = ev.get("sigma")
        reg = ev.get("regime", "?")
        detail = f" | σ={sig} {reg}" if sig is not None else ""
        # TEMPORARY diagnostic (THE COMPUTED BELL §4): put the raw book keys on the phone
        # line so the fp-parse-vs-thin-newborn question is settled from a screenshot.
        # Remove once a real gate row confirms ob=['orderbook_fp'] parses.
        ob = f" ob={ev.get('ob_keys')}" if "ob_keys" in ev else ""
        return f"⏭ W{tag} — SAT_OUT (gate: {r}{detail}){ob} | DONE"
    if kind == KIND_FLOOR:
        cost = sum(l.entry_price * l.count for l in win.legs)
        return f"🔁 W{tag} — bundle {_bundle_str(win)}={cost} | rode floor → {sign}{net}¢ | DONE"
    if kind == KIND_BUNDLE:
        flips = "/".join(str(l.exit_price) for l in win.legs)
        cost = sum(l.entry_price * l.count for l in win.legs)
        return f"🔁 W{tag} — bundle {_bundle_str(win)}={cost} | flips {len(win.legs)}/{len(win.legs)} @{flips} → {sign}{net}¢ | DONE"
    if kind == KIND_LONE:
        l = win.legs[0]
        return f"🔁 W{tag} — lone {l.side.upper()}@{l.entry_price} flipped {l.exit_price} → {sign}{net}¢ | DONE"
    # salvage / recovery
    legdesc = ", ".join(f"{l.side.upper()}@{l.entry_price}→{l.exit_price}" for l in win.legs)
    return f"🩹 W{tag} — salvage {legdesc} → {sign}{net}¢ (recovered) | DONE"


def _bundle_str(win: "W.Window") -> str:
    return "+".join(str(l.entry_price) for l in win.legs)


def _hhmm(close_ts: Optional[int]) -> str:
    if not close_ts:
        return "??:??"
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    dt = datetime.fromtimestamp(int(close_ts), tz=timezone.utc).astimezone(ZoneInfo("America/New_York"))
    return dt.strftime("%H:%M")


# -----------------------------------------------------------------------------
# Manager — drives HOLDING/EXITING and books the window
# -----------------------------------------------------------------------------
class Manager:
    def __init__(self, gateway: Any, ledger: Ledger, pricebrain: Any, config: Config,
                 notifier: Any = None):
        self.gw = gateway
        self.ledger = ledger
        self.pb = pricebrain
        self.cfg = config
        self.notifier = notifier

    # ---- SEEKING: post the one entry phase ----
    def _expiry(self, win: "W.Window") -> Optional[int]:
        """W4b: orders die with their window at the exchange — at T-flat_at_t."""
        return (win.close_ts - self.cfg.flat_at_t) if win.close_ts else None

    def post_entry(self, win: "W.Window", gate: Any, seconds_to_close: Optional[int]) -> None:
        win.posted_yes, win.posted_no = gate.yes_price, gate.no_price
        yoid, noid = self.gw.post_entry_pair(
            win.window_id, win.market_ticker, gate.yes_price, gate.no_price, seconds_to_close,
            expiration_ts=self._expiry(win),
            yes_price_str=getattr(gate, "yes_price_str", None),
            no_price_str=getattr(gate, "no_price_str", None))
        win.yes_entry_oid, win.no_entry_oid = yoid, noid

    def settle_entry_phase(self, win: "W.Window", fills: Dict[str, int],
                           seconds_to_close: Optional[int],
                           touch: Optional[Dict[str, Any]] = None) -> None:
        """fills: {side: fill_price} for legs that filled during the entry phase.
        `touch` is a final best-bid snapshot (yes_bid/no_bid) taken at phase end so a
        no-fill sat-out can show WHERE the market was while our bids sat. Cancels any
        unfilled entry order, then routes per the One-Shot Law."""
        # cancel unfilled resting entry bids
        if "yes" not in fills and win.yes_entry_oid:
            self.gw.cancel(win.yes_entry_oid)
        if "no" not in fills and win.no_entry_oid:
            self.gw.cancel(win.no_entry_oid)

        for side, price in fills.items():
            oid = win.yes_entry_oid if side == "yes" else win.no_entry_oid
            win.add_fill(side, price, win.lots, oid)
            self.ledger.record_fill(win.window_id, side, price, win.lots)

        if len(fills) == 2:
            win.mode = "bundle"
            win.transition(W.HOLDING, {"mode": "bundle", "fills": fills})
        elif len(fills) == 1:
            side, price = next(iter(fills.items()))
            if self.gw.may_hold_lone(price):          # W2 predicate
                win.mode = "lone"
                win.transition(W.HOLDING, {"mode": "lone", "side": side, "price": price})
            else:
                win.mode = "lone"
                win.transition(W.EXITING, {"reason": "lone leg > single_leg_max",
                                           "side": side, "price": price})
        else:
            win.sat_reason = "posted, no fill in entry phase"
            win.sat_evidence = {"posted_yes": win.posted_yes, "posted_no": win.posted_no,
                                "touch": touch or {}, "entry_window_sec": self.cfg.entry_window_sec}
            win.transition(W.SAT_OUT, {"reason": win.sat_reason, "evidence": win.sat_evidence})
            self._book(win)

    # ---- HOLDING: post flip asks for every still-held leg ----
    def post_flips(self, win: "W.Window", seconds_to_close: Optional[int]) -> None:
        is_lone = win.mode == "lone"
        for leg in win.held_legs():
            ask = self.pb.flip_ask_price(leg.side, leg.entry_price)
            oid = self.gw.post_flip_ask(win.window_id, win.market_ticker, leg.side,
                                        leg.entry_price, ask, is_lone, seconds_to_close,
                                        expiration_ts=self._expiry(win))
            leg.order_id = oid

    def on_flip_fill(self, win: "W.Window", side: str, exit_price: int) -> None:
        """A resting flip ask got taken. Mark the leg flipped and record the fill."""
        self.ledger.record_fill(win.window_id, side, exit_price, win.lots)
        win.resolve_leg(side, exit_price, FLIP)
        if not win.held_legs():
            win.transition(W.DONE, {"resolution": "all flipped"})
            self._book(win)
        else:
            # bundle became a lone leg — the other side now salvages (§3)
            win.mode = "lone"
            win.transition(W.HOLDING, {"resolution": "one flipped, other -> lone salvage",
                                       "flipped": side, "exit": exit_price})

    # ---- salvage walk (quote-skew) for a leftover / lone leg ----
    def salvage_walk(self, win: "W.Window", best_bid: Optional[int],
                     seconds_to_close: Optional[int]) -> None:
        """Cross to recover if the resting flip won't clear and the tape says the leg is
        unlikely to flip. Prices the taker fee BEFORE deciding (W8)."""
        for leg in win.held_legs():
            if best_bid is None:
                continue
            # recover when crossing now beats holding a leg that likely won't flip
            if best_bid >= leg.entry_price or seconds_to_close is None or seconds_to_close <= self.cfg.flat_at_t + 15:
                oid, fee = self.gw.salvage_cross(win.window_id, win.market_ticker, leg.side,
                                                 leg.entry_price, best_bid, win.mode == "lone")
                leg.order_id = oid
                self.ledger.record_fill(win.window_id, leg.side, best_bid, leg.count,
                                        fee_cents=fee, is_taker=True)
                win.resolve_leg(leg.side, best_bid, SALVAGE)
        if win.state == W.HOLDING and not win.held_legs():
            win.transition(W.DONE, {"resolution": "salvaged"})
            self._book(win)

    # ---- ride the floor: complete bundle intact at T-90 ----
    def ride_floor(self, win: "W.Window") -> None:
        for leg in win.held_legs():
            win.resolve_leg(leg.side, None, FLOOR)
        win.transition(W.DONE, {"resolution": "bundle rode floor to settlement"})
        self._book(win)

    # ---- EXITING: T-90 flat wall (W4) — also the orphan-recovery flatten path (F1.1) ----
    def flat_out(self, win: "W.Window", marks: Optional[Dict[str, int]] = None,
                 reason: str = "T-90 flat wall") -> None:
        marks = marks or {}
        # HOLDING->EXITING is the normal edge; SEEKING->EXITING carries a recovered orphan.
        if win.state in (W.HOLDING, W.SEEKING):
            win.transition(W.EXITING, {"reason": reason})
        for leg in win.held_legs():
            mark = marks.get(leg.side)
            fee = feemath.fee_cents(mark, leg.count) if mark is not None else 0
            oid = self.gw.market_out(win.window_id, win.market_ticker, leg.side,
                                     win.mode == "lone", entry_price=leg.entry_price, mark_price=mark)
            leg.order_id = oid
            win.resolve_leg(leg.side, mark if mark is not None else 0, MARKET_OUT)
            self.ledger.record_fill(win.window_id, leg.side, mark if mark is not None else 0,
                                    leg.count, fee_cents=fee, is_taker=True)
        win.transition(W.DONE, {"resolution": reason})
        self._book(win)

    # ---- book one window_pnl row + emit the one-line story ----
    def _book(self, win: "W.Window") -> str:
        pnl = compute_window_pnl(win, self.cfg)
        self.ledger.record_pnl(win.window_id, pnl["kind"], pnl["gross_cents"], pnl["fees_cents"],
                               pnl["net_cents"], pnl["stopped"], win.rung,
                               regime=getattr(win, "regime", None),
                               detail={"legs": [vars(l) for l in win.legs],
                                       "reason": pnl.get("reason"), "evidence": pnl.get("evidence")})
        story = window_story(win, pnl)
        if self.notifier is not None:
            self.notifier.send(story)
        return story
