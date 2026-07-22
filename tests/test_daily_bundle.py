"""WO-2026-07-22-K — THE DAILY BUNDLE. One /daily command, one read-only .xlsx,
every logged table bounded to the day, decimated book tape, built-sent-deleted.
No trading path touched. openpyxl is not a dependency, so the workbook is read
back by parsing the OOXML zip directly."""
import re
import sqlite3
import time
import zipfile

import pytest

from relay_engine import daily_bundle
from relay_engine.ledger import Ledger


def _sheet_names(path):
    with zipfile.ZipFile(path) as z:
        wb = z.read("xl/workbook.xml").decode()
    return re.findall(r'<sheet name="([^"]+)"', wb)


def _sheet_rowcounts(path):
    """name -> data-row count (excluding the header)."""
    names = _sheet_names(path)
    counts = {}
    with zipfile.ZipFile(path) as z:
        for i, name in enumerate(names, start=1):
            xml = z.read(f"xl/worksheets/sheet{i}.xml").decode()
            counts[name] = max(0, xml.count("<row ") - 1)
    return counts


@pytest.fixture
def populated(tmp_path):
    """A file-backed ledger with rows today and rows from yesterday (explicit
    timestamps, since the export scopes by `ts`)."""
    dbf = str(tmp_path / "ledger.db")
    led = Ledger(dbf)
    now = 1_700_050_000.0                      # comfortably inside a local day
    yesterday = now - 86_400
    fill = ("INSERT INTO fills (ts, market, lane, side, action, price_cents, "
            "count, size_tier, settled) VALUES (?,?,?,?,?,?,?,?,0)")
    led.db.execute(fill, (now, "KXBTC15M-A", "FLIP", "yes", "ENTRY", 60, 1, "PROBE"))
    led.db.execute(fill, (now, "KXBTC15M-A", "FLIP", "yes", "EXIT", 77, 1, "PROBE"))
    led.db.execute(
        "INSERT INTO settlements (ts, market, lane, pnl_cents, detail, divergent)"
        " VALUES (?,?,?,?,?,0)", (now, "KXBTC15M-A", "FLIP", 17, "won"))
    led.db.execute(
        "INSERT INTO surface_rows (ts, lane, market, window_id, state, terminal,"
        " detail) VALUES (?,?,?,?,?,0,?)",
        (now, "FLIP", "KXBTC15M-A", "w1", "ENTERED",
         "OPEN50 favored yes@60c · pile all-of met"))
    # yesterday (must be excluded when scoped to today)
    led.db.execute(fill, (yesterday, "KXBTC15M-OLD", "FLIP", "no", "ENTRY", 55, 1,
                          "PROBE"))
    # book_snapshots: 300 frames at 1/s ending now, + the burst around the fill
    frames = [(now - 300 + i, "KXBTC15M-A", '{"y":60,"n":40}') for i in range(301)]
    led.db.executemany(
        "INSERT INTO book_snapshots (ts, market, snapshot) VALUES (?,?,?)", frames)
    led.db.commit()
    return dbf, now


def test_daily_builds_one_valid_xlsx_with_a_sheet_per_table(populated, tmp_path):
    dbf, now = populated
    out = str(tmp_path / "daily.xlsx")
    res = daily_bundle.build_daily_workbook(dbf, ["SCOREBOARD", "FLIP n=5"],
                                            out, now=now)
    # a real xlsx: a zip carrying the OOXML parts
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        assert "[Content_Types].xml" in names and "xl/workbook.xml" in names
    sheets = _sheet_names(out)
    assert sheets[0] == "SCOREBOARD"                 # the computed table, first
    # every table that EXISTS is a sheet (the reasoning ledger + the join key)
    assert "decisions" in sheets and "cell_outcomes" in sheets
    assert "fills" in sheets and "settlements" in sheets
    assert "book_sample" in sheets
    assert res["dropped_book"] is False


def test_scoped_to_the_day(populated, tmp_path):
    dbf, now = populated
    out = str(tmp_path / "daily.xlsx")
    daily_bundle.build_daily_workbook(dbf, ["x"], out, now=now)
    counts = _sheet_rowcounts(out)
    # today: 2 fills (entry+exit on KXBTC15M-A); yesterday's KXBTC15M-OLD excluded
    assert counts["fills"] == 2


def test_book_sample_is_decimated_and_keeps_trades(populated, tmp_path):
    dbf, now = populated
    out = str(tmp_path / "daily.xlsx")
    # 300 frames/300s at N=15 → ~20 decimated rows, PLUS the ±10s burst around
    # the two fills at `now` (~11 frames) — far fewer than 300, more than 20.
    daily_bundle.build_daily_workbook(dbf, ["x"], out, now=now,
                                      decimate_s=15, trade_pad_s=10)
    n = _sheet_rowcounts(out)["book_sample"]
    assert 20 <= n <= 60 and n < 300           # decimated, not dumped; trades kept


def test_export_is_read_only_and_leaves_the_db_unchanged(populated, tmp_path):
    dbf, now = populated
    before = sqlite3.connect(dbf).execute(
        "SELECT COUNT(*) FROM book_snapshots").fetchone()[0]
    out = str(tmp_path / "daily.xlsx")
    daily_bundle.build_daily_workbook(dbf, ["x"], out, now=now)
    after = sqlite3.connect(dbf).execute(
        "SELECT COUNT(*) FROM book_snapshots").fetchone()[0]
    assert after == before                     # nothing written
    # and the discipline itself: a mode=ro connection CANNOT write
    ro = sqlite3.connect(f"file:{dbf}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO boots (ts) VALUES (1.0)")
    ro.close()


def test_size_guard_drops_book_sample_over_the_limit(populated, tmp_path):
    dbf, now = populated
    out = str(tmp_path / "daily.xlsx")
    res = daily_bundle.build_daily_workbook(dbf, ["x"], out, now=now,
                                            size_limit=1)   # force the guard
    assert res["dropped_book"] is True and "DROPPED" in res["note"]
    assert "book_sample" not in _sheet_names(out)


def test_filename_is_dated(populated):
    dbf, now = populated
    name = daily_bundle.daily_filename(now=now)
    assert name.startswith("daily_") and name.endswith(".xlsx")
    assert daily_bundle.daily_filename(now=now, days_back=1) != name


# ── the command surface ─────────────────────────────────────────────────────
def test_daily_is_on_the_whitelist_and_dispatches(ledger):
    from relay_engine.ledger import CashProtocol
    from relay_engine.ops import Telegram
    tg = Telegram(CashProtocol(ledger, alert_fn=lambda m: None),
                  send_fn=lambda m: None)
    assert "/daily" in tg.COMMANDS
    tg.daily_fn = lambda text: f"built:{text}"
    assert tg.handle_command("/daily") == "built:/daily"
    assert tg.handle_command("/daily 2") == "built:/daily 2"
    # unwired default never raises and never trades
    tg2 = Telegram(CashProtocol(ledger, alert_fn=lambda m: None),
                   send_fn=lambda m: None)
    assert "no daily export wired" in tg2.handle_command("/daily")


def test_send_document_unwired_falls_back_to_log_not_raise(ledger, tmp_path):
    from relay_engine.ledger import CashProtocol
    from relay_engine.ops import Telegram
    tg = Telegram(CashProtocol(ledger, alert_fn=lambda m: None),
                  send_fn=lambda m: None)
    f = tmp_path / "x.xlsx"
    f.write_text("data")
    assert tg.send_document(str(f), "cap") is False        # unwired → False, no raise
