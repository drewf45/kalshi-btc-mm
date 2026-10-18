"""WO-END-1 (rewrite 2026-10-18) — Lane END (the end-game pile-on), replayed.

Mirrors tests/test_rot_desk.py's harness: the desk (lane_end.EndGameDesk) drives
the REAL gateway and ledger (shadow), with books and the listing this file
controls and fills booked through the gateway exactly as the fills/shadow sweep
would. The desk never books a fill itself.

The load-bearing proofs (COST IS THE SIGNAL — no probability model):
  (a) END_ENABLED=False => step() is a HARD no-op (byte-identical to no Lane END)
  (b) the favorite is the RICHEST side of the book, in the 95-99c solved band
  (c) the CONFIRM LADDER is the solved test — 97c needs 3 consecutive confirms
      across cycles (fewer -> SKIP_UNCONFIRMED); the counter RESETS on side-flip,
      below-floor, out-of-band, or a one-sided (empty-counterparty) book
  (d) entry only inside the final window (10s < secs_left <= END_WINDOW_S)
  (e) multi-room equal-split across solved markets with a 1-contract floor
  (f) a filled favorite RIDES TO SETTLEMENT (no p_flip mechanics stop)
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


# the deepest confirm tier governs the final window: floor 97c, 3 confirms.
CONFIRMS = config.END_CONFIRM_LADDER[-1]["confirms"]


class Env:
    """The END desk on a real gateway; books and the listing are ours to control.
    spot/avg/strike feeds are wired for constructor stability but UNUSED by the
    cost-is-signal decision."""

    def __init__(self, gateway, ledger):
        self.gateway, self.ledger = gateway, ledger
        self.sent = []
        self.spot = {}            # underlying -> index value (wired, unused)
        self.avg = {}             # underlying -> (running_avg, samples_in, approx) (wired, unused)
        self.strike = {}          # market -> strike (tape only)
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

    def confirm_and_step(self, now=T, n=CONFIRMS):
        """Step the cyclic confirm ladder to completion: step() is ONE read, so
        drive it `n` times at the same clock (the favorite holds its pin) — the
        entry fires on the read that reaches the tier's required confirms."""
        for _ in range(n):
            self.step(now)

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


# a clear, decided YES favorite deep in the endgame window: a 97c YES pin (the
# deepest tier's floor) that holds across the confirm reads.
def _solved_btc(env, m, close_in=45):
    env.market(m, 97, 99, close_in=close_in)      # favorite yes@97, counterparty no_bid=1


# ════════════════════ (a) disabled => no-op ════════════════════

def test_disabled_is_a_hard_noop(env, monkeypatch):
    monkeypatch.setattr(config, "END_ENABLED", False)
    m = mk("KXBTC15M", 1)
    env.market(m, 97, 99, close_in=45)           # a textbook entry otherwise
    env.confirm_and_step(T)
    assert env.desk.trips == {}
    assert env.desk.verdicts == {}               # touched NOTHING
    assert env.desk.events == []
    assert env.desk.confirm_state == {}
    assert not env.resting(m)


# ════════════════════ (b) the favorite is the richest side ════════

def test_favorite_is_the_richest_side_of_the_book(env):
    m = mk("KXBTC15M", 1)
    # YES bid 97, YES ask 99 => NO bid = 1. YES maker cost 97 > NO maker cost 1.
    env.book(m, 97, 99)
    b = env.books[m]
    side, cost = env.desk.favorite(b)
    assert (side, cost) == ("yes", 97)
    # mirror: a NO-rich book (YES bid 2, YES ask 4 => NO bid 96) -> NO favorite 96
    env.book(m, 2, 4)
    side, cost = env.desk.favorite(env.books[m])
    assert (side, cost) == ("no", 96)


# ════════════════════ (c) the confirm ladder (the solved test) ════

def test_confirm_ladder_97c_needs_three_confirms(env):
    """Drew 2026-10-18: COST IS THE SIGNAL via the confirm ladder. A 97c favorite
    in the final window qualifies only after 3 consecutive confirms; fewer reads
    leave it SKIP_UNCONFIRMED and NOTHING is placed."""
    m = mk("KXBTC15M", 1)
    env.market(m, 97, 99, close_in=45)
    # fewer than the required confirms -> unconfirmed, no entry
    for i in range(1, CONFIRMS):
        env.step(T)
        assert m not in env.desk.trips
        assert env.desk.verdicts[m] == le.SKIP_UNCONFIRMED
        assert env.desk.confirm_state[m]["count"] == i
    # the read that reaches the required count -> SOLVED, enters
    env.step(T)
    t = env.trip(m)
    assert t is not None, env.desk.verdicts.get(m)
    assert t.confirms == CONFIRMS
    o = env.gateway.order_index[t.entry_oid]
    assert (o.lane, o.purpose, o.action, o.side, o.price_cents) == ("END", "ENTRY", "buy", "yes", 97)


def test_confirm_counter_resets_on_side_flip(env):
    m = mk("KXBTC15M", 1)
    env.market(m, 97, 99, close_in=45)           # YES favorite
    env.step(T)
    env.step(T)
    assert env.desk.confirm_state[m]["count"] == 2
    # the favorite FLIPS to NO at the floor (YES bid 2, ask 3 => NO bid 97) —
    # same-floor, so the only reset cause is the SIDE change; the count restarts.
    env.book(m, 2, 3)
    env.step(T)
    assert env.desk.confirm_state[m]["side"] == "no"
    assert env.desk.confirm_state[m]["count"] == 1


def test_confirm_counter_resets_below_floor(env):
    m = mk("KXBTC15M", 1)
    env.market(m, 97, 99, close_in=45)
    env.step(T)
    env.step(T)
    assert env.desk.confirm_state[m]["count"] == 2
    # favorite slips to 96c (below the deepest tier's 97c floor) — reset, skip
    env.book(m, 96, 98)
    env.step(T)
    assert m not in env.desk.confirm_state
    assert env.desk.verdicts[m] == le.SKIP_UNCONFIRMED
    assert m not in env.desk.trips


def test_out_of_band_is_a_price_skip(env):
    m = mk("KXBTC15M", 1)
    # 94c favorite: below the 95c band floor -> SKIP_PRICE, never confirms
    env.market(m, 94, 96, close_in=45)
    env.confirm_and_step(T)
    assert m not in env.desk.trips
    assert env.desk.verdicts[m] == le.SKIP_PRICE
    assert m not in env.desk.confirm_state


def test_above_band_cap_is_a_price_skip(env):
    m = mk("KXBTC15M", 1)
    # 100c favorite: above the 99c band cap -> SKIP_PRICE (fee/break-even trap)
    env.market(m, 100, 102, close_in=45)
    env.confirm_and_step(T)
    assert m not in env.desk.trips
    assert env.desk.verdicts[m] == le.SKIP_PRICE


def test_99c_is_in_band_now(env):
    # the cap moved 97->99: a 99c favorite is IN band (the top of the solved band)
    m = mk("KXBTC15M", 1)
    env.market(m, 99, 100, close_in=45)          # favorite yes@99, no_bid=0? -> no, ask100 => no_bid 0
    # YES bid 99, YES ask 100 => NO bid 0 (present, qty 50) -> counterparty ok
    env.confirm_and_step(T)
    t = env.trip(m)
    assert t is not None, env.desk.verdicts.get(m)
    assert t.cost == 99


def test_one_sided_book_is_no_counterparty(env):
    """R2 (the certified watch ladder): a solved favorite shows a two-sided touch.
    A one-sided book (no counterparty bid) is SKIP_NO_COUNTERPARTY and resets."""
    m = mk("KXBTC15M", 1)
    env.market(m, 97, 99, close_in=45)
    env.step(T)
    assert env.desk.confirm_state[m]["count"] == 1
    # the counterparty vanishes: no YES ask => no NO bid at all (one-sided)
    env.book(m, 97, None)
    env.step(T)
    assert env.desk.verdicts[m] == le.SKIP_NO_COUNTERPARTY
    assert m not in env.desk.confirm_state
    assert m not in env.desk.trips


# ════════════════════ (d) window gating ════════════════════

def test_never_enters_outside_the_entry_window(env):
    m = mk("KXBTC15M", 1)
    # too early: beyond the watch window entirely
    env.market(m, 97, 99, close_in=config.END_WATCH_WINDOW_S + 100)
    env.confirm_and_step(T)
    assert m not in env.desk.trips and env.desk.verdicts[m] == le.SKIP_WINDOW
    # inside the entry window now -> confirms then enters
    env.meta[m]["close_ts"] = T + 40
    env.confirm_and_step(T)
    assert m in env.desk.trips


def test_confirmed_but_past_the_cancel_buffer_is_not_entered(env):
    """Confirmed, but inside the final 10s cancel buffer: no NEW entry is placed
    (the certified engine cancels resting at T-10 and never opens inside it)."""
    m = mk("KXBTC15M", 1)
    env.market(m, 97, 99, close_in=5)            # 5s left < 10s cancel buffer
    env.confirm_and_step(T)
    assert m not in env.desk.trips
    assert env.desk.verdicts[m] == le.SKIP_WINDOW


def test_series_not_in_end_series_is_skipped(env, monkeypatch):
    monkeypatch.setattr(config, "END_SERIES", frozenset({"KXBTC15M"}))
    m = mk("KXETH15M", 1)
    env.market(m, 97, 99, close_in=30, strike=4000.0)
    env.confirm_and_step(T)
    assert m not in env.desk.trips and env.desk.verdicts[m] == le.SKIP_NO_SERIES


# ════════════════════ (e) sizing: count-first, spread across the board ════

def test_wide_gap_favorite_enters_and_sizes_count_first(env):
    """Drew 2026-10-17: sizing is COUNT-FIRST, scaled to the bankroll — NOT the
    old 1%-of-book cap. A single qualifier gets the full available cash slice,
    clamped only by the END per-market wall so it never goes all-in."""
    m = mk("KXBTC15M", 1)
    env.market(m, 97, 99, close_in=45)
    env.confirm_and_step(T)
    t = env.trip(m)
    assert t is not None, env.desk.verdicts.get(m)
    o = env.gateway.order_index[t.entry_oid]
    assert (o.lane, o.purpose, o.action, o.side, o.price_cents) == ("END", "ENTRY", "buy", "yes", 97)
    assert o.band == (config.END_PRICE_FLOOR, config.END_PRICE_CAP)
    assert not o.crossfire and not o.ioc and not o.reduce_only
    # single qualifier: slice = min(spendable, END per-market wall). On the $100
    # book the END wall (AT_RISK_PCT["END"]=0.50) binds at 5000c, so
    #   size = floor(5000 / 97 / 0.01) * 0.01 = 51.54 contracts.
    wall = config.at_risk_cap_cents("END", env.desk.spendable_cents())  # 5000c
    assert wall == 5000
    assert t.size == Decimal("51.54")
    # never over the per-market wall (never all-in on one market)
    assert float(t.size) * t.cost <= wall + 0.5
    assert "COST-IS-SIGNAL" in o.why and "confirms" in o.why


def test_single_market_never_goes_all_in(env):
    m = mk("KXBTC15M", 1)
    env.market(m, 97, 99, close_in=30)
    env.confirm_and_step(T)
    t = env.trip(m)
    assert t is not None
    cost_c = float(t.size) * t.cost
    wall = config.at_risk_cap_cents("END", env.desk.spendable_cents())
    assert cost_c <= wall + 0.5
    assert cost_c < env.desk.spendable_cents()   # strictly under all-in


def test_equal_split_spreads_across_the_board(env, ledger):
    """Drew's own example (2026-10-17): '$10, BTC ETH ... use $X across the
    board ... win a couple cents per ... not betting it all on one market.' On a
    $10 book with several solved markets the available cash equal-splits — each
    market gets a slice and a few contracts, none goes all-in."""
    ledger.baseline(1_000, confirmed_by="boot")          # a $10 bankroll
    ledger.snapshot_caps_at_boot()
    und = {"KXBTC15M": ("BTC", 100_000.0), "KXETH15M": ("ETH", 4_000.0),
           "KXXRP15M": ("XRP", 3.0)}
    markets = []
    for i, (series, (u, strike)) in enumerate(und.items(), start=1):
        m = mk(series, i)
        env.market(m, 97, 99, close_in=45, strike=strike)
        markets.append(m)
    env.confirm_and_step(T)
    trips = [m for m in markets if env.trip(m) is not None]
    # every solved market got a bet — the 1%-cap no longer zeroes anyone out
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
    than a single lot STILL places one lot, as long as spendable cash covers it."""
    ledger.baseline(300, confirmed_by="boot")             # $3 — thin
    ledger.snapshot_caps_at_boot()
    m = mk("KXBTC15M", 1)
    env.market(m, 97, 99, close_in=45)
    env.confirm_and_step(T)
    t = env.trip(m)
    assert t is not None, env.desk.verdicts.get(m)
    assert float(t.size) >= 1                             # the 1-contract floor

    # the floor is cash-honest: a zero slice still takes one lot when spendable
    # cash covers a single lot, but SKIP_NO_CASH when it truly cannot. A lot is
    # one ROT_LOT_STEP; at 97c a 0.01 step costs 1c (ceil of 0.97c).
    step = config.ROT_LOT_STEP
    size, why = env.desk._size(97, budget_cents=0, spendable_cents=1)
    assert size == step and why == ""                     # floor fires: 1c ≥ 1c lot
    size, why = env.desk._size(97, budget_cents=0, spendable_cents=0)
    assert size is None and why == le.SKIP_NO_CASH        # cannot afford a lot


# ════════════════════ (f) ride to settlement (no p_flip stop) ════

def test_favorite_held_to_the_bell_settles(env):
    m = mk("KXBTC15M", 1)
    env.market(m, 97, 99, close_in=40)
    env.confirm_and_step(T)
    t = env.trip(m)
    env.fill(t.entry_oid)
    env.step(T + 1)
    assert t.state == "HELD"
    env.step(T + 40)                             # the bell
    assert env.desk.closed[-1].closed_path == le.SETTLE
    assert t.key not in env.gateway.positions    # the position settled at the venue


def test_filled_favorite_rides_even_when_book_decays(env):
    """COST IS THE SIGNAL, ride to settlement: there is NO per-trade exit. Even
    if the book visibly decays after the fill, the held favorite is NOT cut — it
    rides to the bell (the certified engine takes no active exit)."""
    m = mk("KXBTC15M", 1)
    env.market(m, 97, 99, close_in=60)
    env.confirm_and_step(T)
    t = env.trip(m)
    env.fill(t.entry_oid)
    env.step(T + 1)
    assert t.state == "HELD" and env.desk.held(t) > 0
    env.book(m, 70, 72)                          # the favorite is decaying
    env.step(T + 2)
    # no sell was placed — we ride to settlement
    sells = [o for o in env.gateway.order_index.values()
             if o.market == m and o.action == "sell"]
    assert sells == []
    assert t.state == "HELD"
    env.step(T + 60)                             # the bell
    assert env.desk.closed[-1].closed_path == le.SETTLE


def test_unfilled_entry_is_nothing_owed(env):
    m = mk("KXBTC15M", 1)
    env.market(m, 97, 99, close_in=40)
    env.confirm_and_step(T)
    t = env.trip(m)
    assert t is not None
    env.step(T + 40)                             # window closes, never filled
    assert m not in env.desk.trips
    assert env.desk.closed[-1].closed_path == le.NO_FILL


def test_one_entry_per_market_ever(env):
    m = mk("KXBTC15M", 1)
    env.market(m, 97, 99, close_in=40)
    env.confirm_and_step(T)
    t = env.trip(m)
    env.fill(t.entry_oid)
    env.step(T + 40)                             # settle
    assert m not in env.desk.trips
    env.market(m, 97, 99, close_in=40, now=T + 41)
    env.confirm_and_step(T + 42)
    assert m not in env.desk.trips
    assert env.desk.verdicts[m] == le.SKIP_ENTERED_BEFORE
    entries = [o for o in env.gateway.order_index.values()
               if o.market == m and o.purpose == "ENTRY"]
    assert len(entries) == 1


def test_held_elsewhere_is_skipped(env):
    m = mk("KXBTC15M", 1)
    env.gateway.positions[(m.rsplit("-", 1)[0], m, "ROT")] = 2
    env.market(m, 97, 99, close_in=30)
    env.confirm_and_step(T)
    assert env.desk.verdicts[m] == le.SKIP_HELD


# ════════════════════ shadow safety ════════════════════

def test_shadow_only_when_no_live_feed(env, monkeypatch):
    # no working spot feed for the underlying -> the trip is rehearsed, every
    # order shadow_only (a shadow position never meets a live sell)
    env.desk.live_fn = lambda u: False
    m = mk("KXBTC15M", 1)
    env.market(m, 97, 99, close_in=30)
    env.confirm_and_step(T)
    t = env.trip(m)
    assert t is not None and t.shadow_only
    o = env.gateway.order_index[t.entry_oid]
    assert o.shadow_only is True
    assert "SHADOW-ONLY" in o.why
