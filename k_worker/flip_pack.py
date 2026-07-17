"""k_worker/flip_pack.py — WO-LANE-FLIP-3 §4: the hourly pack, book truth not vibes.

Reports the account in real DOLLARS (via the engine's proven get_balance — the one that
read $11.24 correctly for weeks), a per-window table from the flip_windows ledger (§3),
and a day rollup whose Δ$ is EXPLAINED to the cent: settled P&L + fees, with any
unexplained residual ≥ 2¢ printed red and paged. The book can no longer move silently.

The pure formatters (fmt_window / reconcile / build_pack) import nothing crypto-bound, so
they unit-test in the offline sandbox; only hourly() touches the live client (lazy import).
"""

import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import store, notify

NY = ZoneInfo("America/New_York")

RESID_ALERT_CENTS = 2          # §4: unexplained residual ≥ 2¢ prints red + pages Drew


def _daystr(now_ts: float) -> str:
    return datetime.fromtimestamp(now_ts, NY).strftime("%Y-%m-%d")


def fmt_window(w: dict) -> str:
    """One window line: `W20:00 NETTED_2R 98→ +8¢ flat✓`."""
    tag = w.get("tag") or "??:??"
    outcome = w.get("outcome_tag") or "?"
    bundle = w.get("bundle_cost")
    realized = int(w.get("realized_cents") or 0)
    flat = w.get("broker_flat")
    flat_mark = "flat✓" if flat == 1 else ("🚨inv" if flat == 0 else "flat?")
    head = f"{bundle}→" if bundle is not None else "lone→"
    sign = "+" if realized >= 0 else ""
    return f"W{tag} {outcome} {head} {sign}{realized}¢ {flat_mark}"


def reconcile(account_delta_usd: float, settled_pnl_usd: float,
              thresh_cents: int = RESID_ALERT_CENTS):
    """Δ$ − settled P&L = unexplained. Returns (unexplained_usd, flagged)."""
    unexplained = account_delta_usd - settled_pnl_usd
    flagged = round(abs(unexplained) * 100) >= thresh_cents
    return unexplained, flagged


def build_pack(*, account_usd: float, midnight_usd: float, hour_windows: list,
               day_windows: list, settled_pnl_usd: float, fees_usd: float):
    """Assemble the pack text. Returns (text, unexplained_usd, flagged)."""
    delta = account_usd - midnight_usd
    unexplained, flagged = reconcile(delta, settled_pnl_usd)

    entered = sum(1 for w in day_windows
                  if not str(w.get("outcome_tag") or "").startswith("SAT"))
    sat = sum(1 for w in day_windows
              if str(w.get("outcome_tag") or "").startswith("SAT"))
    captured_pending = sum(int(w.get("realized_cents") or 0) for w in day_windows)

    lines = ["🔁 <b>FLIP HOURLY</b>",
             f"account: ${account_usd:.2f} (Δ {delta:+.2f} today)"]
    if hour_windows:
        for w in hour_windows:
            lines.append("  " + fmt_window(w))
    else:
        lines.append("  (no windows this hour)")
    lines.append(f"day: {entered} entered · {sat} sat · captured {captured_pending:+d}¢ "
                 f"(pending settlement)")
    resid = "🔴" if flagged else "✓"
    lines.append(f"Δ$ = settled ${settled_pnl_usd:+.2f} (fees ${fees_usd:.2f} incl.) "
                 f"— unexplained ${unexplained:+.2f} {resid}")
    return "\n".join(lines), unexplained, flagged


def hourly(client) -> str:
    """Build the hourly pack from live balance + the flip_windows ledger, alert on an
    unexplained book move. Returns the pack text ('' if balance unreadable)."""
    from . import kalshi                     # lazy: keep module import crypto-free for tests
    now = time.time()
    cash, pv = kalshi.get_balance(client)
    if cash is None:
        return ""
    account = cash + (pv or 0)

    key = "flip_midnight_bal:" + _daystr(now)
    raw = store.get_state(key)
    if raw is None:                          # first read of the ET day anchors midnight
        store.set_state(key, f"{account:.4f}")
        midnight_bal = account
    else:
        try:
            midnight_bal = float(raw)
        except (TypeError, ValueError):
            midnight_bal = account

    mid = store.et_midnight_ts()
    hour_windows = store.flip_windows_between(now - 3600, now)
    day_windows = store.flip_windows_between(mid, now)
    daily = store.daily_stats("live-traded")
    settled_pnl = float(daily.get("net_pnl") or 0.0)
    fees = float(daily.get("total_fees") or 0.0)

    text, unexplained, flagged = build_pack(
        account_usd=account, midnight_usd=midnight_bal,
        hour_windows=hour_windows, day_windows=day_windows,
        settled_pnl_usd=settled_pnl, fees_usd=fees,
    )
    if flagged:
        notify.alert(f"🔴 UNEXPLAINED BOOK MOVE ${unexplained:+.2f} — account Δ not "
                     f"explained by settled P&L (≥{RESID_ALERT_CENTS}¢). Investigate.")
    return text
