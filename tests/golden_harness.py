"""Golden-tape harness (gate 5, C.4).

Loads the VENDORED LIVE k_worker code (reference/live_k_worker/, byte-identical
to B1) with its I/O stubbed and its clock faked, replays deterministic market
tapes through BOTH the live implementation and the relay port
(relay_engine/lane_fh8.py), and compares proposals/passes frame-for-frame.

The tape corpus is deterministic (seeded) and covers: band edges (94.99/95/99/
99.4), fp-string vs integer books, side flips mid-tier, counterparty vanishing
(R2), floor dropouts, tier transitions, H8 distance/time/probe-kill/budget
edges, cross-lane and single-entry states, hourly-cap edges, and delta-table
loaded/absent (top-rung pass/fail/low-tail).

Declared-change normalization (see lane_fh8.py header): the live treasury
accrual is stubbed to zero — with zero accrual the live arithmetic and the
EPOCH 2 arithmetic coincide, which is exactly the equivalence the tape pins.
Comparison keys: (passed, lane, side, cost_cents, cost_exact, reject_code,
why_tag). reject_reason free text is NOT compared (D1 wording).
"""

import importlib.util
import sys
import types
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
LIVE_DIR = ROOT / "reference" / "live_k_worker"
sys.path.insert(0, str(ROOT))

from relay_engine import delta as relay_delta  # noqa: E402
from relay_engine import lane_fh8 as port  # noqa: E402

POLL = 5  # POLL_INTERVAL_SEC in both implementations


# ---------------------------------------------------------------------------
# Fake clock
# ---------------------------------------------------------------------------
class FakeTime:
    def __init__(self, start: float):
        self.now = start

    def time(self) -> float:
        return self.now

    def sleep(self, secs: float) -> None:
        self.now += secs

    def monotonic(self) -> float:
        return self.now


# ---------------------------------------------------------------------------
# Scenario
# ---------------------------------------------------------------------------
@dataclass
class Scenario:
    name: str
    close_ts: float
    t0: float                      # clock at ladder start
    tape: Callable[[float], object]  # time -> Book (live) / TouchBook fields
    cash: float = 100.0
    spot: Optional[float] = 65_000.0
    boundary_lo: Optional[float] = 64_000.0
    boundary_hi: Optional[float] = 64_500.0
    traded: frozenset = frozenset()          # {(lane, ticker)}
    hourly_exposure: tuple = ()              # ((ts_offset_from_t0, usd), ...)
    lane_losses: Dict[str, int] = field(default_factory=dict)
    h8_lifetime: Dict[str, int] = field(default_factory=lambda: {"wins": 0, "losses": 0})
    h8_daily_at_risk: float = 0.0
    table_cells: Optional[Dict[Tuple[int, int, str], dict]] = None  # delta table injection
    ticker: str = "KXBTC15M-TAPE"


def book_frame(yes_bid=None, no_bid=None, yes_fp=None, no_fp=None,
               yes_qty=50, no_qty=50):
    """A neutral book description both sides can consume."""
    return {"yes_bid": yes_bid, "no_bid": no_bid,
            "yes_bid_fp": yes_fp, "no_bid_fp": no_fp,
            "yes_bid_qty": yes_qty, "no_bid_qty": no_qty,
            "yes_ask": (100 - no_bid) if no_bid is not None else None,
            "no_ask": (100 - yes_bid) if yes_bid is not None else None}


def constant_tape(frame):
    return lambda now: frame


def schedule_tape(t0, schedule):
    """schedule: list of (offset_seconds, frame); frame in force from its offset."""
    def fn(now):
        current = schedule[0][1]
        for off, fr in schedule:
            if now - t0 >= off:
                current = fr
            else:
                break
        return current
    return fn


# ---------------------------------------------------------------------------
# Live-side loading
# ---------------------------------------------------------------------------
def _load_module(name: str, path: Path, package: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = package
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def build_live(scenario: Scenario, fake: FakeTime):
    """Build a fresh live_kw package wired to this scenario. Returns (engine, gateway, captured)."""
    # tear down any previous copy so module state resets per scenario
    for m in list(sys.modules):
        if m == "live_kw" or m.startswith("live_kw."):
            del sys.modules[m]

    pkg = types.ModuleType("live_kw")
    pkg.__path__ = [str(LIVE_DIR)]
    sys.modules["live_kw"] = pkg

    captured = {"rows": [], "alerts": []}

    # ---- kalshi stub ----
    kalshi = types.ModuleType("live_kw.kalshi")

    @dataclass
    class Book:
        yes_bid: Optional[int] = None
        yes_ask: Optional[int] = None
        no_bid: Optional[int] = None
        no_ask: Optional[int] = None
        yes_bid_qty: int = 0
        no_bid_qty: int = 0
        yes_bid_fp: Optional[str] = None
        no_bid_fp: Optional[str] = None

    class OrderStatusUnavailable(Exception):
        pass

    kalshi.Book = Book
    kalshi.OrderStatusUnavailable = OrderStatusUnavailable
    kalshi.KalshiClient = object
    kalshi.fetch_orderbook = lambda client, ticker: Book(**scenario.tape(fake.now))
    kalshi.get_balance = lambda client: (scenario.cash, 0.0)
    kalshi.get_btc_spot = lambda: scenario.spot
    kalshi.extract_boundaries = lambda mo: (scenario.boundary_lo, scenario.boundary_hi)
    kalshi.get_fills = lambda client, ticker: []
    kalshi.position_for_market = lambda client, ticker: 0
    kalshi.get_order_queue_position = lambda client, oid: None
    kalshi.cancel_all_for_market = lambda client, ticker: None
    kalshi.cancel_order = lambda client, oid: None
    kalshi.order_status = lambda client, ticker, oid: {"state": "resting", "raw": {}}
    kalshi.parse_fills = lambda fills, side: (None, 0, None)
    kalshi.place_order_maker = lambda *a, **k: ("LIVE-STUB-OID", {})
    kalshi.amend_order = lambda *a, **k: (None, None)
    kalshi.get_settlement_result = lambda client, ticker: None
    sys.modules["live_kw.kalshi"] = kalshi

    # ---- store stub ----
    store = types.ModuleType("live_kw.store")
    store.T_BANDS = [(900, 600), (600, 300), (300, 180), (180, 120), (120, 60), (60, 10)]

    class SurfaceRow:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    store.SurfaceRow = SurfaceRow
    store.insert_row = lambda row: captured["rows"].append(row) or len(captured["rows"])
    for fn in ("update_settlement", "update_fill", "update_drift", "update_reprice",
               "update_order_id", "update_why_tag", "update_session_vol",
               "update_queue_pos"):
        setattr(store, fn, lambda *a, **k: None)
    store.get_state = lambda key: None
    store.set_state = lambda key, val: None
    store.query_lane_losses_recent = lambda lane, window: scenario.lane_losses.get(lane, 0)
    store.query_h8_probe_lifetime = lambda: dict(scenario.h8_lifetime)
    store.query_h8_probe_daily = lambda: {"at_risk": scenario.h8_daily_at_risk}
    store.query_band_stats = lambda lo, hi, env, lane: {"n": 0, "wins": 0}
    store.query_band_friction = lambda lo, hi, env: None
    sys.modules["live_kw.store"] = store

    # ---- treasury stub (declared-change normalization: zero accrual) ----
    treasury = types.ModuleType("live_kw.treasury")
    treasury.tradeable_balance = lambda cash: cash
    treasury.accrued_total = lambda: 0.0
    treasury.waterfall = lambda pnl: None
    treasury.record_loss = lambda pnl: None
    sys.modules["live_kw.treasury"] = treasury

    # ---- discipline stub ----
    discipline = types.ModuleType("live_kw.discipline")
    discipline.is_halted = lambda: False
    discipline.halt_reason = lambda: ""
    discipline.check_drawdown = lambda cash: None
    discipline.record_win = lambda: None
    discipline.record_loss = lambda: None
    sys.modules["live_kw.discipline"] = discipline

    # ---- notify stub ----
    notify = types.ModuleType("live_kw.notify")
    notify.send = lambda msg: None
    notify.alert = lambda msg: captured["alerts"].append(msg)
    sys.modules["live_kw.notify"] = notify

    # ---- real vendored sessions + delta_table_loader ----
    _load_module("live_kw.sessions", LIVE_DIR / "sessions.py", "live_kw")
    live_loader = _load_module("live_kw.delta_table_loader",
                               LIVE_DIR / "delta_table_loader.py", "live_kw")
    if scenario.table_cells is not None:
        live_loader._TABLE = dict(scenario.table_cells)
        live_loader._LOADED = True
    else:
        live_loader._TABLE = {}
        live_loader._LOADED = False

    # ---- real vendored gateway + engine ----
    gateway = _load_module("live_kw.gateway", LIVE_DIR / "gateway.py", "live_kw")
    engine = _load_module("live_kw.engine", LIVE_DIR / "engine.py", "live_kw")

    # fake clocks
    engine.time = fake
    gateway.time = fake
    engine.heartbeat = lambda: None

    # scenario gateway state
    gateway._traded_tickers = set(scenario.traded)
    gateway._hourly_exposure = [(scenario.t0 + off, usd)
                                for off, usd in scenario.hourly_exposure]

    return engine, gateway, captured


def set_relay_table(scenario: Scenario):
    if scenario.table_cells is not None:
        relay_delta._TABLE = dict(scenario.table_cells)
        relay_delta._LOADED = True
    else:
        relay_delta._TABLE = {}
        relay_delta._LOADED = False


def port_state_for(scenario: Scenario, now_fn) -> Tuple[port.FH8State, port.FH8Stats]:
    state = port.FH8State(now_fn=now_fn)
    state.traded = set(scenario.traded)
    state.hourly_exposure = [(scenario.t0 + off, usd)
                             for off, usd in scenario.hourly_exposure]
    stats = port.FH8Stats(
        lane_losses_recent=lambda lane, window: scenario.lane_losses.get(lane, 0),
        h8_probe_lifetime=lambda: dict(scenario.h8_lifetime),
        h8_probe_daily=lambda: {"at_risk": scenario.h8_daily_at_risk},
    )
    return state, stats


# ---------------------------------------------------------------------------
# Runners
# ---------------------------------------------------------------------------
def eval_key(res, passed=None) -> tuple:
    """Comparison key for an evaluation outcome."""
    if res is None:
        return ("PASS_NONE",)
    allowed = res.allowed if passed is None else passed
    return (allowed, res.lane, res.side, res.cost_cents, res.cost_exact,
            res.reject_code, res.why_tag)


def run_live_ladder(scenario: Scenario) -> Optional[tuple]:
    fake = FakeTime(scenario.t0)
    engine, gateway, captured = build_live(scenario, fake)
    result = engine._run_watch_ladder(None, scenario.ticker, scenario.close_ts, {})
    if result is None:
        return None
    eval_result = result[0]
    return eval_key(eval_result)


def run_port_ladder(scenario: Scenario) -> Optional[tuple]:
    set_relay_table(scenario)
    fake = FakeTime(scenario.t0)
    state, stats = port_state_for(scenario, now_fn=lambda: fake.now)
    ladder = port.WatchLadder()
    while True:
        secs_left = scenario.close_ts - fake.now
        book = port.TouchBook(**scenario.tape(fake.now))
        r = ladder.tick(secs_left, book)
        if r == "EXIT":
            return None
        if r == "CONFIRMED":
            res = port.ladder_confirm_decision(
                ladder, scenario.ticker, book, secs_left, scenario.cash,
                scenario.spot, scenario.boundary_lo, scenario.boundary_hi,
                state=state, stats=stats)
            if res is not None:
                return eval_key(res)
        fake.sleep(POLL)


def run_live_evaluate(scenario: Scenario, secs_to_expiry: float) -> tuple:
    fake = FakeTime(scenario.t0)
    engine, gateway, captured = build_live(scenario, fake)
    book_kw = scenario.tape(fake.now)
    book = sys.modules["live_kw.kalshi"].Book(**book_kw)
    res = gateway.evaluate(scenario.ticker, book, secs_to_expiry, scenario.cash,
                           spot=scenario.spot, boundary_lo=scenario.boundary_lo,
                           boundary_hi=scenario.boundary_hi)
    return eval_key(res)


def run_port_evaluate(scenario: Scenario, secs_to_expiry: float) -> tuple:
    set_relay_table(scenario)
    fake = FakeTime(scenario.t0)
    state, stats = port_state_for(scenario, now_fn=lambda: fake.now)
    book = port.TouchBook(**scenario.tape(fake.now))
    res = port.evaluate(scenario.ticker, book, secs_to_expiry, scenario.cash,
                        spot=scenario.spot, boundary_lo=scenario.boundary_lo,
                        boundary_hi=scenario.boundary_hi, state=state, stats=stats)
    return eval_key(res)


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------
T0 = 1_752_000_000.0  # fixed epoch anchor for all tapes (determinism)


def _cells(wub_near: float, wub_far: float = 0.0005):
    """A small delta table: near cells risky (wub_near), far cells clean."""
    cells = {}
    for d in range(50, 2001, 5):
        for t in [10, 30, 60, 120, 180, 300, 600, 900]:
            wub = wub_near if d <= 200 else wub_far
            cells[(d, t, "ALL")] = {"p_cross": wub / 2, "n": 1000,
                                    "effective_n": 500, "wilson_ub": wub}
    return cells


def ladder_scenarios() -> List[Scenario]:
    s = []
    close = T0 + 900  # ladder starts at T-900

    # 1. Clean tier-0 pass: 99c favorite holds 9 confirms
    s.append(Scenario("tier0_clean_pass", close, T0,
                      constant_tape(book_frame(yes_bid=99, no_bid=1))))
    # 2. Holds at 98c in tier 0 (below 99 floor) -> no tier-0 pass; passes in tier 1
    s.append(Scenario("tier0_floor_dropout_tier1_pass", close, T0,
                      constant_tape(book_frame(yes_bid=98, no_bid=2))))
    # 3. 97c only -> passes first in tier 2
    s.append(Scenario("tier2_pass_97", close, T0,
                      constant_tape(book_frame(yes_bid=97, no_bid=3))))
    # 4. 94c always -> never in main band -> ladder never passes
    s.append(Scenario("below_band_never", close, T0,
                      constant_tape(book_frame(yes_bid=94, no_bid=6))))
    # 5. Side flip mid-tier resets confirms
    s.append(Scenario("side_flip_reset", close, T0, schedule_tape(T0, [
        (0, book_frame(yes_bid=99, no_bid=1)),
        (20, book_frame(yes_bid=1, no_bid=99)),
        (40, book_frame(yes_bid=99, no_bid=1)),
    ])))
    # 6. Counterparty vanishes (R2): favorite yes, no_bid None
    s.append(Scenario("r2_counterparty_vanish", close, T0, schedule_tape(T0, [
        (0, book_frame(yes_bid=99, no_bid=1)),
        (15, book_frame(yes_bid=99, no_bid=None)),
        (60, book_frame(yes_bid=99, no_bid=1)),
    ])))
    # 7. Book vanishes entirely mid-tier
    s.append(Scenario("book_vanish", close, T0, schedule_tape(T0, [
        (0, book_frame(yes_bid=99, no_bid=1)),
        (25, book_frame()),
        (50, book_frame(yes_bid=99, no_bid=1)),
    ])))
    # 8. fp-string book (99.40c) — Decimal path
    s.append(Scenario("fp_string_subpenny", close, T0,
                      constant_tape(book_frame(yes_bid=99, no_bid=1,
                                               yes_fp="0.9940", no_fp="0.0060"))))
    # 9. NO side favorite clean pass
    s.append(Scenario("no_side_pass", close, T0,
                      constant_tape(book_frame(yes_bid=1, no_bid=99))))
    # 10. Flicker below floor every few ticks (dropout resets)
    s.append(Scenario("floor_flicker", close, T0, schedule_tape(T0, [
        (0, book_frame(yes_bid=99, no_bid=1)),
        (30, book_frame(yes_bid=98, no_bid=2)),
        (35, book_frame(yes_bid=99, no_bid=1)),
        (65, book_frame(yes_bid=98, no_bid=2)),
        (70, book_frame(yes_bid=99, no_bid=1)),
    ])))
    # 11. Top-rung guard FAIL (table loaded, near cells over gate)
    s.append(Scenario("toprung_guard_fail", close, T0,
                      constant_tape(book_frame(yes_bid=99, no_bid=1)),
                      spot=65_000.0, boundary_lo=64_900.0, boundary_hi=64_950.0,
                      table_cells=_cells(wub_near=0.9)))
    # 12. Top-rung guard PASS with low-tail tag (far distance, tiny wub)
    s.append(Scenario("toprung_lowtail_tag", close, T0,
                      constant_tape(book_frame(yes_bid=99, no_bid=1)),
                      spot=65_000.0, boundary_lo=63_000.0, boundary_hi=63_500.0,
                      table_cells=_cells(wub_near=0.9)))
    # 13. Hourly cap already consumed -> evaluate rejects at confirm, ladder keeps watching
    s.append(Scenario("hourly_cap_blocks", close, T0,
                      constant_tape(book_frame(yes_bid=99, no_bid=1)),
                      hourly_exposure=((-100, 3.90),)))
    # 14. Lane F killed (3 recent losses)
    s.append(Scenario("lane_f_killed", close, T0,
                      constant_tape(book_frame(yes_bid=99, no_bid=1)),
                      lane_losses={"F": 3}))
    # 15. Already traded (single entry)
    s.append(Scenario("single_entry_blocks", close, T0,
                      constant_tape(book_frame(yes_bid=99, no_bid=1)),
                      traded=frozenset({("F", "KXBTC15M-TAPE")})))
    # 16. Cross-lane cap full
    s.append(Scenario("cross_lane_cap_blocks", close, T0,
                      constant_tape(book_frame(yes_bid=99, no_bid=1)),
                      traded=frozenset({("H8", "KXBTC15M-TAPE"),
                                        ("D", "KXBTC15M-TAPE"),
                                        ("MM", "KXBTC15M-TAPE")})))
    # 17. Tier transition: 99c through tier 0, drops to 98 exactly at tier 1
    s.append(Scenario("tier_transition", close, T0, schedule_tape(T0, [
        (0, book_frame(yes_bid=99, no_bid=1)),
        (299, book_frame(yes_bid=98, no_bid=2)),
    ])))
    # 18. Low balance blocks at confirm
    s.append(Scenario("low_balance", close, T0,
                      constant_tape(book_frame(yes_bid=99, no_bid=1)), cash=0.50))
    # 19. Ladder joined late (T-400, tier 1 direct)
    s.append(Scenario("late_join_tier1", T0 + 400, T0,
                      constant_tape(book_frame(yes_bid=98, no_bid=2))))
    # 20. 100c book (clamps? cost 100 > band hi) — never passes band
    s.append(Scenario("cost_100_oob", close, T0,
                      constant_tape(book_frame(yes_bid=100, no_bid=1))))
    # 21-24. Floor edges: one cent BELOW each tier floor — a floor off-by-one
    # in either implementation diverges here (mutation coverage).
    s.append(Scenario("floor_edge_96_tier2", close, T0,
                      constant_tape(book_frame(yes_bid=96, no_bid=4))))
    s.append(Scenario("floor_edge_95_all_tiers", close, T0,
                      constant_tape(book_frame(yes_bid=95, no_bid=5))))
    s.append(Scenario("floor_edge_97_tier1", T0 + 600, T0,  # joins at tier-1 start
                      constant_tape(book_frame(yes_bid=97, no_bid=3))))
    s.append(Scenario("floor_edge_98_tier0", close, T0,
                      constant_tape(book_frame(yes_bid=98, no_bid=2))))
    # 25-26. Confirm-count edges: hold exactly confirms-1 ticks, flip side one
    # tick, return — a confirms off-by-one diverges here.
    s.append(Scenario("confirm_edge_tier2", T0 + 300, T0, schedule_tape(T0, [
        (0, book_frame(yes_bid=97, no_bid=3)),      # T-300 tier2 needs 3 confirms
        (10, book_frame(yes_bid=3, no_bid=97)),     # flip on the 3rd tick
        (15, book_frame(yes_bid=97, no_bid=3)),
    ])))
    s.append(Scenario("confirm_edge_tier1", T0 + 600, T0, schedule_tape(T0, [
        (0, book_frame(yes_bid=98, no_bid=2)),      # tier1 needs 6 confirms
        (25, book_frame(yes_bid=2, no_bid=98)),     # flip on the 6th tick
        (30, book_frame(yes_bid=98, no_bid=2)),
    ])))
    return s


def evaluate_grid() -> List[Tuple[Scenario, float]]:
    """Cartesian sweep for the final-window evaluate comparison."""
    cases: List[Tuple[Scenario, float]] = []
    books = [
        book_frame(yes_bid=95, no_bid=5), book_frame(yes_bid=99, no_bid=1),
        book_frame(yes_bid=94, no_bid=6), book_frame(yes_bid=80, no_bid=20),
        book_frame(yes_bid=79, no_bid=21), book_frame(yes_bid=100, no_bid=1),
        book_frame(yes_bid=97, no_bid=3, yes_fp="0.9725", no_fp="0.0275"),
        book_frame(yes_bid=94, no_bid=6, yes_fp="0.9499", no_fp="0.0501"),
        book_frame(yes_bid=1, no_bid=99), book_frame(yes_bid=20, no_bid=80),
        book_frame(yes_bid=50, no_bid=50), book_frame(),
        book_frame(yes_bid=99, no_bid=None), book_frame(yes_bid=None, no_bid=88),
    ]
    times = [45.0, 59.0, 60.0, 61.0, 179.0]
    spots = [(65_000.0, 64_000.0, 64_500.0),   # far distance (>0.15%)
             (65_000.0, 64_960.0, 64_990.0),   # near (<0.15%)
             (None, None, None)]               # no spot
    stats_variants = [
        {},
        {"lane_losses": {"F": 3}},
        {"lane_losses": {"H8": 3}},
        {"h8_lifetime": {"wins": 0, "losses": 3}},
        {"h8_lifetime": {"wins": 1, "losses": 3}},
        {"h8_daily_at_risk": 1.95},
        {"traded": frozenset({("F", "KXBTC15M-TAPE")})},
        {"traded": frozenset({("H8", "KXBTC15M-TAPE")})},
        {"traded": frozenset({("F", "KXBTC15M-TAPE"), ("D", "KXBTC15M-TAPE"),
                              ("MM", "KXBTC15M-TAPE")})},
        {"hourly_exposure": ((-10, 3.20),)},
        {"cash": 4.99},
        {"cash": 0.80},
        {"table_cells": _cells(wub_near=0.005)},
        {"table_cells": _cells(wub_near=0.5)},
    ]
    i = 0
    for b in books:
        for t in times:
            for spot, blo, bhi in spots:
                for sv in stats_variants:
                    i += 1
                    kw = dict(sv)
                    cash = kw.pop("cash", 100.0)
                    s = Scenario(f"grid_{i}", T0 + t, T0, constant_tape(b),
                                 cash=cash, spot=spot, boundary_lo=blo,
                                 boundary_hi=bhi, **kw)
                    cases.append((s, t))
    # wrong ticker family
    cases.append((Scenario("wrong_family", T0 + 60, T0,
                           constant_tape(book_frame(yes_bid=99, no_bid=1)),
                           ticker="KXETH15M-TAPE"), 60.0))
    return cases


# ---------------------------------------------------------------------------
# Comparison driver
# ---------------------------------------------------------------------------
def compare_all() -> dict:
    ladder_results = []
    for sc in ladder_scenarios():
        live = run_live_ladder(sc)
        ported = run_port_ladder(sc)
        ladder_results.append((sc.name, live, ported, live == ported))

    eval_results = []
    for sc, t in evaluate_grid():
        live = run_live_evaluate(sc, t)
        ported = run_port_evaluate(sc, t)
        eval_results.append((sc.name, live, ported, live == ported))

    # restore relay delta module state
    relay_delta._TABLE = {}
    relay_delta._LOADED = False

    mismatches = ([r for r in ladder_results if not r[3]]
                  + [r for r in eval_results if not r[3]])
    return {
        "ladder_total": len(ladder_results),
        "eval_total": len(eval_results),
        "ladder_results": ladder_results,
        "eval_results": eval_results,
        "mismatches": mismatches,
    }


def write_report(result: dict, path: Path) -> None:
    lines = [
        "# GOLDEN-TAPE REGRESSION REPORT — F / H8 port (gate 5)",
        "",
        "Live side: `reference/live_k_worker/{engine,gateway,delta_table_loader}.py`",
        "(vendored byte-identical from B1, I/O stubbed, clock faked).",
        "New side: `relay_engine/lane_fh8.py` + `relay_engine/delta.py`.",
        "Comparison key per frame: (passed, lane, side, cost_cents, cost_exact,",
        "reject_code, why_tag). Free-text reject_reason excluded (declared change D1).",
        "",
        f"## Result: {'GREEN' if not result['mismatches'] else 'RED'}",
        "",
        f"- Watch-ladder tapes: {result['ladder_total']} scenarios, "
        f"{sum(1 for r in result['ladder_results'] if r[3])} identical",
        f"- Final-window evaluate sweep: {result['eval_total']} cases, "
        f"{sum(1 for r in result['eval_results'] if r[3])} identical",
        f"- Mismatches: {len(result['mismatches'])}",
        "",
        "## Watch-ladder tape outcomes",
        "",
        "| tape | live verdict | port verdict | identical |",
        "|---|---|---|---|",
    ]
    for name, live, ported, ok in result["ladder_results"]:
        lv = "Pass(no entry)" if live is None else f"`{live}`"
        pv = "Pass(no entry)" if ported is None else f"`{ported}`"
        lines.append(f"| {name} | {lv} | {pv} | {'✓' if ok else '✗ MISMATCH'} |")
    lines += ["", "## Evaluate sweep", "",
              f"{result['eval_total']} grid cases (books × times × spot/boundary × "
              f"evidence-state × table-state). All compared on the full key.", ""]
    if result["mismatches"]:
        lines += ["## MISMATCH DETAIL", ""]
        for name, live, ported, _ in result["mismatches"]:
            lines += [f"- **{name}**", f"  - live: `{live}`", f"  - port: `{ported}`"]
    else:
        lines += ["No mismatches. Ladder + gates are decision-identical on this corpus.", ""]
    path.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    result = compare_all()
    write_report(result, ROOT / "docs" / "GOLDEN_TAPE_REPORT.md")
    print(f"ladder: {result['ladder_total']} eval: {result['eval_total']} "
          f"mismatches: {len(result['mismatches'])}")
    for m in result["mismatches"][:20]:
        print("MISMATCH", m[0])
        print("  live:", m[1])
        print("  port:", m[2])
