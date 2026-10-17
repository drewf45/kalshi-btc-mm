"""WO-ROT-1 Chunk 7 — rot_trips, one line per trip, settlement truth, and the
daily bundle's ROT sheet. Done when a replayed day produces a bundle whose ROT
sheet sums match the trips table to the cent."""

import re
import time
import zipfile
from decimal import Decimal
from xml.etree import ElementTree as ET

import pytest

from relay_engine import config, daily_bundle, failures, lane_rot as lr, registry, rot_trips
from relay_engine.book import OrderBook
from relay_engine.series_discovery import SeriesInfo
from relay_engine.shadow_runner import ShadowEngine


def _info(series, und, cadence="15M"):
    return SeriesInfo(series, cadence, "t", und, ["CF"], "1c", "q", "1", 0, 1)


SERIES = {s: _info(s, u, c) for s, u, c in [
    ("KXBTC15M", "BTC", "15M"), ("KXETH15M", "ETH", "15M"), ("KXXRP15M", "XRP", "15M"),
    ("KXSOL15M", "SOL", "15M"), ("KXBTCD", "BTC", "1H"), ("KXDOGE15M", "DOGE", "15M")]}


@pytest.fixture
def day(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DESK_MODE", "ROTATION")
    monkeypatch.setattr(config, "SERIES_INFO", dict(SERIES))
    # This chunk proves the exit-family / ledger plumbing against the ORIGINAL
    # 8/10/20 stop geometry its replay was built on (+8c targets, bid<=entry-20
    # hard line). The live defaults are now 3/4/4 (see test_rot_chunk0); the dial
    # VALUES are asserted there. Pin the old dials here so these mechanics keep
    # testing the walk/IOC/settlement machinery, not the new dial arithmetic.
    monkeypatch.setattr(config, "ROT_TARGET_CENTS", 8)
    monkeypatch.setattr(config, "ROT_STOP_CENTS", 10)
    monkeypatch.setattr(config, "ROT_HARD_STOP_CENTS", 20)
    failures._warn_last.clear()
    db = str(tmp_path / "day.db")
    e = ShadowEngine(db_path=db)
    e.boot()
    e.sent = []
    e.telegram.send = e.sent.append
    failures.configure(e.ledger, alert_fn=e.telegram.alert, run_mode="TEST", boot_id=1)
    monkeypatch.setattr(e.gateway.governor, "take", lambda now=None: True)
    e.books, e.meta = {}, {}
    e.db_path = db
    yield e
    failures._ledger = None
    failures._alert_fn = None


NOW = time.time()


def _mk(e, m, yb, ya, close_in=600.0, depth=50):
    e.meta[m] = {"close_ts": NOW + close_in, "rec_yes_bid": yb, "rec_yes_ask": ya}
    _bk(e, m, yb, ya, depth)


def _bk(e, m, yb, ya, depth=50):
    b = OrderBook(market=m)
    b.apply_snapshot({yb: depth} if yb is not None else {},
                     {100 - ya: depth} if ya is not None else {}, ts=NOW)
    e.books[m] = b
    if m in e.meta:
        e.meta[m]["rec_yes_bid"], e.meta[m]["rec_yes_ask"] = yb, ya


def _step(e, dt):
    e.rot.step(NOW + dt, dict(e.books), e.meta, 16)


def _fill(e, oid, n=None):
    e.gateway.on_fill(oid, count=n)


def _sells(e, m, purpose=None):
    return [oid for oid, o in e.gateway.resting.items()
            if o.market == m and o.action == "sell" and (purpose is None or o.purpose == purpose)]


def replay_day(e):
    """Five trips through every exit family, fractional sizes throughout."""
    a, b, c, d, f = ("KXBTC15M-26OCT14A-00", "KXETH15M-26OCT14B-00",
                     "KXXRP15M-26OCT14C-00", "KXSOL15M-26OCT14D-00",
                     "KXDOGE15M-26OCT14F-00")
    for m in (a, b, c, d, f):
        _mk(e, m, 46, 48)
    _step(e, 0)
    for m in (a, b, c, d, f):
        _fill(e, e.rot.trips[m].entry_oid)
    _step(e, 1)                                         # cash-outs rest
    # A: target
    _fill(e, _sells(e, a, "EXIT")[0])
    # B: hard stop after grace (IOC into the bid; taker fee)
    _bk(e, b, 25, 27)
    # C: time stop scratch (bid within 2c), filled at the scratch price
    _bk(e, c, 45, 47)
    # D and F ride to the bell as favourite (D) / underdog-held-by-late-window... (F favourite too)
    _bk(e, d, 70, 72)
    _bk(e, f, 60, 62)
    _step(e, 2)
    _step(e, 61)                                        # B fires (grace over)
    _step(e, 62)
    _step(e, 361)                                       # C time stop -> walk @46
    _fill(e, e.rot.trips[c].exit_oid)
    _step(e, 362)
    _step(e, 600)                                       # the bell: D, F settle
    _step(e, 601)
    booked = e.rot_settle_sweep(lambda m: {d: "yes", f: "no"}.get(m))
    assert booked == 2
    return a, b, c, d, f


def _read_sheet(path, name):
    z = zipfile.ZipFile(path)
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    names = [s.get("name") for s in wb.find("m:sheets", ns)]
    idx = names.index(name) + 1
    sh = ET.fromstring(z.read(f"xl/worksheets/sheet{idx}.xml"))
    rows = []
    for r in sh.iter("{%s}row" % ns["m"]):
        vals = []
        for c in r:
            v = c.find("m:v", ns)
            t = c.find("m:is/m:t", ns)
            vals.append(None if (v is None and t is None) else
                        (t.text if t is not None else float(v.text)))
        rows.append(vals)
    return rows[0], rows[1:]


def test_every_exit_family_writes_its_row(day):
    a, b, c, d, f = replay_day(day)
    rows = {r[0]: r for r in day.ledger.db.execute(
        "SELECT market, exit_path, settled, net_cents, fees_cents, size, sold,"
        " settle_count, settle_result, trigger_px, fill_px, why, shadow FROM rot_trips")}
    assert {m: rows[m][1] for m in rows} == {a: "TARGET", b: "HARD_STOP", c: "TIME",
                                             d: "SETTLE", f: "SETTLE"}
    size = Decimal(str(rows[a][5]))
    assert size == Decimal("10.86")                     # fractional: 5% of $100 at 46c
    assert rows[a][3] == pytest.approx(float(size * 8))           # +8c a contract, no fee
    assert rows[a][4] == 0
    # hard stop: sold at the 25c bid, taker fee per the schedule
    fee = rot_trips.taker_fee_cents(size, 25)
    assert rows[b][4] == float(fee) and rows[b][9] == 25 and rows[b][10] == 25
    assert rows[b][3] == pytest.approx(float(size * (25 - 46) - fee))
    assert rows[c][3] == pytest.approx(0.0)             # scratch at entry
    # settlement truth booked from the venue result
    assert rows[d][8] == "yes" and rows[d][3] == pytest.approx(float(size * (100 - 46)))
    assert rows[f][8] == "no" and rows[f][3] == pytest.approx(float(-size * 46))
    assert all(r[2] == 1 for r in rows.values())
    assert all(r[11] for r in rows.values())            # Why Law: every row says why
    assert all(r[12] == 1 for r in rows.values())       # SHADOW run


def test_one_telegram_line_per_closed_trip(day):
    replay_day(day)
    closed = [s for s in day.sent if "🔁 ROT #" in s]
    assert len(closed) == 5
    assert sum("riding to settlement" in s for s in closed) == 2
    settled = [s for s in day.sent if "🔔 ROT #" in s]
    assert len(settled) == 2 and any("lost" in s for s in settled)


def test_rot_sheet_sums_match_the_trips_table_to_the_cent(day, tmp_path):
    replay_day(day)
    out = str(tmp_path / "bundle.xlsx")
    daily_bundle.build_daily_workbook(day.db_path, [], out, now=time.time())
    header, rows = _read_sheet(out, "ROT")
    assert header == rot_trips.SHEET_HEADER
    col = {h: i for i, h in enumerate(header)}
    table = day.ledger.db.execute(
        "SELECT band, series, net_cents, fees_cents FROM rot_trips").fetchall()
    tot_net = sum(Decimal(str(r[2])) for r in table)
    tot_fee = sum(Decimal(str(r[3])) for r in table)
    total = [r for r in rows if r[col["scope"]] == "total"][0]
    cents = lambda d: (d / 100).quantize(Decimal("0.01"))
    assert Decimal(str(total[col["net_usd"]])).quantize(Decimal("0.01")) == cents(tot_net)
    assert Decimal(str(total[col["fees_usd"]])).quantize(Decimal("0.01")) == cents(tot_fee)
    assert total[col["trips"]] == 5
    for scope, idx in (("band", 0), ("series", 1)):
        for r in [r for r in rows if r[col["scope"]] == scope]:
            sub = [t for t in table if t[idx] == r[col["key"]]]
            assert r[col["trips"]] == len(sub)
            assert Decimal(str(r[col["net_usd"]])).quantize(Decimal("0.01")) == \
                cents(sum(Decimal(str(t[2])) for t in sub))
    # h, W, L, h* on the total row: 2 wins (A, D), 2 losses (B, F), 1 scratch
    assert total[col["wins"]] == 2
    assert total[col["h"]] == pytest.approx(0.4)
    W, L = total[col["W_c"]], total[col["L_c"]]
    assert total[col["h_star"]] == pytest.approx(L / (W + L), abs=1e-4)
    # the raw table rides along as its own sheet
    h2, raw = _read_sheet(out, "rot_trips")
    assert len(raw) == 5 and "why" in h2


def test_pending_settle_is_counted_not_guessed(day):
    m = "KXSOL15M-26OCT14P-00"
    _mk(day, m, 46, 48)
    _step(day, 0)
    _fill(day, day.rot.trips[m].entry_oid)
    _step(day, 1)
    _bk(day, m, 70, 72)
    _step(day, 600)
    row = day.ledger.db.execute(
        "SELECT settled, net_cents, settle_count FROM rot_trips WHERE market=?", (m,)).fetchone()
    assert row[0] == 0 and row[1] is None and row[2] > 0
    _h, rows = rot_trips.sheet_rows(day.ledger.db, 0)
    total = rows[-1]
    assert total[2] == 0 and total[-1] == 1             # 0 booked trips, 1 pending
    assert day.rot_settle_sweep(lambda mk: None) == 0   # not settled yet: still pending


def test_hourly_line_has_open_slots_and_deploy(day):
    m = "KXBTC15M-26OCT14H-00"
    _mk(day, m, 46, 48)
    _step(day, 0)
    _fill(day, day.rot.trips[m].entry_oid)
    line = day.rot_hourly_line()
    assert "open 1" in line and "deploy 5% of 50% cap" in line


def test_rot_trips_registers_its_question():
    q = registry.get("ROT_TRIPS")
    assert q is not None and q.is_complete()
    assert "ROT_TRIPS" in registry.SEED_SURFACES


def test_taker_fee_schedule():
    # 0.07 x 10 x 0.5 x 0.5 = $0.175 -> 18c (rounded up)
    assert rot_trips.taker_fee_cents(10, 50) == 18
    assert rot_trips.taker_fee_cents(Decimal("0.70"), 25) == 1


def test_band_ranks_on_its_own_last_50_trips_once_it_has_them(day):
    db = day.ledger.db
    rot_trips.ensure(db)
    assert rot_trips.band_stats(db, "40-49", 50) is None          # seed until 50
    for i in range(50):
        win = i % 2 == 0                                         # h = 0.5
        rot_trips.write(day.ledger, {
            "ts": 1000.0 + i, "opened_ts": 900.0 + i, "series": "KXBTC15M",
            "market": f"M{i}", "grp": "BTC", "side": "yes", "entry_cost": 45,
            "size": 1.0, "target": 53, "stop": 35, "hard_stop": 25, "band": "40-49",
            "fill_ts": 990.0 + i, "exit_path": "TARGET" if win else "SOFT_STOP",
            "trigger_px": None, "fill_px": 53.0 if win else 33.0, "gap": None,
            "spot_move": None, "reads_held": 5, "sold": 1.0, "settle_count": 0.0,
            "settle_result": None, "settled": 1, "net_cents": 8.0 if win else -12.0,
            "fees_cents": 0.0, "shadow": 1, "rank": 1, "score": 0.1, "why": "t"})
    st = rot_trips.band_stats(db, "40-49", 50)
    assert (st.h, st.W, st.L, st.n) == (0.5, 8.0, 12.0, 50)
    assert st.h_star() == pytest.approx(0.6) and st.ev_cents() < 0
    day._band_stats_cache.clear()
    assert day._rot_band_stats("40-49").source.startswith("measured")
    assert day._rot_band_stats("50-54").source.startswith("CSV seed")
    # a measured band below break-even scores <= 0: the selector stops entering it
    assert lr.score(day._rot_band_stats("40-49"), 45) < 0
