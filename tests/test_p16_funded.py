"""WO-P16 FINAL "THE SCALP PROFILE, FUNDED" — env-name resilience (§1), the
PROFILE block (§2, with the F-passthrough STOP-AND-REPORT stated honestly),
deposit-day rescale (§3), and the P16 expected tape (§4)."""

import pytest

from relay_engine import config, failures
from relay_engine.boot import boot_tape, sizing_line
from relay_engine.config import resolve_db_path
from relay_engine.ops import worst_day_bound_line
from relay_engine.shadow_runner import ShadowEngine

TICKER = "KXBTC15M-02JAN251000-T99"


@pytest.fixture
def engine(tmp_path):
    e = ShadowEngine(db_path=str(tmp_path / "p16.db"))
    e.boot()
    e.telegram_sent = []
    e.telegram.send = e.telegram_sent.append
    failures.configure(e.ledger, alert_fn=e.telegram.alert, run_mode="TEST", boot_id=1)
    yield e
    failures._ledger = None
    failures._alert_fn = None


# ── §1: the fallback chain — the disk is the constant ──────────────────────
def test_relay_db_path_wins():
    path, src = resolve_db_path({"RELAY_DB_PATH": "/var/data/relay_live.db",
                                 "K_WORKER_DB": "/var/data/k_worker_surface.db"})
    assert (path, src) == ("/var/data/relay_live.db", "RELAY_DB_PATH")


def test_legacy_dir_derived_never_its_file():
    path, src = resolve_db_path({"K_WORKER_DB": "/var/data/k_worker_surface.db"})
    assert (path, src) == ("/var/data/relay_live.db", "derived")
    assert "k_worker_surface" not in path   # single-writer law: never that file


def test_ephemeral_default_with_warning_path():
    path, src = resolve_db_path({})
    assert (path, src) == ("relay_shadow.db", "ephemeral")


def test_boot_tape_notes_derived_source(monkeypatch):
    monkeypatch.setattr(config, "DB_PATH_SOURCE", "derived")
    tape = "\n".join(boot_tape())
    assert "derived from legacy K_WORKER_DB dir" in tape
    assert "set RELAY_DB_PATH to pin" in tape


# ── §2: the PROFILE block, honesty included ────────────────────────────────
def test_profile_block_prints_every_lane():
    tape = "\n".join(boot_tape())
    assert "PROFILE — bank small wins, every lane, every market:" in tape
    # P21 A4/A5: FLIP's PAIR line retired — the profile now speaks HUNT
    # (fast intent) and OPEN (patient intent) separately.
    # WO-2026-07-22-E/-F: the thesis line is "buy the FAVORED side, sell into
    # the pile-in, NO hold — one momentum stop; the curfew hands winners to F";
    # WO-...-F adds the WAIT-FOR-THE-PILE window gate.
    for token in ("FLIP/HUNT: needle", "FLIP/OPEN: band",
                  "FLIP THESIS (WO-2026-07-22-E)", "INTO the pile-in",
                  "endgame curfew hands winners to F", "trend/depth LOGGED, not gated",
                  "WAIT-FOR-THE-PILE", "No pile = no",
                  "PAIR retired", "F: hold-to-settlement",
                  "H8: delta-gated", "D: cheap entry", "P: displacement fade",
                  "ORPHAN: adopted at boot", "WALLS: gross+net",
                  "REJECT_SELF_NET"):
        assert token in tape
    # P19 resolved the P16 STOP-AND-REPORT: the tape says salvage is armed
    assert "SALVAGE armed" in tape and "stop-and-report resolved" in tape
    # P21 B1: the boot cites the doctrine page
    assert "DOCTRINE: docs/SEMANTICS.md" in tape


# ── §3: deposit day — the confirm law rescales and speaks ──────────────────
def test_positive_deposit_rescales_caps_and_pages_sizing(cash, ledger):
    assert ledger.boot_caps.book_cents == 10_000
    state = cash.reconcile(13_500, in_flight_orders=0, unsettled_fills=0,
                           now=1000.0)
    assert state == "CONFIRMED_POSITIVE"          # no halt on a deposit
    assert ledger.boot_caps.book_cents == 13_500  # caps rescaled NOW
    assert any("SIZING" in a and "1125¢" in a for a in cash.test_alerts)


def test_confirm_cash_branch_also_rescales(cash, ledger):
    cash.reconcile(9_000, in_flight_orders=0, unsettled_fills=0, now=1000.0)
    assert cash.entries_halted                     # negative prompts + halts
    assert cash.confirm_cash(now=1010.0)
    assert ledger.boot_caps.book_cents == 9_000    # rescaled on Drew's word
    assert any("SIZING" in a for a in cash.test_alerts)


def test_rescale_math_at_35_50_100():
    assert "budget/window 291¢" in sizing_line(3_500)
    # WO-2026-07-24-G Part 4 / WO-L P2: the line states the REAL per-lane notional
    # sizes; F @97¢ at $35 book = 3500*0.24//97 lots at dial 0.24.
    assert f"F @97¢ → {int(3500 * config.F_NOTIONAL_PCT // 97)} lots" in sizing_line(3_500)
    assert "budget/window 416¢" in sizing_line(5_000)
    assert "budget/window 833¢" in sizing_line(10_000)


def test_worst_day_bound_arms_at_50(ledger):
    ledger.baseline(5_000, confirmed_by="/confirm_cash")
    assert "$25.00" in worst_day_bound_line(ledger)   # rail = $50 − $25 floor
    ledger.baseline(2_000, confirmed_by="/confirm_cash")
    assert "$0.00" in worst_day_bound_line(ledger)    # under the floor: zero


# ── §4: the P16 expected tape grades deposit day ───────────────────────────
def test_p16_grade_pends_until_the_deposit_and_f_trade(engine):
    import time as _t

    from scripts.tape_grade import CHECKS_P16, grade
    results = {n: ok for n, ok, _ in grade(engine.ledger.db, checks=CHECKS_P16)}
    assert results["deposit confirm round-trip landed"] is False   # pending Drew
    assert results["F's first live entries (depth floor proof)"] is False
    assert results["orphan settlements attribute to ORPHAN"] is True   # conditional
    assert results["closed brackets source=venue"] is True             # conditional
    assert results["zero unexplained orientation pages"] is True

    engine.ledger.db.execute(
        "INSERT INTO cash_movements (ts, amount_cents, kind, confirmed_by)"
        " VALUES (?,?,?,?)", (_t.time(), 3_000, "CONFIRMED_DEPOSIT", "auto_positive"))
    engine.ledger.db.commit()
    engine.ledger.record_fill(TICKER, "F", "yes", "ENTRY", 97, 1, "PROBE")
    results = {n: ok for n, ok, _ in grade(engine.ledger.db, checks=CHECKS_P16)}
    assert results["deposit confirm round-trip landed"] is True
    assert results["F's first live entries (depth floor proof)"] is True


def test_pack_grades_both_suites_with_separate_retirement(engine):
    from relay_engine.ops import daily_pack
    p1 = daily_pack(engine.ledger, engine.surface, engine.cash, econ=engine.econ)
    assert "DEPLOY GRADE (P15 expected tape): 6/6" in p1
    assert "DEPLOY GRADE (P16 deposit day expected tape):" in p1
    p2 = daily_pack(engine.ledger, engine.surface, engine.cash, econ=engine.econ)
    p3 = daily_pack(engine.ledger, engine.surface, engine.cash, econ=engine.econ)
    # P15 retires on its own clean tape; P16 stays active until deposit day
    assert "DEPLOY GRADE (P15): retired" in p3
    assert "DEPLOY GRADE (P16 deposit day expected tape):" in p3
