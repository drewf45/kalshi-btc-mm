"""THE INTERROGATOR (DIAG-1) — questions ship as code, answers come back
on the phone.

Architecture law: the builder cannot see the live DB — Claude Code writes,
Render runs, TELEGRAM IS THE ONLY WAY ANSWERS COME BACK. So any question
becomes a registry entry here: it runs itself ONCE on the next boot (after
reconcile, before the first cycle), pages its answer in chunks, and marks
itself done (`diag_done:{id}` in engine state). Read-only, always; when
data is missing it says so honestly (§1.1).

§4 THE STANDING PATTERN: a future question = a new (id, fn) entry below.
The code carries the question in; the tape carries the answer out.
"""

import logging
import re
import statistics
import time
from typing import List

from . import config

log = logging.getLogger("relay.diagnostics")

TELEGRAM_CHUNK = 3500          # under the 4096 hard limit, with headroom

# DIAG-001 §0: the two rival hypotheses, separable by one histogram
_GAP_RE = re.compile(r"TABLE_NON_REVERSAL p=([0-9.]+)<bar([0-9.]+)"
                     r"(?: d=([0-9.]+) t=([0-9.]+))?")


def _chunk(text: str) -> List[str]:
    """Chunked Telegram paging — split on line boundaries, every chunk
    under the limit."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > TELEGRAM_CHUNK and cur:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks


def diag_f_silence(engine, now=None) -> str:
    """DIAG-001 F-SILENCE — why was the founding lane quiet? The §0
    question: p_cross is built from candle highs/lows (ANY-TOUCH); a
    settle-at-close position can be touched intraday and still finish
    green. One histogram separates H-SEMANTICS from H-MARKET."""
    now = time.time() if now is None else now
    since = now - 24 * 3600
    rows = engine.ledger.db.execute(
        "SELECT detail FROM surface_rows WHERE lane='F' AND"
        " state IN ('WATCHING','PASS') AND ts>? ORDER BY ts",
        (since,)).fetchall()
    if not rows:
        return ("🔎 DIAG F-SILENCE: NO F pass rows in the last 24h — the "
                "lane never even reached a decision (data honestly absent; "
                "check the brain line and window lifecycle first)")
    reasons = {}
    gaps, samples = [], []
    for (detail,) in rows:
        reason = (detail or "?").split(" ", 1)[0]
        reasons[reason] = reasons.get(reason, 0) + 1
        m = _GAP_RE.search(detail or "")
        if m:
            surv, bar = float(m.group(1)), float(m.group(2))
            gaps.append(bar - surv)
            samples.append((surv, bar,
                            m.group(3) or "n/a", m.group(4) or "n/a"))
    lines = [f"🔎 DIAG F-SILENCE: {len(rows)} passes · reasons: "
             + ", ".join(f"{r}: {c}" for r, c in
                         sorted(reasons.items(), key=lambda kv: -kv[1]))]
    if gaps:
        lo, med, hi = min(gaps), statistics.median(gaps), max(gaps)
        uniform = (hi - lo) <= 0.04 and 0.02 <= lo and hi <= 0.06
        lines.append(f"   surv-vs-bar gap: min {lo:.3f} · median {med:.3f}"
                     f" · max {hi:.3f} · uniform 2-6pt? "
                     f"{'yes' if uniform else 'no'}")
        step = max(1, len(samples) // 8)
        picks = samples[::step][:8]
        lines.append("   sample (surv, bar, d, t): "
                     + " ".join(f"({s:.2f},{b:.2f},{d},{t})"
                                for s, b, d, t in picks))
        if any(d == "n/a" for _, _, d, _ in picks):
            lines.append("   note: (d,t) absent on pre-DIAG rows — new "
                         "rows carry them (honest gap, closes itself)")
        table_mute_share = reasons.get("TABLE_NON_REVERSAL", 0) / len(rows)
        if uniform:
            hint = ("H-SEMANTICS — uniform definitional gap: the table "
                    "proves any-touch, the position settles at close. "
                    "Flip F_PROOF_MODE=v2 on Render.")
        elif table_mute_share < 0.5:
            hint = ("DISCIPLINE — table mutes are the minority; the varied "
                    "reasons above are the story. Touch nothing.")
        else:
            hint = ("H-MARKET — gaps vary widely: the mute is protecting "
                    "real edge-absence; F meets its scoreboard honestly. "
                    "Touch nothing.")
        lines.append(f"   verdict hint: {hint}")
    else:
        lines.append("   surv-vs-bar: no TABLE_NON_REVERSAL rows — the "
                     "table gate never fired (brain absent all night, or "
                     "other reasons above); verdict hint: DISCIPLINE — "
                     "read the reason histogram")
    try:
        from . import delta_builder
        doc = (delta_builder.compute_delta_table.__doc__ or "")
        sem = next((ln.strip() for ln in doc.splitlines()
                    if "p_cross measures" in ln
                    or "ANY-TOUCH" in ln), "").strip()
        follow = next((ln.strip() for ln in doc.splitlines()
                       if "excursion" in ln or "NOT the at-close" in ln), "")
        quoted = f"{sem} {follow}".strip() or doc.strip().splitlines()[0]
        lines.append(f'   TABLE SEMANTICS: "{quoted}"')
    except Exception as e:
        lines.append(f"   TABLE SEMANTICS: builder docstring unreadable "
                     f"({e})")
    return "\n".join(lines)


def diag_table_coverage(engine, now=None) -> str:
    """DIAG-002 TABLE-COVERAGE — the (d,t) miss log summarized: top missing
    cells by frequency + the loaded grid's bounds. Feeds the regrid."""
    from . import delta
    rows = engine.ledger.db.execute(
        "SELECT how_json, COUNT(*) FROM failures WHERE"
        " why_tag='TABLE_CELL_MISS' GROUP BY how_json"
        " ORDER BY COUNT(*) DESC LIMIT 10").fetchall()
    lines = ["🔎 DIAG TABLE-COVERAGE:"]
    if rows:
        import json as _json
        for how, n in rows:
            try:
                d = _json.loads(how)
                lines.append(f"   miss d={d.get('d')} t={d.get('t')} "
                             f"session={d.get('session')} ×{n}")
            except Exception:
                lines.append(f"   miss (unparsed) ×{n}")
    else:
        lines.append("   zero persisted (d,t) misses — either full grid "
                     "coverage or the table was never loaded to miss "
                     "against (see the brain line)")
    if delta.is_loaded() and delta._TABLE:
        ds = [k[0] for k in delta._TABLE]
        ts = [k[1] for k in delta._TABLE]
        sessions = sorted({k[2] for k in delta._TABLE})
        lines.append(f"   loaded grid: d ${min(ds)}-${max(ds)} · "
                     f"t {min(ts)}-{max(ts)}s · cells {len(delta._TABLE)}"
                     f" · sessions {','.join(sessions)}")
    else:
        lines.append("   grid bounds: table not loaded right now "
                     f"({delta.refusal_reason() or 'unknown'})")
    return "\n".join(lines)


# §4: THE REGISTRY — any future question is one more (id, fn) entry.
REGISTRY = [
    ("DIAG-001", diag_f_silence),
    ("DIAG-002", diag_table_coverage),
]


def run_boot_diagnostics(engine, now=None) -> int:
    """§1.1: execute each un-done diagnostic ONCE ever — sentinel row
    `diag_done:{id}` in engine state. Chunk-paged, read-only, never
    boot-fatal. Returns how many ran."""
    ran = 0
    for diag_id, fn in REGISTRY:
        if engine.ledger.get_state(f"diag_done:{diag_id}"):
            continue
        try:
            answer = fn(engine, now=now)
        except Exception as e:
            answer = f"🔎 {diag_id}: diagnostic error — {e} (still marked done)"
            log.error("diagnostic %s failed: %s", diag_id, e, exc_info=True)
        for chunk in _chunk(answer):
            engine.telegram.alert(chunk)
        engine.ledger.set_state(f"diag_done:{diag_id}", "1")
        ran += 1
    return ran


# ── §3: THE DAILY ANSWERS — the pack's permanent DIAGNOSTICS section ───────
def pack_section(ledger, now=None) -> List[str]:
    """Every morning answers "why didn't we trade" before it's asked:
    pass-reason histogram per lane, anchor sources + top misses, and the
    proof-mode line with entries per era."""
    now = time.time() if now is None else now
    since = now - 24 * 3600
    lines = ["DIAGNOSTICS (last 24h):"]
    rows = ledger.db.execute(
        "SELECT lane, detail FROM surface_rows WHERE"
        " state IN ('WATCHING','PASS') AND ts>?", (since,)).fetchall()
    per_lane: dict = {}
    for lane, detail in rows:
        reason = (detail or "?").split(" ", 1)[0]
        per_lane.setdefault(lane, {})
        per_lane[lane][reason] = per_lane[lane].get(reason, 0) + 1
    if per_lane:
        for lane in sorted(per_lane):
            top = sorted(per_lane[lane].items(), key=lambda kv: -kv[1])[:4]
            lines.append(f"  {lane} passes: "
                         + " · ".join(f"{r}×{c}" for r, c in top))
    else:
        lines.append("  no pass rows this window")
    anchors = dict(ledger.db.execute(
        "SELECT CASE WHEN detail LIKE '%anchor=table%' THEN 'table'"
        " ELSE 'price' END, COUNT(*) FROM surface_rows WHERE"
        " state='ENTERED' AND detail LIKE '%anchor=%' AND ts>?"
        " GROUP BY 1", (since,)).fetchall())
    misses = ledger.db.execute(
        "SELECT COUNT(*) FROM failures WHERE why_tag='TABLE_CELL_MISS'"
        " AND ts>?", (since,)).fetchone()[0]
    lines.append(f"  anchors: table×{anchors.get('table', 0)} "
                 f"price×{anchors.get('price', 0)} · "
                 f"(d,t) misses: {misses}")
    eras = dict(ledger.db.execute(
        "SELECT COALESCE(NULLIF(proof, ''), 'v1'), COUNT(*) FROM"
        " cell_outcomes WHERE ts>? GROUP BY 1", (since,)).fetchall())
    era_s = " ".join(f"{k}×{v}" for k, v in sorted(eras.items())) or "none"
    lines.append(f"  F-proof: {config.F_PROOF_MODE} · closed risk by era: "
                 f"{era_s}")
    return lines
