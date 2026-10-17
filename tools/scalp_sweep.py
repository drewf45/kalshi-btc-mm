"""scalp_sweep — which (target, soft_stop, hard_stop) dials MAXIMIZE realized
EV per Lane A window on Drew's REAL settled crypto markets?

READ-ONLY. GET-only Kalshi reads (settled-market listing + per-market
candlesticks) reusing tools/reversion_test.py's data layer verbatim; never
places/amends/cancels an order, never writes engine state. Lives in tools/ for
that reason (see tools/__init__).

THE DOCTRINE UNDER TEST (Drew, 2026-10-16):
  The current ROT dials are ROT_TARGET_CENTS=8 / ROT_STOP_CENTS=10 /
  ROT_HARD_STOP_CENTS=20 (relay_engine/config.py). Drew's call: that is a WILD,
  ASYMMETRIC swing — a -20c tail loss against a +8c win is a losing trade even at
  a decent hit rate. He wants SMALL measured wins (1-3c) at a HIGH hit rate with
  TIGHT, roughly-symmetric stops (defense == offense). This tool SWEEPS the dial
  grid against the real traded path and RANKS every combo by EV per window, so we
  pick the disciplined small-scalp dials from DATA, not a guess.

THE SIMULATED PLAY (Lane A round-trip, one YES-side maker bet per window):
  ENTRY: the first minute inside the first ENTRY_FRAC of the window whose yes_bid
  is in [ENTRY_LO, ENTRY_HI] (~50c straddle, both sides present). The modelled
  fill is a maker BUY resting at that minute's yes_bid, cost in cents. (Identical
  entry model to reversion_test — YES-bid/ask candlesticks give the cleaner path;
  a symmetric NO read is out of scope.)

  EXIT, walked minute-by-minute AFTER entry, time-ordered, first rule to fire:
    hard stop — yes_bid LOW <= entry - hard_stop in a minute  -> EXIT at
                (entry - hard_stop), the worse of the breached prices; mirrors
                the engine's "ANY read" hard stop (lane_rot.py:510,598).
    soft stop — yes_bid LOW <= entry - soft_stop in a minute   -> EXIT at
                (entry - soft_stop). (The live engine demands confirmation reads
                + a spot move; a per-minute candlestick cannot see those, so this
                is the OPTIMISTIC-for-the-stop reading: we take the stop the
                instant the low breaches. That makes losses no SMALLER than real.)
    target    — yes_bid HIGH >= entry + target in a minute     -> CASH OUT at
                (entry + target). Credited ONLY when the bid HIGH traded through
                the resting sell (a real bid reached up through it), never on a
                mid/ask touch. Same conservative fill as reversion_test.
    none      — no rule fired by window end  -> mark to the LAST observed yes_bid
                (close of the final in-window minute). We do NOT credit the 0/100
                settlement outcome — marking to the last traded bid is the
                conservative, path-only read (a held favorite is not a sure win).
    SAME-MINUTE TIE-BREAK: within one minute the sub-minute order is unknown; a
    minute that shows BOTH a target-reach high AND a stop-breach low is counted
    STOP-FIRST (pessimistic — the loss you would have taken). hard stop beats
    soft stop in the same minute.

  P&L per window (cents, maker both legs): exit_price - entry_cost.
    win  = +target          (target fired, no stop first)
    loss = -soft/-hard      (that stop fired first)
    flat = last_bid - entry (neither fired; marked out, usually small)

THE GRID (only combos with target <= soft_stop <= hard_stop are kept; the rest
are nonsensical for a symmetric-defense doctrine and skipped):
    target    in {1, 2, 3, 4, 6, 8}
    soft_stop in {2, 3, 4, 6, 10}
    hard_stop in {4, 6, 8, 12, 20}
The live baseline 8/10/20 is injected into the grid explicitly and flagged in the
ranking so the improvement from tighter dials is visible. The best combo whose
target <= 3 (Drew's stated small-win preference) is flagged separately.

HONEST CAVEATS (read before trusting a number):
  * MAKER-FILL ASSUMPTION — a cash-out is credited only when the yes_bid HIGH
    traded through the resting sell, with NO queue/partial/size model. A thin
    one-lot bid that merely printed the price is credited here, so every win rate
    and every EV is an UPPER BOUND on what a real maker achieves.
  * CONSERVATIVE TIE-BREAKS — same-minute target+stop counts STOP-FIRST; the
    soft stop is taken the instant a minute's low breaches it (no confirmation /
    spot-move gate the live engine enforces). Both push realized EV DOWN vs this
    model's, i.e. the numbers here still flatter the winners.
  * CANDLESTICK GRANULARITY — one candle per minute; the intra-minute price path
    (and thus the true order of a same-minute target and stop) is unknown.
  * FEES NOT MODELLED — Lane A exits are maker (resting cash-out / post-only
    stop), maker fee ~0 on these markets, so an unmodelled fee is ~0 for the
    cash-out leg. A hard-stop CUT is an IOC taker on the live desk and WOULD pay a
    taker fee — not modelled here, so hard-stop losses are understated.
  * SETTLEMENT NOT USED TO FLATTER — a window held to the end is marked to its
    last traded bid, never the $1/$0 settle.

CORRELATED-ENTRY FINDING (second deliverable): with --series ALL (or any run
covering >1 crypto series) the report aligns windows by CLOSE TIME across series
and, for the best disciplined combo, counts how often the desk's same-side
entries in one time slot ALL WIN vs ALL LOSE together — the clustering that
justifies a correlated-position cap. Single-series runs note it as N/A.

CLI:
  python -m tools.scalp_sweep --series KXBTC15M --days 5
  python -m tools.scalp_sweep --series ALL --days 3 --json
"""

import argparse
import itertools
import json
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

# reuse the proven GET-only data layer verbatim (pull + parse + enumerate)
from tools.reversion_test import (
    _cents,
    _iso_ts,
    list_settled_markets,
    pull_candles,
    SLEEP_BETWEEN_CALLS_S,
)

SERIES_DEFAULT = "KXBTC15M"
ALL_SERIES = ("KXBTC15M", "KXETH15M", "KXXRP15M", "KXSOL15M")

# entry model — identical band to reversion_test / lane_rot's 40-54 doctrine
ENTRY_LO = 40          # cents — entry only when the yes_bid is in [ENTRY_LO, ENTRY_HI]
ENTRY_HI = 56
ENTRY_FRAC = 0.30      # entry must land in the first 30% of the window

# the live baseline, always injected + flagged
BASELINE = (8, 10, 20)

# the sweep grid
TARGETS = (1, 2, 3, 4, 6, 8)
SOFT_STOPS = (2, 3, 4, 6, 10)
HARD_STOPS = (4, 6, 8, 12, 20)

SMALL_WIN_MAX_TARGET = 3   # Drew's stated preference: target <= 3c
THIN_N = 30


def valid_combos() -> List[Tuple[int, int, int]]:
    """All (target, soft, hard) with target <= soft <= hard, plus the baseline."""
    combos = set()
    for t, s, h in itertools.product(TARGETS, SOFT_STOPS, HARD_STOPS):
        if t <= s <= h:
            combos.add((t, s, h))
    combos.add(BASELINE)  # inject the live dials even though 8>none-of-grid-soft
    return sorted(combos)


# ── per-window path, parsed ONCE then replayed against every combo ───────────

class WindowPath:
    """One window's entry + the ordered post-entry minute path (bid hi/lo/close).
    Parsed once; every grid combo is simulated against this cheap structure so we
    pull each market's candlesticks exactly once."""
    __slots__ = ("ticker", "series", "close_ts", "entry_cost", "steps")

    def __init__(self, ticker: str, series: str, close_ts: int, entry_cost: int):
        self.ticker = ticker
        self.series = series
        self.close_ts = close_ts          # window close (for cross-series alignment)
        self.entry_cost = entry_cost
        # ordered list of (bid_high, bid_low, bid_close) AFTER the entry minute
        self.steps: List[Tuple[Optional[int], Optional[int], Optional[int]]] = []


def build_path(ticker: str, series: str, candles: List[dict], open_ts: int,
               close_ts: int) -> Optional[WindowPath]:
    """Parse one window into a WindowPath, or None when no early in-band entry.
    Same entry logic as reversion_test.measure_window."""
    rows = []
    for c in candles:
        ep = c.get("end_period_ts")
        if not isinstance(ep, (int, float)):
            continue
        if ep < open_ts or ep > close_ts + 1:
            continue
        yb = c.get("yes_bid") or {}
        ya = c.get("yes_ask") or {}
        rows.append({
            "ts": int(ep),
            "bid_open": _cents(yb.get("open_dollars")),
            "bid_high": _cents(yb.get("high_dollars")),
            "bid_low": _cents(yb.get("low_dollars")),
            "bid_close": _cents(yb.get("close_dollars")),
            "ask_open": _cents(ya.get("open_dollars")),
        })
    rows.sort(key=lambda r: r["ts"])
    if len(rows) < 3:
        return None

    window_s = close_ts - open_ts
    entry_deadline = open_ts + window_s * ENTRY_FRAC

    entry_idx = None
    entry_cost = None
    for i, r in enumerate(rows):
        if r["ts"] > entry_deadline:
            break
        bid = r["bid_open"] if r["bid_open"] is not None else r["bid_close"]
        ask = r["ask_open"]
        if bid is None:
            continue
        if ENTRY_LO <= bid <= ENTRY_HI and (ask is None or ENTRY_LO <= ask <= 100):
            entry_idx = i
            entry_cost = bid
            break
    if entry_idx is None or entry_cost is None:
        return None

    path = WindowPath(ticker, series, close_ts, entry_cost)
    for r in rows[entry_idx + 1:]:
        path.steps.append((r["bid_high"], r["bid_low"], r["bid_close"]))
    return path


# ── the exit simulation for one combo on one path ───────────────────────────

# outcome codes
WIN, SOFT, HARD, FLAT = "win", "soft", "hard", "flat"


def simulate(path: WindowPath, target: int, soft_stop: int,
             hard_stop: int) -> Tuple[str, int]:
    """Walk the post-entry path applying the exit rule. Returns (outcome, pnl_c).
    Tie-breaks per module header: hard > soft > target within one minute; a
    terminal mark-out uses the last observed bid (never settlement)."""
    entry = path.entry_cost
    last_bid = entry
    for hi, lo, close in path.steps:
        if close is not None:
            last_bid = close
        elif hi is not None:
            last_bid = hi
        # evaluate the three triggers for THIS minute, worst-first (pessimistic)
        if lo is not None and lo <= entry - hard_stop:
            return HARD, -hard_stop
        if lo is not None and lo <= entry - soft_stop:
            return SOFT, -soft_stop
        if hi is not None and hi >= entry + target:
            return WIN, target
    # neither fired — mark to the last traded bid (conservative, path-only)
    return FLAT, last_bid - entry


# ── aggregation across all windows for one combo ────────────────────────────

class ComboStat:
    __slots__ = ("target", "soft_stop", "hard_stop", "n", "wins", "softs",
                 "hards", "flats", "win_c", "loss_c", "net_c", "flat_c")

    def __init__(self, t, s, h):
        self.target, self.soft_stop, self.hard_stop = t, s, h
        self.n = 0
        self.wins = self.softs = self.hards = self.flats = 0
        self.win_c = 0      # sum of winning pnl (positive)
        self.loss_c = 0     # sum of losing pnl (negative; soft+hard)
        self.flat_c = 0     # sum of flat pnl (signed)
        self.net_c = 0      # sum of all pnl

    def add(self, outcome: str, pnl: int) -> None:
        self.n += 1
        self.net_c += pnl
        if outcome == WIN:
            self.wins += 1
            self.win_c += pnl
        elif outcome == SOFT:
            self.softs += 1
            self.loss_c += pnl
        elif outcome == HARD:
            self.hards += 1
            self.loss_c += pnl
        else:
            self.flats += 1
            self.flat_c += pnl

    @property
    def stops(self) -> int:
        return self.softs + self.hards

    @property
    def win_rate(self) -> float:
        return self.wins / self.n if self.n else 0.0

    @property
    def stop_rate(self) -> float:
        return self.stops / self.n if self.n else 0.0

    @property
    def avg_win(self) -> float:
        return self.win_c / self.wins if self.wins else 0.0

    @property
    def avg_loss(self) -> float:
        return self.loss_c / self.stops if self.stops else 0.0

    @property
    def loss_win_ratio(self) -> Optional[float]:
        if self.avg_win == 0:
            return None
        return abs(self.avg_loss) / self.avg_win

    @property
    def ev_per_window(self) -> float:
        return self.net_c / self.n if self.n else 0.0

    @property
    def net_dollars(self) -> float:
        # one contract per window, cents -> dollars
        return self.net_c / 100.0

    def as_dict(self) -> dict:
        return {
            "target": self.target, "soft_stop": self.soft_stop,
            "hard_stop": self.hard_stop, "n": self.n,
            "win_pct": self.win_rate, "stop_pct": self.stop_rate,
            "wins": self.wins, "softs": self.softs, "hards": self.hards,
            "flats": self.flats, "avg_win_c": self.avg_win,
            "avg_loss_c": self.avg_loss, "loss_win_ratio": self.loss_win_ratio,
            "ev_per_window_c": self.ev_per_window, "net_dollars": self.net_dollars,
        }


def sweep(paths: List[WindowPath]) -> List[ComboStat]:
    """Simulate every valid combo across every window path; rank by EV desc."""
    stats: List[ComboStat] = []
    for t, s, h in valid_combos():
        cs = ComboStat(t, s, h)
        for p in paths:
            outcome, pnl = simulate(p, t, s, h)
            cs.add(outcome, pnl)
        stats.append(cs)
    stats.sort(key=lambda c: (c.ev_per_window, c.win_rate), reverse=True)
    return stats


# ── correlated-entry finding ─────────────────────────────────────────────────

def correlated_entry(by_series: Dict[str, List[WindowPath]],
                     combo: Tuple[int, int, int]) -> Optional[dict]:
    """For the given combo, align windows across series by close time (minute
    bucket) and, among slots where >=2 series had an entry, count how often ALL
    entries won together vs ALL lost (stop) together vs mixed. All-same-direction
    clustering is what a correlated-position cap defends against."""
    if len(by_series) < 2:
        return None
    t, s, h = combo
    # slot key = close_ts rounded to the minute (all 15M windows share a grid)
    slots: Dict[int, List[str]] = {}
    for series, paths in by_series.items():
        for p in paths:
            outcome, _ = simulate(p, t, s, h)
            if outcome in (WIN, SOFT, HARD):   # a decisive directional exit
                slots.setdefault(int(round(p.close_ts / 60) * 60), []).append(
                    WIN if outcome == WIN else "loss")
    multi = {k: v for k, v in slots.items() if len(v) >= 2}
    if not multi:
        return None
    all_win = all_loss = mixed = 0
    for v in multi.values():
        if all(x == WIN for x in v):
            all_win += 1
        elif all(x == "loss" for x in v):
            all_loss += 1
        else:
            mixed += 1
    n = len(multi)
    return {
        "combo": {"target": t, "soft_stop": s, "hard_stop": h},
        "multi_series_slots": n,
        "all_win": all_win, "all_loss": all_loss, "mixed": mixed,
        "all_win_rate": all_win / n, "all_loss_rate": all_loss / n,
        "mixed_rate": mixed / n,
    }


# ── report ───────────────────────────────────────────────────────────────────

def _pct(x) -> str:
    return f"{x*100:5.1f}%"


def _lwr(cs: ComboStat) -> str:
    r = cs.loss_win_ratio
    return f"{r:4.2f}" if r is not None else "  - "


def _combo_row(cs: ComboStat, tag: str = "") -> str:
    return (f"  {tag:3s}T{cs.target:<2d}/S{cs.soft_stop:<2d}/H{cs.hard_stop:<2d} "
            f"N={cs.n:4d}  win {_pct(cs.win_rate)}  stop {_pct(cs.stop_rate)}  "
            f"Wavg {cs.avg_win:+4.1f}c  Lavg {cs.avg_loss:+5.1f}c  L/W {_lwr(cs)}  "
            f"EV {cs.ev_per_window:+5.2f}c  net ${cs.net_dollars:+7.2f}")


def print_report(stats: List[ComboStat], by_series: Dict[str, List[WindowPath]],
                 args, meta: dict) -> None:
    all_paths = [p for ps in by_series.values() for p in ps]
    print("=" * 96)
    print("  SCALP SWEEP — the (target / soft-stop / hard-stop) dials that MAXIMIZE EV per window")
    print("=" * 96)
    print(f"  series={','.join(by_series)}  days={args.days}  "
          f"entry_band={ENTRY_LO}-{ENTRY_HI}c  entry_frac={ENTRY_FRAC:.0%}  "
          f"windows={len(all_paths)}")
    print(f"  grid: target{list(TARGETS)}  soft{list(SOFT_STOPS)}  hard{list(HARD_STOPS)}"
          f"  (kept: target<=soft<=hard, +baseline)")
    print(f"  markets_listed={meta['listed']}  measured={meta['measured']}  "
          f"skipped_no_entry={meta['skipped']}")
    print("  FILL MODEL (UPPER BOUND): cash-out credited only when yes_bid HIGH traded")
    print("  through the sell; same-minute target+stop => STOP-FIRST; soft stop taken on")
    print("  first breach (no confirm gate); held-to-end marked to LAST bid, NOT settlement;")
    print("  maker fees ~0 (cash-out leg), hard-stop taker fee NOT modelled -> real EV lower.")
    if len(all_paths) < THIN_N:
        print(f"  WARN: THIN sample (N<{THIN_N}); treat the ranking as directional only.")
    print("=" * 96)

    print(f"\n  RANKED BY EV PER WINDOW (top {min(len(stats), args.top)} of {len(stats)} combos)")
    print("  " + "-" * 92)
    baseline_cs = next((c for c in stats if (c.target, c.soft_stop, c.hard_stop) == BASELINE), None)
    best_cs = stats[0] if stats else None
    for i, cs in enumerate(stats[:args.top]):
        tag = ""
        if (cs.target, cs.soft_stop, cs.hard_stop) == BASELINE:
            tag = "!"   # the live baseline
        print(f"{i+1:3d}." + _combo_row(cs, tag))

    # baseline row (always shown, even if outside the top N)
    print("\n  LIVE BASELINE (current ROT dials 8/10/20):")
    if baseline_cs is not None:
        rank = next((i + 1 for i, c in enumerate(stats)
                     if (c.target, c.soft_stop, c.hard_stop) == BASELINE), None)
        print(_combo_row(baseline_cs, "!") + f"   <- rank {rank}/{len(stats)}")
    else:
        print("    (baseline produced no measurable windows)")

    # best small-win combo (target <= 3) — Drew's stated preference
    small = [c for c in stats if c.target <= SMALL_WIN_MAX_TARGET]
    best_small = small[0] if small else None
    print(f"\n  BEST SMALL-WIN COMBO (Drew's preference, target<={SMALL_WIN_MAX_TARGET}c):")
    if best_small is not None:
        rank = next((i + 1 for i, c in enumerate(stats)
                     if c is best_small), None)
        print(_combo_row(best_small, "*") + f"   <- rank {rank}/{len(stats)}")
    else:
        print("    (no small-win combo measurable)")

    # correlated-entry finding
    print("\n  CORRELATED-ENTRY FINDING (same-side clustering across series):")
    ref_combo = (best_small.target, best_small.soft_stop, best_small.hard_stop) \
        if best_small else (best_cs.target, best_cs.soft_stop, best_cs.hard_stop)
    ce = correlated_entry(by_series, ref_combo)
    if ce is None:
        print(f"    N/A — need >=2 series with overlapping windows "
              f"({len(by_series)} series this run). Re-run with --series ALL.")
    else:
        c = ce["combo"]
        print(f"    combo T{c['target']}/S{c['soft_stop']}/H{c['hard_stop']} across "
              f"{len(by_series)} series, {ce['multi_series_slots']} time-slots with >=2 entries:")
        print(f"      ALL WIN together  {_pct(ce['all_win_rate'])}  ({ce['all_win']})")
        print(f"      ALL LOSE together {_pct(ce['all_loss_rate'])}  ({ce['all_loss']})")
        print(f"      mixed             {_pct(ce['mixed_rate'])}  ({ce['mixed']})")
        print("    High all-win+all-lose (vs mixed) => entries are CORRELATED; a")
        print("    correlated-position cap limits the clustered drawdown.")

    print("\n" + "=" * 96)
    if best_cs is not None and baseline_cs is not None:
        delta = best_cs.ev_per_window - baseline_cs.ev_per_window
        print(f"  TOP EV combo T{best_cs.target}/S{best_cs.soft_stop}/H{best_cs.hard_stop}: "
              f"EV {best_cs.ev_per_window:+.2f}c/window  vs baseline {baseline_cs.ev_per_window:+.2f}c  "
              f"(delta {delta:+.2f}c/window)")
    if best_small is not None:
        print(f"  DISCIPLINED SCALP (target<={SMALL_WIN_MAX_TARGET}c): "
              f"T{best_small.target}/S{best_small.soft_stop}/H{best_small.hard_stop}  "
              f"win {_pct(best_small.win_rate)}  EV {best_small.ev_per_window:+.2f}c/window")
    print("  REMEMBER: every EV above is an UPPER BOUND (maker-fill + optimistic stop).")
    print("=" * 96)


# ── main ─────────────────────────────────────────────────────────────────────

def run(args) -> dict:
    from relay_engine import mac_main
    mac_main.prepare_environment()
    from relay_engine import venue
    client = venue.build_client()

    series_list = list(ALL_SERIES) if args.series.upper() == "ALL" else [args.series.upper()]
    since_ts = time.time() - args.days * 86400
    by_series: Dict[str, List[WindowPath]] = {}
    meta = {"listed": 0, "measured": 0, "skipped": 0}

    for series in series_list:
        paths: List[WindowPath] = []
        markets = list_settled_markets(client, series, since_ts)
        meta["listed"] += len(markets)
        if not args.json:
            print(f"[info] {series}: {len(markets)} settled markets since "
                  f"{datetime.fromtimestamp(since_ts, timezone.utc):%Y-%m-%d %H:%M}Z",
                  file=sys.stderr)
        for i, m in enumerate(markets):
            ticker = m.get("ticker")
            open_ts = _iso_ts(m.get("open_time"))
            close_ts = _iso_ts(m.get("close_time"))
            if not ticker or open_ts is None or close_ts is None:
                meta["skipped"] += 1
                continue
            if close_ts - open_ts < 600 or close_ts - open_ts > 1200:
                meta["skipped"] += 1   # not a ~15-minute window
                continue
            candles = pull_candles(client, series, ticker, open_ts, close_ts)
            time.sleep(SLEEP_BETWEEN_CALLS_S)
            path = build_path(ticker, series, candles, open_ts, close_ts)
            if path is None:
                meta["skipped"] += 1
                continue
            paths.append(path)
            meta["measured"] += 1
            if not args.json and (i + 1) % 25 == 0:
                print(f"[info] {series}: {i+1}/{len(markets)} markets, "
                      f"{len(paths)} measured", file=sys.stderr)
        by_series[series] = paths

    return {"by_series": by_series, "meta": meta}


def to_json(stats: List[ComboStat], by_series: Dict[str, List[WindowPath]],
            meta: dict, args) -> dict:
    baseline_cs = next((c for c in stats if (c.target, c.soft_stop, c.hard_stop) == BASELINE), None)
    small = [c for c in stats if c.target <= SMALL_WIN_MAX_TARGET]
    best_small = small[0] if small else None
    ref_combo = (best_small.target, best_small.soft_stop, best_small.hard_stop) \
        if best_small else ((stats[0].target, stats[0].soft_stop, stats[0].hard_stop) if stats else BASELINE)
    return {
        "params": {"series": args.series, "days": args.days,
                   "entry_band": [ENTRY_LO, ENTRY_HI], "entry_frac": ENTRY_FRAC,
                   "grid": {"target": list(TARGETS), "soft_stop": list(SOFT_STOPS),
                            "hard_stop": list(HARD_STOPS)}, "baseline": list(BASELINE)},
        "meta": meta,
        "windows_by_series": {k: len(v) for k, v in by_series.items()},
        "ranking": [c.as_dict() for c in stats],
        "baseline": baseline_cs.as_dict() if baseline_cs else None,
        "best_small_win": best_small.as_dict() if best_small else None,
        "correlated_entry": correlated_entry(by_series, ref_combo),
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="tools.scalp_sweep",
        description="Sweep (target, soft_stop, hard_stop) dials for max EV per Lane A window.")
    p.add_argument("--series", default=SERIES_DEFAULT,
                   help="KXBTC15M | KXETH15M | KXXRP15M | KXSOL15M | ALL (default KXBTC15M)")
    p.add_argument("--days", type=int, default=5, help="look-back window in days (default 5)")
    p.add_argument("--top", type=int, default=15, help="how many ranked combos to print (default 15)")
    p.add_argument("--json", action="store_true", help="emit a JSON report to stdout")
    args = p.parse_args(argv)

    data = run(args)
    stats = sweep([p for ps in data["by_series"].values() for p in ps])
    if args.json:
        print(json.dumps(to_json(stats, data["by_series"], data["meta"], args), indent=2))
    else:
        print_report(stats, data["by_series"], args, data["meta"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
