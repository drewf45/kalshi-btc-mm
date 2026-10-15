"""WO-ROT-1 Chunks 3, 3A, 4, 5 — the Rotation Desk, replayed.

Desk-level tests drive lane_rot.RotationDesk against the REAL gateway and
ledger (shadow), with books this file controls and fills booked through the
gateway exactly as the fills sweep / shadow sweep would. Engine-level tests
drive ShadowEngine.cycle in ROTATION end to end (pessimistic shadow fills).
"""

from decimal import Decimal

import pytest

from relay_engine import config, failures, lane_rot as lr
from relay_engine.book import OrderBook
from relay_engine.series_discovery import SeriesInfo
from relay_engine.shadow_runner import ShadowEngine

T = 1_800_000_000.0


def _info(series, und, cadence="15M"):
    return SeriesInfo(series=series, cadence=cadence, cadence_source="test",
                      underlying=und, settle_sources=["CF Benchmarks"], tick="1c",
                      fee_type="quadratic", fee_multiplier="1", fee_changes=0,
                      open_markets=1)


SERIES = {
    "KXBTC15M": _info("KXBTC15M", "BTC"),
    "KXETH15M": _info("KXETH15M", "ETH"),
    "KXXRP15M": _info("KXXRP15M", "XRP"),
    "KXSOL15M": _info("KXSOL15M", "SOL"),
    "KXBTCD": _info("KXBTCD", "BTC", "1H"),
}


@pytest.fixture(autouse=True)
def _rot(monkeypatch):
    monkeypatch.setattr(config, "DESK_MODE", "ROTATION")
    monkeypatch.setattr(config, "SERIES_INFO", dict(SERIES))


def mk(series, n):
    return f"{series}-26OCT14{n:04d}-00"


class Env:
    """The desk on a real gateway; books and the listing are ours."""

    def __init__(self, gateway, ledger):
        self.gateway, self.ledger = gateway, ledger
        self.sent = []
        self.spot = {}
        self.venue = {}
        self.desk = lr.RotationDesk(gateway, ledger, alert=self.sent.append,
                                    spot_fn=lambda u: self.spot.get(u),
                                    venue_held_fn=lambda m: self.venue.get(m))
        self.books = {}
        self.meta = {}

    def market(self, ticker, yes_bid, yes_ask, depth=50, close_in=600.0, now=T):
        self.meta[ticker] = {"close_ts": now + close_in,
                             "rec_yes_bid": yes_bid, "rec_yes_ask": yes_ask}
        self.book(ticker, yes_bid, yes_ask, depth)

    def book(self, ticker, yes_bid, yes_ask, depth=50, ts=T):
        b = OrderBook(market=ticker)
        yb = {yes_bid: depth} if yes_bid is not None else {}
        nb = {100 - yes_ask: depth} if yes_ask is not None else {}
        b.apply_snapshot(yb, nb, ts=ts)
        self.books[ticker] = b
        if ticker in self.meta:
            self.meta[ticker]["rec_yes_bid"] = yes_bid
            self.meta[ticker]["rec_yes_ask"] = yes_ask

    def step(self, now=T, budget=16):
        self.desk.step(now, dict(self.books), self.meta, budget)

    def trip(self, ticker):
        return self.desk.trips.get(ticker)

    def fill(self, oid, count=None):
        self.gateway.on_fill(oid, count=count)

    def resting(self, ticker, purpose=None):
        return {oid: o for oid, o in self.gateway.resting.items()
                if o.market == ticker and (purpose is None or o.purpose == purpose)}


@pytest.fixture
def env(gateway, ledger, monkeypatch):
    # the gateway's order-rate governor (2/s) is not under test here: a replay
    # submits a dozen orders in one instant. (It still applies in production;
    # a RATE_GOVERNED entry is a SKIP_WALL verdict, retried next cycle.)
    monkeypatch.setattr(gateway.governor, "take", lambda now=None: True)
    failures._warn_last.clear()   # the funnel's 5-min page throttle is module-global
    sent = []
    failures.configure(ledger, alert_fn=sent.append, run_mode="TEST", boot_id=1)
    e = Env(gateway, ledger)
    e.pages = sent
    yield e
    failures._ledger = None
    failures._alert_fn = None


def enter_and_fill(env, ticker, yes_bid=46, yes_ask=48, now=T, count=None):
    env.market(ticker, yes_bid, yes_ask, now=now)
    env.step(now)
    t = env.trip(ticker)
    assert t is not None, env.desk.verdicts.get(ticker)
    env.fill(t.entry_oid, count)
    return t


# ════════════════════ Chunk 3 — the selector ════════════════════

@pytest.mark.parametrize("yb,ya,secs,cad,want", [
    (46, 48, 600, "15M", ("yes", 46, "")),          # YES cost 46 in band
    (60, 62, 600, "15M", ("no", 38, "SKIP_BAND")),  # NO 38 below band
    (52, 54, 600, "15M", ("no", 46, "")),           # NO = 100-54 = 46
    (47, 53, 600, "15M", ("yes", 47, "")),          # YES 47, NO 47: tie -> YES
    (45, 52, 600, "15M", ("yes", 45, "")),          # YES 45, NO 48: cheaper
    (30, 32, 600, "15M", (None, None, "SKIP_BAND")),
    (46, 48, 200, "15M", (None, None, "SKIP_TIME")),   # < 5 min on 15M
    (46, 48, 800, "1H", (None, None, "SKIP_TIME")),    # < 15 min on 1H
    (46, 48, 1000, "1H", ("yes", 46, "")),
    (None, None, 600, "15M", (None, None, "SKIP_NO_QUOTE")),
])
def test_pick_side_table(yb, ya, secs, cad, want):
    side, cost, why = lr.pick_side(yb, ya, secs, cad)
    if want[2] == "SKIP_BAND" and want[0] == "no":
        assert (side, why) == (None, "SKIP_BAND")
        return
    assert (side, cost, why) == want


def test_no_cost_is_100_minus_the_quote_through_to_yes_terms():
    assert lr.side_costs(46, 52) == {"yes": 46, "no": 48}


def test_entry_is_a_maker_buy_at_the_touch(env):
    t = enter_and_fill(env, mk("KXBTC15M", 1))
    o = env.gateway.order_index[t.entry_oid]
    assert (o.lane, o.purpose, o.action, o.side, o.price_cents) == ("ROT", "ENTRY", "buy", "yes", 46)
    assert not o.crossfire and o.band == config.ROT_ENTRY_BAND
    assert "rank 1" in o.why and "h 0.71" in o.why


def test_skip_depth(env):
    m = mk("KXBTC15M", 1)
    env.market(m, 46, 48, depth=1)        # 1 contract bid; size ~10.8 needs 2x
    env.step()
    assert env.desk.verdicts[m] == lr.SKIP_DEPTH and m not in env.desk.trips


def test_skip_held_any_lane_including_quarantine(env):
    m = mk("KXBTC15M", 1)
    env.gateway.positions[(m.rsplit("-", 1)[0], m, "QUARANTINE")] = 2
    env.market(m, 46, 48)
    env.step()
    assert env.desk.verdicts[m] == lr.SKIP_HELD


def test_skip_band_paused(env):
    m = mk("KXBTC15M", 1)
    env.desk.paused_bands.add("40-49")
    env.market(m, 46, 48)
    env.step()
    assert env.desk.verdicts[m] == lr.SKIP_BAND_PAUSED


def test_skip_below_one_lot(env, monkeypatch):
    monkeypatch.setattr(config, "ROT_POSITION_PCT", 0.00001)   # budget < 1 lot
    m = mk("KXBTC15M", 1)
    env.market(m, 46, 48)
    env.step()
    assert env.desk.verdicts[m] == lr.SKIP_BELOW_ONE_LOT


def test_unadmitted_series_is_skipped(env):
    m = "KXWHATEVER-26OCT140000-00"
    env.market(m, 46, 48)
    env.step()
    assert env.desk.verdicts[m] == lr.SKIP_NO_SERIES


def test_engine_registry_has_rot_and_old_lanes_off(tmp_path):
    from relay_engine.lanes import LaneRot, OffLane
    e = ShadowEngine(db_path=str(tmp_path / "r.db"))
    assert isinstance(e.lanes[-1], LaneRot)
    assert all(isinstance(l, OffLane) for l in e.lanes[:5])
    d = e.lanes[-1].evaluate("KXBTC15M-X", {})
    assert d.proposal is None and d.pass_reason == "ROT_NOT_EVALUATED"


def test_at_risk_pct_has_rot_at_the_market_cap():
    assert config.AT_RISK_PCT["ROT"] == config.ROT_MARKET_CAP_PCT
    assert config.NET_RISK_CROSS_LANE_CAP == 3


# ════════════════════ Chunk 3A — ranking and the two loops ════════════════════

def test_seed_scores():
    s40 = lr.score(lr.SEED_STATS["40-49"], 45)
    s50 = lr.score(lr.SEED_STATS["50-54"], 52)
    # 40-49: ev = .71*8.4 - .29*14.9 = 1.643c/contract; 50-54: .75*7.6-.25*20.9 = 0.475
    assert lr.SEED_STATS["40-49"].ev_cents() == pytest.approx(1.643, abs=1e-3)
    assert lr.SEED_STATS["50-54"].ev_cents() == pytest.approx(0.475, abs=1e-3)
    assert s40 > s50 > 0
    assert lr.score(None, 45) == 0.0
    assert lr.band_of(39) is None and lr.band_of(55) is None


def test_ranking_and_tie_breaks(env):
    a, b, c = mk("KXBTC15M", 1), mk("KXETH15M", 2), mk("KXXRP15M", 3)
    env.market(a, 52, 54)                  # 50-54 band: lower score
    env.market(b, 45, 47)                  # 40-49 @45
    env.market(c, 45, 47, close_in=900)    # same score, more time -> ahead of b
    env.desk.outside(T, env.books, env.meta, budget_left=10)
    assert [x.market for x in env.desk.queue] == [c, b, a]
    assert [x.rank for x in env.desk.queue] == [1, 2, 3]


def test_queue_keeps_top_20_and_shortlist_respects_budget(env):
    for i in range(25):
        env.market(mk("KXBTC15M", i), 46, 48, close_in=600 + i)
    env.desk.outside(T, env.books, env.meta, budget_left=2)
    assert len(env.desk.queue) == config.ROT_QUEUE_SIZE
    assert len(env.desk.shortlist) == 2
    skipped = [m for m, v in env.desk.verdicts.items() if v == lr.SKIP_QUEUE]
    assert len(skipped) == 5
    env.desk.outside(T, env.books, env.meta, budget_left=0)
    assert env.desk.shortlist == []


def _ten_positions(env):
    """Ten held positions across five groups (2 each keeps under 25%)."""
    tickers = []
    for i, s in enumerate(["KXBTC15M", "KXETH15M", "KXXRP15M", "KXSOL15M", "KXBTCD"] * 2):
        m = mk(s, i)
        env.market(m, 46, 48, close_in=3000 if s == "KXBTCD" else 600)
        tickers.append(m)
    for _ in range(12):
        env.step()
    for t in list(env.desk.trips.values()):
        if t.filled == 0:
            env.fill(t.entry_oid)
    return tickers


def test_inside_loop_services_every_held_market_before_any_outside_read(env):
    _ten_positions(env)
    assert len(env.desk.trips) == 10
    for i in range(5):                                 # more candidates outside
        env.market(mk("KXETH15M", 50 + i), 46, 48)
    env.desk.events.clear()
    for _ in range(3):
        env.step(T + 1)
    cycles = []
    for e in env.desk.events:
        if e[0] == "cycle":
            cycles.append([])
        else:
            cycles[-1].append(e)
    assert len(cycles) == 3
    for c in cycles:
        inside = [e[1] for e in c if e[0] == "inside"]
        assert sorted(inside) == sorted(env.desk.trips)          # every held market
        first_outside = next((i for i, e in enumerate(c) if e[0] == "outside"), len(c))
        last_inside = max(i for i, e in enumerate(c) if e[0] == "inside")
        assert last_inside < first_outside                        # all before any outside


def test_engine_reads_held_books_before_the_shortlist(tmp_path):
    e = ShadowEngine(db_path=str(tmp_path / "o.db"))
    for m in ("KXBTC15M-A-00", "KXETH15M-B-00", "KXXRP15M-C-00", "KXSOL15M-D-00"):
        e.market_meta[m] = {"close_ts": T + 600}
    e.rot.shortlist = ["KXBTC15M-A-00"]
    e.gateway.positions[("KXXRP15M-C", "KXXRP15M-C-00", "ROT")] = 1
    e.gateway.positions[("KXSOL15M-D", "KXSOL15M-D-00", "ROT")] = -1
    assert e.book_poll_set() == ["KXSOL15M-D-00", "KXXRP15M-C-00", "KXBTC15M-A-00"]


def test_freed_slot_refilled_by_rank_1_on_the_same_cycle(env, monkeypatch):
    # deploy cap sized so exactly one position fits
    monkeypatch.setattr(config, "ROT_DEPLOY_CAP_PCT", 0.06)
    a = mk("KXBTC15M", 1)
    t = enter_and_fill(env, a)
    b, c = mk("KXETH15M", 2), mk("KXXRP15M", 3)
    env.market(b, 45, 47)                       # rank 1 (cheaper, same band)
    env.market(c, 48, 50)
    env.step(T + 1)
    # b is rank 1 but the deploy cap holds it out while a is open
    assert env.desk.queue[0].market == b
    assert env.desk.verdicts[b] == lr.SKIP_CAP_DEPLOY and b not in env.desk.trips
    # the cash-out fills: the inside loop frees the slot, rank 1 takes it NOW
    co = next(iter(env.resting(a, "EXIT")))
    env.fill(co)
    env.step(T + 2)
    assert a not in env.desk.trips and env.desk.closed[-1].closed_path == lr.TARGET
    assert b in env.desk.trips and c not in env.desk.trips
    rep = env.desk.replacements[-1]
    assert rep["market"] == b and rep["rank"] == 1 and a in rep["freed_by"]
    assert env.desk.first_reuse_delay_s == 0.0
    assert any("first cash reuse" in m for m in env.sent)


def test_better_candidate_never_cancels_a_resting_cashout(env, monkeypatch):
    monkeypatch.setattr(config, "ROT_DEPLOY_CAP_PCT", 0.06)
    a = mk("KXBTC15M", 1)
    t = enter_and_fill(env, a, 50, 52)          # 50-54 band: low score
    env.step(T + 1)
    cashouts = set(env.resting(a, "EXIT"))
    assert len(cashouts) == 1
    env.market(mk("KXETH15M", 2), 41, 43)       # a far better market appears
    for k in range(2, 30):
        env.step(T + k)
        assert set(env.resting(a, "EXIT")) == cashouts
    assert mk("KXETH15M", 2) not in env.desk.trips   # waits for a real exit


# ════════════════════ Chunk 4 — slots and caps ════════════════════

def test_position_size_is_5pct_fractional_at_674(env, ledger, monkeypatch):
    monkeypatch.setattr(ledger, "ensemble_base_cents", lambda: 674)
    monkeypatch.setattr(ledger, "tradeable_cents", lambda: 674)
    size, why = env.desk.size_for(47)
    # 5% of 674 = 33.7 -> 33c budget; 33/47 = 0.702 -> 0.70 contracts
    assert (size, why) == (Decimal("0.70"), "")


def test_fractional_entry_goes_through_the_gateway(env, ledger, monkeypatch):
    monkeypatch.setattr(ledger, "ensemble_base_cents", lambda: 674)
    monkeypatch.setattr(ledger, "tradeable_cents", lambda: 674)
    t = enter_and_fill(env, mk("KXBTC15M", 1), 47, 49)
    assert t.size == Decimal("0.70")
    o = env.gateway.order_index[t.entry_oid]
    assert o.count == Decimal("0.70")
    assert env.gateway.positions[t.key] == Decimal("0.70")
    env.step(T + 1)
    co = env.gateway.order_index[next(iter(env.resting(t.market, "EXIT")))]
    assert co.count == Decimal("0.70") and co.price_cents == 47 + config.ROT_TARGET_CENTS


def test_market_cap_blocks_at_its_edge(env):
    d = env.desk
    book = Decimal(d.book_cents())
    edge = book * Decimal("0.05")
    assert d.caps_check("BTC", "M", edge) == ""
    assert d.caps_check("BTC", "M", edge + Decimal("0.01")) == lr.SKIP_CAP_MARKET


def test_group_cap_blocks_at_its_edge_and_frees_on_exit(env):
    # 5 BTC positions at ~5% each = 25%: the 6th BTC is blocked by the group cap
    btc = [mk("KXBTC15M", i) for i in range(6)]
    for m in btc:
        env.market(m, 46, 48)
    for _ in range(8):
        env.step()
    held = [m for m in btc if m in env.desk.trips]
    assert len(held) == 5
    blocked = [m for m in btc if m not in env.desk.trips][0]
    assert env.desk.verdicts[blocked] == lr.SKIP_CAP_GROUP
    # another group still enters
    env.market(mk("KXETH15M", 9), 46, 48)
    env.step()
    assert mk("KXETH15M", 9) in env.desk.trips
    # one BTC exits -> the group frees
    t = env.desk.trips[held[0]]
    env.fill(t.entry_oid)
    env.step(T + 1)
    env.fill(next(iter(env.resting(held[0], "EXIT"))))
    env.step(T + 2)
    assert blocked in env.desk.trips


def test_deploy_cap_blocks_at_its_edge_and_frees_on_exit(env, monkeypatch):
    # six groups, at most two markets each: only the deploy cap can bind
    extra = {"KXDOGE15M": _info("KXDOGE15M", "DOGE"),
             "KXGOLDH": _info("KXGOLDH", "GOLD", "1H")}
    monkeypatch.setattr(config, "SERIES_INFO", dict(SERIES, **extra))
    groups = ["KXBTC15M", "KXETH15M", "KXXRP15M", "KXSOL15M", "KXDOGE15M", "KXGOLDH"]
    tickers = [mk(groups[i % 6], i) for i in range(11)]
    for m in tickers:
        env.market(m, 46, 48, close_in=3000 if m.startswith("KXGOLDH") else 600)
    for _ in range(15):
        env.step()
    held = [m for m in tickers if m in env.desk.trips]
    assert len(held) == 10                       # ~4.97% each: ten fit in 50%
    tot, _g, _m = env.desk.exposure()
    assert tot <= Decimal(env.desk.book_cents()) * Decimal("0.50")
    out = [m for m in tickers if m not in held][0]
    assert env.desk.verdicts[out] in (lr.SKIP_CAP_DEPLOY,) or not env.desk.free_slots()
    # one exits: the slot frees and the eleventh enters
    t = env.desk.trips[held[0]]
    env.fill(t.entry_oid)
    env.step(T + 1)
    env.fill(next(iter(env.resting(held[0], "EXIT"))))
    env.step(T + 2)
    assert out in env.desk.trips


# ════════════════════ Chunk 5 — entry and the bracket exit ════════════════════

def test_target_fill(env):
    m = mk("KXBTC15M", 1)
    t = enter_and_fill(env, m)
    env.step(T + 1)
    co = env.resting(m, "EXIT")
    assert len(co) == 1
    o = next(iter(co.values()))
    assert (o.action, o.side, o.price_cents, o.purpose) == ("sell", "yes", 54, "EXIT")
    assert o.count == t.size
    env.fill(next(iter(co)))
    env.step(T + 2)
    assert m not in env.desk.trips
    assert env.desk.closed[-1].closed_path == lr.TARGET
    assert any("ROT TARGET" in s for s in env.sent)


def test_entry_unfilled_after_60s_is_cancelled_nothing_owed(env):
    m = mk("KXBTC15M", 1)
    env.market(m, 46, 48)
    env.step(T)
    oid = env.trip(m).entry_oid
    env.step(T + 59)
    assert oid in env.gateway.resting
    env.step(T + 60)
    assert oid not in env.gateway.resting
    env.step(T + 61)
    assert m not in env.desk.trips and env.desk.closed[-1].closed_path == lr.NO_FILL


def test_partial_fill_grows_the_exit(env):
    m = mk("KXBTC15M", 1)
    env.market(m, 46, 48)
    env.step(T)
    t = env.trip(m)
    env.fill(t.entry_oid, 4)
    env.step(T + 1)
    assert sum(o.count for o in env.resting(m, "EXIT").values()) == 4
    env.fill(t.entry_oid, 3)
    env.step(T + 2)
    exits = env.resting(m, "EXIT").values()
    assert sum(o.count for o in exits) == 7 and len(exits) == 2
    assert sum(o.count for o in exits) <= env.desk.held(t)


def test_grace_then_hard_stop_cancels_confirms_then_ioc_reduce_only(env):
    m = mk("KXBTC15M", 1)
    t = enter_and_fill(env, m)
    env.step(T + 1)
    co = set(env.resting(m, "EXIT"))
    env.book(m, 25, 27)                          # bid 25 <= 46 - 20
    env.step(T + 30)                             # inside grace: nothing
    assert set(env.resting(m, "EXIT")) == co and t.state == "HELD"
    env.step(T + 61)
    cuts = [o for o in env.gateway.order_index.values() if o.purpose == "CUT"]
    assert len(cuts) == 1
    c = cuts[0]
    assert (c.ioc, c.reduce_only, c.crossfire, c.price_cents, c.action) == (True, True, True, 25, "sell")
    assert not (co & set(env.gateway.resting))   # cash-out cancelled first
    # shadow IOC sold into the 50 bid at 25: flat
    env.step(T + 62)
    assert env.desk.closed[-1].closed_path == lr.HARD_STOP


def test_soft_stop_needs_two_spaced_reads_and_spot(env):
    m = mk("KXBTC15M", 1)
    env.spot["BTC"] = 100_000.0
    t = enter_and_fill(env, m)
    env.step(T + 1)
    env.book(m, 35, 37)                          # bid 35 <= 46 - 10, > 46 - 20
    env.step(T + 61)                             # read 1
    env.step(T + 65)                             # too soon: not a 2nd read
    assert t.state == "HELD"
    env.step(T + 71)                             # read 2, but spot has not moved
    assert t.state == "HELD"
    env.spot["BTC"] = 99_950.0                   # 50 points against YES (>= 40)
    env.step(T + 72)
    assert t.state == "WALK" and t.pending == lr.SOFT_STOP
    ex = env.resting(m, "EXIT")
    assert len(ex) == 1 and next(iter(ex.values())).price_cents == 36   # one inside the bid
    env.step(T + 73)                             # walks down one tick per read
    assert next(iter(env.resting(m, "EXIT").values())).price_cents == 36  # floor: bid+1
    env.book(m, 33, 35)
    env.step(T + 74)                             # one tick per read, not a jump
    assert next(iter(env.resting(m, "EXIT").values())).price_cents == 35
    env.step(T + 75)
    assert next(iter(env.resting(m, "EXIT").values())).price_cents == 34
    env.step(T + 76)                             # floor: one tick inside the bid
    assert next(iter(env.resting(m, "EXIT").values())).price_cents == 34
    env.fill(next(iter(env.resting(m, "EXIT"))))
    env.step(T + 77)
    assert env.desk.closed[-1].closed_path == lr.SOFT_STOP


def test_soft_stop_without_spot_never_confirms_hard_still_armed(env):
    m = mk("KXETH15M", 1)                         # no ETH spot until Chunk 6
    t = enter_and_fill(env, m)
    env.step(T + 1)
    env.book(m, 35, 37)
    for k in (61, 71, 81, 91):
        env.step(T + k)
    assert t.state == "HELD"
    assert any("soft stop unconfirmed" in w for w in t.why_log)
    env.book(m, 26, 28)
    env.step(T + 100)
    assert t.pending == lr.HARD_STOP


def test_time_stop_scratches_near_entry(env):
    m = mk("KXBTC15M", 1)
    t = enter_and_fill(env, m)
    env.step(T + 1)
    env.book(m, 45, 47)                          # within 2c of entry 46
    env.step(T + 359)
    assert t.state == "HELD"
    env.step(T + 361)
    assert t.pending == lr.TIME_STOP and t.state == "WALK"
    assert next(iter(env.resting(m, "EXIT").values())).price_cents == 46


def test_late_window_holds_the_favorite_to_settle(env):
    m = mk("KXBTC15M", 1)
    t = enter_and_fill(env, m)
    env.step(T + 1)
    env.book(m, 58, 60)                          # we are the favorite
    env.step(T + 600 - 100)                      # inside the final 2 min
    assert t.late_hold and t.state == "HELD"
    env.step(T + 600)                            # the bell
    assert env.desk.closed[-1].closed_path == lr.SETTLE


def test_late_window_exits_the_underdog(env):
    m = mk("KXBTC15M", 1)
    t = enter_and_fill(env, m)
    env.step(T + 1)
    env.book(m, 40, 42)                          # underdog, not a stop yet
    env.step(T + 600 - 100)
    assert t.pending == lr.LATE and t.state == "WALK"


def test_cancel_unknown_means_no_stop_sell(env, monkeypatch):
    m = mk("KXBTC15M", 1)
    t = enter_and_fill(env, m)
    env.step(T + 1)
    real = env.gateway.cancel_tristate
    monkeypatch.setattr(env.gateway, "cancel_tristate", lambda oid: "UNKNOWN")
    env.book(m, 25, 27)
    env.step(T + 61)
    env.step(T + 62)
    assert t.state == "UNWIND"
    assert not [o for o in env.gateway.order_index.values() if o.purpose == "CUT"]
    assert any("cancel UNKNOWN" in w for w in t.why_log)
    monkeypatch.setattr(env.gateway, "cancel_tristate", real)
    env.step(T + 63)
    assert len([o for o in env.gateway.order_index.values() if o.purpose == "CUT"]) == 1


def test_exit_size_never_exceeds_held(env):
    m = mk("KXBTC15M", 1)
    t = enter_and_fill(env, m)
    env.venue[m] = Decimal(3)                    # the venue says we hold only 3
    env.step(T + 1)
    assert sum(o.count for o in env.resting(m, "EXIT").values()) == 3
    with pytest.raises(Exception):
        env.desk._assert_exit_size(t, Decimal(1))     # 1 + 3 live > 3 held
    assert any("ROT_EXIT_OVERSIZE" in p for p in env.pages)


def test_no_entry_twice_in_one_market(env):
    m = mk("KXBTC15M", 1)
    enter_and_fill(env, m)
    for k in range(1, 5):
        env.step(T + k)
    entries = [o for o in env.gateway.order_index.values()
               if o.market == m and o.purpose == "ENTRY"]
    assert len(entries) == 1


def test_entries_halted_by_storage_halt(env, ledger):
    ledger.entry_halts.add("DURABLE_WRITE")
    m = mk("KXBTC15M", 1)
    env.market(m, 46, 48)
    env.step()
    assert m not in env.desk.trips
    assert env.desk.verdicts[m] == lr.SKIP_ENTRIES_HALTED


# ════════════════════ engine end to end (pessimistic shadow fills) ═══════════

def test_engine_round_trip_in_shadow(tmp_path):
    e = ShadowEngine(db_path=str(tmp_path / "e2e.db"))
    e.boot()
    sent = []
    e.telegram.send = sent.append
    m = mk("KXBTC15M", 7)
    e.market_meta[m] = {"close_ts": T + 600, "rec_yes_bid": 46, "rec_yes_ask": 48}
    b = e.feed.book(m)
    b.apply_snapshot({46: 50}, {52: 50}, ts=T)
    e.cycle([m], now=T)            # outside: queue + shortlist (book read next cycle)
    assert m in e.rot.shortlist and m not in e.rot.trips
    e.cycle([m], now=T + 1)        # shortlisted book read -> entry
    assert m in e.rot.trips
    b.apply_snapshot({46: 50}, {54: 50}, ts=T + 2)   # yes ask falls to 46: through
    e.cycle([m], now=T + 2)        # rested 1 poll -> fills on through-price
    e.cycle([m], now=T + 3)
    t = e.rot.trips[m]
    assert t.filled > 0
    b.apply_snapshot({54: 50}, {44: 50}, ts=T + 4)   # yes bid reaches 54: cash-out fills
    e.cycle([m], now=T + 4)
    e.cycle([m], now=T + 5)
    e.cycle([m], now=T + 6)
    assert m not in e.rot.trips
    assert e.rot.closed[-1].closed_path == lr.TARGET
    assert any("ROT #1 TARGET" in s for s in sent)        # Chunk 7: one line per trip


def test_rate_governed_entry_is_a_skip_and_retries(gateway, ledger):
    e = Env(gateway, ledger)
    gateway.governor.tokens = 0.0
    gateway.governor.refill = 0.0
    m = mk("KXBTC15M", 1)
    e.market(m, 46, 48)
    e.step()
    assert e.desk.verdicts[m] == "SKIP_WALL:RATE_GOVERNED" and m not in e.desk.trips
    gateway.governor.tokens = 5.0
    e.step()
    assert m in e.desk.trips


# ════════════════════ restarts, settlement, shutdown ════════════════════

def _rot_fill_row(engine, market, side="yes", price=46, count=10):
    engine.ledger.record_fill(market, "ROT", side, "ENTRY", price, count, "PROBE")


class _Resting:
    def __init__(self, orders):
        self.orders = orders

    def request(self, method, path, params=None, **k):
        return {"orders": self.orders}


def _boot_engine(tmp_path):
    e = ShadowEngine(db_path=str(tmp_path / "rb.db"))
    e.boot()
    e.sent = []
    e.telegram.send = e.sent.append
    failures.configure(e.ledger, alert_fn=e.telegram.alert, run_mode="TEST", boot_id=1)
    return e


def test_boot_readopts_a_rot_position_with_nothing_resting(tmp_path, monkeypatch):
    from relay_engine import venue
    from relay_engine.reconcile import live_boot_reconcile
    e = _boot_engine(tmp_path)
    m = mk("KXBTC15M", 1)
    _rot_fill_row(e, m)
    monkeypatch.setattr(venue, "get_balance", lambda c: (100.0, 0.0))
    monkeypatch.setattr(venue, "get_positions", lambda c: [{"ticker": m, "position": 10}])
    summary = live_boot_reconcile(e, _Resting([]))
    assert summary["rot_adopted"] == [(m, 10)]
    t = e.rot.trips[m]
    assert (t.state, t.side, t.cost, t.filled) == ("HELD", "yes", 46, 10)
    assert not any(p.market == m for p in e.custodian.positions.values())
    # next cycle (real clock: adoption stamped the grace at time.time()):
    # the desk re-arms the cash-out
    import time as _time
    now = _time.time()
    b = e.feed.book(m)
    b.apply_snapshot({46: 50}, {52: 50}, ts=now)
    e.market_meta[m] = {"close_ts": now + 600}
    e.cycle([m], now=now)
    exits = [o for o in e.gateway.resting.values() if o.market == m and o.purpose == "EXIT"]
    assert len(exits) == 1 and exits[0].count == 10 and exits[0].price_cents == 54
    failures._ledger = None
    failures._alert_fn = None


def test_boot_quarantines_a_rot_position_with_orders_still_resting(tmp_path, monkeypatch):
    from relay_engine import venue
    from relay_engine.reconcile import live_boot_reconcile
    e = _boot_engine(tmp_path)
    m = mk("KXBTC15M", 1)
    _rot_fill_row(e, m)
    monkeypatch.setattr(venue, "get_balance", lambda c: (100.0, 0.0))
    monkeypatch.setattr(venue, "get_positions", lambda c: [{"ticker": m, "position": 10}])
    summary = live_boot_reconcile(e, _Resting([{"order_id": "OLD-1", "ticker": m}]))
    assert m not in e.rot.trips
    assert summary["positions_quarantined"] == 1
    assert e.gateway.positions[(m.rsplit("-", 1)[0], m, "QUARANTINE")] == 10
    failures._ledger = None
    failures._alert_fn = None


def test_settled_trip_releases_its_position_and_cap(env):
    m = mk("KXBTC15M", 1)
    t = enter_and_fill(env, m)
    env.step(T + 1)
    env.book(m, 70, 72)                          # favorite into the bell
    env.step(T + 600)
    assert env.desk.closed[-1].closed_path == lr.SETTLE
    assert t.key not in env.gateway.positions
    assert env.desk.exposure()[0] == 0
    assert not env.resting(m)                    # cash-out cancelled at the bell


def test_clean_shutdown_cancels_rot_resting_orders(tmp_path):
    e = _boot_engine(tmp_path)
    m = mk("KXBTC15M", 1)
    e.rot.trips[m] = lr.adopt_trip(e.rot, m, "yes", 10, 46, T, T + 600)
    e.gateway.positions[(m.rsplit("-", 1)[0], m, "ROT")] = 10
    e.gateway.gross_open[(m.rsplit("-", 1)[0], m, "ROT")] = 10
    b = e.feed.book(m)
    b.apply_snapshot({46: 50}, {52: 50}, ts=T)
    e.market_meta[m] = {"close_ts": T + 600}
    e.cycle([m], now=T)
    assert any(o.market == m for o in e.gateway.resting.values())
    e.rot_shutdown()
    assert not any(o.market == m for o in e.gateway.resting.values())
    assert any("stops UNWATCHED" in s for s in e.sent)
    failures._ledger = None
    failures._alert_fn = None


# ════════════════════ the LIVE door (mocked venue) ════════════════════

def test_live_hard_stop_is_ioc_reduce_only_and_needs_no_cash(gateway, monkeypatch):
    from relay_engine import venue
    from relay_engine.gateway import Order
    monkeypatch.setattr(config, "RUN_MODE", "LIVE")
    monkeypatch.setattr(config, "I_UNDERSTAND_LIVE", config.I_UNDERSTAND_LIVE_PHRASE)
    calls = []
    monkeypatch.setattr(venue, "get_balance", lambda c: (None, None))   # unreadable
    monkeypatch.setattr(venue, "place_order_maker",
                        lambda *a, **k: (calls.append((a, k)) or ("LIVE-1", {})))
    gateway.venue_client = object()
    m = mk("KXBTC15M", 1)
    key = (m.rsplit("-", 1)[0], m, "ROT")
    gateway.positions[key] = 10
    gateway.gross_open[key] = 10
    b = OrderBook(market=m)
    b.apply_snapshot({25: 50}, {73: 50}, ts=T)
    o = Order(lane="ROT", event=key[0], market=m, side="yes", action="sell",
              price_cents=25, count=10, size_tier="PROBE", purpose="CUT",
              band=(1, 99), crossfire=True, ioc=True, reduce_only=True, reason="t")
    r = gateway.submit(o, b)
    assert r.order_id == "LIVE-1" and not r.shadow
    (_args, kw), = calls
    assert kw["post_only"] is False
    assert kw["time_in_force"] == "immediate_or_cancel" and kw["reduce_only"] is True
    assert "LIVE-1" not in gateway.resting and "LIVE-1" in gateway.order_index


def test_ioc_and_reduce_only_are_cut_only(gateway):
    from relay_engine.errors import WallRejection
    from relay_engine.gateway import Order
    m = mk("KXBTC15M", 1)
    b = OrderBook(market=m)
    b.apply_snapshot({46: 50}, {52: 50}, ts=T)
    for kw in ({"ioc": True}, {"reduce_only": True, "ioc": True}):
        o = Order(lane="ROT", event="E", market=m, side="yes", action="buy",
                  price_cents=46, count=1, size_tier="PROBE", purpose="ENTRY",
                  band=(40, 54), why="w", **kw)
        with pytest.raises(WallRejection):
            gateway.submit(o, b)
    o = Order(lane="ROT", event="E", market=m, side="yes", action="sell",
              price_cents=46, count=1, size_tier="PROBE", purpose="CUT",
              band=(1, 99), crossfire=True, reduce_only=True)
    with pytest.raises(WallRejection) as ei:
        gateway.submit(o, b)
    assert ei.value.wall == "REDUCE_ONLY_NEEDS_IOC"


def test_venue_ioc_reduce_only_body():
    from relay_engine import venue

    class C:
        def request(self, method, path, json_body=None, **k):
            self.body = json_body
            return {"order_id": "X"}
    c = C()
    venue.place_order_maker(c, "KXBTC15M-X", "no", 75, count=Decimal("0.70"),
                            post_only=False, time_in_force="immediate_or_cancel",
                            reduce_only=True)
    assert c.body["time_in_force"] == "immediate_or_cancel"
    assert c.body["reduce_only"] is True and c.body["count"] == "0.70"
    with pytest.raises(ValueError):
        venue.place_order_maker(c, "KXBTC15M-X", "no", 75, reduce_only=True)


def test_parse_fill_keeps_fractional_counts():
    from relay_engine import venue
    _c, _f, n = venue.parse_fill({"yes_price": 46, "count_fp": "0.70"}, "yes")
    assert n == Decimal("0.70")
    _c, _f, n = venue.parse_fill({"yes_price": 46, "count": 3}, "yes")
    assert n == 3
    _c, _f, n = venue.parse_fill({"yes_price": 46}, "yes")
    assert n is None


def test_fill_without_a_count_is_paged_not_booked(gateway, ledger, surface):
    from relay_engine.fills import FillBooker
    from relay_engine.gateway import Order
    sent = []
    failures.configure(ledger, alert_fn=sent.append, run_mode="TEST", boot_id=1)
    try:
        m = mk("KXBTC15M", 1)
        b = OrderBook(market=m)
        b.apply_snapshot({46: 50}, {52: 50}, ts=T)
        o = Order(lane="ROT", event=m.rsplit("-", 1)[0], market=m, side="yes",
                  action="buy", price_cents=46, count=Decimal("0.70"),
                  size_tier="PROBE", purpose="ENTRY", band=(40, 54), why="w")
        oid = gateway.submit(o, b).order_id
        fb = FillBooker(gateway, ledger, surface)
        rec = {"fill_id": "F1", "order_id": oid, "yes_price": 46}   # no count
        st = fb.sweep([rec], now=T)
        assert st.get("count_unreadable") == 1 and st["booked"] == 0
        assert gateway.positions.get((o.event, m, "ROT"), 0) == 0
        fb.sweep([rec], now=T + 1)                                  # retried, paged once
        assert sum("FILL_COUNT_UNREADABLE" in s for s in sent) == 1
        st = fb.sweep([dict(rec, fill_id="F2", count_fp="0.70")], now=T + 2)
        assert st["booked"] == 1
        assert gateway.positions[(o.event, m, "ROT")] == Decimal("0.70")
    finally:
        failures._ledger = None
        failures._alert_fn = None



# ════════════════════ Drew 2026-10-15: one walk order, never re-enter ═══════

def _sold(env, market):
    """Contracts actually sold on the market (every sell order's fills)."""
    return sum(Decimal(str(env.gateway.filled_counts.get(oid, 0)))
               for oid, o in env.gateway.order_index.items()
               if o.market == market and o.action == "sell")


def _live_sells(env, market):
    return [oid for oid, o in env.gateway.resting.items()
            if o.market == market and o.action == "sell"]


def _soft_confirmed(env, m):
    env.spot["BTC"] = 100_000.0
    t = enter_and_fill(env, m)
    env.step(T + 1)
    env.book(m, 35, 37)
    env.step(T + 61)
    env.spot["BTC"] = 99_950.0
    env.step(T + 72)
    assert t.state == "WALK" and t.pending == lr.SOFT_STOP
    return t


def test_walk_down_is_one_order_amended_never_a_second(env):
    m = mk("KXBTC15M", 1)
    t = _soft_confirmed(env, m)
    oid = t.exit_oid
    o = env.gateway.order_index[oid]
    assert (o.purpose, o.action, o.crossfire, o.ioc, o.count) == ("EXIT", "sell", False, False, t.filled)
    env.book(m, None, 40)                        # nobody bids YES: keep stepping down
    prices = []
    for k in range(73, 90):
        env.step(T + k)
        sells = _live_sells(env, m)
        assert sells == [oid]                    # ONE order, the same order, every read
        prices.append(env.gateway.resting[oid].price_cents)
    assert prices[:10] == list(range(35, 25, -1))    # one tick per read
    assert prices[-1] == 46 - config.ROT_HARD_STOP_CENTS == 26   # floored at the hard-stop price
    assert len([x for x in env.gateway.order_index.values()
                if x.market == m and x.action == "sell" and x.purpose == "EXIT"
                and "cash-out" not in x.reason]) == 1


def test_walk_at_floor_then_bid_through_minus_20_cancel_confirmed_then_ioc(env):
    """Drew's test: the walk-down rests at the floor; the bid drops through
    entry-20; the walk order's cancel is CONFIRMED; only then the IOC
    reduce_only fires — and total sold never exceeds held."""
    m = mk("KXBTC15M", 1)
    t = _soft_confirmed(env, m)
    held0 = env.desk.held(t)
    walk_oid = t.exit_oid
    env.book(m, None, 40)
    for k in range(73, 90):
        env.step(T + k)
    assert env.gateway.resting[walk_oid].price_cents == 26         # at the floor
    order = []
    real_cancel = env.gateway.cancel_tristate
    real_submit = env.gateway.submit

    def cancel(oid):
        st = real_cancel(oid)
        order.append(("cancel", oid, st))
        return st

    def submit(o, book, *a, **k):
        order.append(("submit", o.purpose, o.ioc, o.reduce_only, o.count, o.price_cents))
        return real_submit(o, book, *a, **k)
    env.gateway.cancel_tristate = cancel
    env.gateway.submit = submit
    env.book(m, 24, 40)                          # bid 24 <= 46 - 20: through the hard line
    env.step(T + 90)
    assert order[0] == ("cancel", walk_oid, "CANCELED")
    sub = [e for e in order if e[0] == "submit"]
    assert sub == [("submit", "CUT", True, True, held0, 24)]
    assert order.index(sub[0]) > order.index(("cancel", walk_oid, "CANCELED"))
    assert walk_oid not in env.gateway.resting
    env.step(T + 91)
    assert env.desk.closed[-1].closed_path == lr.HARD_STOP
    assert _sold(env, m) <= held0
    assert _sold(env, m) == held0                # the bid had depth: all sold, no more


def test_ioc_never_fires_while_the_walk_cancel_is_unconfirmed(env, monkeypatch):
    m = mk("KXBTC15M", 1)
    t = _soft_confirmed(env, m)
    monkeypatch.setattr(env.gateway, "cancel_tristate", lambda oid: "UNKNOWN")
    env.book(m, 24, 40)
    env.step(T + 90)
    env.step(T + 91)
    assert not [o for o in env.gateway.order_index.values() if o.purpose == "CUT"]
    assert _live_sells(env, m) == [t.exit_oid]


def test_amend_is_size_checked_and_follows_a_shrinking_hold(env):
    m = mk("KXBTC15M", 1)
    t = _soft_confirmed(env, m)
    oid = t.exit_oid
    held0 = env.desk.held(t)
    env.venue[m] = held0 - 3                     # the venue says 3 fewer are held
    env.step(T + 73)
    o = env.gateway.resting[oid]
    assert o.count == held0 - 3 and _live_sells(env, m) == [oid]
    # a stray second sell on the market: the next amend is refused, never sold
    from relay_engine.gateway import Order
    stray = Order(lane="ROT", event=t.event, market=m, side="yes", action="sell",
                  price_cents=60, count=2, size_tier="PROBE", purpose="EXIT",
                  band=(1, 99), reason="stray")
    env.gateway.resting["STRAY"] = stray
    env.gateway.order_index["STRAY"] = stray
    px_before = env.gateway.resting[oid].price_cents
    env.book(m, 33, 37)                          # the walk wants to step down: an amend
    env.step(T + 74)
    assert env.gateway.resting[oid].price_cents == px_before   # refused, not moved
    assert any("ROT_EXIT_OVERSIZE" in p for p in env.pages)
    assert env.gateway.resting[oid].count == held0 - 3     # not amended


def test_live_walk_amends_through_venue_amend_order(gateway, monkeypatch):
    from relay_engine import venue
    from relay_engine.gateway import Order
    calls = []
    monkeypatch.setattr(venue, "amend_order",
                        lambda c, oid, tk, side, px, count=1, **k:
                        (calls.append((oid, tk, side, px, count)) or ("LIVE-2", {})))
    gateway.venue_client = object()
    o = Order(lane="ROT", event="E", market="KXBTC15M-X-00", side="yes", action="sell",
              price_cents=36, count=Decimal("0.70"), size_tier="PROBE", purpose="EXIT",
              band=(1, 99), reason="walk")
    gateway.resting["LIVE-1"] = o
    gateway.order_index["LIVE-1"] = o
    gateway.live_order_ids.add("LIVE-1")
    new = gateway.amend_exit("LIVE-1", 35, Decimal("0.70"))
    assert new == "LIVE-2" and "LIVE-1" not in gateway.resting
    # sell YES @35 is the complement buy NO @65 (flip_math.exit_args) — V2 ask YES 0.35
    assert calls == [("LIVE-1", "KXBTC15M-X-00", "no", 65, Decimal("0.70"))]
    assert gateway.resting["LIVE-2"].price_cents == 35
    monkeypatch.setattr(venue, "amend_order", lambda *a, **k: (None, {}))
    assert gateway.amend_exit("LIVE-2", 34, Decimal("0.70")) is None
    assert gateway.resting["LIVE-2"].price_cents == 35      # unchanged: never assumed


def test_amend_is_exit_only(gateway):
    from relay_engine.errors import WallRejection
    from relay_engine.gateway import Order
    o = Order(lane="ROT", event="E", market="M", side="yes", action="buy",
              price_cents=46, count=1, size_tier="PROBE", purpose="ENTRY",
              band=(40, 54), why="w")
    gateway.resting["S-1"] = o
    with pytest.raises(WallRejection):
        gateway.amend_exit("S-1", 45, 1)


def test_an_entered_market_is_never_entered_again(env):
    m = mk("KXBTC15M", 1)
    t = enter_and_fill(env, m)
    env.step(T + 1)
    env.fill(next(iter(env.resting(m, "EXIT"))))
    env.step(T + 2)
    assert m not in env.desk.trips
    for k in range(3, 10):
        env.step(T + k)
    assert m not in env.desk.trips
    assert env.desk.verdicts[m] == lr.SKIP_ENTERED_BEFORE
    entries = [o for o in env.gateway.order_index.values()
               if o.market == m and o.purpose == "ENTRY"]
    assert len(entries) == 1


def test_an_unfilled_entry_is_never_chased(env):
    m = mk("KXBTC15M", 1)
    env.market(m, 46, 48)
    env.step(T)
    for k in range(1, 70):
        env.step(T + k)
    entries = [o for o in env.gateway.order_index.values()
               if o.market == m and o.purpose == "ENTRY"]
    assert len(entries) == 1 and env.desk.verdicts[m] == lr.SKIP_ENTERED_BEFORE


def test_never_reentered_across_a_restart_or_window_close(env, gateway, ledger):
    m = mk("KXBTC15M", 1)
    enter_and_fill(env, m)
    env.desk.forget_market(m)                    # the engine's window-close prune
    fresh = lr.RotationDesk(gateway, ledger, alert=lambda s: None)   # a reboot
    assert m in fresh.entered
    env2 = Env(gateway, ledger)
    env2.desk = fresh
    env2.market(m, 46, 48)
    gateway.positions.clear()
    gateway.resting.clear()
    env2.step(T + 5)
    assert m not in fresh.trips and fresh.verdicts[m] == lr.SKIP_ENTERED_BEFORE


def test_a_fill_between_reads_is_never_counted_twice(env):
    """Exposure reads fill truth from the gateway: a fill booked after the
    desk's last read counts once (held), not twice (held + still resting)."""
    m = mk("KXBTC15M", 1)
    env.market(m, 46, 48)
    env.step()
    t = env.trip(m)
    before = env.desk.exposure()[0]
    env.fill(t.entry_oid)                      # booked by the sweep, desk not yet read
    assert env.desk.exposure()[0] == before    # same money, now held instead of resting
