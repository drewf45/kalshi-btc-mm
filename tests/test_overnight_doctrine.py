"""WO-2026-07-26-N "THE OVERNIGHT DOCTRINE" — the 07/25→26 run, audited and ruled
in one deploy. The machine's newest idea (untuned salvage) fired twice without
confirmation and sold two winners at the bottom; its bookkeeper wrote each cut
down twice; and every alarm it owns rang true while it happened.

  P4.1 the ruled shadow modes enforced (FLIP/OPEN/HUNT/H8 SHADOW, F LIVE)
  P4.2 salvage GAGGED (telemetry-only) — both Saturday events NO-FIRE; -M re-arm
  P4.3 the two build-6 fixes replayed: duplicate-cut books once + sells only
       ledger-remaining; the same venue fill delivered twice books once
  P4.4 lifetime RESTATED from the settlements ledger alone
  P4.6 NO dial changes — F stays 24/30

HARD RAIL: F byte-identical (mode/env/restatement only)."""

import pytest

from relay_engine import config, failures
from relay_engine.book import OrderBook
from relay_engine.custodian import Custodian, OpenPosition, salvage_params
from relay_engine.feed import DegradeLadder
from relay_engine.gateway import Gateway, Order

TICKER = "KXBTC15M-02JAN251230-T30"
EVENT = TICKER.rsplit("-", 1)[0]
CLOSE = 1_000_000.0
STRIKE = 118_000.0


@pytest.fixture(autouse=True)
def funnel(ledger):
    failures._warn_last.clear()
    failures.configure(ledger, alert_fn=lambda m: None, run_mode="TEST", boot_id=1)
    yield
    failures._ledger = None


def _book(held_bid):
    # the position side is "no", so its mark IS the no-bid; set that directly.
    b = OrderBook(market=TICKER)
    b.apply_snapshot({max(1, 100 - held_bid - 2): 50}, {held_bid: 50}, ts=1.0)
    return b


@pytest.fixture
def custodian(gateway, ledger, surface):
    c = Custodian(gateway, ledger, surface, ladder=DegradeLadder())
    c.set_lane_params("F", salvage_params())
    return c


def _f_pos(custodian, ledger, entry=97):
    pos = OpenPosition(event=EVENT, market=TICKER, lane="F", side="no",
                       count=18, entry_price_cents=entry, entry_p_win=entry / 100,
                       size_tier=config.TIER_PROBE, entry_time=CLOSE - 600,
                       d_entry=None, t_entry=None, p_entry=None)
    custodian.adopt(pos)
    ledger.record_fill(TICKER, "F", "no", "ENTRY", entry, 18, "PROBE")
    return pos


def _tick(custodian, now, held_bid, spot=None):
    return custodian.tick(
        books={TICKER: _book(held_bid)}, close_ts_of=lambda m: CLOSE,
        now=now, balance_usd=100.0, spot=spot,
        boundaries={TICKER: (None, STRIKE)})


# ══ P4.1 · THE RULED SHADOW MODES ═══════════════════════════════════════════
def test_desk_and_h8_are_shadow_f_is_live():
    assert config.LANE_MODE["F"] == "LIVE"
    for ln in ("FLIP", "OPEN", "HUNT", "H8"):
        assert config.LANE_MODE[ln] == "SHADOW"


# ══ P4.2 · SALVAGE GAGGED — the two Saturday events NO-FIRE ══════════════════
def _rows(ledger, state):
    return ledger.db.execute(
        "SELECT COUNT(*) FROM surface_rows WHERE state=?", (state,)).fetchone()[0]


def test_saturday_slip_no_fire_when_gagged(custodian, ledger):
    """10:27 PM: F no@97 ×18, mark slips to 50 and SUSTAINS — the untuned SLIP cut
    it at maximum pain on a window that settled a WINNER. GAGGED: it holds to the
    bell, writes the counterfactual, takes NO cut."""
    assert config.SALVAGE_GAGGED is True                    # the ruled state
    pos = _f_pos(custodian, ledger, entry=97)
    _tick(custodian, CLOSE - 400, held_bid=50)              # tick 1 (sustain needed)
    cuts = _tick(custodian, CLOSE - 399, held_bid=50)       # tick 2: would fire
    assert pos.salvage_fired is None                         # NO cut
    assert pos.salvage_would_fire == "SALVAGE_SLIP"          # counterfactual logged
    assert cuts == [] or all(t != "SALVAGE_SLIP" for _, _, t in cuts)
    assert _rows(ledger, "SALVAGE_WOULD_FIRE") >= 1
    # the position is HELD — no CUSTODIAN_EXIT fill was booked
    assert ledger.db.execute(
        "SELECT COUNT(*) FROM fills WHERE action='CUSTODIAN_EXIT'").fetchone()[0] == 0
    assert f"{TICKER}:F" in custodian.positions              # rode to the bell


def test_second_saturday_slip_also_no_fire(custodian, ledger):
    """11:43 PM: F yes@95 ×11 → SLIP at 52, same class. Also NO-FIRE."""
    pos = _f_pos(custodian, ledger, entry=95)
    _tick(custodian, CLOSE - 400, held_bid=52)
    _tick(custodian, CLOSE - 399, held_bid=52)
    assert pos.salvage_fired is None and pos.salvage_would_fire == "SALVAGE_SLIP"


def test_rearm_path_fires_on_a_sustained_reversal(custodian, ledger, monkeypatch):
    """The -M re-arm is real: un-gagged, a SUSTAINED decisive reversal salvages
    MAKER-FIRST (never the overnight's immediate crossfire)."""
    monkeypatch.setattr(config, "SALVAGE_GAGGED", False)
    pos = _f_pos(custodian, ledger, entry=97)
    _tick(custodian, CLOSE - 400, held_bid=50)
    _tick(custodian, CLOSE - 399, held_bid=50)
    assert pos.salvage_fired == "SALVAGE_SLIP" and pos.salvage_oid is not None
    assert ledger.db.execute(
        "SELECT COUNT(*) FROM fills WHERE action='CUSTODIAN_EXIT'").fetchone()[0] == 0


def test_rearm_single_tick_dip_still_no_fire(custodian, ledger, monkeypatch):
    """Even un-gagged, the -M confirms-symmetric guard keeps a transient dip (the
    exact Saturday shape) from salvaging — a single tick that recovers NO-FIRES."""
    monkeypatch.setattr(config, "SALVAGE_GAGGED", False)
    pos = _f_pos(custodian, ledger, entry=97)
    _tick(custodian, CLOSE - 400, held_bid=50)              # dip
    _tick(custodian, CLOSE - 399, held_bid=97)              # recovered
    assert pos.salvage_fired is None and pos.salvage_slip_strikes == 0


# ══ P4.3 · THE TWO BUILD-6 FIXES, REPLAYED ══════════════════════════════════
def test_duplicate_cut_books_once_and_sells_only_ledger_remaining(custodian, ledger):
    """BATON LIFECYCLE: a cut fires and books; a SECOND cut attempt on the now-flat
    position re-derives from the ledger (remaining 0) and FLAT_RACE-skips — never a
    second booking (the 11:45 double-cut class, dead)."""
    pos = _f_pos(custodian, ledger, entry=97)
    b = _book(50)
    custodian.execute_cut(pos, 50, b, "CATASTROPHIC", crossfire=True)
    n1 = ledger.db.execute(
        "SELECT COUNT(*) FROM fills WHERE action='CUSTODIAN_EXIT'").fetchone()[0]
    assert n1 == 1                                           # booked once
    # a duplicate cut attempt on the concluded position: ledger-remaining is 0
    pos2 = OpenPosition(event=EVENT, market=TICKER, lane="F", side="no",
                        count=18, entry_price_cents=97, entry_p_win=0.97,
                        size_tier=config.TIER_PROBE, entry_time=CLOSE - 600)
    custodian.adopt(pos2)
    assert custodian.execute_cut(pos2, 50, b, "CATASTROPHIC", crossfire=True) is None
    n2 = ledger.db.execute(
        "SELECT COUNT(*) FROM fills WHERE action='CUSTODIAN_EXIT'").fetchone()[0]
    assert n2 == 1                                           # STILL once — no double-book


def test_duplicate_venue_fill_books_once(gateway, ledger, surface):
    """UNIFIED FILL DEDUP: the same venue fill (same fill_id) delivered twice books
    ONCE — fill_id primary-keyed, no path privileged (the double-booking root)."""
    from relay_engine.fills import FillBooker
    booker = FillBooker(gateway, ledger, surface)
    o = Order(lane="F", event=EVENT, market=TICKER, side="no", action="buy",
              price_cents=97, count=18, size_tier=config.TIER_PROBE, purpose="ENTRY")
    gateway.order_index["OID-1"] = o
    rec = {"fill_id": "FILL-DUP-1", "order_id": "OID-1", "count": 18,
           "yes_price": 3, "no_price": 97}
    s1 = booker.sweep([rec], now=CLOSE)
    s2 = booker.sweep([rec], now=CLOSE + 1)                  # same fill re-delivered
    assert s1["booked"] == 1 and s2["duplicate"] == 1 and s2["booked"] == 0
    assert ledger.db.execute(
        "SELECT COUNT(*) FROM fills WHERE market=?", (TICKER,)).fetchone()[0] == 1


# ══ P4.4 · THE RESTATEMENT ══════════════════════════════════════════════════
def test_lifetime_is_settlements_only_unhurt_by_phantom_cells(ledger):
    """The double-booked cuts corrupted cell/window REPORTING; lifetime reads the
    settlements ledger alone, so a phantom cell outcome never moves it."""
    from relay_engine import ops
    before = ledger.lifetime_pnl_cents()                    # settlements-only
    # a phantom (double-booked-shaped) cell outcome — cell/window REPORTING only
    ledger.record_cell_outcome("F", 97, won=False, pnl_cents=-878, fees_cents=32,
                               market="PHANTOM", kind="trip")
    assert ledger.lifetime_pnl_cents() == before            # lifetime untouched
    lines = ops.restated_money_lines(ledger)
    body = "\n".join(lines)
    assert "RESTATED" in body and "settlements ledger alone" in body


def test_daily_pack_carries_the_restated_tag(ledger, surface, cash):
    from relay_engine.ops import daily_pack
    pack = daily_pack(ledger, surface, cash)
    assert "RESTATED" in pack and "settlements ledger alone" in pack


# ══ P4.6 · NO DIAL CHANGES ══════════════════════════════════════════════════
def test_no_dial_changes_f_stays_24_30():
    assert config.F_NOTIONAL_PCT == 0.24 and config.AT_RISK_PCT["F"] == 0.30
    assert config.dial_wall_violations() == []


# ══ BOOT + HARD RAIL ════════════════════════════════════════════════════════
def test_boot_prints_the_doctrine():
    from relay_engine import boot
    tape = "\n".join(boot.boot_tape())
    assert "SALVAGE: GAGGED" in tape
    assert "RESTATED" in tape
    assert "CASH-SENTINEL DOCTRINE" in tape
    assert "NO CHANGES this deploy" in tape


def test_f_sizing_logic_untouched():
    from relay_engine import scoring
    f = scoring.size_order(4162, 97, 10_000, lane="F")
    assert f.contracts == int(4162 * config.F_NOTIONAL_PCT // 97)
    assert "cap n/a" in f.reason
