"""Ops — Telegram accounting/alerts, book-snapshot recorder, daily pack.

Telegram law (§F): accounting and alerts ONLY. /confirm_cash and /deny_cash
adjust baselines through the cash protocol. Telegram never places orders and
never moves tiers — there is no code path from a chat message to the gateway
or the sizing ladder, and this module is where that absence is enforced.
"""

import json
import logging
import os
import time
from typing import Optional

from . import config

log = logging.getLogger("relay.ops")


class Telegram:
    """R6: Telegram is the ledger of record and the operator's phone.

    Real transport (borrowed shape: k_worker/notify.py — HTTP send, retry x2,
    400 falls back to plain text, NEVER raises; a broken pager can't crash the
    patient). Env: TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID. Absent in LIVE mode
    = boot-stop (the pager is safety equipment); absent in SHADOW = one loud
    log line and alerts fall back to the log.

    Command surface is EXACTLY the accounting pair — the old tree's richer
    command set does NOT port (single-gateway law outranks nostalgia)."""

    COMMANDS = ("/confirm_cash", "/deny_cash", "/reset_halt")

    def __init__(self, cash_protocol, send_fn=None):
        self.cash = cash_protocol
        # P8 §2.3: /reset_halt — Drew's key to the two-strike leash. Wired by
        # the runner to WindowEcon.reset_halt; re-enables ENTRIES only.
        self.reset_halt_fn = lambda: "no halt manager wired"
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        self._session = None
        if send_fn is not None:
            self.send = send_fn
        elif self.token and self.chat_id:
            import requests
            self._session = requests.Session()
            self.send = self._send_http
            log.info("[TELEGRAM] transport armed (chat %s…)", self.chat_id[:4])
        else:
            self.send = lambda msg: log.warning("TELEGRAM(unwired): %s", msg)
            log.warning("[TELEGRAM] TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID absent — "
                        "alerts fall back to the log (LIVE mode refuses to boot this way)")

    def wired(self) -> bool:
        return self._session is not None

    def _send_http(self, text: str) -> None:
        """One send, one retry, 400 falls back to plain text. Never raises."""
        for attempt in range(2):
            try:
                resp = self._session.post(
                    f"https://api.telegram.org/bot{self.token}/sendMessage",
                    json={"chat_id": self.chat_id, "text": text,
                          "disable_web_page_preview": True},
                    timeout=10)
                if resp.status_code == 200:
                    return
                log.warning("[TELEGRAM] send HTTP %s: %s",
                            resp.status_code, resp.text[:200])
                if resp.status_code == 400:
                    return  # malformed content won't improve on retry
            except Exception as e:
                log.warning("[TELEGRAM] send error (attempt %d): %s", attempt + 1, e)
        return

    def alert(self, msg: str) -> None:
        self.send(msg)

    def handle_command(self, text: str) -> str:
        cmd = text.strip().split()[0] if text.strip() else ""
        if cmd == "/confirm_cash":
            return "ok" if self.cash.confirm_cash() else "nothing pending"
        if cmd == "/deny_cash":
            self.cash.deny_cash()
            return "denied — FATAL"
        if cmd == "/reset_halt":
            return self.reset_halt_fn()
        # Anything else — including anything order-shaped — is refused by design.
        return f"unknown command; accounting commands only: {', '.join(self.COMMANDS)}"

    def poll_updates_once(self, ledger, timeout: int = 25) -> int:
        """One getUpdates long-poll (offset persisted in engine_state — the old
        listener's shape). Dispatches EXACTLY the accounting pair; every other
        message gets the refusal line. Never raises. Returns updates handled."""
        if not self.wired():
            return 0
        offset = int(ledger.get_state("tg_update_offset") or 0)
        handled = 0
        try:
            resp = self._session.get(
                f"https://api.telegram.org/bot{self.token}/getUpdates",
                params={"offset": offset, "timeout": timeout,
                        "allowed_updates": '["message"]'},
                timeout=timeout + 10)
            if resp.status_code != 200:
                log.warning("[TELEGRAM] getUpdates HTTP %s", resp.status_code)
                return 0
            for upd in resp.json().get("result", []):
                offset = int(upd.get("update_id", offset)) + 1
                text = ((upd.get("message") or {}).get("text") or "").strip()
                if text:
                    self.send(self.handle_command(text))
                    handled += 1
            ledger.set_state("tg_update_offset", str(offset))
        except Exception as e:
            log.warning("[TELEGRAM] poll error (continuing): %s", e)
        return handled


class Recorder:
    """Book-snapshot recorder — ON from first boot (C.2).
    READER (streams-name-readers law): the replay harness + shadow verdicts.

    P6 §4: frames BUFFER and commit on the 1s cycle gate (the runner calls
    flush()) with a flush on shutdown — no per-frame sqlite commit churn at
    BTC frame rates. confirmed_writing counts the buffer."""

    def __init__(self, ledger):
        self.ledger = ledger
        self.frames_written = 0
        self._buffer: list = []

    def record(self, market: str, raw_frame: str, ts: Optional[float] = None) -> None:
        self._buffer.append((ts if ts is not None else time.time(), market, raw_frame))
        self.frames_written += 1

    def flush(self) -> int:
        """Commit the buffer. Called on the cycle gate and at shutdown."""
        if not self._buffer:
            return 0
        n = len(self._buffer)
        self.ledger.db.executemany(
            "INSERT INTO book_snapshots (ts, market, snapshot) VALUES (?,?,?)",
            self._buffer)
        self.ledger.db.commit()
        self._buffer.clear()
        return n

    def confirmed_writing(self) -> bool:
        if self.frames_written > 0:
            return True
        n = self.ledger.db.execute("SELECT COUNT(*) FROM book_snapshots").fetchone()[0]
        return n > 0


def worst_day_bound_line(ledger) -> str:
    """CEO knob (P3): the tuition is a known figure, not a vibe. Three bounds,
    the minimum rules:
      (1) cap x events: at-risk cap/event x 96 fifteen-minute events/day
      (2) kill clamp: 5 lanes x 3 losses/hour x one-lot max loss x 24h
      (3) the drawdown rail: book minus the absolute floor (the binding one
          at today's book size)."""
    cap_events_usd = config.AT_RISK_CAP_CENTS * 96 / 100.0
    kill_clamp_usd = 5 * 3 * config.ONE_LOT_MAX_LOSS_CENTS * 24 / 100.0
    rail_usd = max(0.0, ledger.book_cents() / 100.0 - config.DRAWDOWN_ABSOLUTE_FLOOR_USD)
    binding = min(cap_events_usd, kill_clamp_usd, rail_usd)
    return (f"WORST-DAY BOUND: ${binding:.2f} = min(cap x events ${cap_events_usd:.2f}, "
            f"kill clamp ${kill_clamp_usd:.2f}, drawdown rail ${rail_usd:.2f})")


LANES_LIVE = ("F", "H8", "FLIP", "D", "P")
LANES_PENDING = ()  # every lane that exists trades (R1); empty until a new lane is designed


def daily_pack(ledger, surface, cash_protocol, venue_statement_cents: Optional[int] = None,
               foreign_fills: int = 0, econ=None) -> str:
    """The daily pack: EPOCH 2 header, the worst-day bound, live-vs-pending
    lanes (a reader never wonders why a lane is silent), honest lifetime,
    per-lane sections from terminal rows, and (monthly) the true-up line."""
    lines = [
        f"=== DAILY PACK — EPOCH {config.EPOCH} ===",
        "EPOCH BOUNDARY: this engine's birth; no prior-era rows exist to count.",
        worst_day_bound_line(ledger),
        f"LANES LIVE: {', '.join(LANES_LIVE)}"
        + (f" · NOT YET BUILT: {', '.join(LANES_PENDING)}" if LANES_PENDING else
           " · NOT YET BUILT: none (every lane that exists trades)"),
        f"book={ledger.book_cents()}c lifetime_pnl={ledger.lifetime_pnl_cents()}c "
        f"(honest lifetime = settlements ledger only)",
    ]
    rows = ledger.db.execute(
        "SELECT lane, state, COUNT(*) FROM surface_rows WHERE terminal=1"
        " GROUP BY lane, state ORDER BY lane, state").fetchall()
    by_lane = {}
    for lane, state, n in rows:
        by_lane.setdefault(lane, []).append(f"{state}={n}")
    for lane in sorted(by_lane):
        lines.append(f"[lane {lane}] " + " ".join(by_lane[lane]))
    if not by_lane:
        lines.append("[lanes] no terminal rows this window")
    if foreign_fills:
        lines.append(f"FOREIGN FILLS seen: {foreign_fills} "
                     f"(live Kal's until cutover; NONZERO AFTER CUTOVER = ALARM)")
    # P8 §2.4: the streak, halts, and resets
    if econ is not None:
        lines.extend(econ.pack_lines())
    # P10 §5: episode accounting — a book that went incoherent is a FINDING,
    # not a nuisance; the pack names the market and the span.
    try:
        ep_rows = ledger.db.execute(
            "SELECT why_tag, how_json, ts FROM failures WHERE why_tag IN"
            " ('BOOK_INCOHERENT','BOOK_DIVERGENCE','QUARANTINE')").fetchall()
    except Exception:
        ep_rows = []
    if ep_rows:
        by = {}
        for tag, how_json, ts in ep_rows:
            try:
                mkt = json.loads(how_json).get("market", "?")
            except Exception:
                mkt = "?"
            d = by.setdefault((tag, mkt), [0, ts, ts])
            d[0] += 1
            d[1] = min(d[1], ts)
            d[2] = max(d[2], ts)
        lines.append("BOOK EPISODES (by market, first/last seen):")
        for (tag, mkt), (n, first, last_seen) in sorted(by.items()):
            lines.append(
                f"  {tag} {mkt}: x{n} "
                f"(first {time.strftime('%H:%M', time.gmtime(first))}"
                f" last {time.strftime('%H:%M', time.gmtime(last_seen))} UTC)")
        for (tag, mkt), (n, _f, _l) in sorted(by.items()):
            if tag == "QUARANTINE" or (tag == "BOOK_INCOHERENT" and n >= 3):
                lines.append(f"  FINDING: {mkt} {tag} x{n} — "
                             f"pull this window's banked tape")
    # R5: the FAILURES section — the curriculum includes how things DON'T work
    from . import failures as failure_ledger
    fail_lines = failure_ledger.pack_section(ledger)
    if fail_lines:
        lines.append("FAILURES (by tag, first/last seen):")
        lines.extend(fail_lines)
    else:
        lines.append("FAILURES: none recorded")
    if venue_statement_cents is not None:
        lines.append(cash_protocol.monthly_true_up_line(venue_statement_cents))
    return "\n".join(lines)
