"""WO-2026-07-25-K "THE DESK EARNS ITS SIZE" — the +4 desk was 6-of-11 (55%
conversion) against a ~73% breakeven; no recorded feature separated the winners
from the losers at n=11. All four Saturday losses were structurally identical: a
56–62¢ favorite with spot pinned to the strike, where one $10–30 drift flips the
book. Armor held every fall to budget — but armor is not edge.

  P1 TUITION SIZE — demote instantly (FLIP_NOTIONAL_PCT 0.14 → 0.04); keep the
     armor, keep buying cells, at ~−35¢/day worst. rate-halt re-derives from the
     dial. F untouched.
  P2 THE CONFIDENCE INSTRUMENT — the -J settle table's desk duty: the favored
     side's SETTLE-fair must beat the join by FLIP_CONF_MIN_C. The four Saturday
     losses (pinned to strike → settle-fair ≈ 50) are REFUSED.
  P3 PROMOTION BY CONVERSION — the size ladder is mechanical: trailing-N
     conversion ≥ 75% promotes to full, < 65% demotes to tuition instantly; the
     entry cell's negative Wilson margin blocks a lucky streak from up-sizing.

HARD RAIL: F byte-identical."""

import json
import logging

import pytest

from relay_engine import config, delta, failures, flip_ladder, scoring, spotlead
from relay_engine.book import OrderBook
from relay_engine.custodian import Custodian
from relay_engine.feed import DegradeLadder
from relay_engine.lane_flip import LaneFlip

TICKER = "KXBTC15M-02JAN251000-T99"
EVENT = TICKER.rsplit("-", 1)[0]
CLOSE = 1_000_000.0


# ══ P1 · TUITION SIZE ═══════════════════════════════════════════════════════
def test_tuition_is_the_new_default_dial():
    assert config.FLIP_NOTIONAL_PCT == 0.04        # demoted instantly (P1)
    assert config.FLIP_FULL_NOTIONAL_PCT == 0.14   # the earned full size (P3)
    assert config.FLIP_NOTIONAL_PCT < config.AT_RISK_PCT["FLIP"]  # under the wall
    assert config.FLIP_FULL_NOTIONAL_PCT < config.AT_RISK_PCT["FLIP"]


def test_tuition_sizes_five_or_six_lots_at_the_live_book():
    """~$90 book (post-deposit): tuition = 9000 · 0.04 // 58 ≈ 6 lots."""
    dec = scoring.size_order(9000, 58, 10_000, lane="FLIP")   # bare = tuition
    assert dec.contracts == int(9000 * config.FLIP_NOTIONAL_PCT // 58)
    assert 5 <= dec.contracts <= 6 and "@4%" in dec.reason


def test_rate_halt_re_derives_from_the_tuition_dial():
    """The halt is 4 stop-outs at the CURRENT (tuition) size — it re-derives from
    the dial, not a stale constant: shrinking the dial shrinks the halt."""
    book = 9000
    assert config.rate_halt_drawdown_c(book) == \
        4 * int(book * config.FLIP_NOTIONAL_PCT / config.FLIP_HALT_REF_PRICE_C) \
        * config.OPEN_MOMENTUM_STOP_C
    # the demotion halved-and-more the halt vs the old full dial
    full_halt = 4 * int(book * config.FLIP_FULL_NOTIONAL_PCT
                        / config.FLIP_HALT_REF_PRICE_C) * config.OPEN_MOMENTUM_STOP_C
    assert config.rate_halt_drawdown_c(book) < full_halt


def test_boot_banner_prints_both_tuition_and_the_halt():
    from relay_engine.boot import sizing_line
    line = sizing_line(9000)
    assert "TUITION" in line and "FULL" in line
    assert "rate-halt bound" in line
    assert f"conversion ≥{config.FLIP_PROMOTE_CONV:.0%}" in line


# ══ P2 · THE CONFIDENCE INSTRUMENT ══════════════════════════════════════════
def _p_end_stub(d, t, session="ALL", **_kw):
    """A monotone settle surface: p_end HIGH near the strike (any drift closes
    beyond a tiny d), LOW far out. Pinned → settle-fair ≈ 50; far → ≈ 90."""
    return max(0.05, 1.0 - d / 600.0)


def test_settle_fair_favored_prices_fragility_forward(monkeypatch):
    monkeypatch.setattr(delta, "p_end", _p_end_stub)
    # pinned to the strike (d≈0, floored to the $50 grid) → a coin on its edge
    pinned = spotlead.settle_fair_favored(66_000.0, 66_000.0, "yes", 500)
    assert pinned is not None and pinned < 60      # ≈ 54 — barely favored
    # $300 above the strike → the physics genuinely favors yes
    favored = spotlead.settle_fair_favored(66_000.0, 65_700.0, "yes", 500)
    assert favored > 70 and favored > pinned
    # BLIND on a legacy touch-only tape
    monkeypatch.setattr(delta, "p_end", lambda d, t, session="ALL", **_kw: None)
    assert spotlead.settle_fair_favored(66_000.0, 65_700.0, "yes", 500) is None


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


def _book(yes, no):
    b = OrderBook(market=TICKER)
    b.apply_snapshot({yes: 1000}, {no: 1000}, ts=1.0)
    return b


def _ctx(book, secs_left, spot, strike):
    return {"book": book, "now": CLOSE - secs_left, "close_ts": CLOSE,
            "spot": spot, "grain": None, "spotlead": None,
            "boundary_lo": None, "boundary_hi": strike}


def _drive_open(flip, strike, entry_spot):
    """A passing PILE (baseline then a grown, agreeing, in-band entry) at the
    given strike geometry — returns the entry proposals (empty = refused)."""
    # baseline (secs_into 65): small skew, spot low
    flip.evaluate(TICKER, _ctx(_book(54, 48), 835, entry_spot - 20, strike))
    # entry (secs_into 80): favored yes@60, skew grown 20, spot moved +20 (agree)
    return flip.evaluate(TICKER, _ctx(_book(60, 40), 820, entry_spot, strike))


def test_saturday_losses_replay_as_refused(flip, monkeypatch):
    """The four Saturday losses: a 60¢ favorite with spot PINNED to the strike →
    settle-fair ≈ 54, conf ≈ −6 < 4 → REFUSED at the fragility veto."""
    monkeypatch.setattr(delta, "p_end", _p_end_stub)
    props = _drive_open(flip, strike=66_000.0, entry_spot=66_000.0)
    assert [p for p in props if p.purpose == "ENTRY"] == []   # fragile → refused
    assert flip.windows[TICKER].last_skip_reason == "conf_fragile"


def test_genuine_edge_passes_the_conf_gate(flip, monkeypatch):
    """Spot $300 above the strike → settle-fair ≈ 75, conf ≈ +15 ≥ 4 → the same
    pile fires, and the entry card carries the settle-fair and conf."""
    monkeypatch.setattr(delta, "p_end", _p_end_stub)
    props = _drive_open(flip, strike=65_700.0, entry_spot=66_000.0)
    entries = [p for p in props if p.purpose == "ENTRY"]
    assert len(entries) == 1
    assert "settle-fair" in entries[0].why and "conf +" in entries[0].why


def test_blind_settle_surface_proceeds_at_tuition(flip, monkeypatch):
    """No settle surface (legacy tape) → the conf gate ABSTAINS (BLIND); the desk
    still trades on its pile gates, bounded by tuition size (the seal's ordering:
    tuition pays while the instrument gets built)."""
    monkeypatch.setattr(delta, "p_end", lambda d, t, session="ALL", **_kw: None)
    props = _drive_open(flip, strike=66_000.0, entry_spot=66_000.0)
    entries = [p for p in props if p.purpose == "ENTRY"]
    assert len(entries) == 1
    assert "conf n/a" in entries[0].why and "BLIND" in entries[0].why


# ══ P3 · PROMOTION BY CONVERSION ════════════════════════════════════════════
def _swing(surface, market, gross):
    surface.write_row("FLIP", market, f"w-{market}", "FLIP_SWING",
                      detail=json.dumps({"market": market, "gross_cents": gross}))


def test_trailing_conversion_reads_flip_swing(ledger, surface):
    for i in range(6):
        _swing(surface, f"M{i}", gross=4 if i < 4 else -14)   # 4 of 6 won
    conv = flip_ladder.trailing_conversion(ledger, window=20)
    assert conv["n"] == 6 and conv["wins"] == 4
    assert abs(conv["rate"] - 4 / 6) < 1e-9


def test_promotion_and_demotion_fire_mechanically(ledger, surface):
    alerts = []
    # a full window at 80% conversion (16 of 20) → PROMOTE to full
    for i in range(20):
        _swing(surface, f"P{i}", gross=4 if i < 16 else -14)
    tier = flip_ladder.evaluate_size_tier(ledger, alert_fn=alerts.append)
    assert tier == flip_ladder.FULL
    assert any("DESK PROMOTED" in a and "80%" in a for a in alerts)
    # now the recent trips collapse (a fresh 20 at 40%) → DEMOTE, instantly
    alerts.clear()
    for i in range(20):
        _swing(surface, f"D{i}", gross=4 if i < 8 else -14)
    tier = flip_ladder.evaluate_size_tier(ledger, alert_fn=alerts.append)
    assert tier == flip_ladder.TUITION
    assert any("DESK DEMOTED" in a for a in alerts)


def test_hysteresis_band_holds_the_tier(ledger, surface):
    ledger.set_state("flip_size_tier", flip_ladder.FULL)
    for i in range(20):                       # 70% — inside the 65–75 band
        _swing(surface, f"H{i}", gross=4 if i < 14 else -14)
    assert flip_ladder.evaluate_size_tier(ledger) == flip_ladder.FULL  # holds


def test_margin_tiebreaker_blocks_up_sizing_a_losing_cell(ledger, surface):
    """ADVERSARY's gaming check: a promoted desk STILL sizes tuition into a cell
    whose Wilson margin is negative — a lucky streak cannot up-size a losing cell."""
    ledger.set_state("flip_size_tier", flip_ladder.FULL)
    # negative margin → tuition despite being promoted
    assert flip_ladder.active_notional_pct(ledger, cell_margin=-0.10) \
        == config.FLIP_NOTIONAL_PCT
    # non-negative margin → the earned full size
    assert flip_ladder.active_notional_pct(ledger, cell_margin=0.05) \
        == config.FLIP_FULL_NOTIONAL_PCT


def test_conversion_is_size_independent_no_deadlock(ledger, surface):
    """ADVERSARY's deadlock check: a demoted desk can still EARN promotion —
    conversion counts trips, not size, so tuition trips keep the ladder alive."""
    ledger.set_state("flip_size_tier", flip_ladder.TUITION)
    for i in range(20):
        _swing(surface, f"E{i}", gross=4 if i < 16 else -14)   # 80% at tuition size
    assert flip_ladder.evaluate_size_tier(ledger) == flip_ladder.FULL


def test_ladder_line_reports_the_number(ledger, surface):
    for i in range(10):
        _swing(surface, f"L{i}", gross=4 if i < 7 else -14)
    line = flip_ladder.ladder_line(ledger)
    assert "DESK SIZE" in line and "70%" in line and "7/10" in line


# ══ HARD RAIL: F byte-identical ═════════════════════════════════════════════
def test_f_sizing_untouched():
    f = scoring.size_order(4162, 97, 10_000, lane="F")
    assert f.contracts == int(4162 * config.F_NOTIONAL_PCT // 97)
    assert "cap n/a" in f.reason
    # F never reads the FLIP dials
    assert "4%" not in f.reason and "14%" not in f.reason
