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

    Command surface is EXACTLY the accounting pair + Drew's key — plus, per
    P22 §5, the ONE read-only addition /scoreboard (Adversary: it places
    nothing, changes nothing — accounting-read only). The old tree's richer
    command set does NOT port (single-gateway law outranks nostalgia)."""

    # P-CASH-FATAL-1 §4.4: /clear_cash_fatal — the ONLY key to a denied
    # cash delta (a restart is not); symmetric with /reset_halt.
    COMMANDS = ("/confirm_cash", "/deny_cash", "/reset_halt", "/scoreboard",
                "/clear_cash_fatal", "/daily", "/owed", "/series")

    def __init__(self, cash_protocol, send_fn=None):
        self.cash = cash_protocol
        # P8 §2.3: /reset_halt — Drew's key to the two-strike leash. Wired by
        # the runner to WindowEcon.reset_halt; re-enables ENTRIES only.
        self.reset_halt_fn = lambda: "no halt manager wired"
        # P22 §5: the scoreboard on demand — wired by the runner to
        # scoring.scoreboard_lines. Read-only by construction.
        self.scoreboard_fn = lambda: "no scoreboard wired"
        # WO-2026-07-22-K: /daily — the read-only day export. Wired by the runner
        # to build the .xlsx, send it as a document, and delete it. Takes the raw
        # command text (for the optional "/daily N" days-back arg).
        self.daily_fn = lambda text="": "no daily export wired"
        # WO-2026-07-26-O §O4: /owed — the operator's read-only look at the scrape
        # (book / owed / tradeable / distance to the next milestone). Read-only.
        self.owed_fn = lambda: "no scrape wired"
        # WO-2026-07-26-S §1 — /series: the room start command, without a second
        # deploy. "/series xrp on|off|live|shadow" opens/parks a room (roster +
        # mode + F family + halt-key migration). Wired by the runner to the engine.
        self.series_fn = lambda text="": "no series manager wired"
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

    def send_document(self, path: str, caption: str = "") -> bool:
        """WO-2026-07-22-K: one multipart sendDocument (the /daily bundle). Falls
        back to a log line when the transport is unwired; never raises."""
        if not self.wired():
            log.warning("TELEGRAM(unwired): document %s (%s)", path, caption)
            return False
        try:
            with open(path, "rb") as fh:
                resp = self._session.post(
                    f"https://api.telegram.org/bot{self.token}/sendDocument",
                    data={"chat_id": self.chat_id, "caption": caption[:1024]},
                    files={"document": (os.path.basename(path), fh)},
                    timeout=60)
            if resp.status_code == 200:
                return True
            log.warning("[TELEGRAM] sendDocument HTTP %s: %s",
                        resp.status_code, resp.text[:200])
        except Exception as e:
            log.warning("[TELEGRAM] sendDocument error: %s", e)
        return False

    def handle_command(self, text: str) -> str:
        cmd = text.strip().split()[0] if text.strip() else ""
        if cmd == "/confirm_cash":
            return "ok" if self.cash.confirm_cash() else "nothing pending"
        if cmd == "/deny_cash":
            self.cash.deny_cash()
            return "denied — FATAL"
        if cmd == "/reset_halt":
            return self.reset_halt_fn()
        if cmd == "/scoreboard":
            return self.scoreboard_fn()
        if cmd == "/daily":
            return self.daily_fn(text)      # read-only export; never trades
        if cmd == "/clear_cash_fatal":
            return self.cash.clear_cash_fatal()
        if cmd == "/owed":
            return self.owed_fn()           # read-only scrape look; never trades
        if cmd == "/series":
            return self.series_fn(text)     # opens/parks a room; never places directly
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
    # WO-2026-07-26-O §O2: the worst-day rail is measured against TRADEABLE
    # (book − owed) — the operator's scrape is not risk capital to draw down.
    rail_usd = max(0.0, ledger.tradeable_cents() / 100.0 - config.DRAWDOWN_ABSOLUTE_FLOOR_USD)
    binding = min(cap_events_usd, kill_clamp_usd, rail_usd)
    return (f"WORST-DAY BOUND: ${binding:.2f} = min(cap x events ${cap_events_usd:.2f}, "
            f"kill clamp ${kill_clamp_usd:.2f}, drawdown rail ${rail_usd:.2f})")


LANES_LIVE = ("F", "H8", "FLIP", "D", "P")
LANES_PENDING = ()  # every lane that exists trades (R1); empty until a new lane is designed


def owed_line(ledger) -> str:
    """WO-2026-07-26-O §O3/O4 — the scrape, in one line: book / owed / tradeable
    and the distance to the next milestone. Used by /owed, the daily pack, boot."""
    seed = ledger.scrape_seed_cents()
    if seed is None:
        return "SCRAPE: not seeded yet"
    book = ledger.book_cents()
    owed = ledger.owed_cents()
    tradeable = ledger.tradeable_cents()
    hwm = ledger.high_water_cents()
    into = max(0, hwm - seed) % config.SCRAPE_MILESTONE_C   # cents into the current $10
    to_next = config.SCRAPE_MILESTONE_C - into if into else config.SCRAPE_MILESTONE_C
    return (f"SCRAPE: book ${book / 100:.2f} · owed ${owed / 100:.2f} · tradeable "
            f"${tradeable / 100:.2f} · high-water ${hwm / 100:.2f} (seed "
            f"${seed / 100:.2f}) · ${to_next / 100:.2f} of new high to the next "
            f"${config.SCRAPE_PER_MILESTONE_C / 100:.0f}")


def restated_money_lines(ledger) -> list:
    """WO-2026-07-26-N §P4.4 — the MONEY section, RESTATED. The 07/25→26 overnight
    double-booked its custodian cuts (custodian direct-write + fills-poller booked
    the SAME cut twice), corrupting the DISPLAYED window P&L and the cell/fills
    reporting (the −2389¢ line, the inflated window). But `lifetime_pnl_cents`
    reads the SETTLEMENTS ledger alone (ledger.py:239) — settlements are written
    once at bell, and the phantom cuts were never settlements — so the honest
    lifetime needs no data rebuild; it was correct throughout. This publishes the
    before/after with the delta explained line-item, tagged RESTATED once."""
    settle = int(ledger.db.execute(
        "SELECT COALESCE(SUM(pnl_cents),0) FROM settlements WHERE divergent=0"
    ).fetchone()[0])
    cash = int(ledger.db.execute(
        "SELECT COALESCE(SUM(amount_cents),0) FROM cash_movements").fetchone()[0])
    lifetime = ledger.lifetime_pnl_cents()
    book = ledger.book_cents()
    # a pre-restate displayed figure, if the operator/boot banked one
    prior = ledger.get_state("prerestate_lifetime_c")
    delta_line = (f"  Δ vs the pre-restate displayed −2389¢ line: the phantom "
                  f"double-booked cuts touched cell/window REPORTING only, never "
                  f"settlements → lifetime delta from the corruption is 0c"
                  if prior is None else
                  f"  Δ before {int(prior)}c → after {lifetime}c "
                  f"({lifetime - int(prior):+d}c, the phantom cuts backed out)")
    return [
        f"MONEY (RESTATED — WO-N P4.4): lifetime {lifetime}c rebuilt from the "
        f"settlements ledger alone (Σ settlements[divergent=0]={settle}c)",
        delta_line,
        f"  book {book}c = cash_movements {cash}c + settlements {settle}c; the "
        f"/confirm_cash re-baselines absorbed a booking error into cash_movements "
        f"— the cash-sentinel doctrine (P4.5) blocks the recurrence",
    ]


def recon_cadence_lines(ledger) -> list:
    """WO-2026-07-27-V T1 — THE SENTINEL'S PULSE. Clean reconciles per UTC hour
    vs total cycles: the cash sentinel's duty cycle, made visible. A run of hours
    with 0 clean reads is quiescence starvation — the sentinel asleep — the exact
    condition the ~$45 withdrawal walked through. If this shows part-time hours
    even absent a withdrawal, the sentinel has been part-time longer than tonight."""
    try:
        rows = ledger.db.execute(
            "SELECT strftime('%H', ts, 'unixepoch') AS hr, "
            " SUM(CASE WHEN result IN ('CLEAN','SILENT_REBASED','CONFIRMED_POSITIVE',"
            " 'PROMPTED') THEN 1 ELSE 0 END) AS clean, COUNT(*) AS total "
            "FROM recon_cycles GROUP BY hr ORDER BY hr").fetchall()
    except Exception as e:
        return [f"RECON CADENCE: unavailable ({e})"]
    if not rows:
        return ["RECON CADENCE (WO-V T1): no reconcile cycles recorded yet "
                "(SHADOW, or freshly booted)"]
    parts = [f"{hr}h:{clean}/{total}" for hr, clean, total in rows]
    starved = [hr for hr, clean, total in rows if clean == 0 and total > 0]
    line = ("RECON CADENCE (WO-V T1) — clean reconciles / total, by UTC hour "
            "(the sentinel's pulse): " + " ".join(parts))
    out = [line]
    if starved:
        out.append(f"  ⚠ STARVED HOURS (0 clean reads): {', '.join(starved)} — "
                   "the sentinel slept these hours; the book went unverified")
    return out


def no_counterparty_by_series_hour(ledger) -> list:
    """WO-2026-07-26-T Guard 1 — the room's liquidity map. NO_COUNTERPARTY
    refusals (opposite side empty at F entry) counted by series and UTC hour: a
    free read of when each room actually has counterparties. A room that refuses
    all afternoon has none and SHOULD starve — the gate telling the truth."""
    try:
        rows = ledger.db.execute(
            "SELECT json_extract(how_json,'$.series') AS series, "
            " strftime('%H', ts, 'unixepoch') AS hr, COUNT(*) "
            "FROM failures WHERE why_tag='NO_COUNTERPARTY' "
            "GROUP BY series, hr ORDER BY series, hr").fetchall()
    except Exception as e:
        return [f"NO_COUNTERPARTY (liquidity map): unavailable ({e})"]
    if not rows:
        return ["NO_COUNTERPARTY (liquidity map, WO-T): none — every F entry had "
                "a counterparty this window"]
    by_series = {}
    for series, hr, n in rows:
        by_series.setdefault(series or "?", []).append(f"{hr}h:{n}")
    lines = ["NO_COUNTERPARTY (liquidity map, WO-T Guard 1) — opposite side empty "
             "at F entry, by room · UTC hour:"]
    for s in sorted(by_series):
        lines.append(f"  [{s}] {' '.join(by_series[s])}")
    return lines


def series_chapter_lines(ledger) -> list:
    """WO-2026-07-26-S §1 — THE PER-SERIES CHAPTER (the pack section every room
    gets from day one). One block per rostered room: its mode, F dial, settled
    windows and P&L (from terminal surface rows, grouped by series_of(market)).
    A NEW room (not BTC) carries its FIRST-DAY MECHANICS WATCHLIST — the venue
    facts no BTC record can vouch for: maker $0 must be OBSERVED on its own tape,
    settlement attribution clean, tick/strike matching discovery. These PAGE on
    anomaly; they never pre-block (Part 3 item 3). The room's own halt is the
    stated bound on the cost of learning them live."""
    lines = ["=== SERIES CHAPTERS (WO-S) — one book, a room each ==="]
    rows = ledger.db.execute(
        "SELECT market, state, detail FROM surface_rows WHERE terminal=1"
    ).fetchall()
    by_series = {}
    for market, state, detail in rows:
        s = config.series_of(market)
        agg = by_series.setdefault(s, {"settled": 0, "pnl": 0, "pass": 0})
        if state == "PASS":
            agg["pass"] += 1
        else:
            agg["settled"] += 1
            try:
                agg["pnl"] += int(json.loads(detail).get("pnl_cents", 0))
            except Exception:
                pass
    for s in config.SERIES:
        a = by_series.get(s, {"settled": 0, "pnl": 0, "pass": 0})
        lines.append(
            f"[{s}] {config.series_mode(s)} · F@{config.f_notional_pct_of(s):.0%} "
            f"· settled {a['settled']} ({a['pnl']:+d}¢) · passes {a['pass']}")
        if s != "KXBTC15M" and config.series_mode(s) != "OFF":
            lines.append(
                f"   FIRST-DAY WATCHLIST ({s}): confirm maker $0 on ITS tape · "
                "settlement attribution clean · tick/strike match discovery — "
                "pages on anomaly, never pre-blocks; the room's own halt bounds "
                "the cost of learning the venue live (Part 3)")
    return lines


def book_stale_by_hour(ledger) -> list:
    """WO-2026-07-26-R: the orientation watch's registry question, answered —
    endpoint-lag (BOOK_STALE) counts by UTC hour. The market-summary endpoint
    trails the orderbook by a spread on quiet books; if weekday data shows the
    offsets clustering with real trouble, BOOK_STALE_OFFSET_C gets a DERIVED
    number instead of a DREW-DEFAULT. Read-only; a demoted alarm, never a halt."""
    try:
        rows = ledger.db.execute(
            "SELECT strftime('%H', ts, 'unixepoch') AS hr, COUNT(*), "
            " MAX(json_extract(how_json,'$.offset')) "
            "FROM failures WHERE why_tag='BOOK_STALE' GROUP BY hr ORDER BY hr"
        ).fetchall()
    except Exception as e:
        return [f"BOOK_STALE (endpoint lag): unavailable ({e})"]
    if not rows:
        return ["BOOK_STALE (endpoint lag, WO-R): none — the watch never demoted "
                "a read this window (no halt, no lag noise)"]
    total = sum(n for _, n, _ in rows)
    by_hr = " ".join(f"{hr}h:{n}" for hr, n, _ in rows)
    worst = max((mx or 0) for _, _, mx in rows)
    return [f"BOOK_STALE (endpoint lag, WO-R): {total} demoted read(s) — never "
            f"halted; by UTC hour {by_hr}; worst offset {worst}¢ "
            f"(threshold {config.BOOK_STALE_OFFSET_C}¢, gross halt "
            f"≥{config.ORIENTATION_GROSS_DIVERGENCE_C}¢). Clustering with real "
            "trouble → the threshold earns a DERIVED number."]


def fill_economics(ledger, lanes=("FLIP", "F", "HUNT")) -> list:
    """WO-DAILY-PACK-FILL-ECON (build 47): read-only fill economics per lane —
    what we paid to enter, what we sold at, the gross spread, fees, the NET
    after fees, and the MAKER/TAKER split (maker = 0-fee, taker > 0¢: the fee
    drag that eats the nickel). Plus FLIP's resting-take fill (reversion) rate
    and its distribution by realized take distance. All from the honest fills
    table + the FLIP_SWING instrument; no trading change. Turns 'is +5 the
    right nickel or is it 7' into a number instead of a feeling."""
    import json as _json
    out = ["FILL ECONOMICS (honest fills; maker = 0-fee, taker > 0¢):"]
    for lane in lanes:
        e_val, e_cnt, e_n = ledger.db.execute(
            "SELECT COALESCE(SUM(price_cents*count),0), COALESCE(SUM(count),0),"
            " COUNT(*) FROM fills WHERE lane=? AND action='ENTRY'",
            (lane,)).fetchone()
        x_val, x_cnt, x_n = ledger.db.execute(
            "SELECT COALESCE(SUM(price_cents*count),0), COALESCE(SUM(count),0),"
            " COUNT(*) FROM fills WHERE lane=? AND action IN "
            "('EXIT','CUSTODIAN_EXIT')", (lane,)).fetchone()
        tot_fee, tot_cnt, maker_cnt = ledger.db.execute(
            "SELECT COALESCE(SUM(fee_cents),0), COALESCE(SUM(count),0),"
            " COALESCE(SUM(CASE WHEN fee_cents=0 THEN count ELSE 0 END),0)"
            " FROM fills WHERE lane=?", (lane,)).fetchone()
        if e_n == 0 and x_n == 0:
            out.append(f"  [{lane}] no fills yet")
            continue
        avg_e = e_val / e_cnt if e_cnt else 0.0
        avg_x = x_val / x_cnt if x_cnt else 0.0
        spread = (avg_x - avg_e) if (e_cnt and x_cnt) else None
        fee_pc = tot_fee / tot_cnt if tot_cnt else 0.0
        maker_pct = 100.0 * maker_cnt / tot_cnt if tot_cnt else 0.0
        net = (spread - fee_pc) if spread is not None else None
        out.append(
            f"  [{lane}] entry~{avg_e:.1f}c exit~{avg_x:.1f}c spread~"
            f"{('%+.1f' % spread) if spread is not None else 'na'}c | fees "
            f"{tot_fee}c ({fee_pc:.2f}c/ct · maker {maker_pct:.0f}%) | net~"
            f"{('%+.1f' % net) if net is not None else 'na'}c/ct | {x_n} exits"
            f"/{e_n} entries")
    # FLIP's resting-take fill (reversion) rate + its by-distance distribution
    rows = ledger.db.execute(
        "SELECT detail FROM surface_rows WHERE state='FLIP_SWING'"
        " ORDER BY id DESC LIMIT 500").fetchall()
    took = tot = 0
    by_dist: dict = {}
    for (d,) in rows:
        try:
            r = _json.loads(d)
        except Exception:
            continue
        tot += 1
        if r.get("took_swing"):
            took += 1
            k = int(round(r.get("gross_cents", 0)))
            by_dist[k] = by_dist.get(k, 0) + 1
    if tot:
        rate = 100.0 * took / tot
        out.append(f"  [FLIP fill-rate] resting take lifted {took}/{tot} = "
                   f"{rate:.0f}% — the reversion gate; size only once it "
                   "clears fees")
        if by_dist:
            dist_str = " ".join(f"+{k}c:{v}" for k, v in sorted(by_dist.items()))
            out.append(f"  [FLIP take-distance] filled by realized spread: "
                       f"{dist_str} (the post-here-to-fill curve)")
    return out


def flip_fill_rate_hourly(ledger) -> str:
    """WO-DAILY-PACK-FILL-ECON: the compact hourly line — FLIP's take fill
    rate and maker %, the two numbers that say whether the lane is working."""
    took, tot = 0, 0
    import json as _json
    for (d,) in ledger.db.execute(
            "SELECT detail FROM surface_rows WHERE state='FLIP_SWING'"
            " ORDER BY id DESC LIMIT 200").fetchall():
        try:
            tot += 1
            if _json.loads(d).get("took_swing"):
                took += 1
        except Exception:
            tot -= 1
    tot_cnt, maker_cnt = ledger.db.execute(
        "SELECT COALESCE(SUM(count),0), COALESCE(SUM(CASE WHEN fee_cents=0 "
        "THEN count ELSE 0 END),0) FROM fills WHERE lane='FLIP'").fetchone()
    fr = f"{100.0 * took / tot:.0f}%({took}/{tot})" if tot else "na"
    mk = f"{100.0 * maker_cnt / tot_cnt:.0f}%" if tot_cnt else "na"
    return f"flip_fill={fr} flip_maker={mk}"


def flip_fill_rate_by_price(ledger, limit: int = 500) -> str:
    """WO-INSTRUMENTATION-AND-FLIP-TIMING (build 51) — THE MONEY CURVE.
    Of the takes posted at each gouge level (the posted middle-target price),
    what % FILLED (exit_reason TAKE_FILL) vs walked-down / cut / handed off.
    This is how the broker sees how much each side of the middle is worth: if
    posting at 52 fills 40% and posting at 50 fills 70%, the curve rules the
    optimal gouge. Every concluded take is a data point here (post-reconcile
    surface truth). Bucketed by posted_take price, most-populated first."""
    import json as _json
    buckets = {}   # posted_take -> [filled, total]
    for (d,) in ledger.db.execute(
            "SELECT detail FROM surface_rows WHERE state='FLIP_SWING'"
            " ORDER BY id DESC LIMIT ?", (limit,)).fetchall():
        try:
            row = _json.loads(d)
        except Exception:
            continue
        px = row.get("posted_take")
        if px is None:
            continue
        b = buckets.setdefault(int(px), [0, 0])
        b[1] += 1
        if row.get("exit_reason") == "TAKE_FILL":
            b[0] += 1
    if not buckets:
        return "FILL-RATE BY POSTED PRICE (24h): no concluded takes yet"
    parts = []
    for px in sorted(buckets):
        filled, tot = buckets[px]
        parts.append(f"{px}c:{100.0 * filled / tot:.0f}%({filled}/{tot})")
    return "FILL-RATE BY POSTED PRICE (24h): " + " · ".join(parts)


import re as _re

# WO-2026-07-24-J §P4 — the ENTRY casefile is the full tag set; DIVERGENCE
# reads it back. Every HUNT entry line carries {p_end (settle%), p_end_LB,
# p_touch, book, edge, ev_c}; the arrow is the side. One regex, one row.
_HUNT_CASE_RE = _re.compile(
    r"HUNT (?P<arrow>[↑↓]) spot \$(?P<dist>\d+) off strike, "
    r"T-\d+:\d+ · settle (?P<settle>\d+)% \(LB (?P<lb>\d+)\) · "
    r"book (?P<book>\d+)¢ · edge (?P<edge>[+-]?\d+) · "
    r"touch (?P<touch>\d+)% \(info\) · EV (?P<ev>[+-]?\d+)")


def _parse_hunt_case(detail: str) -> Optional[dict]:
    m = _HUNT_CASE_RE.search(detail or "")
    if not m:
        return None
    return {"side": "yes" if m.group("arrow") == "↑" else "no",
            "dist": int(m.group("dist")),
            "p_end": int(m.group("settle")), "p_end_lb": int(m.group("lb")),
            "book": int(m.group("book")), "edge": int(m.group("edge")),
            "p_touch": int(m.group("touch")), "ev_c": int(m.group("ev"))}


def hunt_divergence_lines(ledger, since: Optional[float] = None) -> list:
    """WO-2026-07-24-J §P5 — the DIVERGENCE read-back, HUNT-scoped. Settled
    HUNT-eligible windows bucketed by EDGE answer three calibration questions
    the desk must confront every day:
      (1) REALIZED — of the HUNT-side bets in this edge bucket, how many
          actually SETTLED in favor (window_outcomes.settled_yes vs side)?
      (2) p_end   — what did the TABLE predict (mean settle%)?
      (3) book    — what did the MARKET price (mean book cost = implied %)?
    Divergence between (1), (2), (3) is the whole point: if realized tracks
    p_end and both beat book, the forward gate has an edge; if realized lags
    p_end, the table is overconfident and HUNT stays PROBE. HUNT tier is NOT
    promoted here — this section only measures; promotion waits on the Wilson
    bar clearing, reported per bucket."""
    from .sizing import wilson_lower_bound
    since = (time.time() - 86400) if since is None else since
    rows = ledger.db.execute(
        "SELECT detail, market FROM surface_rows WHERE lane='FLIP'"
        " AND state='PROPOSED'"
        " AND (detail LIKE 'HUNT ↑%' OR detail LIKE 'HUNT ↓%') AND ts>?",
        (since,)).fetchall()
    outcomes = dict(ledger.db.execute(
        "SELECT market, settled_yes FROM window_outcomes").fetchall())
    # edge buckets: at-floor, mid, rich — coarse (HUNT is selective, low count)
    def _bucket(edge: int) -> str:
        if edge < 8:
            return "6-7"
        if edge < 12:
            return "8-11"
        return "12+"
    buckets = {}   # label -> {n, settled_known, settled_yes, p_end_sum, book_sum}
    for detail, market in rows:
        c = _parse_hunt_case(detail)
        if c is None:
            continue
        b = buckets.setdefault(_bucket(c["edge"]),
                               {"n": 0, "known": 0, "won": 0,
                                "p_end_sum": 0, "book_sum": 0})
        b["n"] += 1
        b["p_end_sum"] += c["p_end"]
        b["book_sum"] += c["book"]
        sy = outcomes.get(market)
        if sy is not None:
            b["known"] += 1
            hunt_side_won = (bool(sy) and c["side"] == "yes") or \
                            ((not sy) and c["side"] == "no")
            if hunt_side_won:
                b["won"] += 1
    if not buckets:
        return ["HUNT DIVERGENCE (24h): no HUNT-eligible windows yet — the "
                "forward gate is selective (low count reads as discipline)"]
    out = ["HUNT DIVERGENCE (24h) — realized settle vs p_end vs book, by edge:"]
    for label in ("6-7", "8-11", "12+"):
        b = buckets.get(label)
        if not b:
            continue
        mean_pe = b["p_end_sum"] / b["n"]
        mean_bk = b["book_sum"] / b["n"]
        if b["known"]:
            realized = 100.0 * b["won"] / b["known"]
            lb = wilson_lower_bound(b["won"], b["known"])
            # the Wilson bar: realized-LB must clear the market's implied price
            # for the edge to be real; until then HUNT is PROBE.
            cleared = (lb * 100.0) > mean_bk
            realized_s = (f"realized {realized:.0f}% (LB {lb * 100:.0f}%, "
                          f"n={b['known']})")
            bar_s = (" · Wilson bar CLEARED (LB>book) → promotable"
                     if cleared else " · PROBE (LB≤book, not yet earned)")
        else:
            realized_s = "realized n/a (no settled windows yet)"
            bar_s = " · PROBE (unmeasured)"
        out.append(
            f"  edge {label}: {realized_s} vs p_end {mean_pe:.0f}% vs "
            f"book {mean_bk:.0f}%{bar_s} · entries {b['n']}")
    return out


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
        # P11 (Trader's knob): the transport header — so nobody later mistakes
        # the accepted 3s fill lag for a bug.
        ("TRANSPORT: WS" if config.WS_ENABLED else
         "TRANSPORT: REST 1s (A3 proven ground) · fills sweep 3s — fill "
         "knowledge up to 3s late: accepted at one-lot scope, not a bug"),
        f"book={ledger.book_cents()}c lifetime_pnl={ledger.lifetime_pnl_cents()}c "
        f"(honest lifetime = settlements ledger only)",
    ]
    # WO-2026-07-26-N §P4.4 — THE RESTATEMENT. The overnight double-booking
    # corrupted REPORTING (cell/window P&L, the −2389¢ line) but the lifetime is
    # rebuilt from the settlements ledger ALONE (build 6's law), which those
    # phantom cuts never touched. The MONEY section carries a RESTATED tag with
    # the line-item delta so the meter is trusted before any size conversation.
    try:
        lines.extend(restated_money_lines(ledger))
    except Exception as e:
        lines.append(f"MONEY (RESTATED): unavailable ({e})")
    # WO-2026-07-26-O §O3: the scrape owed line rides the daily pack.
    try:
        lines.append(owed_line(ledger))
    except Exception as e:
        lines.append(f"SCRAPE: unavailable ({e})")
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
    # P13 (CEO knob): tuition itemized, never mysterious — scratches, their
    # cost, and fees, from the fills ledger.
    try:
        # WO-2026-07-24-H "ONE POSITION, ONE STORY": scratches and FLIP trip P&L
        # read the POSITION-level cell outcomes (one row per concluded position,
        # blended basis × total count), not the retired per-exit-fill fill pair
        # (last ENTRY before each EXIT × exit count) — which on a pieced entry
        # counted the same position many times at one leg's price. gross = net +
        # fees reconstructs the pre-fee round-trip (the scratch basis); flip_trips
        # is the position net (fee-inclusive). FLIP intents split to OPEN/HUNT.
        concluded = ledger.db.execute(
            "SELECT lane, pnl_cents, fees_cents FROM cell_outcomes").fetchall()
        total_fees = int(ledger.db.execute(
            "SELECT COALESCE(SUM(fee_cents),0) FROM fills").fetchone()[0])
        scratches = 0
        scratch_cost = 0
        flip_trips = []   # P15 R-1: net per completed FLIP TRIP (position), fee incl.
        for lane, pnl, fees in concluded:
            gross = pnl + fees          # the pre-fee round-trip (old scratch basis)
            if gross < 0:
                scratches += 1
                scratch_cost += -gross
            if lane in ("OPEN", "HUNT"):
                flip_trips.append(pnl)
        lines.append(f"scratches: {scratches} · scratch cost: {scratch_cost}¢ "
                     f"· fees: {total_fees}¢")
        # P15 R-1: the lane's own pack line is REQUIRED READING — yesterday's
        # tape measured FLIP's margin NEGATIVE at the lower bound before the
        # port called it proven. Mechanism PROVEN; margin UNPROVEN until this
        # line says otherwise.
        if flip_trips:
            from .sizing import wilson_lower_bound
            n = len(flip_trips)
            wins = sum(1 for t in flip_trips if t > 0)
            avg = sum(flip_trips) / n
            lb = wilson_lower_bound(wins, n)
            lines.append(
                f"FLIP R6: trips {n} · WR {wins / n:.0%} · net/trip "
                f"{avg:+.1f}¢ · WR-WilsonLB {lb:.0%} · bar ≥50% to break even "
                f"(+{config.HUNT_TAKE_CENTS} wins vs ~−{config.HUNT_TAKE_CENTS} "
                f"worst-typical bails) — margin UNPROVEN, mechanism proven")
        else:
            lines.append("FLIP R6: no completed trips yet · bar ≥50% to break "
                         "even · HUNT volume expected LOW (gate A is selective; "
                         "low count reads as discipline, not failure) — margin "
                         "UNPROVEN, mechanism proven")
    except Exception:
        pass
    # WO-2026-07-25-K §P3: the desk-size ladder — the trailing-N conversion, the
    # promote/demote bars, and the LIVE dial. The tape moves the size, not a
    # ruling; the desk re-earns full notional when the conversion clears the bar.
    try:
        from . import flip_ladder
        lines.append(flip_ladder.ladder_line(ledger))
    except Exception as e:
        lines.append(f"DESK SIZE: unavailable ({e})")
    # WO-2026-07-25-L §P1/P3/P4: the ghosts' pack — the pessimistic fill model's
    # assumptions (Scientist's load-bearing honesty), and each shadow lane's
    # distance to promotion (the door, marked with numbers, printed daily).
    try:
        from . import flip_ladder, shadow_fill
        lines.append(shadow_fill.MODEL_STATEMENT)
        lines.extend(flip_ladder.promotion_distance_lines(ledger))
    except Exception as e:
        lines.append(f"EARN-BACK: unavailable ({e})")
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
                how = json.loads(how_json)
            except Exception:
                how = {}
            mkt = how.get("market", "?")
            # CHUNK B(4): transient single-frame trips split from poison
            # episodes so the science can see both.
            if tag == "BOOK_INCOHERENT" and how.get("transient"):
                tag = "BOOK_INCOHERENT(transient trips)"
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
    # P22 §5: THE SCOREBOARD — the daily confrontation, sorted by margin,
    # red where it bleeds, unasked. The offense map and the fee-negative-
    # cell exposure become visible the day they exist.
    try:
        from . import scoring
        lines.extend(scoring.scoreboard_lines(ledger))
    except Exception as e:
        lines.append(f"CELL SCOREBOARD: unavailable ({e})")
    # SALV-1 §2.5: the salvage review — saves vs backstop exits vs rides,
    # with gag-reason totals. The data source for the DODGED_LOSS vs
    # SALVAGE_REGRET curves that K/S tuning requires (thresholds untouched).
    try:
        rows = ledger.db.execute(
            "SELECT detail FROM surface_rows WHERE state='SALVAGE_SUMMARY'"
            " AND ts>?", (time.time() - 86400,)).fetchall()
        fired = backstop = rides = 0
        reason_totals = {}
        for (d,) in rows:
            j = json.loads(d)
            if j.get("salvage_fired"):
                fired += 1
            elif j.get("exit_trigger") == "CATASTROPHIC":
                backstop += 1
            elif j.get("exit_trigger") == "SETTLED":
                rides += 1
            for r, n in (j.get("gagged") or {}).items():
                reason_totals[r] = reason_totals.get(r, 0) + n
        gag_s = " ".join(f"{k}×{v}" for k, v in
                         sorted(reason_totals.items())) or "none"
        lines.append(f"SALVAGE REVIEW (24h): saves {fired} · backstop exits "
                     f"{backstop} · rides-to-settlement {rides} · "
                     f"gagged ticks: {gag_s}")
    except Exception as e:
        lines.append(f"SALVAGE REVIEW: unavailable ({e})")
    # WO-FLIP-CHEAP-LIVE §3 INSTRUMENT 1: the measured swing rate — after
    # 20-30 rows the guessed 80% becomes this number, from tape.
    try:
        rows = [json.loads(d) for (d,) in ledger.db.execute(
            "SELECT detail FROM surface_rows WHERE state='FLIP_SWING'"
            " AND ts>?", (time.time() - 86400,)).fetchall()]
        if rows:
            swings = [r for r in rows if r.get("took_swing")]
            losers = [r for r in rows if r.get("gross_cents", 0) < 0]
            avg_win = (sum(r["gross_cents"] for r in swings) / len(swings)
                       if swings else 0.0)
            avg_loss = (sum(r["gross_cents"] for r in losers) / len(losers)
                        if losers else 0.0)
            net = sum(r["gross_cents"] for r in rows) / len(rows)
            breaches = ledger.db.execute(
                "SELECT COUNT(*) FROM surface_rows WHERE"
                " state='FLIP_LOSER_CUT' AND detail LIKE '%\"ok\": false%'"
                " AND ts>?", (time.time() - 86400,)).fetchone()[0]
            lines.append(
                f"FLIP SWING (24h): rate {len(swings)}/{len(rows)} "
                f"({len(swings) / len(rows):.0%}) · avg win "
                f"{avg_win:+.1f}c · avg salvaged {avg_loss:+.1f}c · "
                f"net/market {net:+.1f}c · floor breaches {breaches}"
                + (" ⚠" if breaches else ""))
        else:
            lines.append("FLIP SWING (24h): no concluded cheap entries yet")
    except Exception as e:
        lines.append(f"FLIP SWING: unavailable ({e})")
    # WO-INSTRUMENTATION-AND-FLIP-TIMING (build 51) — THE MONEY CURVE: of the
    # takes posted at each gouge level, what % filled. How much each side is worth.
    try:
        lines.append(flip_fill_rate_by_price(ledger))
    except Exception as e:
        lines.append(f"FILL-RATE BY POSTED PRICE: unavailable ({e})")
    # WO-SWING-GATE-EVENT §3: the gate's calibration — the OLD strike-touch
    # proxy (constant ~0.89 = the bug) vs Instrument 1's measured
    # took_swing, and §2's shadow two-barrier prediction. When the shadow
    # tracks the measured rate, the two-barrier gate has earned the wheel.
    try:
        rows = [json.loads(d) for (d,) in ledger.db.execute(
            "SELECT detail FROM surface_rows WHERE state='SWING_GATE_COMPARE'"
            " AND ts>?", (time.time() - 86400,)).fetchall()]
        if rows:
            def _avg(key):
                vals = [r[key] for r in rows if r.get(key) is not None]
                return sum(vals) / len(vals) if vals else None

            def _fmt(v):
                return f"{v:.2f}" if v is not None else "na"
            proxy = _avg("old_proxy_p")
            meas = _avg("measured_rate")
            pu, pd = _avg("shadow_p_up"), _avg("shadow_p_down")
            lines.append(
                f"SWING GATE CAL (24h, n={len(rows)}): old-proxy "
                f"{_fmt(proxy)} (the bug: ~const) · measured took_swing "
                f"{_fmt(meas)} · shadow p_up {_fmt(pu)}/p_down {_fmt(pd)} "
                "— shadow drives live only once it tracks measured")
    except Exception as e:
        lines.append(f"SWING GATE CAL: unavailable ({e})")
    # WO-2026-07-24-J §P5: THE DIVERGENCE READ-BACK — HUNT's forward gate on
    # trial. Realized settle vs the table's p_end vs the market's book, by
    # edge; HUNT stays PROBE until a bucket's Wilson LB clears the book.
    try:
        lines.extend(hunt_divergence_lines(ledger))
    except Exception as e:
        lines.append(f"HUNT DIVERGENCE: unavailable ({e})")
    # WO-BLEED-DIAGNOSIS §3: the data for Drew's F passthrough ruling —
    # break-even at +3-5c wins vs -93.5c tails is ~95%+; rule A-vs-B from
    # THIS number, never from one bad print.
    try:
        rows = ledger.db.execute(
            "SELECT market, SUM(pnl_cents) FROM settlements WHERE lane='F'"
            " AND divergent=0 GROUP BY market").fetchall()
        if rows:
            wins = [p for (_, p) in rows if p >= 0]
            tails = [p for (_, p) in rows if p < 0]
            avg_w = sum(wins) / len(wins) if wins else 0.0
            avg_t = sum(tails) / len(tails) if tails else 0.0
            lines.append(
                f"F TAIL (§3 ruling data): markets {len(rows)} · wins "
                f"{len(wins)} avg {avg_w:+.1f}c · tails {len(tails)} avg "
                f"{avg_t:+.1f}c · decided-against rate "
                f"{len(tails) / len(rows):.1%} (passthrough breaks even "
                "~95%+ win-rate)")
    except Exception as e:
        lines.append(f"F TAIL: unavailable ({e})")
    # DIAG-1 §3: THE DAILY ANSWERS — every morning answers "why didn't we
    # trade" before it's asked. Permanent section.
    try:
        from . import diagnostics
        lines.extend(diagnostics.pack_section(ledger))
    except Exception as e:
        lines.append(f"DIAGNOSTICS: unavailable ({e})")
    # P15 §1: THE TAPE GRADES THE DEPLOY — each WO's expected-tape section
    # runs in every pack until its lines pass twice, then retires to the
    # archive. 24h without the expected tape materializing = a FINDING (the
    # code and the world disagree), never silently forgotten.
    try:
        from scripts.tape_grade import (CHECKS, CHECKS_DIAG1, CHECKS_P16,
                                        CHECKS_P17, CHECKS_P18, CHECKS_P19,
                                        CHECKS_P21, CHECKS_P22, CHECKS_P24,
                                        CHECKS_P26, CHECKS_P27)
        suites = (("P15", "p15", CHECKS), ("P16 deposit day", "p16", CHECKS_P16),
                  ("P17 show up", "p17", CHECKS_P17),
                  ("P18 the detective", "p18", CHECKS_P18),
                  ("P19 let it run", "p19", CHECKS_P19),
                  ("P21 the doctrine engine", "p21", CHECKS_P21),
                  ("P22 the cell scoreboard", "p22", CHECKS_P22),
                  ("P24 shield, fees, reversal", "p24", CHECKS_P24),
                  ("P26 every why is a proof", "p26", CHECKS_P26),
                  ("DIAG-1 the interrogator", "diag1", CHECKS_DIAG1),
                  ("P27 the governor is the halt", "p27", CHECKS_P27))
        from scripts.tape_grade import grade
        for label, prefix, checks in suites:
            passes = int(ledger.get_state(f"{prefix}_grade_passes") or 0)
            if passes >= 2:
                lines.append(f"DEPLOY GRADE ({label}): retired — "
                             f"expected tape passed twice")
                continue
            deploy_ts = ledger.get_state(f"{prefix}_deploy_ts")
            if deploy_ts is None:
                deploy_ts = time.time()
                ledger.set_state(f"{prefix}_deploy_ts", str(deploy_ts))
            results = grade(ledger.db, checks=checks)
            fails = [r for r in results if not r[1]]
            lines.append(f"DEPLOY GRADE ({label} expected tape): "
                         f"{len(results) - len(fails)}/{len(results)}")
            for name, ok, detail in results:
                lines.append(f"  [{'PASS' if ok else 'FAIL'}] {name} — {detail}")
            if not fails:
                ledger.set_state(f"{prefix}_grade_passes", str(passes + 1))
            else:
                ledger.set_state(f"{prefix}_grade_passes", "0")
                if time.time() - float(deploy_ts) > 86400:
                    lines.append("  FINDING: expected tape has NOT materialized "
                                 "within 24h of deploy — the code and the world "
                                 "disagree; read the failing lines above")
                    from . import failures as _f
                    try:
                        _f.fail("DEPLOY_GRADE_INCOMPLETE",
                                f"({label}) {len(fails)} expected-tape line(s) "
                                f"unmet 24h after deploy: {[n for n, _, _ in fails]}")
                    except Exception:
                        pass
    except Exception as e:
        lines.append(f"DEPLOY GRADE: grader unavailable ({e})")
    # P21 B2 — KNOWLEDGE_DRIFT: the registry's tape tests run in EVERY pack;
    # unlike the deploy grades above, doctrine tests NEVER retire. A failing
    # KNOWN demotes to QUESTION here, pages once per transition, blocks
    # nothing — reality outranks the registry.
    try:
        from . import semantics
        lines.extend(semantics.drift_section(ledger))
    except Exception as e:
        lines.append(f"DOCTRINE: registry unavailable ({e})")
    # WO-DAILY-PACK-FILL-ECON (build 47): the fill-economics section — read-only
    try:
        lines.extend(fill_economics(ledger))
    except Exception as e:
        lines.append(f"FILL ECONOMICS: unavailable ({e})")
    # WO-2026-07-26-R: endpoint-lag (BOOK_STALE) by hour — the registry answer.
    try:
        lines.extend(book_stale_by_hour(ledger))
    except Exception as e:
        lines.append(f"BOOK_STALE: unavailable ({e})")
    # WO-2026-07-26-T Guard 1: the counterparty liquidity map by series/hour.
    try:
        lines.extend(no_counterparty_by_series_hour(ledger))
    except Exception as e:
        lines.append(f"NO_COUNTERPARTY: unavailable ({e})")
    # WO-2026-07-27-V T1: the cash sentinel's pulse — clean reconciles by hour.
    try:
        lines.extend(recon_cadence_lines(ledger))
    except Exception as e:
        lines.append(f"RECON CADENCE: unavailable ({e})")
    # WO-2026-07-26-S §1: the per-series chapter — one room each, from day one.
    try:
        lines.extend(series_chapter_lines(ledger))
    except Exception as e:
        lines.append(f"SERIES CHAPTERS: unavailable ({e})")
    # WO-2026-07-26-P §A3: the data-question registry rides the pack — every
    # surface with its question and last-read; unread>14d pages DATA_WITHOUT_
    # QUESTION. The pack IS a read of these surfaces, so stamp them read here.
    try:
        from . import registry
        lines.extend(registry.registry_pack_lines(ledger.db))
        registry.page_unread(ledger.db)          # page BEFORE stamping this read
        for _s in registry.registered_surfaces():
            registry.record_read(ledger.db, _s)  # the pack IS the read event
    except Exception as e:
        lines.append(f"DATA-QUESTION REGISTRY: unavailable ({e})")
    return "\n".join(lines)
