"""WO-SALV-1 — SALVAGE REASON TAPE: the custodian's seven silent gags get
a voice. State-change-only rows (Engineer), flap-capped with a settlement
summary that counts every tick (Adversary), (d, t_rem) on TABLE_GAP rows
(Scientist). Today a 97¢ position rode to the 5% backstop and no artifact
could say which gag suppressed salvage — next time one row will."""

import json

import pytest

from relay_engine import config, delta, failures
from relay_engine.book import OrderBook
from relay_engine.custodian import Custodian, OpenPosition, salvage_params
from relay_engine.feed import DegradeLadder


@pytest.fixture(autouse=True)
def _rearm_salvage(monkeypatch):
    # WO-2026-07-26-N §P4.2: the salvage execution machinery (the -M re-arm path)
    # runs un-gagged in these mechanics tests; the production gag is tested in
    # test_overnight_doctrine.
    monkeypatch.setattr(config, "SALVAGE_GAGGED", False)

TICKER = "KXBTC15M-02JAN251000-T99"
EVENT = TICKER.rsplit("-", 1)[0]
STRIKE = 118_000.0
CLOSE = 1_000_000.0


def _book(yes=62, no=30):
    b = OrderBook(market=TICKER)
    b.apply_snapshot({yes: 10}, {no: 10}, ts=1.0)
    return b


@pytest.fixture(autouse=True)
def _funnel(ledger):
    failures.configure(ledger, alert_fn=lambda m: None, run_mode="TEST",
                       boot_id=1)
    yield
    failures._ledger = None


@pytest.fixture
def custodian(gateway, ledger, surface):
    c = Custodian(gateway, ledger, surface, ladder=DegradeLadder())
    c.set_lane_params("F", salvage_params())
    return c


def _pos(p_entry=0.93, entry=95):
    return OpenPosition(event=EVENT, market=TICKER, lane="F", side="yes",
                        count=1, entry_price_cents=entry, entry_p_win=0.95,
                        size_tier=config.TIER_PROBE, entry_time=CLOSE - 600,
                        d_entry=200.0, t_entry=500.0, p_entry=p_entry)


def _gag_rows(ledger):
    return [json.loads(d) for (d,) in ledger.db.execute(
        "SELECT detail FROM surface_rows WHERE state='SALVAGE_GAG'"
        " ORDER BY id").fetchall()]


def _summaries(ledger):
    return [json.loads(d) for (d,) in ledger.db.execute(
        "SELECT detail FROM surface_rows WHERE state='SALVAGE_SUMMARY'"
    ).fetchall()]


def _tick(custodian, pos, spot, now):
    return custodian.tick(books={TICKER: _book()},
                          close_ts_of=lambda m: CLOSE, now=now,
                          balance_usd=100.0, spot=spot,
                          boundaries={TICKER: (None, STRIKE)})


def test_oscillation_capped_and_one_summary(custodian, gateway, ledger,
                                            monkeypatch):
    """A reason that flaps (TABLE_GAP ↔ BELOW_K, 30 alternations) logs at
    most the cap in transition rows; the ONE summary still counts every
    gagged tick."""
    pos = _pos()
    custodian.adopt(pos)
    ledger.record_fill(TICKER, "F", "yes", "ENTRY", 95, 1, "PROBE")
    state = {"gap": False}

    def p_survive(d, t, session="ALL"):
        state["gap"] = not state["gap"]
        return None if state["gap"] else 0.93   # healthy: no collapse
    monkeypatch.setattr(delta, "p_survive", p_survive)
    for i in range(30):
        _tick(custodian, pos, spot=STRIKE + 300, now=CLOSE - 500 + i)
    rows = _gag_rows(ledger)
    assert len(rows) <= config.SALVAGE_GAG_MAX_TRANSITIONS
    assert pos.salvage_gag_transitions == 30
    # the Scientist's amendment: TABLE_GAP rows carry (d, t_rem)
    gap_row = next(r for r in rows if "TABLE_GAP" in r["reason"])
    assert "d" in gap_row and "t_rem" in gap_row
    # conclusion (settlement path) → exactly ONE summary, all ticks counted
    custodian.emit_salvage_summary(pos, "SETTLED", realized_cents=5)
    custodian.emit_salvage_summary(pos, "SETTLED", realized_cents=5)  # no-op
    s = _summaries(ledger)
    assert len(s) == 1
    assert s[0]["gagged"]["TABLE_GAP"] == 15
    assert s[0]["gagged"]["BELOW_K"] == 15
    assert s[0]["exit_trigger"] == "SETTLED"


def test_no_anchor_registers_disabled_and_summary_names_it(custodian,
                                                           gateway, ledger,
                                                           monkeypatch):
    pos = _pos(p_entry=None)
    custodian.adopt(pos, disabled_reason="NO_ANCHOR")
    row = ledger.db.execute(
        "SELECT detail FROM surface_rows WHERE"
        " state='SALVAGE_DISABLED_TAGGED'").fetchone()[0]
    assert json.loads(row)["reason"] == "NO_ANCHOR"
    ledger.record_fill(TICKER, "F", "yes", "ENTRY", 95, 1, "PROBE")
    monkeypatch.setattr(delta, "p_survive", lambda d, t, session="ALL": 0.9)
    _tick(custodian, pos, spot=STRIKE + 300, now=CLOSE - 500)
    custodian.emit_salvage_summary(pos, "SETTLED", realized_cents=-95)
    s = _summaries(ledger)
    assert len(s) == 1 and "NO_ANCHOR" in s[0]["gagged"]
    assert s[0]["salvage_fired"] is None


def test_fired_salvage_armed_then_summary_with_save(custodian, gateway,
                                                    ledger, monkeypatch):
    """ARMED at registration → (gags if any) → SALVAGE_MAKER →
    crossfire → summary carrying the realized save."""
    monkeypatch.setattr(delta, "p_survive",
                        lambda d, t, session="ALL":
                        0.5 + 0.43 * min(1.0, d / (0.3 * max(1.0, t))))
    pos = _pos(p_entry=0.93)
    custodian.adopt(pos)
    assert ledger.db.execute(
        "SELECT COUNT(*) FROM surface_rows WHERE state='SALVAGE_ARMED'"
    ).fetchone()[0] == 1
    ledger.record_fill(TICKER, "F", "yes", "ENTRY", 95, 1, "PROBE")
    spot_bad = STRIKE - 90
    _tick(custodian, pos, spot_bad, now=CLOSE - 400)   # strike 1
    _tick(custodian, pos, spot_bad, now=CLOSE - 399)   # strike 2 → maker
    assert pos.salvage_fired == "SALVAGE_MAKER"
    # unfilled maker → crossfire → execute_cut writes the ONE summary
    _tick(custodian, pos, spot_bad,
          now=CLOSE - 399 + config.SALVAGE_R_S + 1)
    s = _summaries(ledger)
    assert len(s) == 1
    assert s[0]["salvage_fired"] == "SALVAGE_CROSSFIRE"
    assert s[0]["exit_trigger"] == "SALVAGE_CROSSFIRE"
    assert s[0]["realized_cents"] == 62 - 95   # sold at the bid vs entry


def test_exit_fill_concludes_with_summary(gateway, ledger, surface,
                                          custodian):
    """The take FILLS → the P14 custody-pop path emits the summary with
    the realized round trip."""
    from relay_engine.fills import FillBooker
    from relay_engine.gateway import Order
    booker = FillBooker(gateway, ledger, surface, custodian=custodian)
    booker.anchor_fn = lambda market, side: (200.0, 500.0, 0.93)
    b = _book(yes=40, no=49)
    r = gateway.submit(Order(lane="F", event=EVENT, market=TICKER,
                             side="yes", action="buy", price_cents=48,
                             count=1, size_tier=config.TIER_PROBE,
                             purpose="ENTRY", why="F tier48 · surv~price"),
                       b)
    booker.sweep([{"fill_id": "e", "order_id": r.order_id,
                   "yes_price_dollars": "0.4800", "count": 1}], now=1000.0)
    x = gateway.submit(Order(lane="F", event=EVENT, market=TICKER,
                             side="yes", action="sell", price_cents=53,
                             count=1, size_tier=config.TIER_PROBE,
                             purpose="EXIT"), b)
    booker.sweep([{"fill_id": "x", "order_id": x.order_id,
                   "yes_price_dollars": "0.5300", "count": 1}], now=1010.0)
    s = _summaries(ledger)
    assert len(s) == 1
    assert s[0]["exit_trigger"] == "EXIT_FILLED"
    assert s[0]["realized_cents"] == 5


def test_pack_carries_salvage_review(ledger, surface):
    surface.write_row("F", "M1", "w1", "SALVAGE_SUMMARY",
                      detail=json.dumps({"gagged": {"BELOW_K": 40},
                                         "salvage_fired": None,
                                         "exit_trigger": "SETTLED",
                                         "realized_cents": 5, "entry": 95}))
    from relay_engine.ledger import CashProtocol
    from relay_engine.ops import daily_pack
    cash = CashProtocol(ledger, alert_fn=lambda m: None)
    pack = daily_pack(ledger, surface, cash)
    assert "SALVAGE REVIEW (24h):" in pack
    assert "rides-to-settlement 1" in pack and "BELOW_K×40" in pack
