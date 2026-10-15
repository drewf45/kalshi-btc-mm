"""WO-ROT-1 Chunk 1 — stay alive and tell the truth.

Durable-Write: a storage-class ledger write failure halts new entries and pages
(once), leaves exits alone. Disk guard: under the floor, page and halt entries.
Heartbeat: every completed cycle writes the file; a killed process goes stale.
Reconcile: the tape prints before cycle 1; unexplained positions quarantine.
"""

import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from relay_engine import config, deadman, failures, ledger as ledger_mod
from relay_engine.book import OrderBook
from relay_engine.errors import WallRejection
from relay_engine.gateway import Order
from relay_engine.shadow_runner import ShadowEngine

ROOT = Path(__file__).resolve().parents[1]
TICKER = "KXBTC15M-26OCT141015-15"
EVENT = TICKER.rsplit("-", 1)[0]


@pytest.fixture
def engine(tmp_path):
    e = ShadowEngine(db_path=str(tmp_path / "rot1.db"))
    e.boot()
    e.telegram_sent = []
    e.telegram.send = e.telegram_sent.append
    failures.configure(e.ledger, alert_fn=e.telegram.alert, run_mode="TEST", boot_id=1)
    yield e
    failures._ledger = None
    failures._alert_fn = None


def _book(yes, no):
    b = OrderBook(market=TICKER)
    b.apply_snapshot({yes: 10}, {no: 10}, ts=1.0)
    return b


def _entry(side="yes", price=46, market=TICKER):
    return Order(lane="FLIP", event=market.rsplit("-", 1)[0], market=market,
                 side=side, action="buy",
                 price_cents=price, count=1, size_tier=config.TIER_PROBE,
                 purpose="ENTRY", band=(1, 49),
                 why=f"OPEN grain {side}x2 · join {price}c · PROBE n=0 · geometry=v2")


def _fill_the_disk(led):
    """A REAL SQLITE_FULL: cap the file at its current page count, then write."""
    pages = led.db.execute("PRAGMA page_count").fetchone()[0]
    led.db.execute(f"PRAGMA max_page_count = {pages}")
    with pytest.raises(sqlite3.OperationalError) as ei:
        led.db.execute("INSERT INTO book_snapshots (ts, market, snapshot) VALUES (?,?,?)",
                       (1.0, TICKER, "x" * 200_000))
        led.db.commit()
    return ei.value


# ── Durable-Write ────────────────────────────────────────────────

def test_disk_full_write_halts_entries_and_pages_once(engine):
    err = _fill_the_disk(engine.ledger)
    assert isinstance(err, ledger_mod.DurableWriteError)
    assert "full" in str(err).lower()
    assert ledger_mod.DURABLE_HALT in engine.ledger.entry_halts
    pages = [m for m in engine.telegram_sent if "DURABLE_WRITE_FAILED" in m]
    assert len(pages) == 1 and pages[0].startswith("⛔ FATAL")
    # a second failure does not page again
    with pytest.raises(sqlite3.OperationalError):
        engine.ledger.db.execute("INSERT INTO book_snapshots (ts, market, snapshot)"
                                 " VALUES (?,?,?)", (2.0, TICKER, "y" * 200_000))
    assert len([m for m in engine.telegram_sent if "DURABLE_WRITE_FAILED" in m]) == 1


def test_durable_halt_blocks_entries_not_exits(engine):
    # a position exists before the fault
    r = engine.gateway.submit(_entry("yes", 46), _book(40, 48))
    engine.gateway.on_fill(r.order_id)
    engine.ledger.entry_halts.add(ledger_mod.DURABLE_HALT)  # the fault, isolated
    # a NEW entry elsewhere (buying NO here would net the YES down — risk-reducing)
    other = "KXBTC15M-26OCT141030-30"
    with pytest.raises(WallRejection) as ei:
        engine.gateway.submit(_entry("yes", 46, market=other), _book(40, 48))
    assert ei.value.wall == "ENTRIES_HALTED"
    assert "DURABLE_WRITE" in ei.value.detail
    # the exit still goes (risk-reducing; walls skipped)
    exit_o = Order(lane="FLIP", event=EVENT, market=TICKER, side="yes", action="sell",
                   price_cents=54, count=1, size_tier=config.TIER_PROBE,
                   purpose="EXIT", band=(1, 99), why="target")
    res = engine.gateway.submit(exit_o, _book(40, 48))
    assert res.order_id


def test_resume_key_cannot_clear_a_storage_halt(engine):
    engine.ledger.entry_halts.add(ledger_mod.DURABLE_HALT)
    engine.gateway.resume_entries_all()
    assert "DURABLE_WRITE" in engine.gateway.entries_halted_for("FLIP")


def test_schema_errors_do_not_trip_the_law(engine):
    with pytest.raises(sqlite3.OperationalError):
        engine.ledger.db.execute("ALTER TABLE fills ADD COLUMN fee_cents INTEGER")
    with pytest.raises(sqlite3.OperationalError):
        engine.ledger.db.execute("SELECT * FROM no_such_table")
    assert engine.ledger.entry_halts == set()
    assert engine.ledger.durable_failure is None


@pytest.mark.skipif(sys.platform != "linux", reason="tmpfs mount is a Linux test")
def test_tmpfs_disk_full_halts_entries_and_pages(tmp_path):
    """The WO's literal test: a real filesystem fills up under the ledger."""
    mnt = tmp_path / "tiny"
    mnt.mkdir()
    r = subprocess.run(["mount", "-t", "tmpfs", "-o", "size=512k", "tmpfs", str(mnt)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip(f"cannot mount tmpfs here: {r.stderr.strip()}")
    try:
        sent = []
        led = ledger_mod.Ledger(str(mnt / "kal.db"))
        failures.configure(led, alert_fn=sent.append, run_mode="TEST", boot_id=1)
        with pytest.raises(sqlite3.OperationalError):
            for i in range(10_000):
                led.db.execute("INSERT INTO book_snapshots (ts, market, snapshot)"
                               " VALUES (?,?,?)", (float(i), TICKER, "z" * 8192))
                led.db.commit()
        assert ledger_mod.DURABLE_HALT in led.entry_halts
        assert any("DURABLE_WRITE_FAILED" in m for m in sent)
        led.db.close()
    finally:
        failures._ledger = None
        failures._alert_fn = None
        subprocess.run(["umount", str(mnt)], capture_output=True)


# ── disk guard ───────────────────────────────────────────────────

def test_disk_guard_low_halts_and_pages_then_clears(engine):
    st = {"free_bytes": 100 * 2**20, "db_bytes": 5 * 2**20, "probe": "/x"}
    out = deadman.check_disk(engine.ledger, status=dict(st))
    assert out["low"]
    assert deadman.DISK_HALT in engine.ledger.entry_halts
    assert "DISK_LOW" in engine.gateway.entries_halted_for("ROT")
    assert sum("DISK_LOW" in m for m in engine.telegram_sent) == 1
    deadman.check_disk(engine.ledger, status=dict(st))           # still low: no 2nd page
    assert sum("DISK_LOW" in m for m in engine.telegram_sent) == 1
    deadman.check_disk(engine.ledger, status=dict(st, free_bytes=2 * 2**30))
    assert deadman.DISK_HALT not in engine.ledger.entry_halts


def test_disk_line_and_real_status(tmp_path):
    st = deadman.disk_status(str(tmp_path / "x.db"))
    assert st["free_bytes"] > 0 and st["db_bytes"] == 0
    assert "DISK: free" in deadman.disk_line(st)


def test_disk_guard_runs_at_boot(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "DISK_MIN_FREE_BYTES", 2**62)   # any disk is "low"
    e = ShadowEngine(db_path=str(tmp_path / "b.db"))
    e.boot()
    assert deadman.DISK_HALT in e.ledger.entry_halts
    assert "DISK: free" in capsys.readouterr().out


# ── heartbeat ────────────────────────────────────────────────────

def test_every_completed_cycle_writes_the_heartbeat(engine):
    path = config.HEARTBEAT_PATH
    engine.cycle(["KXBTC15M-A"], now=100.0)
    hb1 = deadman.read_heartbeat(path)
    assert hb1["cycles"] == 1
    assert set(hb1) >= {"ts", "cycles", "open_positions", "deploy_pct", "last_error"}
    engine.cycle(["KXBTC15M-A"], now=101.0)
    hb2 = deadman.read_heartbeat(path)
    assert hb2["cycles"] == 2 and hb2["ts"] >= hb1["ts"]
    assert not deadman.is_stale(path)


def test_a_failed_cycle_does_not_beat_and_names_its_error(engine, monkeypatch):
    engine.cycle(["KXBTC15M-A"], now=100.0)

    def boom(*a, **k):
        raise RuntimeError("wedged")
    monkeypatch.setattr(engine, "_cycle_body", boom)
    with pytest.raises(RuntimeError):
        engine.cycle(["KXBTC15M-A"], now=101.0)
    assert deadman.read_heartbeat(config.HEARTBEAT_PATH)["cycles"] == 1
    monkeypatch.undo()
    engine.cycle(["KXBTC15M-A"], now=102.0)
    assert "wedged" in deadman.read_heartbeat(config.HEARTBEAT_PATH)["last_error"]


def test_staleness_threshold(tmp_path):
    p = str(tmp_path / "hb.json")
    assert deadman.is_stale(p)                      # missing = stale
    deadman.write_heartbeat(p, cycles=1, open_positions=0, deploy_pct=None, now=1000.0)
    assert not deadman.is_stale(p, now=1009.9)
    assert deadman.is_stale(p, now=1010.1)
    assert config.HEARTBEAT_STALE_S == 10


def test_killed_process_goes_stale_within_10s(tmp_path):
    """A real writer process beating every 0.2 s; SIGKILL it; the file stops
    moving, and it reads stale once 10 s have passed since its last beat."""
    p = tmp_path / "hb.json"
    script = textwrap.dedent(f"""
        import time
        from relay_engine import deadman
        n = 0
        while True:
            n += 1
            deadman.write_heartbeat({str(p)!r}, cycles=n, open_positions=0, deploy_pct=None)
            time.sleep(0.2)
    """)
    proc = subprocess.Popen([sys.executable, "-c", script], cwd=ROOT)
    try:
        deadline = time.time() + 10
        while deadman.read_heartbeat(str(p)) is None and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(0.5)
        assert not deadman.is_stale(str(p))
    finally:
        proc.send_signal(signal.SIGKILL)
        proc.wait()
    last = deadman.read_heartbeat(str(p))
    time.sleep(0.6)
    assert deadman.read_heartbeat(str(p)) == last          # nobody is beating
    assert not deadman.is_stale(str(p), now=last["ts"] + 9.9)
    assert deadman.is_stale(str(p), now=last["ts"] + 10.1)


def test_deadman_check_cli(tmp_path):
    p = str(tmp_path / "hb.json")
    run = lambda: subprocess.run([sys.executable, "-m", "relay_engine.deadman", "--path", p],
                                 cwd=ROOT, capture_output=True, text=True)
    assert run().returncode == 2
    deadman.write_heartbeat(p, cycles=1, open_positions=0, deploy_pct=None)
    assert run().returncode == 0
    deadman.write_heartbeat(p, cycles=1, open_positions=0, deploy_pct=None,
                            now=time.time() - 60)
    assert run().returncode == 1


def test_hourly_alive_line(engine):
    engine._last_hourly_ts = 0.0           # an hour is overdue
    engine.cycle(["KXBTC15M-A"], now=100.0)
    alive = [m for m in engine.telegram_sent if m.startswith("💓 alive")]
    assert len(alive) == 1
    assert "cycles 1" in alive[0] and "trips 0" in alive[0] and "net +$0.00" in alive[0]
    engine.cycle(["KXBTC15M-A"], now=101.0)   # not an hour later: no second line
    assert len([m for m in engine.telegram_sent if m.startswith("💓 alive")]) == 1


def test_hourly_line_never_prints_unknown_as_zero():
    line = deadman.hourly_line(5, None, None, 0, None)
    assert "trips n/a" in line and "net n/a" in line and "deploy n/a" in line
    assert "net -$1.25" in deadman.hourly_line(5, 2, -125, 1, 0.1)


def test_ping_url_unset_and_off_by_default():
    env = {k: v for k, v in os.environ.items() if k != "HEARTBEAT_PING_URL"}
    out = subprocess.run([sys.executable, "-c",
                          "from relay_engine import config; print(repr(config.HEARTBEAT_PING_URL))"],
                         cwd=ROOT, env=env, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "''"


# ── reconcile first ──────────────────────────────────────────────

class _NoResting:
    def request(self, *a, **k):
        raise RuntimeError("no resting sweep in tests")


def test_reconcile_tape_prints_and_quarantines_unexplained(engine, monkeypatch, capsys):
    monkeypatch.setattr(config, "DESK_MODE", "ROTATION")
    from relay_engine import venue
    from relay_engine.reconcile import live_boot_reconcile
    monkeypatch.setattr(venue, "get_balance", lambda c: (6.74, 0.0))
    monkeypatch.setattr(venue, "get_positions",
                        lambda c: [{"ticker": TICKER, "position": 2}])
    summary = live_boot_reconcile(engine, _NoResting())
    out = capsys.readouterr().out
    assert "RECONCILE (venue speaks first — before cycle 1):" in out
    assert f"QUARANTINED {TICKER} net 2" in out
    assert summary["positions_quarantined"] == 1
    # quarantined, NOT adopted: no custody, but the market reads as held
    assert not any(p.market == TICKER for p in engine.custodian.positions.values())
    assert engine.gateway.positions[(EVENT, TICKER, "QUARANTINE")] == 2
    assert any(m.startswith("🧾 QUARANTINED") for m in engine.telegram_sent)


def test_fractional_and_unreadable_positions_quarantine(engine, monkeypatch):
    monkeypatch.setattr(config, "DESK_MODE", "ROTATION")
    from decimal import Decimal
    from relay_engine import venue
    from relay_engine.reconcile import live_boot_reconcile
    monkeypatch.setattr(venue, "get_balance", lambda c: (6.74, 0.0))
    monkeypatch.setattr(venue, "get_positions", lambda c: [
        {"ticker": TICKER, "position_fp": "0.70"},
        {"ticker": "KXETH15M-26OCT141015-15", "position": "garbage"},
    ])
    summary = live_boot_reconcile(engine, _NoResting())
    assert summary["positions_quarantined"] == 2
    q = {t: n for t, n, _ in summary["quarantined"]}
    assert q[TICKER] == Decimal("0.70")
    assert q["KXETH15M-26OCT141015-15"] is None   # unreadable is never flat


def test_position_net_parsing():
    from decimal import Decimal
    from relay_engine.reconcile import position_net
    assert position_net({"position": 3}) == 3
    assert position_net({"position_fp": "-1.25"}) == Decimal("-1.25")
    assert position_net({}) == 0
    assert position_net({"position": "x"}) is None


def test_shadow_run_prints_reconcile_line_before_cycle_1():
    src = (ROOT / "relay_engine" / "shadow_runner.py").read_text()
    run_src = src[src.index("async def run():"):]
    assert run_src.index("RECONCILE: SHADOW") < run_src.index("_rest_main(")
    assert run_src.index("live_boot_reconcile(engine, client)") < run_src.index("_rest_main(")
