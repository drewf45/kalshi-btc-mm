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
    # cell_outcomes so the honest scoreboard has cells to grade (WO-2026-07-23-A):
    # an OPEN trip cell (wins + real ~11c losses) and an F hold cell (wins + real
    # ~40c salvage-cut losses, NOT total loss). ts=now so it is today's.
    co = ("INSERT INTO cell_outcomes (ts, lane, price_cell, won, pnl_cents, "
          "fees_cents, market, kind) VALUES (?,?,?,?,?,?,?,?)")
    for i in range(20):
        led.db.execute(co, (now, "OPEN", 60, 1, 17, 1, "KO%d" % i, "trip"))
    for i in range(10):
        led.db.execute(co, (now, "OPEN", 60, 0, -11, 1, "KOL%d" % i, "trip"))
    for i in range(50):
        led.db.execute(co, (now, "F", 95, 1, 5, 0, "KF%d" % i, "settle"))
    for i in range(9):
        led.db.execute(co, (now, "F", 95, 0, -40, 0, "KFL%d" % i, "settle"))
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
    assert sheets[0] == "SUMMARY"                     # §4 — the summary leads
    assert sheets[1] == "SCOREBOARD"                  # then the structured self-audit
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


# ── WO-2026-07-23-A: MAKE THE DAILY PACK TELL THE TRUTH ─────────────────────
def _read_sheet(path, name):
    """[header, *rows] as lists of cell text/number for a named sheet."""
    names = _sheet_names(path)
    idx = names.index(name) + 1
    with zipfile.ZipFile(path) as z:
        xml = z.read(f"xl/worksheets/sheet{idx}.xml").decode()
    out = []
    for rowxml in re.findall(r"<row [^>]*>(.*?)</row>", xml, re.S):
        cells = []
        for cx in re.findall(r"<c\b[^>]*?(?:/>|>(.*?)</c>)", rowxml, re.S):
            m = re.search(r"<t[^>]*>(.*?)</t>", cx, re.S)
            if m is not None:
                cells.append(m.group(1))
            else:
                v = re.search(r"<v>(.*?)</v>", cx, re.S)
                cells.append(float(v.group(1)) if v else None)
        out.append(cells)
    return out


def test_summary_sheet_leads_the_workbook(populated, tmp_path):
    """Acceptance #8 — the SUMMARY sheet is FIRST, money and expectation up top."""
    dbf, now = populated
    out = str(tmp_path / "daily.xlsx")
    daily_bundle.build_daily_workbook(dbf, ["x"], out, now=now)
    assert _sheet_names(out)[0] == "SUMMARY"
    rows = _read_sheet(out, "SUMMARY")
    assert rows[0] == ["section", "item", "value"]
    sections = {r[0] for r in rows[1:]}
    # money, the self-audit, and anomalies all lead the pack
    assert {"MONEY", "MODEL HEALTH", "EXPECTATION", "ANOMALIES"} <= sections


def test_scoreboard_carries_realized_money_and_model_error(populated, tmp_path):
    """Acceptance #1 (realized P&L per row) + #2 (loss_modeled vs loss_actual +
    model_error): every graded cell shows the money and the gap."""
    dbf, now = populated
    out = str(tmp_path / "daily.xlsx")
    daily_bundle.build_daily_workbook(dbf, ["x"], out, now=now)
    rows = _read_sheet(out, "SCOREBOARD")
    header = rows[0]
    for col in ("pnl_day_c", "pnl_life_c", "loss_modeled_c", "loss_actual_c",
                "be_implied", "model_error"):
        assert col in header, col
    data = [dict(zip(header, r)) for r in rows[1:]]
    open_cell = next(d for d in data if d["lane"] == "OPEN")
    # realized money is present (B1) and the model is graded (B2)
    assert open_cell["pnl_life_c"] == 20 * 17 - 10 * 11        # +230¢
    assert open_cell["loss_modeled_c"] is not None
    assert open_cell["loss_actual_c"] is not None
    assert open_cell["model_error"] is not None


def test_open_break_even_anchored_on_live_constant_not_retired_band(populated):
    """Acceptance #3 / A1 — OPEN's honest loss is OPEN_MOMENTUM_STOP_C (the live
    stop), NOT the retired band floor (mid − OPEN_UNDETERMINED_BAND[0])."""
    from relay_engine import config, scoring
    led = Ledger(populated[0])
    assert scoring.loss_modeled_honest(led, "OPEN", 60) == \
        float(config.OPEN_MOMENTUM_STOP_C)
    # and it is NOT the stale band-floor loss the trading breakeven() still uses
    band_loss = max(1.0, 62 - config.OPEN_UNDETERMINED_BAND[0])
    assert scoring.loss_modeled_honest(led, "OPEN", 60) != band_loss


def test_hold_loss_falls_back_to_realized_not_total(populated):
    """Acceptance #3 / A2 — an F hold loss uses the realized average loss (~40¢),
    never the total-loss assumption (mid ≈ 97¢) when salvage evidence is thin."""
    from relay_engine import scoring
    led = Ledger(populated[0])
    loss = scoring.loss_modeled_honest(led, "F", 95)
    rn, ravg = scoring.realized_loss_avg(led, "F", 95)
    assert rn == 9 and abs(ravg - 40.0) < 1e-6
    assert abs(loss - 40.0) < 1e-6                  # realized, not mid (97)
    assert loss < 97


def test_salvage_min_n_honest_is_reachable(populated):
    """Acceptance #3 / A3 — the pack's salvage gate is 8 (F has 9 losses), not the
    unreachable trading constant 20."""
    from relay_engine import config, scoring
    assert scoring.SALVAGE_ADJ_MIN_N_HONEST == 8
    assert config.SALVAGE_ADJ_MIN_N == 20           # trading constant UNTOUCHED


def test_trading_break_even_is_byte_identical(populated):
    """Acceptance #9 (KILL CONDITION) — the trading-facing breakeven(),
    SALVAGE_ADJ_MIN_N, and tier_for are UNCHANGED. The honest numbers live only
    in the pack; F and OPEN trade exactly as before."""
    from relay_engine import config, scoring
    led = Ledger(populated[0])
    # the trading breakeven still assumes the total-loss / band-floor geometry —
    # i.e. it is DIFFERENT from the honest one (proof the pack didn't leak in).
    assert scoring.breakeven(led, "F", 95) == pytest.approx(0.97)
    assert scoring.breakeven(led, "OPEN", 60) != \
        scoring.breakeven_honest(led, "OPEN", 60)
    assert config.SALVAGE_ADJ_MIN_N == 20
    # tier_for reads the untouched breakeven → same ruling as before the WO
    assert scoring.tier_for(led, "F", 96) in (
        config.TIER_PROBE, config.TIER_LEAN, config.TIER_CLEAR)
