"""WO-FLIP-GEOMETRY-COHERENCE — make the four numbers agree (build 43).

The rate-halt's lens pass started as a one-liner (the geometry gate checks
the stale +20 take) but the gate was stale on BOTH sides: the take (checked
20, real take 5) AND the risk denominator (measured to the band floor 35,
but a loser actually rides to the catastrophe floor 20). Underneath: the
four FLIP numbers did not describe one trade — a 44c entry risked 24c (to
the real bail at 20) to win 5c, structurally negative, the churn that
tripped the halt.

DREW-RULED Option B (SCALP): keep the small take, add a matching TIGHT stop
so risk and win form a ~1:1 trade. A loser exits at entry −
OPEN_SCALP_STOP_CENTS (2-poll sustained, ANY time) — patience-to-catastrophe
is OFF on the loss side. The gate validates risk (the real bail = the scalp
stop) <= take+1, which the whole 39-49c thesis band passes by construction.

Coherence proof (§4): a thesis-range entry PASSES the gate (lane not closed);
risk is measured to the price a loser actually exits at; every admitted trade
risks no more than it can win; the 44c churn does not recur. HARD RAIL: no
Kelly/cash/rate-halt/F change; the take, hold-to-settle, and T-10 handoff
(winner side) unchanged; ships at FLIP_SIZE_CAP=1."""

import pytest

from relay_engine import config, failures
from relay_engine.book import OrderBook
from relay_engine.custodian import Custodian
from relay_engine.feed import DegradeLadder
from relay_engine.lane_flip import LaneFlip, FLIP_X

TICKER = "KXBTC15M-02JAN251000-T99"
EVENT = TICKER.rsplit("-", 1)[0]
CLOSE = 1_000_000.0
GRAIN_YES2 = {"direction": "yes", "length": 2, "k": 4}


def _book(yes=48, no=49):
    b = OrderBook(market=TICKER)
    b.apply_snapshot({yes: 10}, {no: 10}, ts=1.0)
    return b


def _ctx(book, secs_left=800, grain=None, sl=None):
    return {"book": book, "now": CLOSE - secs_left, "close_ts": CLOSE,
            "spot": None, "grain": grain, "spotlead": sl}


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


def _loss_cut(flip, entry, mark, *, secs=700):
    """Drive a booked loser down to `mark` for 2 sustained polls; return the
    CUT proposal (or None)."""
    w = flip._window(TICKER, CLOSE)
    w.opens.clear()
    now = CLOSE - secs
    w.opens["yes"] = {"entry": entry, "fill_ts": now - 10, "count": 1,
                      "take_oid": None, "take_proposed": True,
                      "collapse_polls": 0, "scalp_polls": 0, "det_ts": None,
                      "entry_oid": None, "defer_polls": 0}
    b = _book(yes=mark, no=55)
    ctx = _ctx(b, secs_left=secs)
    flip._open_custody(w, TICKER, EVENT, b, ctx, secs, now)          # poll 1
    props = flip._open_custody(w, TICKER, EVENT, b, ctx, secs, now)  # poll 2
    cuts = [p for p in props if p.purpose == "CUT"]
    return cuts[0] if cuts else None


# ── §4: the lane is NOT closed — the thesis band passes the honest gate ─────
def test_thesis_range_entries_pass_the_gate(flip):
    """The whole 39-49c thesis band clears risk<=take+1 by construction (risk
    is the constant scalp stop, 6 <= take+1 = 6). The gate admits the lane's
    own cheap-into-50/50 entries — it is not the lane-closing fix the naive
    take-only swap would have been."""
    for join in (40, 44, 48, 49):
        # a valid two-sided setup: both bids in OPEN_BAND, coherent (sum<=101)
        no = min(55, 100 - join)
        props = flip.evaluate(TICKER, _ctx(_book(yes=join, no=no),
                                           grain=GRAIN_YES2))
        entries = [p for p in props if p.purpose == "ENTRY"]
        assert len(entries) == 1, f"thesis entry {join} was rejected"
        assert not flip.windows[TICKER].open_geometry_logged
        flip.windows.clear()


def test_gate_rejects_incoherent_stop(flip, monkeypatch):
    """The gate still does its job: a stop WIDER than take+1 is incoherent
    (risk > win) and rejected as OPEN_BAD_GEOMETRY. The fix changed the
    NUMBERS the gate reads, never loosened the CHECK."""
    monkeypatch.setattr(config, "OPEN_SCALP_STOP_CENTS", 12)   # risk 12 > 6
    props = flip.evaluate(TICKER, _ctx(_book(), grain=GRAIN_YES2))
    assert [p for p in props if p.purpose == "ENTRY"] == []
    assert flip.windows[TICKER].open_geometry_logged


def test_coherence_invariant_holds_for_shipped_config():
    """The shipped four numbers form ONE trade: the real risk (scalp stop)
    is no bigger than the win (take) + 1, for the 1-lot regime."""
    assert config.OPEN_SCALP_STOP_CENTS <= LaneFlip._take_cents(1) + 1


# ── §4: risk is measured to the REAL bail (the price a loser exits at) ──────
def test_real_bail_matches_the_risk_denominator(flip):
    """The gate sizes risk against entry − OPEN_SCALP_STOP_CENTS, and a forced
    loser ACTUALLY exits there (via the SCALP stop), not at the catastrophe
    floor. The denominator and the behavior agree."""
    cut = _loss_cut(flip, entry=44, mark=44 - config.OPEN_SCALP_STOP_CENTS)
    assert cut is not None and "SCALP stop" in cut.reason
    assert cut.price_cents == 44 - config.OPEN_SCALP_STOP_CENTS   # exits at the real bail


def test_the_44_churn_loss_is_bounded_not_a_ride(flip):
    """The 201430 disease cured: a 44c loser is cut at the tight stop (loss
    ~6c), it does NOT ride to the catastrophe floor (loss 24c). Risk is now
    no bigger than the win it was sized against."""
    cut = _loss_cut(flip, entry=44, mark=38)          # entry−6
    assert cut is not None and "SCALP stop" in cut.reason
    loss = 44 - cut.price_cents
    assert loss <= config.OPEN_SCALP_STOP_CENTS + 1    # bounded small, not 24


def test_within_tolerance_holds(flip):
    """A dip inside the 6c tolerance (above the stop) is NOT cut — the winner
    side keeps its breathing room; only a real break of the tight stop bails."""
    assert _loss_cut(flip, entry=44, mark=40) is None   # 40 > stop 38


# ── HARD RAIL ──────────────────────────────────────────────────────────────
def test_take_and_winner_side_unchanged():
    """Option B reshaped the LOSS side only — the take is the build-42 nickel,
    untouched."""
    assert LaneFlip._take_cents(1) == 5
    assert config.WINDOW_BOOK_GOAL_CENTS == 5
    assert config.OPEN_TAKE_MIN == 5 and config.OPEN_TAKE_MAX == 20


def test_rails_unchanged():
    assert config.KELLY_FRACTION_CEILING == pytest.approx(1.0 / 12.0)
    assert config.RATE_HALT_LOSSES == 2
    assert config.OPEN_CATASTROPHE_FLOOR == 20     # deep backstop intact
    assert config.OPEN_UNDETERMINED_BAND == (35, 65)
    assert FLIP_X == 4


def test_f_untouched():
    """F stays one-directional and carries no scalp-stop geometry."""
    import inspect
    from relay_engine import lane_fh8
    src = inspect.getsource(lane_fh8)
    assert "OPEN_SCALP_STOP_CENTS" not in src
    assert "scalp_polls" not in src
