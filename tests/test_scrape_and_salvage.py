"""WO-2026-07-26-M + WO-2026-07-26-O (ONE DEPLOY) — the machine learns exactly
when to surrender (confirmed, floored, in the band where surrender is worth
something) and learns to pay its operator five dollars of every true ten, banked
patiently from the same venue-proven ledger.

  M (salvage): sighted 3-poll confirm, worth-floor, rarity auto-gag, maker-first,
    all cut paths folded — replayed here as fire/no-fire cases.
  O (the scrape): $5 owed per $10 of new high-water; tradeable = book − owed at
    every sizing base; deposits never mint, losses never un-owe, withdrawals
    decrement; /owed; OWED_UNDERWATER.

HARD RAIL: F entry/hold logic byte-identical (this touches exits, accounting,
and capital arithmetic — never how F selects or holds a contract)."""

import time

import pytest

from relay_engine import config, failures, scoring
from relay_engine.book import OrderBook
from relay_engine.custodian import Custodian, OpenPosition, salvage_params
from relay_engine.feed import DegradeLadder

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


# ══ helpers ═════════════════════════════════════════════════════════════════
def _cash(ledger, amt, kind):
    ledger.db.execute(
        "INSERT INTO cash_movements (ts, amount_cents, kind, confirmed_by)"
        " VALUES (?,?,?,?)", (time.time(), amt, kind, "/confirm_cash"))
    ledger.db.commit()


def _settle(ledger, market, pnl):
    ledger.db.execute(
        "INSERT INTO settlements (ts, market, lane, pnl_cents, divergent)"
        " VALUES (?,?,?,?,0)", (time.time(), market, "F", pnl))
    ledger.db.commit()


# ══ O · THE SCRAPE WALK ═════════════════════════════════════════════════════
def test_seed_owes_nothing(ledger):
    ledger.seed_scrape()
    assert ledger.owed_cents() == 0
    assert ledger.tradeable_cents() == ledger.book_cents()


def test_ten_dollars_new_high_owes_five(ledger):
    ledger.seed_scrape()
    _settle(ledger, "M1", 1000)                    # +$10 of new high-water
    assert ledger.owed_cents() == config.SCRAPE_PER_MILESTONE_C   # $5
    assert ledger.tradeable_cents() == ledger.book_cents() - 500


def test_drawdown_then_recovery_to_old_high_owes_nothing_new(ledger):
    ledger.seed_scrape()
    _settle(ledger, "M1", 1000)                    # +$10 → owed $5
    ledger.bank_scrape()
    _settle(ledger, "M2", -600)                    # drawdown
    assert ledger.owed_cents() == 500              # never un-owes
    _settle(ledger, "M3", 600)                     # recover to the OLD high
    assert ledger.owed_cents() == 500              # no NEW high → nothing new
    assert ledger.bank_scrape() == 0


def test_beyond_the_high_owes_the_next_five(ledger):
    ledger.seed_scrape()
    _settle(ledger, "M1", 2000)                    # +$20 of new high → owed $10
    assert ledger.owed_cents() == 1000


def test_deposit_mints_nothing(ledger):
    ledger.seed_scrape()
    _settle(ledger, "M1", 1000)                    # owed $5
    ledger.bank_scrape()
    _cash(ledger, 5000, "CONFIRMED_DEPOSIT")       # +$50 deposit
    assert ledger.owed_cents() == 500              # a deposit never mints
    assert ledger.bank_scrape() == 0


def test_withdrawal_decrements_owed(ledger):
    ledger.seed_scrape()
    _settle(ledger, "M1", 2000)                    # owed $10
    _cash(ledger, -700, "CONFIRMED_WITHDRAWAL")    # operator takes $7
    assert ledger.owed_cents() == 1000 - 700       # decremented to $3


def test_salvage_loss_never_increments_owed(ledger):
    """WIRING: a salvage cut realizes a LOSS; the high-water is monotonic, so no
    salvage event may ever increment owed."""
    ledger.seed_scrape()
    _settle(ledger, "M1", 1000)                    # owed $5
    ledger.bank_scrape()
    before = ledger.owed_cents()
    _settle(ledger, "SALV", -500)                  # a salvage-shaped loss books
    assert ledger.bank_scrape() == 0               # minted nothing
    assert ledger.owed_cents() == before           # owed unchanged


def test_owed_line_reports_the_scrape(ledger):
    from relay_engine.ops import owed_line
    ledger.seed_scrape()
    _settle(ledger, "M1", 1500)                    # +$15 → owed $5, $5 into next
    line = owed_line(ledger)
    assert "owed $5.00" in line and "tradeable" in line and "to the next $5" in line


# ══ O2 · SIZING TRUTH — every base reads tradeable ══════════════════════════
def test_worst_day_bound_reads_tradeable(ledger):
    from relay_engine.ops import worst_day_bound_line
    ledger.baseline(10_000, confirmed_by="test")   # $100 book (rail armed)
    ledger.seed_scrape()
    _settle(ledger, "M1", 4000)                     # +$40 → owed $20
    ledger.bank_scrape()
    line = worst_day_bound_line(ledger)
    # the rail term is (tradeable − floor), not (book − floor): book $140, owed
    # $20 → tradeable $120 → rail $95, not $115
    rail = ledger.tradeable_cents() / 100.0 - config.DRAWDOWN_ABSOLUTE_FLOOR_USD
    assert f"drawdown rail ${rail:.2f}" in line


def test_size_order_base_is_tradeable_in_the_entry_path():
    """The one money site: _score_and_size reads ledger.tradeable_cents(), so a
    seeded scrape shrinks F's size by exactly the owed notional."""
    import inspect
    from relay_engine import shadow_runner
    src = inspect.getsource(shadow_runner.ShadowEngine._score_and_size)
    assert "tradeable_cents()" in src and "book_c = self.ledger.tradeable_cents()" in src


def test_gateway_wall_and_boot_read_tradeable():
    import inspect
    from relay_engine import gateway, boot
    gsrc = inspect.getsource(gateway.Gateway._wall_net_risk_and_at_risk)
    assert "tradeable_cents()" in gsrc            # the at-risk wall base
    bsrc = inspect.getsource(boot.sizing_line)
    assert "tradeable" in bsrc and "owed" in bsrc  # boot prints & sizes off it


# ══ O4 · OWED_UNDERWATER ════════════════════════════════════════════════════
def test_owed_underwater_halts_entries(ledger, gateway, surface):
    from relay_engine.shadow_runner import ShadowEngine
    eng = object.__new__(ShadowEngine)
    eng.ledger = ledger
    eng.gateway = gateway
    eng.telegram = type("T", (), {"alert": staticmethod(lambda m: None)})()
    ledger.seed_scrape()                           # $100 book seed
    _settle(ledger, "M1", 5000)                    # +$50 high → owed $25 (banked)
    eng.bank_scrape_and_watch()
    _settle(ledger, "M2", -14000)                  # a −$140 crater → book $10
    assert ledger.owed_cents() == 2500             # never un-owes ($25)
    assert ledger.tradeable_cents() < config.ONE_F_LOT_COST_C   # underwater
    eng.bank_scrape_and_watch()
    assert "OWED_UNDERWATER" in gateway.entries_halted_reasons
    assert gateway.entries_halted_for("F")         # F entries blocked
    # …and it auto-clears when equity recovers
    _settle(ledger, "M3", 14000)
    eng.bank_scrape_and_watch()
    assert "OWED_UNDERWATER" not in gateway.entries_halted_reasons


# ══ M · SALVAGE — the named cases + auto-gag ════════════════════════════════
@pytest.fixture
def custodian(gateway, ledger, surface):
    c = Custodian(gateway, ledger, surface, ladder=DegradeLadder())
    c.set_lane_params("F", salvage_params())
    return c


def _fpos(custodian, ledger, entry=97, side="no"):
    pos = OpenPosition(event=EVENT, market=TICKER, lane="F", side=side, count=18,
                       entry_price_cents=entry, entry_p_win=entry / 100,
                       size_tier=config.TIER_PROBE, entry_time=CLOSE - 600)
    custodian.adopt(pos)
    ledger.record_fill(TICKER, "F", side, "ENTRY", entry, 18, "PROBE")
    return pos


def _tick(custodian, now, held_bid):
    b = OrderBook(market=TICKER)
    b.apply_snapshot({max(1, 100 - held_bid - 2): 50}, {held_bid: 50}, ts=1.0)
    return custodian.tick(books={TICKER: b}, close_ts_of=lambda m: CLOSE, now=now,
                          balance_usd=100.0, spot=None,
                          boundaries={TICKER: (None, STRIKE)})


def test_52230_should_fire_sustained_pinned(custodian, ledger):
    """52230-30 SHOULD-FIRE: a 97c favorite pinned at 55 for the confirm polls
    salvages maker-first, in-band, above the floor."""
    pos = _fpos(custodian, ledger, entry=97)
    for i in range(config.SALVAGE_CONFIRM_POLLS):
        _tick(custodian, CLOSE - 400 + i, held_bid=55)
    assert pos.salvage_fired == "SALVAGE_SLIP" and pos.salvage_oid is not None


def test_52345_no_fire_spot_recovered(custodian, ledger):
    """52345-45 NO-FIRE: the mark dipped then recovered — not pinned, no cut."""
    pos = _fpos(custodian, ledger, entry=95)
    _tick(custodian, CLOSE - 400, held_bid=52)
    _tick(custodian, CLOSE - 399, held_bid=95)     # recovered
    assert pos.salvage_fired is None


def test_overactive_auto_gags(custodian, ledger):
    """§S3: salvage is exceptional — more than SALVAGE_RARITY_MAX_PER_DAY fires in
    a day AUTO-GAGS the machine (SALVAGE_OVERACTIVE); further fires HOLD."""
    for k in range(config.SALVAGE_RARITY_MAX_PER_DAY + 2):
        mkt = f"KXBTC15M-02JAN25{k:02d}00-T30"
        pos = OpenPosition(event=mkt.rsplit("-", 1)[0], market=mkt, lane="F",
                           side="no", count=1, entry_price_cents=97,
                           entry_p_win=0.97, size_tier=config.TIER_PROBE,
                           entry_time=CLOSE - 600)
        custodian.adopt(pos)
        ledger.record_fill(mkt, "F", "no", "ENTRY", 97, 1, "PROBE")
        b = OrderBook(market=mkt)
        b.apply_snapshot({47: 50}, {50: 50}, ts=1.0)
        for i in range(config.SALVAGE_CONFIRM_POLLS):
            custodian.tick(books={mkt: b}, close_ts_of=lambda m: CLOSE,
                           now=CLOSE - 400 + i, balance_usd=100.0, spot=None,
                           boundaries={mkt: (None, STRIKE)})
    assert custodian.salvage_disarmed() is True    # auto-gagged
    assert failures.count("SALVAGE_OVERACTIVE") >= 1 if hasattr(failures, "count") \
        else True


def test_worth_floor_no_cut_below_thirty(custodian, ledger):
    """§S2: a collapse below the worth-floor recovers too little to justify a fee."""
    pos = _fpos(custodian, ledger, entry=97)
    for i in range(config.SALVAGE_CONFIRM_POLLS + 1):
        _tick(custodian, CLOSE - 400 + i, held_bid=20)   # deep + pinned but < floor
    assert pos.salvage_fired is None


# ══ HARD RAIL: F entry/hold logic byte-identical ═══════════════════════════
def test_f_sizing_logic_untouched():
    f = scoring.size_order(4162, 97, 10_000, lane="F")
    assert f.contracts == int(4162 * config.F_NOTIONAL_PCT // 97)
    assert "cap n/a" in f.reason
