"""P22 "THE CELL SCOREBOARD" — the ladder gets its floor sensors: cell
writes at every close (§1), backfill (§1.2), per-cell breakeven and the
generalized fee bar (§2), price-adjusted tier bars — the F-bar blind spot's
regression test (§3), the wired ladder with paged, persisted, instant-demote
tiers (§4), and the /scoreboard render (§5)."""

import inspect

import pytest

from relay_engine import config, failures, scoring
from relay_engine.book import OrderBook
from relay_engine.fills import FillBooker
from relay_engine.gateway import Order
from relay_engine.sizing import wilson_lower_bound

TICKER = "KXBTC15M-02JAN251000-T99"
EVENT = TICKER.rsplit("-", 1)[0]


def _book(yes=48, no=49, qty=40):
    b = OrderBook(market=TICKER)
    b.apply_snapshot({yes: qty}, {no: qty}, ts=1.0)
    return b


@pytest.fixture(autouse=True)
def _funnel(ledger):
    failures.configure(ledger, alert_fn=lambda m: None, run_mode="TEST",
                       boot_id=1)


# ── §1: the cell outcome store ─────────────────────────────────────────────
def test_cell_row_on_round_trip(gateway, ledger, surface):
    """§1.1(a): a booked exit closes a unit of risk — one cell row, keyed by
    the ENTRY price's bucket, net of fees, FLIP intent split to OPEN."""
    booker = FillBooker(gateway, ledger, surface)
    book = _book()
    e = gateway.submit(Order(lane="FLIP", event=EVENT, market=TICKER,
                             side="yes", action="buy", price_cents=48,
                             count=1, size_tier=config.TIER_PROBE,
                             purpose="ENTRY",
                             why="OPEN grain yesx2 · join 48c · PROBE n=0"), book)
    booker.sweep([{"fill_id": "f-e", "order_id": e.order_id,
                   "yes_price_dollars": "0.4800", "count": 1}], now=1000.0)
    x = gateway.submit(Order(lane="FLIP", event=EVENT, market=TICKER,
                             side="yes", action="sell", price_cents=53,
                             count=1, size_tier=config.TIER_PROBE,
                             purpose="EXIT", reason="open take entry+5"),
                       book)
    booker.sweep([{"fill_id": "f-x", "order_id": x.order_id,
                   "yes_price_dollars": "0.5300", "count": 1}], now=1010.0)
    rows = ledger.db.execute(
        "SELECT lane, price_cell, won, pnl_cents, kind FROM cell_outcomes"
    ).fetchall()
    assert rows == [("OPEN", 45, 1, 5, "trip")]


def test_cell_row_on_settlement_attributes_opening_lane(ledger, surface,
                                                        gateway):
    """§1.1(b): a held position closes at settlement, attributed to the
    OPENING lane (the attribution law) — written beside record_outcome."""
    from relay_engine.shadow_runner import ShadowEngine
    ledger.record_fill(TICKER, "F", "no", "ENTRY", 96, 1, config.TIER_PROBE)
    eng = object.__new__(ShadowEngine)   # only the settle path's organs
    eng.ledger, eng.surface = ledger, surface
    eng.custodian = type("C", (), {"positions": {}})()   # SALV-1 sweep
    eng._window_of = {}
    eng._meta = lambda market: {}
    eng.feed = type("Feed", (), {"books": {}})()
    eng.econ = type("Econ", (), {
        "close_bracket": lambda self, *a, **k: None})()
    eng.account_value = lambda now: (None, "none")
    eng.settle_traded_market(TICKER, settled_yes=False, now=2000.0)
    rows = ledger.db.execute(
        "SELECT lane, price_cell, won, pnl_cents, kind FROM cell_outcomes"
    ).fetchall()
    # no@96 held, settled NO -> won, +4¢, cell 95-99
    assert rows == [("F", 95, 1, 4, "settle")]
    # idempotent by (market, lane, kind): a re-settle writes nothing new
    ledger.record_cell_outcome("F", 96, won=False, pnl_cents=-96,
                               fees_cents=0, market=TICKER, kind="settle")
    assert ledger.db.execute(
        "SELECT COUNT(*) FROM cell_outcomes").fetchone()[0] == 1


def test_backfill_replays_history_idempotently(ledger):
    """§1.2: fills+settlements replay once; round-trip lanes bank trip math,
    held lanes bank the settlement verdict; a second call writes nothing."""
    # a flat round-trip (lane OPEN attribution lost to history -> FLIP cell)
    ledger.record_fill("M1", "FLIP", "yes", "ENTRY", 48, 1, config.TIER_PROBE)
    ledger.db.execute("DELETE FROM cell_outcomes")  # record_fill wrote none (entry)
    ledger.record_fill("M1", "FLIP", "yes", "EXIT", 53, 1, config.TIER_PROBE,
                       fee_cents=1)
    ledger.db.execute("DELETE FROM cell_outcomes")  # simulate pre-P22 history
    # a held-to-settlement F clip
    ledger.record_fill("M2", "F", "no", "ENTRY", 96, 1, config.TIER_PROBE)
    ledger.record_settlement("M2", "F", 4)
    ledger.db.commit()
    n = ledger.backfill_cell_outcomes()
    assert n == 2
    rows = dict(((m, l), (w, p, k)) for l, m, w, p, k in ledger.db.execute(
        "SELECT lane, market, won, pnl_cents, kind FROM cell_outcomes"))
    assert rows[("M1", "FLIP")] == (1, 4, "backfill")   # 53-48-1 fee
    assert rows[("M2", "F")] == (1, 4, "backfill")
    assert ledger.backfill_cell_outcomes() == 0          # guarded
    assert ledger.db.execute(
        "SELECT COUNT(*) FROM cell_outcomes").fetchone()[0] == 2


# ── §2: breakeven per lane-kind (the ≥50% bar, generalized) ────────────────
def test_breakeven_hold_is_the_price(ledger):
    """F/H8 hold cells: BE = mid/100 until the DODGED curve has n>=20 —
    the raw price IS the bar, conservative."""
    assert scoring.breakeven(ledger, "F", 95) == pytest.approx(0.97)
    assert scoring.breakeven(ledger, "H8", 45) == pytest.approx(0.47)


def test_breakeven_trip_fee_adjusted(ledger):
    """Trip cells: w = (L+fee)/(T+L+fee) — HUNT's 4/2 geometry lands above
    the naive 1/3, OPEN's band-shaped bail demands more than a coin-flip."""
    be_hunt = scoring.breakeven(ledger, "HUNT", 45)
    fee = scoring.taker_fee_cents(45)
    assert be_hunt == pytest.approx((2 + fee) / (4 + 2 + fee))
    assert 0.33 < be_hunt < 0.5
    # P-FLIP-THESIS-1 §3 (take 5 -> 20): the ~20c scalp target LOWERS the
    # cell's required win-rate below the coin-flip — bail L = 47-35 = 12
    # vs T=20; the loss exit pays taker AT THE BAIL PRICE (35c)
    be_open = scoring.breakeven(ledger, "OPEN", 45)
    fee_open = scoring.taker_fee_cents(35)
    assert be_open == pytest.approx(
        (12 + fee_open) / (config.OPEN_TAKE_CENTS + 12 + fee_open))
    assert 0.33 < be_open < 0.5
    # generic trip lanes: the old symmetric bar, fee-pushed past 50%
    assert 0.5 < scoring.breakeven(ledger, "P", 45) < 0.6


def test_salvage_adjusted_breakeven_after_evidence(ledger, surface):
    """§3.3 lens note: with n>=SALVAGE_ADJ_MIN_N DODGED verdicts, the hold
    breakeven drops below the raw price — salvage recapture is real money."""
    import json
    raw = scoring.breakeven(ledger, "F", 95)
    for i in range(config.SALVAGE_ADJ_MIN_N):
        surface.write_row("F", f"M{i}", f"w{i}", "SALVAGE_VERDICT",
                          detail=json.dumps({"dodged_cents": 60,
                                             "realized": -37,
                                             "counterfactual": -97,
                                             "verdict": "DODGED_LOSS"}))
    adj = scoring.breakeven(ledger, "F", 95)
    assert adj < raw
    assert adj == pytest.approx(37 / (3 + 37))  # L_eff=97-60, W=100-97


# ── §3: price-adjusted bars (the F-bar blind spot dies) ────────────────────
def test_f_high_cell_lean_bar_is_capped_not_flat(ledger):
    """THE regression test: a 97¢ F cell's LEAN bar is .98 (BE .97 + .03,
    capped at the honest ceiling) — NOT the flat .65 that promoted four
    hold-wins into a leveraged coin-toss."""
    lean, clear = scoring.bars_for_cell(ledger, "F", 95)
    assert lean == pytest.approx(0.98)
    assert clear == pytest.approx(0.99)
    # a mid cell keeps the flat floors when BE sits below them
    lean45, clear45 = scoring.bars_for_cell(ledger, "HUNT", 45)
    assert lean45 == pytest.approx(0.65)
    assert clear45 == pytest.approx(0.80)


def test_probe_stays_a_ruling_not_a_bar(ledger):
    """§3.2: zero evidence -> PROBE, never SUPPRESS (R1/R2 stand)."""
    assert scoring.score(ledger, "OPEN", 45)["tier"] == config.TIER_PROBE


# ── §4: the wired ladder ───────────────────────────────────────────────────
def _bank_wins(ledger, lane, price, wins, losses=0):
    for i in range(wins):
        ledger.record_cell_outcome(lane, price, won=True, pnl_cents=5,
                                   fees_cents=0, market=f"W{lane}{price}{i}",
                                   kind="trip")
    for i in range(losses):
        ledger.record_cell_outcome(lane, price, won=False, pnl_cents=-5,
                                   fees_cents=0, market=f"L{lane}{price}{i}",
                                   kind="trip")


def test_tier_up_pages_once_persists_and_demotes_instantly(ledger):
    pages = []
    # 34 straight wins: LB(34,34) ≈ .899 >= .65 LEAN bar (HUNT cell 45)
    _bank_wins(ledger, "HUNT", 45, 34)
    tier = scoring.tier_for(ledger, "HUNT", 45, alert_fn=pages.append)
    assert tier == config.TIER_CLEAR  # LB .899 >= .80+? clear bar .80
    assert len(pages) == 1 and "📶 TIER UP" in pages[0]
    assert "n=34" in pages[0] and "LB 0.9" in pages[0]
    # persisted; a second identical call pages NOTHING
    assert ledger.get_state("tier_HUNT_45") == config.TIER_CLEAR
    assert scoring.tier_for(ledger, "HUNT", 45, alert_fn=pages.append) \
        == config.TIER_CLEAR
    assert len(pages) == 1
    # the row banked its math
    import json
    how = json.loads(ledger.db.execute(
        "SELECT how_json FROM failures WHERE why_tag='TIER_CHANGE'"
    ).fetchone()[0])
    assert how["lb"] >= how["bar"] and how["n"] == 34
    # losses crash the LB -> demotion at the NEXT call, no grace, paged
    _bank_wins(ledger, "HUNT", 45, 0, losses=30)
    tier2 = scoring.tier_for(ledger, "HUNT", 45, alert_fn=pages.append)
    assert tier2 == config.TIER_PROBE
    assert len(pages) == 2 and "📉 TIER DOWN" in pages[1]
    assert ledger.get_state("tier_HUNT_45") == config.TIER_PROBE


def test_runner_sizes_entries_from_the_score(ledger, gateway, surface):
    """P27 §1 OVERTURNED tier-capped sizing: contracts = min(kelly, depth,
    risk cap) for EVERY cell — the tier still computes (the REPORTING
    stamp on the order; pages; custody scaling) but it never votes."""
    from relay_engine.shadow_runner import ShadowEngine
    _bank_wins(ledger, "OPEN", 48, 60)   # LB(60,60)=.94 >= OPEN clear bar
    eng = object.__new__(ShadowEngine)
    eng.ledger = ledger
    eng.telegram = type("T", (), {"alert": staticmethod(lambda m: None)})()
    eng._size_zero_logged = set()   # WO-2026-07-23-B: F sizing logs (guard d)
    # WO-SWING-GATE-EVENT §4.2: FLIP is capped to 1 lot while its swing
    # gate is miscalibrated — the tier still computes and stamps (reporting
    # unchanged), but the count is bounded. The full-Kelly FORMULA is
    # verified on an UNCAPPED lane (F) below.
    prop = Order(lane="FLIP", event=EVENT, market=TICKER, side="yes",
                 action="buy", price_cents=48, count=1,
                 size_tier=config.TIER_PROBE, purpose="ENTRY",
                 why="OPEN grain yesx2 · join 48c")
    eng._score_and_size(prop, _book(yes=48, no=49))
    assert prop.size_tier == config.TIER_CLEAR   # earned, stamped, reported
    assert prop.count == config.FLIP_SIZE_CAP    # §4.2: FLIP capped to 1
    # WO-2026-07-23-B Part 1: F no longer takes the Kelly path — it self-sizes
    # by NOTIONAL (F_NOTIONAL_PCT of book / price), bounded only by real depth.
    # On a $12 book at 48c the notional is 5 lots (depth 40·0.25=10 doesn't
    # bind), driven by the book — NOT clamped to the retired count cap of 3.
    ledger.baseline(1200, confirmed_by="test")
    fprop = Order(lane="F", event=EVENT, market=TICKER, side="yes",
                  action="buy", price_cents=48, count=1,
                  size_tier=config.TIER_PROBE, purpose="ENTRY",
                  why="F tier48 · surv~price")
    eng._score_and_size(fprop, _book(yes=48, no=49))
    assert fprop.count == int(1200 * config.F_NOTIONAL_PCT // 48) == 5
    assert fprop.count > config.NET_RISK_CROSS_LANE_CAP   # the cap no longer binds F
    # a virgin cell: PROBE stamp, FLIP still capped to 1 (ladder reports,
    # never governs; §4.2 caps the lane, not the formula)
    prop2 = Order(lane="FLIP", event=EVENT, market=TICKER, side="no",
                  action="buy", price_cents=44, count=1,
                  size_tier=config.TIER_PROBE, purpose="ENTRY",
                  why="OPEN grain nox2 · join 44c")
    eng._score_and_size(prop2, _book(yes=48, no=44))
    assert prop2.size_tier == config.TIER_PROBE
    assert prop2.count == config.FLIP_SIZE_CAP


def test_no_static_probe_sizing_paths_remain():
    """§6 grep-test: every sized ENTRY passes through scoring; no lane
    module calls size_order with a literal tier (boot's display line is a
    print, not a sizing path)."""
    from relay_engine import lane_flip, lanes, shadow_runner
    runner_src = inspect.getsource(shadow_runner.ShadowEngine)
    assert "scoring.tier_for" in runner_src
    assert "_score_and_size(proposal" in runner_src
    assert "size_order(" not in inspect.getsource(lanes)
    assert "size_order(" not in inspect.getsource(lane_flip)


# ── §5: the scoreboard ─────────────────────────────────────────────────────
def test_scoreboard_renders_sorted_with_warnings(ledger):
    _bank_wins(ledger, "OPEN", 45, 1)          # 1/1: LB .21, BE > .5 -> RED
    ledger.record_cell_outcome("F", 96, won=True, pnl_cents=4, fees_cents=0,
                               market="MF", kind="settle")
    lines = scoring.scoreboard_lines(ledger, book_cents=10_000)
    assert lines[0].startswith("CELL SCOREBOARD")
    body = "\n".join(lines)
    assert "OPEN  45-49" in body and "F     95-99" in body
    assert "⚠" in body                          # red margins warn
    assert "*salvage-adj pending" in body       # hold BE still raw
    assert "HUNT  --" in body                   # empty lanes still listed
    # sorted by margin, best first: OPEN's −.52 sits above F's −.76 (a 1/1
    # hold at 96¢ is the deepest red on the board — the blind spot, visible)
    f_i = next(i for i, ln in enumerate(lines) if ln.startswith("F "))
    o_i = next(i for i, ln in enumerate(lines) if ln.startswith("OPEN"))
    assert o_i < f_i


def test_scoreboard_command_is_read_only_whitelisted(ledger, cash):
    from relay_engine.ops import Telegram
    tg = Telegram(cash, send_fn=lambda m: None)
    assert "/scoreboard" in Telegram.COMMANDS
    tg.scoreboard_fn = lambda: "CELL SCOREBOARD\n(empty)"
    assert tg.handle_command("/scoreboard").startswith("CELL SCOREBOARD")
    # the refusal line still names the whole (grown-by-one) whitelist
    refusal = tg.handle_command("/buy_everything")
    assert "/scoreboard" in refusal and "unknown command" in refusal
