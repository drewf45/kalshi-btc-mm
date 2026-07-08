"""Daily Review Pack — 09:00 ET automated Telegram message.

Fixed format, same order every day so drift is visible.
Day-over-day deltas stored in state table.
"""

import json
import time
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from . import store, treasury, gateway, delta_table_loader, notify
from .scoreboard import wilson_bounds, F_COST_BANDS

log = logging.getLogger("k_worker.review_pack")

NY = ZoneInfo("America/New_York")
EPOCH = datetime(2026, 7, 4, tzinfo=NY)
T_BANDS_DISPLAY = [(900, 600), (600, 300), (300, 180), (180, 120), (120, 60), (60, 10)]


def _day_number() -> int:
    now = datetime.now(NY)
    return (now - EPOCH).days


def _load_yesterday() -> dict:
    raw = store.get_state("review_pack_yesterday")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return {}


def _save_today(metrics: dict) -> None:
    store.set_state("review_pack_yesterday", json.dumps(metrics))


def _delta_str(current: float, yesterday: float, fmt: str = "+.2f") -> str:
    if yesterday is None:
        return ""
    d = current - yesterday
    return f" ({d:{fmt}} vs yday)"


def _pct_delta_str(current: float, yesterday: float) -> str:
    if yesterday is None:
        return ""
    d = current - yesterday
    return f" ({d:+.1f}pp vs yday)"


def build_review_pack() -> str:
    """Build the Daily Review Pack string."""
    now_et = datetime.now(NY)
    date_str = now_et.strftime("%Y-%m-%d")
    day_n = _day_number()
    yesterday = _load_yesterday()
    today_metrics = {}

    lines = [f"=== KAL DAILY REVIEW PACK — {date_str} (day {day_n} since epoch) ==="]

    # 1. COVERAGE
    census_covered = store.get_state("census_covered")
    census_missed = store.get_state("census_missed")
    seen = int(census_covered) if census_covered else 0
    missed = int(census_missed) if census_missed else 0
    total = seen + missed
    pct = (seen / total * 100) if total > 0 else 0
    target_flag = "" if pct >= 95 else " ⚠"
    lines.append(f"1. COVERAGE: {seen}/{total} seen-live ({missed} explained) "
                 f"[{pct:.0f}% — target ≥95%]{target_flag}")
    today_metrics["coverage_pct"] = pct

    # 2. NO-FILL RATE
    daily = store.daily_stats("live-traded")
    nofill = _query_nofill_rate()
    nf_pct = nofill["rate"] * 100
    today_metrics["nofill_pct"] = nf_pct
    lines.append(f"2. NO-FILL RATE: {nf_pct:.1f}% ({nofill['nofill']}/{nofill['attempts']}) "
                 f"[baseline 22%]{_pct_delta_str(nf_pct, yesterday.get('nofill_pct'))}")

    # 3. CLIP BY TIME BAND
    lines.append("3. CLIP BY TIME BAND:")
    clip_bands = store.query_clip_by_time_band()
    for c in clip_bands:
        band_key = f"clip_{c['band']}"
        today_metrics[band_key] = c["avg_clip"]
        delta = _delta_str(c["avg_clip"], yesterday.get(band_key), "+.3f")
        flag = " ← early-band" if c["band"] == "T-900-600" else ""
        if c["n"] > 0:
            lines.append(f"   {c['band']}: n={c['n']} clip=${c['avg_clip']:+.3f}{delta}{flag}")
        else:
            lines.append(f"   {c['band']}: n=0{flag}")

    # 4. GATE MARCH (98c cell is flagship)
    lines.append("4. GATE MARCH:")
    for lo, hi in F_COST_BANDS:
        stats = store.query_band_stats(lo, hi, "live-traded", "F")
        n = stats["n"]
        be = lo / 100.0
        w_lo, _ = wilson_bounds(stats["wins"], n)
        friction = store.query_band_friction(lo, hi, "live-traded")
        friction_val = friction if friction is not None else 0
        margin = (w_lo - be - friction_val) * 100 if n > 0 else 0

        band_key = f"gate_{lo}"
        today_metrics[band_key] = margin
        delta = _delta_str(margin, yesterday.get(band_key), "+.1f")

        if lo == 98:
            lines.append(f"   98c cell N={n} LB={w_lo:.1%} margin={margin:+.1f}%{delta} ← flagship")
        elif n > 0:
            lines.append(f"   {lo}c cell N={n} margin={margin:+.1f}%{delta}")

    # 5. CAUTION LEDGER
    caution = store.query_caution_ledger()
    if caution["n"] > 0:
        lines.append(f"5. CAUTION LEDGER: captured ${caution['captured']:.2f} / "
                     f"cost ${caution['cost_of_caution']:.2f} / "
                     f"net ${caution['net_caution']:+.2f}")
    else:
        lines.append("5. CAUTION LEDGER: no data today")

    # 6. H8 DUAL-GATE
    h8_stats = _query_h8_dual_gate()
    if h8_stats["total"] > 0:
        lines.append(f"6. H8 DUAL-GATE: {h8_stats['agree']}/{h8_stats['total']} agreements")
        if h8_stats["disagreements"]:
            for dis in h8_stats["disagreements"][:5]:
                lines.append(f"   {dis}")
    else:
        lines.append("6. H8 DUAL-GATE: no H8 evaluations today")

    # 7. TABLE STATUS
    ts = delta_table_loader.table_status()
    lines.append(f"7. TABLE STATUS: {ts['detail']}")

    # 8. TREASURY
    t = treasury.get_totals()
    owed = t["accrued_tax"] + t["accrued_fee"]
    trd = treasury.tradeable_balance(t["engine_book"] + owed)
    today_metrics["treasury_book"] = t["engine_book"]
    today_metrics["treasury_owed"] = owed
    today_metrics["treasury_tradeable"] = trd
    book_delta = _delta_str(t["engine_book"], yesterday.get("treasury_book"))
    owed_delta = _delta_str(owed, yesterday.get("treasury_owed"))
    paid_lifetime = t["paid_tax"] + t["paid_fee"]
    lines.append(f"8. TREASURY: tradeable ${trd:.2f} | owed ${owed:.2f}{owed_delta} | "
                 f"book ${t['engine_book']:.2f}{book_delta} | "
                 f"lifetime collected ${paid_lifetime:.2f}")

    # 9. NEW ALERT TYPES
    new_alerts = _query_new_alert_types()
    if new_alerts:
        lines.append(f"9. NEW ALERT TYPES SINCE LAST PACK: {', '.join(new_alerts)}")
    else:
        lines.append("9. NEW ALERT TYPES SINCE LAST PACK: none")

    # VERDICT LINE
    issues = []
    if pct < 95 and total > 0:
        issues.append(f"coverage {pct:.0f}%")
    if nf_pct > 30:
        issues.append(f"no-fill {nf_pct:.0f}%")
    clip_early = next((c for c in clip_bands if c["band"] == "T-900-600"), None)
    if clip_early and clip_early["n"] > 0 and clip_early["avg_clip"] < 0:
        issues.append(f"early-band clip ${clip_early['avg_clip']:+.3f}")

    if issues:
        lines.append(f"VERDICT: ⚠ {', '.join(issues)}")
    else:
        lines.append("VERDICT: all nominal")

    _save_today(today_metrics)
    return "\n".join(lines)


def _query_nofill_rate() -> dict:
    """Today's no-fill rate from surface rows."""
    today_start = store.et_midnight_ts()
    with store._lock:
        rows = store._conn.execute(
            """SELECT
                COUNT(CASE WHEN resolution='no_fill' OR why_tag LIKE '%NO_FILL%' THEN 1 END),
                COUNT(*)
               FROM surface
               WHERE action='ENTER' AND decision_ts >= ?
               AND env='live-traded'""",
            (today_start,),
        ).fetchone()
    nofill = rows[0] if rows else 0
    attempts = rows[1] if rows else 0
    return {
        "nofill": nofill,
        "attempts": attempts,
        "rate": nofill / attempts if attempts > 0 else 0.0,
    }


def _query_h8_dual_gate() -> dict:
    """Scan today's H8 why_tags for dual-gate agreement/disagreement."""
    today_start = store.et_midnight_ts()
    with store._lock:
        rows = store._conn.execute(
            """SELECT why_tag, resolution FROM surface
               WHERE lane='H8' AND decision_ts >= ?
               AND why_tag LIKE '%TBL_%'""",
            (today_start,),
        ).fetchall()
    agree = 0
    total = 0
    disagreements = []
    for tag, resolution in rows:
        total += 1
        if "|TBL_" in tag:
            if "_PASS" in tag:
                agree += 1
            elif "_FAIL" in tag:
                outcome = resolution or "pending"
                disagreements.append(f"DISAGREE: {tag[:60]}... → {outcome}")
    return {"agree": agree, "total": total, "disagreements": disagreements}


def _query_new_alert_types() -> list:
    """Find alert types (skip_reason/reject_code) seen today but not yesterday."""
    today_start = store.et_midnight_ts()
    yesterday_start = today_start - 86400
    with store._lock:
        today_types = store._conn.execute(
            """SELECT DISTINCT COALESCE(skip_reason, why_tag) FROM surface
               WHERE decision_ts >= ? AND action='SKIP'
               AND COALESCE(skip_reason, why_tag) IS NOT NULL""",
            (today_start,),
        ).fetchall()
        yesterday_types = store._conn.execute(
            """SELECT DISTINCT COALESCE(skip_reason, why_tag) FROM surface
               WHERE decision_ts >= ? AND decision_ts < ? AND action='SKIP'
               AND COALESCE(skip_reason, why_tag) IS NOT NULL""",
            (yesterday_start, today_start),
        ).fetchall()
    today_set = {r[0] for r in today_types}
    yesterday_set = {r[0] for r in yesterday_types}
    new = today_set - yesterday_set
    return sorted(new)[:10]


def send_review_pack() -> None:
    """Build and send the Daily Review Pack to Telegram."""
    import html
    text = build_review_pack()
    log.info(f"[REVIEW_PACK]\n{text}")
    notify.send(f"<pre>{html.escape(text)}</pre>")


def is_review_time() -> bool:
    """Check if it's 09:00-09:05 ET (5-minute window for the daily send)."""
    now = datetime.now(NY)
    return now.hour == 9 and now.minute < 5
