"""WO-FLIP-SIDE-ORIENT — THE BOOK MUST INVERT TO THE HELD SIDE (build 41).

FLIP is two-directional (it buys YES or NO), so its exit geometry must be
oriented to the HELD side: a NO@46 and a YES@46 have to receive
mathematically identical treatment. The MIRROR TEST below is the falsifiable
proof and the permanent CI gate — NO@X and YES@X, in mirror-image books,
produce byte-identical exit decisions at every tick.

READ-RULE FINDING (reported honestly to Drew): the WO's premise — that the
LIVE exit geometry is YES-scale and asymmetric — is FALSE at source. The
custody exit already reads `mark = held_price(side, book)` (the held-side
bid) and compares EVERY threshold (band floor 35, catastrophe 20, entry,
take) against THAT mark, so the live geometry was already side-symmetric by
construction. This WO formalizes it (the canonical `held_price` accessor,
routed through the two custody marks) and fixes the ONE genuine orientation
bug — the SHADOW two-barrier (`_shadow_two_barrier`), which drives nothing
live but mapped a NO contract's price straight into P(cross) when the
table's p_cross = P(YES wins), so a NO's implied P(cross) is the COMPLEMENT
(1 − price). The shadow now carries the held-side sign.

HARD RAIL: FLIP exit + swing-gate geometry ONLY. F stays one-directional
(F needs only who-wins-at-settlement — one direction is correct). No
Kelly/cash/rate-halt change. Ships at the 1-lot cap."""

import re

import pytest

from relay_engine import config, delta, failures
from relay_engine.book import OrderBook
from relay_engine.custodian import Custodian
from relay_engine.feed import DegradeLadder
from relay_engine.lane_flip import LaneFlip, FLIP_X
from relay_engine.spotlead import Needle

TICKER = "KXBTC15M-02JAN251000-T99"
EVENT = TICKER.rsplit("-", 1)[0]
CLOSE = 1_000_000.0


def _book(yes=40, no=49):
    b = OrderBook(market=TICKER)
    b.apply_snapshot({yes: 10}, {no: 10}, ts=1.0)
    return b


def _mirror_book(side: str, mark: int) -> OrderBook:
    """A book whose HELD-side bid is exactly `mark` for `side`. The YES-held
    and NO-held books are mirror images: held_price(side, book) == mark on
    both, so any side-relative exit sees identical inputs."""
    other = max(1, min(99, 100 - mark))
    b = OrderBook(market=TICKER)
    if side == "yes":
        b.apply_snapshot({mark: 10}, {other: 10}, ts=1.0)
    else:
        b.apply_snapshot({other: 10}, {mark: 10}, ts=1.0)
    return b


def _ctx(book, secs_left=700, sl=None):
    return {"book": book, "now": CLOSE - secs_left, "close_ts": CLOSE,
            "spot": None, "grain": None, "spotlead": sl}


@pytest.fixture(autouse=True)
def funnel(ledger):
    failures._warn_last.clear()
    failures.configure(ledger, alert_fn=lambda m: None, run_mode="TEST",
                       boot_id=1)
    yield
    failures._ledger = None


@pytest.fixture
def flip(gateway, ledger, surface):
    return LaneFlip(gateway, custodian=Custodian(gateway, ledger, surface,
                                                 ladder=DegradeLadder()))


def _open_props(flip, side, mark, *, entry=44, secs=700, patience_gap=10,
                sl=None):
    """Run ONE cycle of the OPEN custody exit for a held `side` at held
    price `mark`, on a mirror book, and return its proposals. A fresh
    window/record per call — no cross-tick state leaks into the compare."""
    w = flip._window(TICKER, CLOSE)
    w.opens.clear()
    w.hunts.clear()
    w.fills.clear()
    now = CLOSE - secs
    w.opens[side] = {"entry": entry, "fill_ts": now - patience_gap,
                     "count": 1, "take_oid": None, "take_proposed": True,
                     "collapse_polls": 0, "det_ts": None,
                     "entry_oid": None, "defer_polls": 0}
    book = _mirror_book(side, mark)
    ctx = _ctx(book, secs_left=secs, sl=sl)
    return flip._open_custody(w, TICKER, EVENT, book, ctx, secs, now)


def _norm(props, side):
    """The decision, side-name erased — a YES cut and its NO mirror collapse
    to the same tuple. Whole-word strip so 'not'/'now' survive intact."""
    out = []
    pat = re.compile(r"\b" + side + r"\b")
    for p in props:
        out.append((p.purpose, p.action, p.price_cents, p.count,
                    pat.sub("SIDE", p.reason or "")))
    return out


# ── the accessor: the canonical held-side price ────────────────────────────
def test_held_price_is_the_held_side_bid():
    b = OrderBook(market=TICKER)
    b.apply_snapshot({46: 10}, {51: 10}, ts=1.0)
    assert LaneFlip.held_price("yes", b) == b.best_yes_bid() == 46
    assert LaneFlip.held_price("no", b) == b.best_no_bid() == 51


# ── THE MIRROR TEST — the CI gate: NO@X ≡ YES@X at every tick ───────────────
def test_exit_geometry_mirror_no_equals_yes(flip):
    """The falsifiable proof. Across a full sweep of held prices, the NO
    position and the YES position — each on its own mirror book — produce
    IDENTICAL exit decisions. This is the gate: the commit is not complete
    until it passes on both sides."""
    for mark in (10, 15, 19, 20, 21, 25, 30, 34, 35, 36, 40, 44, 50, 60, 66):
        yes = _norm(_open_props(flip, "yes", mark), "yes")
        no = _norm(_open_props(flip, "no", mark), "no")
        assert yes == no, f"mirror BROKEN at held price {mark}: {yes} != {no}"


def test_mirror_holds_after_patience(flip):
    """Post-patience, the band-floor 'the swing did not come' cut is legit —
    and it stays mirror-symmetric."""
    for mark in (20, 30, 34, 35, 40, 50):
        yes = _norm(_open_props(flip, "yes", mark, patience_gap=400), "yes")
        no = _norm(_open_props(flip, "no", mark, patience_gap=400), "no")
        assert yes == no, f"post-patience mirror BROKEN at {mark}"


def test_mirror_holds_t10_handoff(flip):
    """The T-10 book-aware handoff (winner → held-to-settle, loser → cut)
    mirrors: NO and YES are treated identically relative to their basis."""
    for mark in (20, 34, 43, 44, 45, 55, 66):
        yes = _norm(_open_props(flip, "yes", mark, secs=550), "yes")
        no = _norm(_open_props(flip, "no", mark, secs=550), "no")
        assert yes == no, f"T-10 handoff mirror BROKEN at {mark}"


# ── the momentum stop is P&L-blind and side-blind ──────────────────────────
def test_momentum_stop_fires_both_sides(flip):
    """WO-2026-07-22-E: the momentum stop (entry − OPEN_MOMENTUM_STOP_C,
    sustained 2 polls) fires on EITHER held side, at the held-side mark,
    symmetric. Here the book is THROUGH the stop (mark below it), so both sides
    cross out — CUT, crossfire — at their own held mark. The exit is oriented to
    the held side, so a YES and its NO mirror get byte-identical decisions."""
    for side in ("yes", "no"):
        w = flip._window(TICKER, CLOSE)
        w.opens.clear()
        w.hunts.clear()
        w.fills.clear()
        now = CLOSE - 700
        w.opens[side] = {"entry": 44,
                         "fill_ts": now - (config.FLIP_NO_SELL_S + 30),
                         "count": 1, "take_oid": None, "take_proposed": True,
                         "collapse_polls": 0, "catastrophe_polls": 0,
                         "det_ts": None, "entry_oid": None, "defer_polls": 0}
        b = _mirror_book(side, 20)              # 20 < entry−10 (34): book through
        ctx = _ctx(b, secs_left=700)
        flip._open_custody(w, TICKER, EVENT, b, ctx, 700, now)      # poll 1
        cuts = [p for p in flip._open_custody(w, TICKER, EVENT, b, ctx, 700, now)
                if p.purpose == "CUT"]
        assert len(cuts) == 1 and "momentum stop" in cuts[0].reason
        assert cuts[0].price_cents == 20 and cuts[0].crossfire
        assert w.opens[side].get("exit_reason") == "MOMENTUM_STOP"
        flip.windows.clear()


def test_no_climbs_to_66_takes_not_cuts(flip):
    """A NO position that climbs to 66¢ (deep in profit from a 44¢ basis) is
    NOT cut — exactly as a YES@66 is not cut. The swing to the take is held;
    the resting take (entry+20) does the selling, untouched by this WO."""
    for side in ("yes", "no"):
        props = _open_props(flip, side, 66)
        assert [p for p in props if p.purpose == "CUT"] == []


# ── the momentum stop exits either held side, mirrored ─────────────────────
def test_momentum_stop_cuts_both_sides_mirrored(flip):
    """WO-2026-07-22-E: the momentum stop is held-relative and mirror-exact. An
    adverse move to entry − OPEN_MOMENTUM_STOP_C, sustained 2 polls, exits — and
    NO@stop and YES@stop, each on its own mirror book, produce BYTE-IDENTICAL
    decisions. Here the mark sits AT the stop (book not through), so both rest
    maker-first (EXIT, no crossfire) at the same held-side price."""
    out = {}
    for side in ("yes", "no"):
        w = flip._window(TICKER, CLOSE)
        w.opens.clear()
        now = CLOSE - 700
        w.opens[side] = {"entry": 44,
                         "fill_ts": now - (config.FLIP_NO_SELL_S + 30),
                         "count": 1, "take_oid": None, "take_proposed": True,
                         "collapse_polls": 0, "det_ts": None,
                         "entry_oid": None, "defer_polls": 0}
        book = _mirror_book(side, 34)          # 34 == entry−10: at the stop line
        ctx = _ctx(book, secs_left=700)
        flip._open_custody(w, TICKER, EVENT, book, ctx, 700, now)  # poll 1
        props = flip._open_custody(w, TICKER, EVENT, book, ctx, 700, now)
        # the momentum-stop reason carries NO side token (it is the same fixed
        # English on both sides), so the RAW proposal is byte-identical for a
        # YES and its NO mirror — comparing raw is the strongest mirror proof
        # here (and _norm's whole-word erasure would mangle the phrase 'no
        # hold' on the NO side, a false asymmetry).
        out[side] = [(p.purpose, p.action, p.price_cents, p.count,
                      p.crossfire, p.reason) for p in props]
    assert out["yes"] == out["no"]
    assert out["yes"] and out["yes"][0][0] == "EXIT"
    assert not out["yes"][0][4]                       # maker-first: no crossfire
    assert out["yes"][0][2] == 34 and "momentum stop" in out["yes"][0][5]


# ── the SHADOW two-barrier — the one genuine orientation bug, fixed ─────────
def test_shadow_uses_held_side_complement_for_no(flip, monkeypatch):
    """Part C.3: the table's p_cross = P(spot crosses strike) = P(YES wins),
    so a YES contract's price IS p_cross but a NO's is the COMPLEMENT. The
    shadow must query the table with the held-side target: 0.44 for YES@44,
    0.56 for NO@44. The retired code used 0.44 for both — the sign bug."""
    monkeypatch.setattr(delta, "is_loaded", lambda: True)
    monkeypatch.setattr(delta, "p_cross",
                        lambda d, t, session="ALL": 0.5)
    calls = []

    def _rec(p, t, session="ALL"):
        calls.append(round(p, 4))
        return 100.0
    monkeypatch.setattr(delta, "distance_for_p", _rec)

    flip._shadow_two_barrier(_ctx(_book()), 44, 700.0, "yes")
    yes_targets = set(calls)
    calls.clear()
    flip._shadow_two_barrier(_ctx(_book()), 44, 700.0, "no")
    no_targets = set(calls)

    # YES reads held prices directly; NO reads every one as its complement
    assert 0.44 in yes_targets                 # join 44 → 0.44
    assert 0.56 in no_targets                  # join 44 → 1 − 0.44
    assert not (yes_targets & no_targets)      # no shared target: pure sign flip


def test_shadow_symmetric_table_gives_mirror(flip, monkeypatch):
    """With a point-symmetric table, the held-side complement makes NO@44
    and YES@44 produce IDENTICAL p_up/p_down — the shadow inherits the same
    held-price symmetry as the live exit."""
    monkeypatch.setattr(delta, "is_loaded", lambda: True)
    monkeypatch.setattr(delta, "p_cross",
                        lambda d, t, session="ALL": max(0.0, 1.0 - d / 400.0))
    monkeypatch.setattr(delta, "distance_for_p",
                        lambda p, t, session="ALL": (1.0 - p) * 400.0)
    y_up, y_down = flip._shadow_two_barrier(_ctx(_book()), 44, 700.0, "yes")
    n_up, n_down = flip._shadow_two_barrier(_ctx(_book()), 44, 700.0, "no")
    assert (y_up, y_down) == (n_up, n_down)
    assert y_up is not None and y_down is not None


def test_swing_gate_forwards_side_to_shadow(flip, monkeypatch):
    """The gate must carry the held-side sign into the shadow (Part C.3): a
    NO entry drives the complement lookup through _swing_gate, not just the
    bare _shadow_two_barrier."""
    monkeypatch.setattr(delta, "is_loaded", lambda: True)
    monkeypatch.setattr(delta, "p_cross",
                        lambda d, t, session="ALL": 0.5)
    calls = []
    monkeypatch.setattr(delta, "distance_for_p",
                        lambda p, t, session="ALL": calls.append(round(p, 4)) or 100.0)
    flip._window(TICKER, CLOSE)
    flip._swing_gate(_ctx(_book()), CLOSE - 700, 44, "no", TICKER)
    assert 0.56 in set(calls) and 0.44 not in set(calls)


# ── HARD RAIL — FLIP exit/gate geometry ONLY ───────────────────────────────
def test_rails_unchanged():
    # no Kelly / cash / rate-halt change
    assert config.KELLY_FRACTION_CEILING == pytest.approx(1.0 / 12.0)
    assert config.RATE_HALT_LOSSES == 2
    # the exit constants this WO leaves intact
    assert config.OPEN_TAKE_CENTS == 20
    assert config.OPEN_CATASTROPHE_FLOOR == 20
    assert config.OPEN_UNDETERMINED_BAND == (35, 65)
    # HUNT's scalper cut unchanged
    assert FLIP_X == 4


def test_f_stays_one_directional():
    """F needs only who-wins-at-settlement — one direction is CORRECT for F.
    This WO touches FLIP only; F's lane carries no held_price/mirror logic."""
    import inspect
    from relay_engine import lane_fh8
    src = inspect.getsource(lane_fh8)
    assert "held_price" not in src
    assert "_shadow_two_barrier" not in src
