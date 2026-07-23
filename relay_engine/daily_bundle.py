"""WO-2026-07-22-K — THE DAILY BUNDLE. One command (/daily), one .xlsx, every
table the engine logged, bounded to the day, tappable on a phone.

READ-ONLY by construction (§5): the export opens its OWN `mode=ro` connection to
the ledger DB — it can never take a write lock on the settle path. The workbook
is built in /tmp, sent, and deleted by the caller (nothing accumulates on the
cash-rail disk). The xlsx is written with the stdlib alone (a zip of OOXML with
inline strings) — no third-party dependency to miss on a deploy.

The heart of it is `surface_rows.detail` — the engine's own free-text account of
what it looked at and why it acted — placed in the same workbook as
`cell_outcomes` (the per-trade outcomes), so the scoreboard-to-setup join is a
lookup within one file.
"""
import logging
import os
import sqlite3
import time
import zipfile
from typing import List, Optional, Tuple
from xml.sax.saxutils import escape

from . import scoring

log = logging.getLogger("relay.daily")


class _LedgerRO:
    """Minimal read-only ledger shim: `scoring`'s self-audit functions read the
    day's numbers through `.db.execute(...)` alone, so a wrapper over the export's
    own `mode=ro` connection lets the pack compute the honest scoreboard WITHOUT a
    second DB handle and WITHOUT any write path. It exposes nothing that could
    take a lock — reads only (§5 read-only-by-construction)."""

    __slots__ = ("db",)

    def __init__(self, conn):
        self.db = conn


# The honest scoreboard's columns, in phone-reading order: identity, evidence,
# the model, the MONEY beside it (B1), then the model graded against the actual
# loss (B2 — be_implied is what the realized loss demands; model_error is the gap).
_SCOREBOARD_COLS = ("lane", "cell", "n", "W", "LB", "be_modeled", "margin",
                    "pnl_day_c", "pnl_life_c", "loss_modeled_c",
                    "loss_actual_c", "be_implied", "model_error")

# a cell whose model disagrees with realized losses by more than this is flagged
# in SUMMARY (model health): the break-even is off by >8 points of win-rate.
_MODEL_ERROR_FLAG = 0.08
# thin-evidence threshold — cells below this are "open questions", not verdicts.
_THIN_N = 10

# §1: every logged table, in the order the sheets appear. SCOREBOARD (computed)
# is prepended by the builder. `booked_fills`, `window_econ`, `failures`,
# `d_budget_decisions` are created lazily by their writers, so a table that does
# not exist yet is skipped, not an error. `engine_state` (internal offsets) is
# deliberately excluded (§1: "skip — internal").
TABLE_SHEETS: Tuple[Tuple[str, str], ...] = (
    ("decisions", "surface_rows"),          # 🥇 the reasoning ledger (detail)
    ("cell_outcomes", "cell_outcomes"),     # 🥇 the join key to conditions
    ("fills", "fills"),
    ("booked_fills", "booked_fills"),
    ("settlements", "settlements"),
    ("window_econ", "window_econ"),
    ("window_outcomes", "window_outcomes"),
    ("cash_movements", "cash_movements"),
    ("failures", "failures"),
    ("boots", "boots"),
    ("budget", "d_budget_decisions"),
    # book_sample (book_snapshots) is handled specially — decimated (§3).
)
BOOK_TABLE = "book_snapshots"

# Telegram bot document limit is 50 MB; stay under it with headroom (§5 guard).
DEFAULT_SIZE_LIMIT = 48 * 1024 * 1024


def _start_of_day(now: float, days_back: int = 0) -> float:
    """Local midnight `days_back` days ago (Drew's 'bounded by the day')."""
    lt = time.localtime(now - days_back * 86400)
    midnight = time.struct_time((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0,
                                 lt.tm_wday, lt.tm_yday, lt.tm_isdst))
    return time.mktime(midnight)


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def _columns(conn, name: str) -> List[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({name})").fetchall()]


def _read_table(conn, table: str, day_start: float) -> Tuple[List[str], list]:
    """Header + rows for a table, scoped to `ts >= day_start` when the table has
    a `ts` column (else the whole table — those are small)."""
    cols = _columns(conn, table)
    if "ts" in cols:
        rows = conn.execute(
            f"SELECT * FROM {table} WHERE ts >= ? ORDER BY ts", (day_start,)
        ).fetchall()
    else:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    return cols, [list(r) for r in rows]


def _trade_ts(conn, day_start: float) -> List[float]:
    """The moments that matter — every fill ts today (§3: keep full resolution
    within ±pad of an entry/exit)."""
    if not _table_exists(conn, "fills"):
        return []
    return [r[0] for r in conn.execute(
        "SELECT ts FROM fills WHERE ts >= ? ORDER BY ts", (day_start,)).fetchall()]


def _read_book_sample(conn, day_start: float, decimate_s: int,
                      trade_pad_s: int) -> Tuple[List[str], list]:
    """book_snapshots DECIMATED (§3): one frame every `decimate_s` seconds, PLUS
    every frame within ±`trade_pad_s` of a fill (the moments that matter keep
    full resolution). ~86k rows/day → ~5.7k at N=15."""
    cols = _columns(conn, BOOK_TABLE)
    trades = _trade_ts(conn, day_start)
    kept: list = []
    last_kept = -1e18
    for row in conn.execute(
            f"SELECT * FROM {BOOK_TABLE} WHERE ts >= ? ORDER BY ts",
            (day_start,)):
        ts = row[cols.index("ts")]
        near_trade = any(abs(ts - t) <= trade_pad_s for t in trades)
        if near_trade or (ts - last_kept) >= decimate_s:
            kept.append(list(row))
            if not near_trade:
                last_kept = ts
    return cols, kept


# ── the minimal stdlib xlsx writer (a zip of OOXML, inline strings) ──────────
def _cell_xml(col_idx: int, row_idx: int, value) -> str:
    ref = f"{_col_letter(col_idx)}{row_idx}"
    if value is None:
        return f'<c r="{ref}"/>'
    if isinstance(value, bool):
        value = 1 if value else 0
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{ref}"><v>{value}</v></c>'
    s = escape(str(value))
    return (f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">'
            f'{s}</t></is></c>')


def _col_letter(idx: int) -> str:
    s = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        s = chr(65 + rem) + s
    return s


def _sheet_xml(header: List[str], rows: list) -> str:
    parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
             '<worksheet xmlns="http://schemas.openxmlformats.org/'
             'spreadsheetml/2006/main"><sheetData>']
    parts.append("<row r=\"1\">"
                 + "".join(_cell_xml(c, 1, h) for c, h in enumerate(header))
                 + "</row>")
    for ri, row in enumerate(rows, start=2):
        parts.append(f'<row r="{ri}">'
                     + "".join(_cell_xml(c, ri, v) for c, v in enumerate(row))
                     + "</row>")
    parts.append("</sheetData></worksheet>")
    return "".join(parts)


def _safe_sheet_name(name: str, used: set) -> str:
    # Excel: <=31 chars, no []:*?/\, unique.
    n = "".join(ch for ch in name if ch not in '[]:*?/\\')[:31] or "sheet"
    base, i = n, 1
    while n in used:
        suffix = f"~{i}"
        n = base[:31 - len(suffix)] + suffix
        i += 1
    used.add(n)
    return n


def _write_xlsx(path: str, sheets: List[Tuple[str, List[str], list]]) -> None:
    """sheets = [(name, header, rows), ...]."""
    used: set = set()
    named = [(_safe_sheet_name(n, used), h, r) for n, h, r in sheets]
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
        'content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-'
        'package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.'
        'openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
                  'ContentType="application/vnd.openxmlformats-officedocument.'
                  'spreadsheetml.worksheet+xml"/>'
                  for i in range(1, len(named) + 1))
        + '</Types>')
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
        'relationships"><Relationship Id="rId1" Type="http://schemas.'
        'openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/></Relationships>')
    sheets_xml = "".join(
        f'<sheet name="{escape(n)}" sheetId="{i}" r:id="rId{i}"/>'
        for i, (n, _, _) in enumerate(named, start=1))
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/'
        'main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/'
        f'relationships"><sheets>{sheets_xml}</sheets></workbook>')
    wb_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
        'relationships">'
        + "".join(f'<Relationship Id="rId{i}" Type="http://schemas.'
                  'openxmlformats.org/officeDocument/2006/relationships/'
                  f'worksheet" Target="worksheets/sheet{i}.xml"/>'
                  for i in range(1, len(named) + 1))
        + '</Relationships>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", root_rels)
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        for i, (_, header, rows) in enumerate(named, start=1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", _sheet_xml(header, rows))


def _scoreboard_sheet(scoreboard: List[dict]) -> Tuple[List[str], list]:
    """The honest scoreboard as a STRUCTURED table (B1/B2), one row per cell —
    realized MONEY beside the model, and the model graded against the actual
    loss. `None`s (no losses yet ⇒ no implied BE) render as blank cells."""
    rows = [[r.get(c) for c in _SCOREBOARD_COLS] for r in scoreboard]
    return list(_SCOREBOARD_COLS), rows


def _fees_by_lane_action(conn, day_start: float) -> List[Tuple[str, str, int, int]]:
    """(lane, action, n_fills, fee_cents) from today's fills — B6 (the venue's
    cut, split by lane×action). fills carries `action` and `fee_cents`."""
    if not _table_exists(conn, "fills"):
        return []
    return [(str(r[0]), str(r[1]), int(r[2]), int(r[3])) for r in conn.execute(
        "SELECT lane, action, COUNT(*), COALESCE(SUM(fee_cents),0) FROM fills"
        " WHERE ts >= ? GROUP BY lane, action ORDER BY lane, action",
        (day_start,)).fetchall()]


def _fills_by_size(conn, day_start: float) -> List[Tuple[str, int, int]]:
    """(size_tier, n_fills, contracts) today — B8 (do the bigger tiers actually
    fill?). Read-only from fills.size_tier/count."""
    if not _table_exists(conn, "fills"):
        return []
    return [(str(r[0]), int(r[1]), int(r[2])) for r in conn.execute(
        "SELECT size_tier, COUNT(*), COALESCE(SUM(count),0) FROM fills"
        " WHERE ts >= ? GROUP BY size_tier ORDER BY size_tier",
        (day_start,)).fetchall()]


def _failures_by_tag(conn, day_start: float) -> List[Tuple[str, int]]:
    """(why_tag, n) ranked — C3 (what stopped the engine, most-common first)."""
    if not _table_exists(conn, "failures"):
        return []
    return [(str(r[0]), int(r[1])) for r in conn.execute(
        "SELECT why_tag, COUNT(*) FROM failures WHERE ts >= ?"
        " GROUP BY why_tag ORDER BY COUNT(*) DESC", (day_start,)).fetchall()]


def _summary_sheet(ledger, conn, scoreboard: List[dict],
                   day_start: float) -> Tuple[List[str], list]:
    """§4 — the SUMMARY that LEADS the workbook: money, expectation, model
    health, fees, anomalies, open questions. Three columns [section, item,
    value] so it reads on a phone. Everything here is a read of what the day
    already logged — no model output is trusted without its realized number
    beside it."""
    rows: list = []

    def sec(section, item, value):
        rows.append([section, item, value])

    # ── MONEY (B1 / C2): the edge number is fills P&L, per lane, day + life ──
    money = scoring.fills_pnl_by_lane(ledger, day_start)
    day_tot = sum((m["pnl_day_c"] or 0) for m in money)
    life_tot = sum(m["pnl_life_c"] for m in money)
    sec("MONEY", "fills P&L today (all lanes) ¢", day_tot)
    sec("MONEY", "fills P&L lifetime (all lanes) ¢", life_tot)
    for m in money:
        sec("MONEY", f"{m['lane']}  day / life ¢",
            f"{m['pnl_day_c'] if m['pnl_day_c'] is not None else '-'} / "
            f"{m['pnl_life_c']}")

    # ── EXPECTATION (light B7): trades and hit-rate today ──
    n_day = int(conn.execute(
        "SELECT COUNT(*) FROM cell_outcomes WHERE ts >= ?",
        (day_start,)).fetchone()[0]) if _table_exists(conn, "cell_outcomes") else 0
    w_day = int(conn.execute(
        "SELECT COALESCE(SUM(won),0) FROM cell_outcomes WHERE ts >= ?",
        (day_start,)).fetchone()[0]) if n_day else 0
    sec("EXPECTATION", "cell outcomes today (n)", n_day)
    sec("EXPECTATION", "wins today", w_day)
    sec("EXPECTATION", "hit rate today",
        f"{(w_day / n_day):.3f}" if n_day else "-")

    # ── MODEL HEALTH (B2): cells whose modeled BE disagrees with the realized
    # loss by more than the flag — the self-audit surfacing its own worst calls.
    graded = [r for r in scoreboard if r["model_error"] is not None]
    flagged = sorted((r for r in graded
                      if abs(r["model_error"]) >= _MODEL_ERROR_FLAG),
                     key=lambda r: abs(r["model_error"]), reverse=True)
    sec("MODEL HEALTH", "cells graded vs realized loss", len(graded))
    sec("MODEL HEALTH", f"cells off by >={_MODEL_ERROR_FLAG:.0%} win-rate",
        len(flagged))
    for r in flagged[:8]:
        sec("MODEL HEALTH",
            f"{r['lane']} {r['cell']}  be_mod/be_impl",
            f"{r['be_modeled']} / {r['be_implied']}  (err {r['model_error']:+})")

    # ── FEES (B6): the venue's cut by lane×action + total ──
    fees = _fees_by_lane_action(conn, day_start)
    sec("FEES", "total fees today ¢", sum(f[3] for f in fees))
    for lane, action, nf, fee in fees:
        sec("FEES", f"{lane} {action}  (n={nf})", fee)

    # ── FILLS BY SIZE (B8): did the bigger tiers fill? ──
    for tier, nf, contracts in _fills_by_size(conn, day_start):
        sec("FILLS BY SIZE", f"{tier}  fills / contracts", f"{nf} / {contracts}")

    # ── ANOMALIES (C3): failures by tag, ranked ──
    fails = _failures_by_tag(conn, day_start)
    sec("ANOMALIES", "failures logged today", sum(f[1] for f in fails))
    for tag, n in fails[:10]:
        sec("ANOMALIES", tag, n)

    # ── OPEN QUESTIONS: thin-evidence cells (verdicts we cannot yet trust),
    # and the deltas gap (B4) called out honestly rather than left blank. ──
    thin = [r for r in scoreboard if r["n"] < _THIN_N]
    sec("OPEN QUESTIONS", f"cells with n<{_THIN_N} (thin evidence)", len(thin))
    for r in thin[:8]:
        sec("OPEN QUESTIONS", f"{r['lane']} {r['cell']}  n", r["n"])
    sec("OPEN QUESTIONS", "day-over-day deltas (B4)",
        "not yet — no prior-day snapshot persisted")
    sec("OPEN QUESTIONS", "skips + counterfactuals (B5)",
        "partial — see failures/decisions sheets")

    return ["section", "item", "value"], rows


def build_daily_workbook(db_path: str, scoreboard_lines: List[str],
                         out_path: str, now: Optional[float] = None,
                         days_back: int = 0, decimate_s: int = 15,
                         trade_pad_s: int = 10,
                         size_limit: int = DEFAULT_SIZE_LIMIT) -> dict:
    """Build the day's workbook at `out_path` from a READ-ONLY connection to
    `db_path`. Returns {path, sheets, rows, dropped_book, note}. Never touches
    the trading path — it opens its own `mode=ro` connection."""
    now = time.time() if now is None else now
    day_start = _start_of_day(now, days_back)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        ledger_ro = _LedgerRO(conn)
        # the honest self-audit (A1/A2/A3-corrected margins, realized MONEY, and
        # the model graded against actual losses). Computed pack-side — the
        # trading path still reads the untouched breakeven()/SALVAGE_ADJ_MIN_N,
        # so F and OPEN trade byte-identically (acceptance #9).
        board = scoring.scoreboard_rows(ledger_ro, day_start)
        sc_header, sc_rows = _scoreboard_sheet(board)
        sm_header, sm_rows = _summary_sheet(ledger_ro, conn, board, day_start)
        sheets: List[Tuple[str, List[str], list]] = [
            ("SUMMARY", sm_header, sm_rows),           # §4 — leads the workbook
            ("SCOREBOARD", sc_header, sc_rows),        # B1/B2 — the self-audit
            # the legacy text scoreboard (what /scoreboard prints) kept for
            # continuity, after the structured truth.
            ("scoreboard_txt", ["line"], [[ln] for ln in scoreboard_lines])]
        total_rows = 0
        for sheet_name, table in TABLE_SHEETS:
            if not _table_exists(conn, table):
                continue
            header, rows = _read_table(conn, table, day_start)
            sheets.append((sheet_name, header, rows))
            total_rows += len(rows)
        dropped_book = False
        if _table_exists(conn, BOOK_TABLE):
            bh, brows = _read_book_sample(conn, day_start, decimate_s,
                                          trade_pad_s)
            sheets.append(("book_sample", bh, brows))
    finally:
        conn.close()

    _write_xlsx(out_path, sheets)
    note = ""
    # §5 size guard: if the finished file blows the limit, drop book_sample and
    # rebuild — send the small sheets rather than fail silently.
    if os.path.getsize(out_path) > size_limit:
        dropped_book = True
        sheets = [s for s in sheets if s[0] != "book_sample"]
        _write_xlsx(out_path, sheets)
        note = (" (book_sample DROPPED — over the size limit; "
                "raise decimate_s or use /daily on a quieter day)")
    return {"path": out_path, "sheets": len(sheets), "rows": total_rows,
            "dropped_book": dropped_book, "note": note,
            "day_start": day_start}


def daily_filename(now: Optional[float] = None, days_back: int = 0) -> str:
    now = time.time() if now is None else now
    return "daily_" + time.strftime(
        "%Y%m%d", time.localtime(now - days_back * 86400)) + ".xlsx"
