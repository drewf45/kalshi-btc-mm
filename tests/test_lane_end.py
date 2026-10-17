"""WO-END-1 — Lane B (the end-game pile-on), replayed.

Mirrors tests/test_rot_desk.py's harness: the desk (lane_end.EndGameDesk) drives
the REAL gateway and ledger (shadow), with books and the listing this file
controls and fills booked through the gateway exactly as the fills/shadow sweep
would. The desk never books a fill itself.

The five load-bearing proofs the WO names:
  (a) END_ENABLED=False => step() is a HARD no-op (byte-identical to no Lane B)
  (b) the gap-vs-RUNNING-AVERAGE filter blocks a spot-looks-safe-but-average-
      near-the-line entry (the #1 trap)
  (c) a clear wide-gap favorite in the endgame window passes AND sizes flat
      1/20-Kelly capped at 1% of bankroll
  (d) the mechanics stop fires when the gap erodes (p_flip rises past the stop)
  (e) never enters outside the endgame window or outside the 95-97c band
"""

from decimal import Decimal

import pytest

from relay_engine import config, failures, lane_end as le
from relay_engine.book import OrderBook
from relay_engine.series_discovery import SeriesInfo

T = 1_800_000_000.0
STRIKE = 100_000.0


def _info(series, und, cadence="15M"):
    return SeriesInfo(series=series, cadence=cadence, cadence_source="test",
                      underlying=und, settle_sources=["CF Benchmarks"], tick="1c",
                      fee_type="quadratic", fee_multiplier="1", fee_changes=0,
                      open_markets=1)


SERIES = {
    "KXBTC15M": _info("KXBTC15M", "BTC"),
    "KXETH15M": _info("KXETH15M", "ETH"),
    "KXXRP15M": _info("KXXRP15M", "XRP"),
}


@pytest.fixture(autouse=True)
def _end_on(monkeypatch):
    # most tests exercise the lane: turn it ON. (a) flips it back OFF explicitly.
    monkeypatch.setattr(config, "DESK_MODE", "ROTATION")
    monkeypatch.setattr(config, "SERIES_INFO", dict(SERIES))
    monkeypatch.setattr(config, "END_ENABLED", True)
    monkeypatch.setattr(config, "END_SERIES",
                        frozenset({"KXBTC15M", "KXETH15M", "KXXRP15M"}))


def mk(series, n):
    return f"{series}-26OCT14{n:04d}-00"


class Env:
    """The END desk on a real gateway; books, listing, spot and the running
    average are ours to control."""

    def __init__(self, gateway, ledger):
        self.gateway, self.ledger = gateway, ledger
        self.sent = []
        self.spot = {}            # underlying -> index value (points)
        self.avg = {}             # underlying -> (running_avg, samples_in, approx)
        self.strike = {}          # market -> strike
        self.venue = {}
        self.desk = le.EndGameDesk(
            gateway, ledger, alert=self.sent.append,
            spot_fn=lambda u: self.spot.get(u),
            running_avg_fn=lambda u: self.avg.get(u),
            strike_fn=lambda m: self.strike.get(m),
            venue_held_fn=lambda m: self.venue.get(m))
        self.books = {}
        self.meta = {}

    def market(self, ticker, yes_bid, yes_ask, depth=50, close_in=60.0,
               strike=STRIKE, now=T):
        self.meta[ticker] = {"close_ts": now + close_in, "boundary_lo": strike}
        self.strike[ticker] = strike
        self.book(ticker, yes_bid, yes_ask, depth)

    def book(self, ticker, yes_bid, yes_ask, depth=50, ts=T):
        b = OrderBook(market=ticker)
        yb = {yes_bid: depth} if yes_bid is not None else {}
        nb = {100 - yes_ask: depth} if yes_ask is not None else {}
        b.apply_snapshot(yb, nb, ts=ts)
        self.books[ticker] = b

    def step(self, now=T):
        self.desk.step(now, dict(self.books), self.meta)

    def trip(self, ticker):
        return self.desk.trips.get(ticker)

    def fill(self, oid, count=None):
        self.gateway.on_fill(oid, count=count)

    def resting(self, ticker, purpose=None):
        return {oid: o for oid, o in self.gateway.resting.items()
                if o.market == ticker and (purpose is None or o.purpose == purpose)}


@pytest.fixture
def env(gateway, ledger, monkeypatch):
    monkeypatch.setattr(gateway.governor, "take", lambda now=None: True)
    failures._warn_last.clear()
    sent = []
    failures.configure(ledger, alert_fn=sent.append, run_mode="TEST", boot_id=1)
    e = Env(gateway, ledger)
    e.pages = sent
    yield e
    failures._ledger = None
    failures._alert_fn = None


# a clear, decided YES favorite deep in the endgame: running avg well above the
# strike, late in the sample, cheap enough gap to make p_flip tiny
def _wide_gap_btc(env):
    env.spot["BTC"] = STRIKE
    env.avg["BTC"] = (STRIKE + 60.0, 45, True)   # 60 pts above, 45/60 samples in


# ════════════════════ (a) disabled => no-op ════════════════════

def test_disabled_is_a_hard_noop(env, monkeypatch):
    monkeypatch.setattr(config, "END_ENABLED", False)
    _wide_gap_btc(env)
    m = mk("KXBTC15M", 1)
    env.market(m, 96, 98, close_in=45)           # a textbook entry otherwise
    env.step(T)
    assert env.desk.trips == {}
    assert env.desk.verdicts == {}               # touched NOTHING
    assert env.desk.events == []
    assert not env.resting(m)


def test_phi_is_the_standard_normal_cdf():
    assert le.phi(0.0) == pytest.approx(0.5, abs=1e-9)
    assert le.phi(1.96) == pytest.approx(0.975, abs=1e-3)
    assert le.phi(-1.96) == pytest.approx(0.025, abs=1e-3)


# ════════════════════ (c) wide-gap favorite enters & sizes ════════

def test_wide_gap_favorite_enters_and_sizes_count_first(env):
    """Drew 2026-10-17: sizing is COUNT-FIRST, scaled to the bankroll — NOT the
    old 1%-of-book cap (which produced zero bets on a $50 book, since one 96c
    lot costs more than 1% of $50). A single qualifier gets the full available
    cash slice, clamped only by the END per-market wall so it never goes all-in,
    and buys as many lots as that affords."""
    _wide_gap_btc(env)
    m = mk("KXBTC15M", 1)
    env.market(m, 96, 98, close_in=45)
    env.step(T)
    t = env.trip(m)
    assert t is not None, env.desk.verdicts.get(m)
    o = env.gateway.order_index[t.entry_oid]
    assert (o.lane, o.purpose, o.action, o.side, o.price_cents) == ("END", "ENTRY", "buy", "yes", 96)
    assert o.band == (config.END_PRICE_FLOOR, config.END_PRICE_CAP)
    assert not o.crossfire and not o.ioc and not o.reduce_only
    # single qualifier: slice = min(spendable, END per-market wall). On the $100
    # book the END wall (AT_RISK_PCT["END"]=0.50) binds at 5000c, so
    #   size = floor(5000 / 96 / 0.01) * 0.01 = 52.08 contracts  (NOT 1.04).
    wall = config.at_risk_cap_cents("END", env.desk.spendable_cents())  # 5000c
    assert wall == 5000
    assert t.size == Decimal("52.08")
    # never over the per-market wall (never all-in on one market)
    assert float(t.size) * t.cost <= wall + 0.5
    assert t.approx_avg is True                  # the running avg was APPROX
    assert "running60s_avg" in o.why and "APPROX" in o.why and "p_flip" in o.why


def test_single_market_never_goes_all_in(env):
    # one market never takes the whole bankroll: at-risk stays within the END
    # per-market wall even when it is the only qualifier (spread, never all-in).
    _wide_gap_btc(env)
    m = mk("KXBTC15M", 1)
    env.market(m, 95, 97, close_in=30)
    env.step(T)
    t = env.trip(m)
    assert t is not None
    cost_c = float(t.size) * t.cost
    wall = config.at_risk_cap_cents("END", env.desk.spendable_cents())
    assert cost_c <= wall + 0.5
    assert cost_c < env.desk.spendable_cents()   # strictly under all-in


def _wide_gap(env, und, strike):
    env.spot[und] = strike
    env.avg[und] = (strike + strike * 0.0006, 45, True)   # ~0.06% above the line


def test_equal_split_spreads_across_the_board(env, ledger):
    """Drew's own example (2026-10-17): '$10, BTC ETH ... use $X across the
    board ... win a couple cents per ... not betting it all on one market.' On a
    $10 book with several qualifiers the available cash equal-splits — each market
    gets a slice and a few contracts, none goes all-in."""
    ledger.baseline(1_000, confirmed_by="boot")          # a $10 bankroll
    ledger.snapshot_caps_at_boot()
    und = {"KXBTC15M": ("BTC", 100_000.0), "KXETH15M": ("ETH", 4_000.0),
           "KXXRP15M": ("XRP", 3.0)}
    markets = []
    for i, (series, (u, strike)) in enumerate(und.items(), start=1):
        _wide_gap(env, u, strike)
        m = mk(series, i)
        env.market(m, 96, 98, close_in=45, strike=strike)
        markets.append(m)
    env.step(T)
    trips = [m for m in markets if env.trip(m) is not None]
    # every qualifier got a bet — the 1%-cap no longer zeroes anyone out
    assert len(trips) == len(markets), {m: env.desk.verdicts.get(m) for m in markets}
    # spread, not all-in: each market's at-risk is a fraction of the bankroll
    book = env.desk.spendable_cents()
    for m in trips:
        t = env.trip(m)
        assert float(t.size) >= 1                         # at least one contract
        assert float(t.size) * t.cost < book              # never all-in


def test_one_contract_floor_when_cash_can_afford_a_lot(env, ledger):
    """The floor Drew named: 'if you are able to do one contract for market then
    you should be able to bet that.' A bankroll whose equal-split slice is thinner
    than a single lot STILL places one lot, as long as spendable cash covers it —
    the old 1%-of-book cap would have skipped it (SKIP_SIZE)."""
    ledger.baseline(300, confirmed_by="boot")             # $3 — thin
    ledger.snapshot_caps_at_boot()
    _wide_gap_btc(env)
    m = mk("KXBTC15M", 1)
    env.market(m, 96, 98, close_in=45)
    env.step(T)
    t = env.trip(m)
    assert t is not None, env.desk.verdicts.get(m)
    assert float(t.size) >= 1                             # the 1-contract floor

    # the floor is cash-honest: a zero slice still takes one lot when spendable
    # cash covers a single lot, but SKIP_NO_CASH when it truly cannot. A lot is
    # one ROT_LOT_STEP; at 96c a 0.01 step costs 1c (ceil of 0.96c).
    step = config.ROT_LOT_STEP
    size, why = env.desk._size(96, budget_cents=0, spendable_cents=1)
    assert size == step and why == ""                     # floor fires: 1c ≥ 1c lot
    size, why = env.desk._size(96, budget_cents=0, spendable_cents=0)
    assert size is None and why == le.SKIP_NO_CASH        # cannot afford a lot


def test_kelly_fraction_is_flat_and_capped():
    # kelly_fraction is retained for reference/telemetry (no longer gates size)
    assert le.kelly_fraction(0.001, 20, 0.01) == 0.01
    assert le.kelly_fraction(0.02, 20, 0.01) == 0.01
    # no edge after the 1.5c haircut when the win prob is poor
    assert le.kelly_fraction(0.5, 20, 0.01) == 0.0
    assert le.kelly_fraction(0.0, 20, 0.01) == 0.01


# ════════════════════ (b) the gap-vs-running-average trap ════════

def test_gap_vs_running_average_blocks_a_spot_safe_but_average_near_line(env):
    """THE #1 TRAP: spot is comfortably above the strike (the quote looks safe,
    95c favorite) BUT the running 60s AVERAGE sits one point off the line with
    few samples left — a late reversal can still drag the average across. The
    gap-vs-AVERAGE filter must REFUSE, even though spot-vs-strike looks fine."""
    m = mk("KXBTC15M", 1)
    # spot far above the line — the naive (wrong) read says "safe favorite"
    env.spot["BTC"] = STRIKE + 300.0
    # but the RUNNING AVERAGE is 1 point above the line with only 5 samples left
    env.avg["BTC"] = (STRIKE + 1.0, 55, True)
    env.market(m, 96, 98, close_in=20)
    env.step(T)
    assert m not in env.desk.trips
    assert env.desk.verdicts[m] == le.SKIP_PFLIP
    assert not env.resting(m)


def test_no_running_average_feed_is_a_skip_not_a_guess(env):
    m = mk("KXBTC15M", 1)
    env.spot["BTC"] = STRIKE + 300.0           # spot looks great
    # no running-average feed at all -> cannot judge the gap -> refuse
    env.avg["BTC"] = None
    env.market(m, 96, 98, close_in=20)
    env.step(T)
    assert m not in env.desk.trips
    assert env.desk.verdicts[m] == le.SKIP_NO_AVG


def test_average_favors_the_other_side_is_no_favorite(env):
    """The quote makes YES the favorite (96c), but the running average is BELOW
    the strike — the average does not favor YES. Never buy a side the AVERAGE
    does not favor."""
    m = mk("KXBTC15M", 1)
    env.spot["BTC"] = STRIKE
    env.avg["BTC"] = (STRIKE - 40.0, 45, True)   # average favors NO
    env.market(m, 96, 98, close_in=30)
    env.step(T)
    assert m not in env.desk.trips
    assert env.desk.verdicts[m] in (le.SKIP_NO_FAVORITE, le.SKIP_PFLIP)


# ════════════════════ (e) window and price-band gating ════════════

def test_never_enters_outside_the_endgame_window(env):
    _wide_gap_btc(env)
    m = mk("KXBTC15M", 1)
    env.market(m, 96, 98, close_in=config.END_WINDOW_S + 30)   # too early
    env.step(T)
    assert m not in env.desk.trips and env.desk.verdicts[m] == le.SKIP_WINDOW
    # inside the window now -> enters
    env.meta[m]["close_ts"] = T + 40
    env.step(T)
    assert m in env.desk.trips


def test_never_enters_below_the_price_floor(env):
    _wide_gap_btc(env)
    m = mk("KXBTC15M", 1)
    env.market(m, 90, 92, close_in=30)           # 90c < 95c floor
    env.step(T)
    assert m not in env.desk.trips and env.desk.verdicts[m] == le.SKIP_PRICE


def test_never_enters_above_the_price_cap(env):
    _wide_gap_btc(env)
    m = mk("KXBTC15M", 1)
    env.market(m, 98, 100, close_in=30)          # 98c > 97c cap (fee trap)
    env.step(T)
    assert m not in env.desk.trips and env.desk.verdicts[m] == le.SKIP_PRICE


def test_series_not_in_end_series_is_skipped(env, monkeypatch):
    monkeypatch.setattr(config, "END_SERIES", frozenset({"KXBTC15M"}))
    _wide_gap_btc(env)
    env.spot["ETH"] = 4000.0
    env.avg["ETH"] = (4060.0, 45, True)
    m = mk("KXETH15M", 1)
    env.market(m, 96, 98, close_in=30, strike=4000.0)
    env.step(T)
    assert m not in env.desk.trips and env.desk.verdicts[m] == le.SKIP_NO_SERIES


# ════════════════════ (d) the mechanics stop ════════════════════

def test_mechanics_stop_fires_when_the_gap_erodes(env):
    _wide_gap_btc(env)
    m = mk("KXBTC15M", 1)
    env.market(m, 96, 98, close_in=60)
    env.step(T)
    t = env.trip(m)
    assert t is not None
    env.fill(t.entry_oid)                        # the favorite fills
    env.step(T + 1)
    assert t.state == "HELD" and env.desk.held(t) > 0
    # the gap ERODES: the running average collapses onto the line with few
    # samples left -> p_flip rises past the stop -> EXIT
    env.avg["BTC"] = (STRIKE + 0.5, 56, True)
    env.book(m, 70, 72)                          # the favorite is decaying
    env.step(T + 2)
    assert t.pending == le.STOP
    # the sell was placed (maker or taker), sized to the held count, never over
    sell_oids = [oid for oid, o in env.gateway.order_index.items()
                 if o.market == m and o.action == "sell"]
    assert len(sell_oids) == 1
    assert env.gateway.order_index[sell_oids[0]].count == t.filled
    if sell_oids[0] in env.gateway.resting:      # a resting maker exit — fill it
        env.fill(sell_oids[0])
    env.step(T + 3)
    assert m not in env.desk.trips
    assert env.desk.closed[-1].closed_path == le.STOP


def test_stop_never_oversells(env):
    _wide_gap_btc(env)
    m = mk("KXBTC15M", 1)
    env.market(m, 96, 98, close_in=60)
    env.step(T)
    t = env.trip(m)
    env.fill(t.entry_oid)
    env.venue[m] = Decimal(1)                    # the venue says we hold only 1
    env.step(T + 1)
    env.avg["BTC"] = (STRIKE + 0.5, 56, True)    # gap erodes
    env.book(m, 70, 72)
    env.step(T + 2)
    with pytest.raises(Exception):
        env.desk._assert_exit_size(t, Decimal(5))   # 5 > 1 held
    assert any("END_EXIT_OVERSIZE" in p for p in env.pages)


# ════════════════════ settlement / one-entry-per-market ════════════

def test_favorite_held_to_the_bell_settles(env):
    _wide_gap_btc(env)
    m = mk("KXBTC15M", 1)
    env.market(m, 96, 98, close_in=40)
    env.step(T)
    t = env.trip(m)
    env.fill(t.entry_oid)
    env.step(T + 1)
    assert t.state == "HELD"
    env.step(T + 40)                             # the bell
    assert env.desk.closed[-1].closed_path == le.SETTLE
    assert t.key not in env.gateway.positions    # the position settled at the venue


def test_unfilled_entry_is_nothing_owed(env):
    _wide_gap_btc(env)
    m = mk("KXBTC15M", 1)
    env.market(m, 96, 98, close_in=40)
    env.step(T)
    t = env.trip(m)
    assert t is not None
    env.step(T + 40)                             # window closes, never filled
    assert m not in env.desk.trips
    assert env.desk.closed[-1].closed_path == le.NO_FILL


def test_one_entry_per_market_ever(env):
    _wide_gap_btc(env)
    m = mk("KXBTC15M", 1)
    env.market(m, 96, 98, close_in=40)
    env.step(T)
    t = env.trip(m)
    env.fill(t.entry_oid)
    env.step(T + 40)                             # settle
    assert m not in env.desk.trips
    env.market(m, 96, 98, close_in=40, now=T + 41)
    env.step(T + 42)
    assert m not in env.desk.trips
    assert env.desk.verdicts[m] == le.SKIP_ENTERED_BEFORE
    entries = [o for o in env.gateway.order_index.values()
               if o.market == m and o.purpose == "ENTRY"]
    assert len(entries) == 1


def test_held_elsewhere_is_skipped(env):
    _wide_gap_btc(env)
    m = mk("KXBTC15M", 1)
    env.gateway.positions[(m.rsplit("-", 1)[0], m, "ROT")] = 2
    env.market(m, 96, 98, close_in=30)
    env.step(T)
    assert env.desk.verdicts[m] == le.SKIP_HELD


# ════════════════════ shadow safety ════════════════════

def test_shadow_only_when_no_live_feed(env, monkeypatch):
    # no working spot feed for the underlying -> the trip is rehearsed, every
    # order shadow_only (a shadow position never meets a live sell)
    env.desk.live_fn = lambda u: False
    _wide_gap_btc(env)
    m = mk("KXBTC15M", 1)
    env.market(m, 96, 98, close_in=30)
    env.step(T)
    t = env.trip(m)
    assert t is not None and t.shadow_only
    o = env.gateway.order_index[t.entry_oid]
    assert o.shadow_only is True
    assert "SHADOW-ONLY" in o.why
