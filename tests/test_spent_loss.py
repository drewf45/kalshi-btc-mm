"""WO-2026-07-27-W P2 — THE SPENT LOSS (per-lane halt geometry + spent ledger).

W2a — F's rate-halt bound speaks F's OWN loss units (F_HALT_TAIL_MULT × one full
F loss), so a SINGLE ordinary tail never halts F alone (it pages F_BIG_LOSS); the
halt fires on a CLUSTER (a second tail inside the trailing window). The desk lanes
keep their stop-derived rate_halt_drawdown_c.

W2b — when a halt clears the triggering losses are marked SPENT: the trailing
window restarts clean and the clear row records what was spent, so the same loss
can never convict twice (before this a tail poisoned the trailing-8 sum for hours
and re-halted the room on a loss it had already answered for).
"""

import json

import pytest

from relay_engine import config, failures
from relay_engine.window_econ import (LANES_HALTED_KEY, WindowEcon,
                                       _lane_outcomes_key)

BOOK = 10_000  # $100 tradeable (owed 0)


@pytest.fixture(autouse=True)
def _single_room_halt_keys():
    # This file tests the per-LANE halt mechanic (series-agnostic). Pin a
    # single-room roster so halt_scope stays the bare lane; series-scoping is
    # covered in test_ensemble_governor / test_three_rooms.
    config.SERIES[:] = ["KXBTC15M"]
    yield


class _TG:
    def __init__(self):
        self.alerts = []

    def alert(self, m):
        self.alerts.append(m)


@pytest.fixture
def econ(ledger, gateway, surface):
    failures._warn_last.clear()
    failures.configure(ledger, alert_fn=lambda m: None, run_mode="TEST",
                       boot_id=1)
    e = WindowEcon(ledger, gateway, surface, _TG())
    yield e
    failures._ledger = None


# ── W2a: the bound speaks each lane's own loss language ─────────────────────
def test_f_halt_bound_is_tail_units_desk_is_stop_units():
    # F: 1.5 × one full F loss (F_NOTIONAL_PCT × tradeable)
    assert config.f_halt_bound_c(BOOK) == int(round(
        config.F_HALT_TAIL_MULT * config.F_NOTIONAL_PCT * BOOK))
    assert config.lane_halt_bound_c("F", BOOK) == config.f_halt_bound_c(BOOK)
    assert config.lane_halt_bound_c("H8", BOOK) == config.f_halt_bound_c(BOOK)
    # the desk keeps the stop-derived geometry, unchanged
    assert config.lane_halt_bound_c("FLIP", BOOK) == \
        config.rate_halt_drawdown_c(BOOK)
    # and the tail bound is MUCH larger than the desk bound (that was the bug —
    # one F tail instantly cleared the ~3%-of-book desk threshold)
    assert config.f_halt_bound_c(BOOK) > 4 * config.rate_halt_drawdown_c(BOOK)


def test_a_single_f_tail_never_halts_f_alone(econ, gateway):
    """One full-size F loss (1.0× the unit) sits UNDER the 1.5× bound — it pages
    F_BIG_LOSS elsewhere but never rate-halts F by itself."""
    one_tail = -config.f_single_loss_bound_pct() * BOOK   # one full F loss
    econ._apply_streak("M0", int(one_tail), BOOK, per_lane={"F": int(one_tail)})
    assert econ.halted_lanes() == set()                   # no halt on one tail
    assert "RATE_HALT:F" not in gateway.entries_halted_reasons


def test_a_cluster_of_two_f_tails_halts_f(econ, gateway):
    """A SECOND tail inside the window pushes the sum past 1.5× — F parks; the
    page names the cluster and a TAIL_CLUSTER surface datum is written."""
    one_tail = int(-config.f_single_loss_bound_pct() * BOOK)
    econ._apply_streak("M0", one_tail, BOOK, per_lane={"F": one_tail})
    econ._apply_streak("M1", one_tail, BOOK, per_lane={"F": one_tail})
    assert econ.halted_lanes() == {"F"}
    assert "RATE_HALT:F" in gateway.entries_halted_reasons
    page = next(a for a in econ.telegram.alerts if "RATE HALT" in a)
    assert "tail cluster" in page and "F" in page
    row = econ.ledger.db.execute(
        "SELECT detail FROM surface_rows WHERE state='TAIL_CLUSTER'").fetchone()
    assert row is not None and json.loads(row[0])["n_tails"] == 2


def test_desk_geometry_unchanged_flip_halts_at_stop_bound(econ, gateway):
    """The desk lanes are byte-identical: FLIP still halts at its stop-derived
    rate_halt_drawdown_c, and one loss over that bound trips it."""
    over = -(config.rate_halt_drawdown_c(BOOK) + 50)
    econ._apply_streak("M0", int(over), BOOK, per_lane={"FLIP": int(over)})
    assert econ.halted_lanes() == {"FLIP"}
    page = next(a for a in econ.telegram.alerts if "RATE HALT" in a)
    assert "stop-outs" in page                        # desk language, not tail


# ── W2b: the spent loss — a halt clears the losses that caused it ────────────
def test_spent_loss_marked_and_window_restarts_clean(econ, ledger, gateway):
    """At /reset_halt the triggering losses are SPENT: the window restarts clean
    and the clear row + reply record what was spent — so a re-halt on the same
    stale loss is impossible."""
    one_tail = int(-config.f_single_loss_bound_pct() * BOOK)
    econ._apply_streak("M0", one_tail, BOOK, per_lane={"F": one_tail})
    econ._apply_streak("M1", one_tail, BOOK, per_lane={"F": one_tail})
    assert econ.halted_lanes() == {"F"}
    reply = econ.reset_halt(confirmed_by="test")
    assert econ.halted_lanes() == set()
    # the window is clean — a re-halt on the spent losses is impossible
    assert json.loads(ledger.get_state(_lane_outcomes_key("F"))) == []
    # the loss is recorded as SPENT on the reply and the clear row (Article 1)
    assert "SPENT" in reply and "F" in reply
    row = econ.ledger.db.execute(
        "SELECT detail FROM surface_rows WHERE state='HALT_RESET'").fetchone()
    assert row is not None and "spent=" in row[0]


def test_no_spent_line_when_nothing_was_lost(econ, ledger, gateway):
    """A halt cleared with a non-negative window (or a manually-set reason)
    records no spent line — the ledger only reports losses actually served."""
    gateway.halt_entries("RATE_HALT:F")
    ledger.set_state(LANES_HALTED_KEY, json.dumps(["F"]))
    ledger.set_state(_lane_outcomes_key("F"), json.dumps(
        [{"market": "M0", "pnl": 5.0}]))   # a WIN sits in the window
    reply = econ.reset_halt(confirmed_by="test")
    assert "SPENT" not in reply             # nothing lost → nothing spent


# ── registry + config tag ───────────────────────────────────────────────────
def test_tail_cluster_registered_as_a_data_question():
    from relay_engine import registry
    q = registry.get("TAIL_CLUSTER")
    assert q is not None and q.is_complete()
    assert "TAIL_CLUSTER" in registry.SEED_SURFACES


def test_f_halt_tail_mult_is_tagged_new_drew_default():
    tag = next(t for t in config.constant_tags()
               if t.name == "F_HALT_TAIL_MULT")
    assert tag.kind == config.DREW_DEFAULT
    assert config.F_HALT_TAIL_MULT == 1.5
