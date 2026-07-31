"""WO-2026-07-24-J "POINT THE HUNTER FORWARD" — the banked Divergence Engine
promoted into HUNT's first live scope. HUNT no longer buys the TOUCH lag; it
buys the SETTLE edge.

  P1 the SETTLE table — p_end(d,t): P the window CLOSES beyond d, the question
     the market actually prices. Same 180-day corpus, one new column family;
     the physical law p_end <= p_cross proves the two surfaces answer DIFFERENT
     questions (a window that closes beyond d necessarily touched d).
  P2 the FORWARD gate — edge = wilson_LB(p_end)*100 − join >= HUNT_EDGE_MIN_C;
     needle DEMOTES to ATTENTION (gate A: when to look, never whether to buy);
     every entry carries the EV tag.
  P3 exit WATCHES THE THESIS — bail when the settle edge is gone (settle_LB <=
     cost sustained N polls); the two-tick level bail RETIRES.
  P4 the casefile names every probability's question (settle / touch), carries
     $ distances, calls the gate quantity EDGE, marks the info-only touch.
  P5 the DIVERGENCE read-back — realized settle vs p_end vs book, by edge.

HARD RAIL: F byte-identical (lane_fh8 untouched); touch surface untouched, only
LABELED (info)."""

import math

import pytest

from relay_engine import config, delta, delta_builder


# ── P1: the SETTLE surface from the same corpus ─────────────────────────────
def _synth_candles(n=6000, seed=7, vol=28.0):
    """Deterministic 1-min BTC-like bars: (ts, open, high, low, close). A
    seeded LCG random walk (no Math.random dependence) with intrabar wicks, so
    the max excursion (touch) strictly dominates the net close move (settle)."""
    ts0 = 1_700_000_000            # a fixed Mon 00:00-ish epoch, UTC
    price = 60_000.0
    state = seed
    def rnd():                     # LCG in [0,1)
        nonlocal state
        state = (1103515245 * state + 12345) & 0x7FFFFFFF
        return state / 0x7FFFFFFF
    out = []
    for i in range(n):
        o = price
        step = (rnd() - 0.5) * 2 * vol
        c = o + step
        # wicks extend BEYOND both ends → touch >= |net| always
        wick = vol * (0.5 + rnd())
        hi = max(o, c) + wick
        lo = min(o, c) - wick
        out.append((ts0 + i * 60, o, hi, lo, c))
        price = c
    return out


def test_settle_surface_built_question_tagged_and_bounded():
    rows = delta_builder.compute_delta_table(_synth_candles())
    assert rows, "builder produced no rows"
    # every row carries the settle column family, question-tagged alongside touch
    for r in rows:
        assert {"p_cross", "p_end", "p_end_n", "p_end_wilson_lb"} <= set(r)
    # A1: p_end in [0,1]; the LB never exceeds the point (it is a LOWER bound)
    assert all(0.0 <= r["p_end"] <= 1.0 for r in rows)
    assert all(r["p_end_wilson_lb"] <= r["p_end"] + 1e-9 for r in rows)
    # THE PHYSICS (Adversary ii): settle ⊆ touch — a close beyond d must have
    # touched d — so p_end <= p_cross EVERYWHERE. Different questions, proven.
    assert all(r["p_end"] <= r["p_cross"] + 1e-9 for r in rows)
    # and the two surfaces are genuinely DIFFERENT (not the same number twice):
    assert any(r["p_end"] < r["p_cross"] - 1e-6 for r in rows)


def test_csv_fields_carry_the_settle_family():
    for col in ("p_end", "p_end_n", "p_end_wilson_lb"):
        assert col in delta_builder.CSV_FIELDS


def test_wilson_lb_is_the_lower_bound():
    # mirror of _wilson_ub, on the other side of the centre
    p, n = 0.30, 200
    lb = delta_builder._wilson_lb(p, n)
    ub = delta_builder._wilson_ub(p, n)
    assert lb < p < ub
    assert delta_builder._wilson_lb(0.0, 0) == 0.0     # empty cell → floor


# ── P1: accessors are BLIND on absence (legacy touch-only table) ────────────
@pytest.fixture
def settle_table(monkeypatch):
    """A loaded table WITH the settle surface."""
    tbl = {(100, 300, "ALL"): {"p_cross": 0.25, "n": 500, "effective_n": 33,
                               "wilson_ub": 0.30, "p_end": 0.14, "p_end_n": 500,
                               "p_end_wilson_lb": 0.10}}
    monkeypatch.setattr(delta, "_TABLE", tbl)
    monkeypatch.setattr(delta, "_LOADED", True)
    return tbl


@pytest.fixture
def legacy_table(monkeypatch):
    """A legacy touch-only table (no p_end) — HUNT must stay BLIND."""
    tbl = {(100, 300, "ALL"): {"p_cross": 0.25, "n": 500, "effective_n": 33,
                               "wilson_ub": 0.30}}
    monkeypatch.setattr(delta, "_TABLE", tbl)
    monkeypatch.setattr(delta, "_LOADED", True)
    return tbl


def test_accessors_read_settle_surface(settle_table):
    assert delta.p_end(100, 300) == 0.14
    assert delta.p_end_wilson_lb(100, 300) == 0.10
    assert delta.settle_loaded() is True


def test_accessors_blind_on_legacy_table(legacy_table):
    assert delta.p_end(100, 300) is None
    assert delta.p_end_wilson_lb(100, 300) is None
    assert delta.settle_loaded() is False


def test_accessors_blind_when_cell_missing(settle_table):
    assert delta.p_end(999, 300) is None       # no such cell → None, not a guess


# ── P2: the forward gate — config floor is fee-derivable ────────────────────
def test_edge_floor_defaults_and_clears_fees():
    # DREW-DEFAULT 6c, floored above a PROBE round-trip taker cost (~4c) + 2c
    assert config.HUNT_EDGE_MIN_C == 6.0
    assert config.HUNT_EDGE_GONE_POLLS == 3


def test_casefile_names_its_questions_and_banishes_fair():
    from relay_engine.spotlead import Needle
    n = Needle(side="yes", d_before=120, d_after=56, delta_p=8.0,
               fair_cents=70.0, t_remaining=545.0)
    cf = n.casefile()
    assert "touch 70% (info)" in cf and "fair" not in cf


# ── P5: the DIVERGENCE read-back computes the three questions ────────────────
def _hunt_case(dist, settle, lb, book, edge, touch, ev, side="↑"):
    return (f"HUNT {side} spot ${dist} off strike, T-9:05 · settle {settle}% "
            f"(LB {lb}) · book {book}¢ · edge {edge:+d} · touch {touch}% "
            f"(info) · EV {ev:+d}")


def test_divergence_parses_full_tag_set():
    from relay_engine.ops import _parse_hunt_case
    c = _parse_hunt_case(_hunt_case(56, 72, 70, 60, 10, 74, 12))
    # the full tag set (§P4): p_end, p_end_LB, p_touch, book, edge, ev_c
    assert c == {"side": "yes", "dist": 56, "p_end": 72, "p_end_lb": 70,
                 "book": 60, "edge": 10, "p_touch": 74, "ev_c": 12}


def test_divergence_computes_realized_vs_p_end_vs_book(ledger, surface):
    from relay_engine.ops import hunt_divergence_lines
    # three HUNT-side (yes) bets in the 8-11 edge bucket, distinct markets
    for i, settled in enumerate((True, True, False)):
        mkt = f"KXBTC15M-J{i}-T30"
        surface.write_row("FLIP", mkt, f"w{i}", "PROPOSED",
                           detail=_hunt_case(56, 72, 70, 60, 10, 74, 12))
        ledger.record_outcome(mkt, settled_yes=settled)
    lines = hunt_divergence_lines(ledger)
    body = "\n".join(lines)
    assert "HUNT DIVERGENCE" in body
    # all three calibration questions, in the 8-11 bucket
    assert "edge 8-11" in body
    assert "realized 67%" in body          # 2 of 3 yes-bets settled in favor
    assert "p_end 72%" in body             # the table's belief
    assert "book 60%" in body              # the market's implied price
    # HUNT is NOT promoted here — the Wilson bar is reported, tier stays PROBE
    assert "PROBE" in body or "promotable" in body


def test_divergence_blind_when_no_hunts(ledger, surface):
    from relay_engine.ops import hunt_divergence_lines
    lines = hunt_divergence_lines(ledger)
    assert any("no HUNT-eligible windows" in ln for ln in lines)


# ── HARD RAIL: F byte-identical ─────────────────────────────────────────────
def test_f_sizing_untouched():
    from relay_engine import scoring
    f = scoring.size_order(4162, 97, 10_000, lane="F")
    assert f.contracts == int(4162 * config.F_NOTIONAL_PCT // 97)
    assert "cap n/a" in f.reason
