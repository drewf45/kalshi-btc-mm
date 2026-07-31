"""WO-2026-07-25-L "F GETS THE BOOK; EVERYTHING ELSE EARNS IT" — the outside
review, made law: move anything not-F to shadow; F gets as much as possible
(bounded by the one number that keeps a bad hour survivable); everything else
earns it back from shadow with real signals and honest referees.

  P1  per-lane run mode (F LIVE, the rest 👻 SHADOW) + the pessimistic fill model
  P1b two ledgers, one table — shadow P&L NEVER touches tradeable capital
  P2  F MAXIMUM (dial 0.24, wall 0.30) + the stated single-loss bound
  P3  the earn-back protocol (distance to promotion, printed daily)
  P4  break-even recalibration + THIN (n<10 → no gate authority)

HARD RAIL: F byte-identical in logic (size params only)."""

import pytest

from relay_engine import config, scoring, shadow_fill, flip_ladder
from relay_engine.book import OrderBook
from relay_engine.gateway import Gateway, Order

EVENT = "KXBTC15M-02JAN251000"
TICKER = EVENT + "-T30"


def _book(yes=58, no=40):
    b = OrderBook(market=TICKER)
    b.apply_snapshot({yes: 500}, {no: 500}, ts=1.0)
    return b


def _entry(lane, side="yes", price=58, count=3):
    return Order(lane=lane, event=EVENT, market=TICKER, side=side, action="buy",
                 price_cents=price, count=count, size_tier=config.TIER_PROBE,
                 purpose="ENTRY", why="OPEN grain yesx2 · join 58c")


# ══ P2 · F MAXIMUM ══════════════════════════════════════════════════════════
def test_f_dial_and_wall_raised_dial_stays_under_wall():
    assert config.F_NOTIONAL_PCT == 0.24
    assert config.AT_RISK_PCT["F"] == 0.30
    assert config.F_NOTIONAL_PCT < config.AT_RISK_PCT["F"]      # dial < wall
    assert config.dial_wall_violations() == []                 # boot won't FATAL
    # the dial stays ~80% of the wall (the -G headroom rule)
    assert abs(config.F_NOTIONAL_PCT / config.AT_RISK_PCT["F"] - 0.8) < 0.01


def test_single_loss_bound_is_the_dial():
    assert config.f_single_loss_bound_pct() == config.F_NOTIONAL_PCT   # ≈24% of book


# ══ P1 · PER-LANE RUN MODE ══════════════════════════════════════════════════
def test_lane_mode_f_live_everything_else_shadow():
    assert config.LANE_MODE["F"] == "LIVE"
    for ln in ("FLIP", "OPEN", "HUNT", "PAIR", "D", "P", "H8"):
        assert config.LANE_MODE[ln] == "SHADOW"


def test_shadow_run_forces_every_lane_shadow():
    # born state: RUN_MODE=SHADOW → the global kill overrides LANE_MODE entirely
    assert not config.live_submit_enabled()
    assert not config.lane_is_live("F")        # even F is shadow when global is off
    assert not config.lane_books_shadow("F")   # one paper ledger, not the mixed split


@pytest.fixture
def live(monkeypatch):
    """A LIVE global run with a mocked venue — the mixed world where F is live
    and the rest rehearse."""
    from relay_engine import venue
    monkeypatch.setattr(config, "live_submit_enabled", lambda: True)
    placed = []

    def _place(client, ticker, side, price_cents, count=1, expiration_ts=None,
               v2_price_str=None, post_only=True):
        placed.append({"lane_side": side, "price": price_cents})
        return "LIVE-OID-1", {"ok": True}
    monkeypatch.setattr(venue, "get_balance", lambda c: (1000.0, 0.0))
    monkeypatch.setattr(venue, "place_order_maker", _place)
    return placed


def test_f_places_live_desk_lane_is_a_ghost(gateway, live):
    gateway.venue_client = object()
    # F (LIVE): real placement, live oid, broker traffic. The favorite rests at
    # the touch (yes-ask = 100−no_bid = 97), never a taker.
    f_res = gateway.submit(_entry("F", side="yes", price=97), _book(yes=97, no=3))
    assert f_res.shadow is False and f_res.order_id == "LIVE-OID-1"
    assert len(live) == 1                       # F touched the broker
    # FLIP (SHADOW): a SHADOW- oid, ZERO broker traffic — a ghost
    flip_res = gateway.submit(_entry("FLIP", side="yes", price=58),
                              _book(yes=58, no=42))
    assert flip_res.shadow is True and flip_res.order_id.startswith("SHADOW-")
    assert len(live) == 1                        # the desk placed NOTHING


# ══ P1b · SHADOW ISOLATION — simulated money never reaches real capital ═════
def test_shadow_lane_fill_is_fatal_refused_in_live(ledger, live):
    from relay_engine import failures
    failures.configure(ledger, alert_fn=lambda m: None, run_mode="LIVE", boot_id=1)
    # a LIVE fill for a shadow lane is a leak into deployed_cents — FATAL
    with pytest.raises(Exception):
        ledger.record_fill(TICKER, "FLIP", "yes", "ENTRY", 58, 3, "PROBE")
    # F (live) records normally
    failures._warn_last.clear()
    ledger.record_fill(TICKER, "F", "yes", "ENTRY", 97, 3, "PROBE")
    failures._ledger = None


def test_shadow_cell_is_tagged_and_never_drives_live_sizing(ledger, live):
    # a shadow-lane trip books to cell_outcomes tagged shadow=1
    ledger.record_cell_outcome("OPEN", 58, won=False, pnl_cents=-30, fees_cents=0,
                               market=TICKER, kind="trip", contracts=3,
                               shadow=config.lane_books_shadow("OPEN"))
    cell = scoring.price_cell(58)
    live_n, _, _ = scoring.cell_stats(ledger, "OPEN", cell)             # live authority
    shadow_n, _, _ = scoring.cell_stats(ledger, "OPEN", cell, shadow=True)
    assert live_n == 0 and shadow_n == 1        # shadow cell invisible to live sizing


def test_treasury_reads_live_only_by_construction(ledger):
    import inspect
    src = (inspect.getsource(ledger.book_cents.__func__)
           + inspect.getsource(ledger.lifetime_pnl_cents.__func__)
           + inspect.getsource(ledger.deployed_cents.__func__))
    assert "cell_outcomes" not in src and "shadow" not in src


# ══ P1 · THE PESSIMISTIC FILL MODEL ════════════════════════════════════════
def test_through_price_rule_buy_and_sell():
    tp = shadow_fill._through_price
    # buy yes @58 fills when no_bid >= 100-58 = 42 (the yes-ask reached 58)
    assert tp("buy", "yes", 58, yes_bid=58, no_bid=42) is True
    assert tp("buy", "yes", 58, yes_bid=58, no_bid=41) is False   # ask still 59, not reached
    # sell yes @62 fills when yes_bid >= 62 (a buyer lifts the offer)
    assert tp("sell", "yes", 62, yes_bid=62, no_bid=30) is True
    assert tp("sell", "yes", 62, yes_bid=61, no_bid=30) is False
    # None bid → no evidence → no fill (pessimistic)
    assert tp("buy", "no", 58, yes_bid=None, no_bid=50) is False


def test_would_fill_requires_rest_then_through_price():
    o = _entry("FLIP", side="yes", price=58)
    # AT/through book (no_bid 42 → yes-ask 58) but zero rest → no fill
    assert shadow_fill.would_fill(o, _book(yes=58, no=42), rested_polls=0) is False
    # rested + through → fills
    assert shadow_fill.would_fill(o, _book(yes=58, no=42), rested_polls=1) is True
    # rested but the book never reached (no_bid 40 → ask 60 > 58) → no fill
    assert shadow_fill.would_fill(o, _book(yes=58, no=40), rested_polls=3) is False


def test_model_statement_is_pessimistic_and_printed():
    s = shadow_fill.MODEL_STATEMENT
    assert "pessimistic" in s and "THROUGH" in s and "expired" in s


# ══ P3 · EARN-BACK — the door, marked with numbers ═════════════════════════
def test_promotion_distance_prints_the_gap(ledger, surface):
    import json
    for i in range(10):                    # 7/10 shadow conversion
        surface.write_row("FLIP", f"M{i}", f"w{i}", "FLIP_SWING",
                           detail=json.dumps({"gross_cents": 4 if i < 7 else -14}))
    lines = flip_ladder.promotion_distance_lines(ledger)
    body = "\n".join(lines)
    assert "DESK (FLIP/OPEN)" in body and "70%" in body and "HUNT" in body
    assert "short" in body or "MET" in body     # the gap or the met flag


# ══ P4 · THIN — no gate authority by placeholder ═══════════════════════════
def test_thin_cell_holds_no_authority(ledger):
    cell = scoring.price_cell(58)
    for i in range(3):                      # n=3 < CELL_THIN_MIN_N (10)
        ledger.record_cell_outcome("OPEN", 58, won=True, pnl_cents=4, fees_cents=0,
                                   market=f"M{i}", kind="trip")
    s = scoring.score(ledger, "OPEN", cell)
    assert s["thin"] is True
    assert scoring.cell_has_authority(ledger, "OPEN", cell) is False


def test_cell_leaves_thin_only_by_realized_n(ledger):
    cell = scoring.price_cell(58)
    for i in range(config.CELL_THIN_MIN_N):
        ledger.record_cell_outcome("OPEN", 58, won=True, pnl_cents=4, fees_cents=0,
                                   market=f"M{i}", kind="trip")
    assert scoring.score(ledger, "OPEN", cell)["thin"] is False
    assert scoring.cell_has_authority(ledger, "OPEN", cell) is True


def test_scoreboard_separates_live_and_shadow_and_marks_thin(ledger):
    ledger.record_cell_outcome("F", 96, won=True, pnl_cents=4, fees_cents=0,
                               market="MFL", kind="settle")           # live, thin
    ledger.record_cell_outcome("OPEN", 58, won=False, pnl_cents=-30, fees_cents=0,
                               market="MSH", kind="trip", shadow=True)  # shadow
    body = "\n".join(scoring.scoreboard_lines(ledger, book_cents=10_000))
    assert "CELL SCOREBOARD (LIVE)" in body
    assert "SHADOW — rehearsal, NOT tradeable capital" in body
    assert "THIN" in body                       # the n<10 cells greyed


# ══ P1 · THE SHADOW FILL SIMULATOR (booking through the real conclusion path) ═
def _mini_engine(ledger, surface):
    from relay_engine.shadow_runner import ShadowEngine
    gw = Gateway(ledger, surface)
    eng = object.__new__(ShadowEngine)
    eng.gateway = gw
    eng.ledger = ledger
    eng.telegram = type("T", (), {"alert": staticmethod(lambda m: None)})()
    eng.divergence_watches = {}
    eng._shadow_rested = {}
    return eng, gw


def test_shadow_sim_roundtrip_books_a_shadow_cell(ledger, surface, live):
    """A shadow round-trip driven by the pessimistic sim books its outcome to
    cell_outcomes tagged shadow=1 — never a fills/settlement/cash row (treasury
    stays clean), and every line is 👻."""
    eng, gw = _mini_engine(ledger, surface)
    # shadow ENTRY buy H8 yes @58 → SHADOW- oid
    r = gw.submit(_entry("H8", side="yes", price=58), _book(yes=58, no=42))
    assert r.order_id.startswith("SHADOW-")
    # book trades through (no_bid 42 → yes-ask 58): rest+fill in one sweep
    assert eng.simulate_shadow_fills(TICKER, _book(yes=58, no=42), 1000.0) == 1
    # shadow EXIT sell yes @62 → fills when yes_bid reaches 62
    ex = Order(lane="H8", event=EVENT, market=TICKER, side="yes", action="sell",
               price_cents=62, count=3, size_tier=config.TIER_PROBE,
               purpose="EXIT", reason="take")
    gw.submit(ex, _book(yes=58, no=42))
    assert eng.simulate_shadow_fills(TICKER, _book(yes=62, no=30), 1002.0) == 1
    # the round-trip concluded → a shadow cell, +4/contract, and NO fills row
    row = ledger.db.execute(
        "SELECT shadow, won FROM cell_outcomes WHERE lane='H8'").fetchone()
    assert row is not None and row[0] == 1 and row[1] == 1
    assert ledger.db.execute(
        "SELECT COUNT(*) FROM fills WHERE lane='H8'").fetchone()[0] == 0


def test_pessimistic_sim_never_fills_a_price_the_book_missed(ledger, surface, live):
    """ACCEPTANCE #5 (the 07/25 lesson, mechanized): the desk's losers did NOT
    convert — a take the market never reaches must NOT fill. The sim leaves an
    entry resting-unfilled when the book never trades through it (no fantasy
    fills buying a fake promotion)."""
    eng, gw = _mini_engine(ledger, surface)
    # a Saturday-shape entry: buy yes @60, but the book stays away (no_bid 38 →
    # yes-ask 62 > 60) — the pile never trades down to us.
    gw.submit(_entry("OPEN", side="yes", price=60), _book(yes=60, no=38))
    for t in range(3):                          # three polls of rest, still away
        assert eng.simulate_shadow_fills(TICKER, _book(yes=60, no=38), 1000.0 + t) == 0
    # nothing booked, the order simply never filled — the honest rehearsal
    assert ledger.db.execute("SELECT COUNT(*) FROM cell_outcomes").fetchone()[0] == 0
    # and the moment the book DOES trade through, the same order fills (the model
    # is a referee, not a wall)
    assert eng.simulate_shadow_fills(TICKER, _book(yes=60, no=40), 1010.0) == 1


# ══ HARD RAIL: F byte-identical in logic (size params only) ═════════════════
def test_f_sizing_logic_untouched():
    f = scoring.size_order(4162, 97, 10_000, lane="F")
    assert f.contracts == int(4162 * config.F_NOTIONAL_PCT // 97)
    assert "cap n/a" in f.reason                # the F branch's shape is unchanged
