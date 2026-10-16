"""WO-ROT-REGIME-1 — the ROT turbulence gate (ENTRIES ONLY).

The RULED finding this proves out: ROT's mean-reversion edge inverts into
adverse selection on high-vol trending days. The gate STANDS ASIDE on new
entries while the underlying is TURBULENT and resumes when calm. The four
non-negotiables, each with a test below:
  (a) ROT_REGIME_ENABLED=False is a no-op — byte-identical to no regime code.
  (b) a simulated vol spike classifies TURBULENT -> SKIP_REGIME on entry.
  (c) calm tape classifies QUIET/NORMAL -> entry allowed.
  (d) EXITS are NEVER blocked by regime (cash-out, soft/time/late, hard stop).

The desk is driven on the REAL gateway/ledger (shadow), books and the listing
are this file's, exactly as test_rot_desk.py drives it.
"""

from decimal import Decimal

import pytest

from relay_engine import config, failures, lane_rot as lr, rot_regime as rr
from relay_engine.book import OrderBook
from relay_engine.series_discovery import SeriesInfo

T = 1_800_000_000.0


def _info(series, und, cadence="15M"):
    return SeriesInfo(series=series, cadence=cadence, cadence_source="test",
                      underlying=und, settle_sources=["CF Benchmarks"], tick="1c",
                      fee_type="quadratic", fee_multiplier="1", fee_changes=0,
                      open_markets=1)


SERIES = {
    "KXBTC15M": _info("KXBTC15M", "BTC"),
    "KXETH15M": _info("KXETH15M", "ETH"),
}


@pytest.fixture(autouse=True)
def _rot(monkeypatch):
    monkeypatch.setattr(config, "DESK_MODE", "ROTATION")
    monkeypatch.setattr(config, "SERIES_INFO", dict(SERIES))


def mk(series, n):
    return f"{series}-26OCT14{n:04d}-00"


class Env:
    """The desk on a real gateway; books and the listing are ours (mirrors
    test_rot_desk.Env, with a per-underlying spot the tests drive)."""

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
    monkeypatch.setattr(gateway.governor, "take", lambda now=None: True)
    failures._warn_last.clear()
    sent = []
    failures.configure(ledger, alert_fn=sent.append, run_mode="TEST", boot_id=1)
    e = Env(gateway, ledger)
    e.pages = sent
    yield e
    failures._ledger = None
    failures._alert_fn = None


# ── spot tapes: calm vs. a turbulent spike ───────────────────────

def _quiet_tape(base=100_000.0, n=10):
    """A dead-flat tape: zero vol -> QUIET."""
    return [(T + i, base) for i in range(n)]


def _turbulent_tape(base=100_000.0, n=10, step_bps=50.0):
    """A one-direction grind of step_bps per sample (>> 12 bps) -> TURBULENT."""
    out = []
    px = base
    for i in range(n):
        out.append((T + i, px))
        px *= (1.0 + step_bps / 10_000.0)
    return out


def _feed(env, underlying, tape):
    """Replay a (ts, spot) tape straight into the tracker (not through full
    cycles) so the regime is established BEFORE the one step() under test — a
    cycle both samples AND tries to enter, so stepping the tape in would let an
    early UNKNOWN cycle enter the market before turbulence is proven."""
    for ts, px in tape:
        env.desk.regime.observe(underlying, px, ts)


# ════════════════════ the tracker in isolation ════════════════════

def test_disabled_is_a_no_op(monkeypatch):
    """(a) OFF (the DEFAULT): observe records nothing, regime/vol are None,
    blocks_entry is False. Byte-identical to no regime logic at all."""
    assert config.ROT_REGIME_ENABLED is False          # the shipped default
    g = rr.RotRegime()
    for px in (100_000.0, 100_500.0, 99_000.0, 101_000.0, 98_000.0, 102_000.0):
        g.observe("BTC", px, T)
    assert g._samples == {}                             # nothing recorded
    assert g.vol_bps("BTC") is None
    assert g.regime("BTC") is None
    assert g.blocks_entry("BTC") is False


def test_quiet_and_normal_and_turbulent_classify(monkeypatch):
    monkeypatch.setattr(config, "ROT_REGIME_ENABLED", True)
    g = rr.RotRegime()
    for ts, px in _quiet_tape():
        g.observe("BTC", px, ts)
    assert g.regime("BTC") == rr.QUIET and not g.blocks_entry("BTC")

    g = rr.RotRegime()
    for ts, px in _turbulent_tape():
        g.observe("BTC", px, ts)
    assert g.vol_bps("BTC") == pytest.approx(50.0, abs=1.0)
    assert g.regime("BTC") == rr.TURBULENT and g.blocks_entry("BTC")

    # a middling grind (~8 bps/sample) sits in NORMAL -> still trades
    g = rr.RotRegime()
    for ts, px in _turbulent_tape(step_bps=8.0):
        g.observe("BTC", px, ts)
    assert g.regime("BTC") == rr.NORMAL and not g.blocks_entry("BTC")


def test_unknown_until_min_samples_never_blocks(monkeypatch):
    """Not enough fresh tape -> UNKNOWN, and UNKNOWN never stands the desk
    aside (no proof of calm is not proof of turbulence)."""
    monkeypatch.setattr(config, "ROT_REGIME_ENABLED", True)
    monkeypatch.setattr(config, "ROT_REGIME_MIN_SAMPLES", 6)
    g = rr.RotRegime()
    for ts, px in _turbulent_tape(n=3):                 # only 3 samples
        g.observe("BTC", px, ts)
    assert g.regime("BTC") == rr.UNKNOWN
    assert g.blocks_entry("BTC") is False


def test_window_prunes_stale_samples(monkeypatch):
    """A turbulent burst that has aged out of the window leaves no vol — the
    gate reads the RECENT tape, not the whole history."""
    monkeypatch.setattr(config, "ROT_REGIME_ENABLED", True)
    monkeypatch.setattr(config, "ROT_REGIME_WINDOW_S", 120.0)
    g = rr.RotRegime()
    for ts, px in _turbulent_tape():                    # spike at T..T+9
        g.observe("BTC", px, ts)
    assert g.blocks_entry("BTC", now=T + 9)             # turbulent now
    # 300 s later with one flat sample: the spike is pruned, too few samples
    g.observe("BTC", 100_000.0, T + 300)
    assert g.regime("BTC", now=T + 300) == rr.UNKNOWN
    assert g.blocks_entry("BTC", now=T + 300) is False


# ════════════════════ the gate on the desk ════════════════════

def test_disabled_gate_enters_normally(env):
    """(a) end to end: with the gate OFF a would-be-turbulent tape still enters,
    exactly as the desk does today."""
    assert config.ROT_REGIME_ENABLED is False
    m = mk("KXBTC15M", 1)
    env.market(m, 46, 48)
    _feed(env, "BTC", _turbulent_tape())               # spike, but gate is OFF
    env.step(T + 10)
    assert m in env.desk.trips                          # entered anyway
    assert env.desk.regime_skips == 0


def test_turbulent_underlying_skips_entry(env, monkeypatch):
    """(b) a vol spike classifies TURBULENT -> SKIP_REGIME, no entry, counted."""
    monkeypatch.setattr(config, "ROT_REGIME_ENABLED", True)
    m = mk("KXBTC15M", 1)
    env.market(m, 46, 48)
    _feed(env, "BTC", _turbulent_tape())
    env.step(T + 10)
    assert m not in env.desk.trips
    assert env.desk.verdicts[m] == lr.SKIP_REGIME
    assert env.desk.regime_skips >= 1
    assert not env.resting(m, "ENTRY")                  # nothing rested


def test_calm_underlying_allows_entry(env, monkeypatch):
    """(c) calm tape classifies QUIET -> the entry goes through."""
    monkeypatch.setattr(config, "ROT_REGIME_ENABLED", True)
    m = mk("KXBTC15M", 1)
    env.market(m, 46, 48)
    _feed(env, "BTC", _quiet_tape())
    env.step(T + 10)
    assert env.desk.regime.regime("BTC", now=T + 10) == rr.QUIET
    assert m in env.desk.trips
    assert env.desk.verdicts[m] == "ENTERED"


def test_unknown_regime_allows_entry(env, monkeypatch):
    """(c, corollary) too little tape -> UNKNOWN -> trade, never stand aside."""
    monkeypatch.setattr(config, "ROT_REGIME_ENABLED", True)
    monkeypatch.setattr(config, "ROT_REGIME_MIN_SAMPLES", 6)
    m = mk("KXBTC15M", 1)
    env.market(m, 46, 48)
    env.spot["BTC"] = 100_000.0
    env.step(T)                                         # one sample only
    assert m in env.desk.trips


def test_exits_are_never_gated_by_regime(env, monkeypatch):
    """(d) THE SACRED ONE: a position is held, then the underlying turns
    TURBULENT. The cash-out must still rest, the hard stop must still fire, the
    trip must still close — the gate touches ENTRIES ONLY."""
    monkeypatch.setattr(config, "ROT_REGIME_ENABLED", True)
    m = mk("KXBTC15M", 1)
    env.market(m, 46, 48)
    # enter while calm
    _feed(env, "BTC", _quiet_tape())
    env.step(T + 10)
    t = env.trip(m)
    assert t is not None
    env.fill(t.entry_oid)
    env.step(T + 11)
    co = set(env.resting(m, "EXIT"))
    assert co, "cash-out must rest even though we will now go turbulent"

    # now the tape turns violent AND the held side craters through the hard line
    _feed(env, "BTC", _turbulent_tape(base=100_000.0))
    assert env.desk.regime.blocks_entry("BTC", now=T + 20)   # gate would block ENTRIES
    env.book(m, 25, 27)                                 # bid 25 <= 46 - 20: hard stop
    env.step(T + 200)                                   # well past grace
    cuts = [o for o in env.gateway.order_index.values() if o.purpose == "CUT"]
    assert len(cuts) == 1, "hard-stop CUT must fire regardless of regime"
    env.step(T + 201)
    assert env.desk.closed[-1].closed_path == lr.HARD_STOP

    # and the cash-out path is never gated either: a fresh calm trip whose
    # target fills closes TARGET even if a later entry would be blocked
    m2 = mk("KXETH15M", 2)
    env.market(m2, 46, 48)
    env.spot["ETH"] = 2_000.0
    for i in range(8):
        env.step(T + 210 + i)                           # calm ETH -> QUIET -> enters
    t2 = env.trip(m2)
    assert t2 is not None
    env.fill(t2.entry_oid)
    env.step(T + 220)
    env.fill(next(iter(env.resting(m2, "EXIT"))))
    env.step(T + 221)
    assert env.desk.closed[-1].closed_path == lr.TARGET


def test_turbulent_one_underlying_does_not_gate_another(env, monkeypatch):
    """Per-underlying: BTC turbulent stands BTC aside; a calm ETH still enters
    the same cycle."""
    monkeypatch.setattr(config, "ROT_REGIME_ENABLED", True)
    mb, me = mk("KXBTC15M", 1), mk("KXETH15M", 2)
    env.market(mb, 46, 48)
    env.market(me, 46, 48)
    # establish both regimes first: BTC violent, ETH flat
    _feed(env, "BTC", _turbulent_tape(base=100_000.0))
    _feed(env, "ETH", _quiet_tape(base=2_000.0))
    env.step(T + 10)
    assert mb not in env.desk.trips and env.desk.verdicts[mb] == lr.SKIP_REGIME
    assert me in env.desk.trips
