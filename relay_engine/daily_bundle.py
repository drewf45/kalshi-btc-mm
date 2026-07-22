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

log = logging.getLogger("relay.daily")

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
        sheets: List[Tuple[str, List[str], list]] = [
            ("SCOREBOARD", ["line"], [[ln] for ln in scoreboard_lines])]
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
