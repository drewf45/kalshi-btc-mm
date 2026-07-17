"""Ops — Telegram accounting/alerts, book-snapshot recorder, daily pack.

Telegram law (§F): accounting and alerts ONLY. /confirm_cash and /deny_cash
adjust baselines through the cash protocol. Telegram never places orders and
never moves tiers — there is no code path from a chat message to the gateway
or the sizing ladder, and this module is where that absence is enforced.
"""

import json
import logging
import time
from typing import Optional

from . import config

log = logging.getLogger("relay.ops")


class Telegram:
    """Transport-agnostic alert sink. In shadow (and tests) it logs; a token wires
    it to real Telegram. Command surface is EXACTLY the accounting pair."""

    COMMANDS = ("/confirm_cash", "/deny_cash")

    def __init__(self, cash_protocol, send_fn=None):
        self.cash = cash_protocol
        self.send = send_fn or (lambda msg: log.warning("TELEGRAM: %s", msg))

    def alert(self, msg: str) -> None:
        self.send(msg)

    def handle_command(self, text: str) -> str:
        cmd = text.strip().split()[0] if text.strip() else ""
        if cmd == "/confirm_cash":
            return "ok" if self.cash.confirm_cash() else "nothing pending"
        if cmd == "/deny_cash":
            self.cash.deny_cash()
            return "denied — FATAL"
        # Anything else — including anything order-shaped — is refused by design.
        return f"unknown command; accounting commands only: {', '.join(self.COMMANDS)}"


class Recorder:
    """Book-snapshot recorder — ON from first boot (C.2).
    READER (streams-name-readers law): the replay harness + shadow verdicts."""

    def __init__(self, ledger):
        self.ledger = ledger
        self.frames_written = 0

    def record(self, market: str, raw_frame: str, ts: Optional[float] = None) -> None:
        self.ledger.db.execute(
            "INSERT INTO book_snapshots (ts, market, snapshot) VALUES (?,?,?)",
            (ts if ts is not None else time.time(), market, raw_frame),
        )
        self.ledger.db.commit()
        self.frames_written += 1

    def confirmed_writing(self) -> bool:
        n = self.ledger.db.execute("SELECT COUNT(*) FROM book_snapshots").fetchone()[0]
        return n > 0 or self.frames_written > 0


def daily_pack(ledger, surface, cash_protocol, venue_statement_cents: Optional[int] = None) -> str:
    """The daily pack: EPOCH 2 header, honest lifetime, per-lane sections from
    terminal rows, and (monthly) the true-up line."""
    lines = [
        f"=== DAILY PACK — EPOCH {config.EPOCH} ===",
        "EPOCH BOUNDARY: this engine's birth; no prior-era rows exist to count.",
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
    if venue_statement_cents is not None:
        lines.append(cash_protocol.monthly_true_up_line(venue_statement_cents))
    return "\n".join(lines)
