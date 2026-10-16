"""reversion_test — does Lane A's oscillation thesis hold on REAL candlesticks?

READ-ONLY. GET-only Kalshi reads (settled-market listing + per-market
candlesticks); never places/amends/cancels an order, never writes engine state.
Lives in tools/ for that reason (see tools/__init__).

THE THESIS UNDER TEST (Lane A / ROT, relay_engine/lane_rot.py):
  A maker buys near 50c EARLY, when a 15-minute crypto binary (KXBTC15M /
  KXETH15M / KXXRP15M, settles $1/$0) is unpriced; the price is hypothesized to
  OSCILLATE back toward ~50/50 (or far enough) BEFORE the end-game, letting a
  maker CASH OUT the swing at a profit (+target cents) WITHOUT holding to
  settlement. This tool measures whether that cash-out was actually reachable.

HOW IT MEASURES (candlesticks, one per minute — the venue CONFIRMED shape):
  Each minute carries price{open/high/low/close/mean} and the best yes_bid and
  yes_ask {open/high/low/close} for that minute, in `_dollars` strings.

  ENTRY: the FIRST minute inside the first ENTRY_FRAC of the window whose
  yes_bid/yes_ask straddle the entry band (ENTRY_LO..ENTRY_HI around 50c). The
  modelled fill is a maker BUY resting at that minute's yes_bid (a maker joins
  the bid). We enter on the YES side; the entry cost is that bid in cents. (A
  symmetric NO-side read is out of scope here — the question is reachability of
  the swing, and YES-bid/ask candlesticks give the cleaner path.)

  CASH-OUT reachability (the conservative fill model, stated loudly below): a
  maker resting a sell at price X (X = entry + target) is assumed FILLED in a
  minute only if that minute's yes_bid HIGH trades at or above X — i.e. a real
  bid reached up through the resting sell. We do NOT assume a fill merely because
  the mid/mean or the ask touched X. This is deliberately conservative: it counts
  only minutes where the HELD side's bid demonstrably traded through the target.

  STOP: a stop at entry - stop_cents is modelled HIT in a minute if that
  minute's yes_bid LOW fell to or below (entry - stop_cents) BEFORE the target's
  first reach (time-ordered). Reported alongside, not netted — this is a path
  study, not a P&L backtest.

  END-GAME: the final ENDGAME_MIN minutes are EXCLUDED from the cash-out search
  (Lane A exits BEFORE the end-game; the last minutes are Lane B's pile-on).

ASSUMPTIONS, EXPLICIT (this measures PRICE PATHS, not guaranteed fills):
  * A maker fill is credited ONLY when the yes_bid HIGH traded through the
    resting sell price. Queue position, partial fills and size are NOT modelled;
    a thin one-lot bid that merely printed the price would be credited here, so
    the real hit-rate is an UPPER bound on cash-out reachability.
  * Entry is credited at the yes_bid of the entry minute with no queue/slippage
    model — again optimistic on the entry side.
  * Candlestick highs/lows are per-minute extremes; sub-minute ordering within a
    single minute is unknown, so when the SAME minute shows both a target-reach
    high and a stop-breach low, that minute is counted as STOP-FIRST (the
    pessimistic tie-break — a stop you would have taken first).
  * Settlement outcome is NOT used to credit a cash-out — only the traded path.

CLI:
  python -m tools.reversion_test --series KXBTC15M --days 7 --endgame-min 3
  python -m tools.reversion_test --series KXBTC15M --days 3 --json
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Tuple

SERIES_DEFAULT = "KXBTC15M"
ALL_SERIES = ("KXBTC15M", "KXETH15M", "KXXRP15M")

# Entry model: the window is "unpriced" when the touch straddles ~50c.
ENTRY_LO = 40          # cents — entry only when the yes_bid is in [ENTRY_LO, ENTRY_HI]
ENTRY_HI = 60
ENTRY_FRAC = 0.20      # entry must land in the first 20% of the window
TARGETS = (4, 8, 12)   # cash-out targets, cents above entry
STOPS = (8, 10)        # stop distances, cents below entry
THIN_N = 30            # a bucket with fewer windows than this is flagged thin

# polite pacing between candlestick calls (the client has its own governor, but
# a read-only sweep of hundreds of markets should still be gentle)
SLEEP_BETWEEN_CALLS_S = 0.15


def _cents(dollar_str) -> Optional[int]:
    """A `_dollars` string -> integer cents, or None when absent/garbage."""
    if dollar_str is None:
        return None
    try:
        d = Decimal(str(dollar_str)) * 100
    except (InvalidOperation, ValueError, TypeError):
        return None
    # round to nearest cent (sub-penny prints exist; the band is cents)
    return int(d.to_integral_value())


def _iso_ts(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


# ── data pull (GET-only) ─────────────────────────────────────────

def list_settled_markets(client, series: str, since_ts: float,
                         log=print) -> List[dict]:
    """Every settled/finalized 15M market in this series that CLOSED at or after
    since_ts. Paginates /markets by cursor; stops a page early once a page's
    markets are all older than the window (listings come newest-first)."""
    out: List[dict] = []
    cursor = None
    pages = 0
    while True:
        params = {"series_ticker": series, "status": "settled", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        resp = client.request("GET", "/markets", params=params)
        pages += 1
        mkts = resp.get("markets", []) if isinstance(resp, dict) else []
        if not mkts:
            break
        kept_any_fresh = False
        for m in mkts:
            close_ts = _iso_ts(m.get("close_time"))
            if close_ts is None:
                continue
            if close_ts >= since_ts:
                out.append(m)
                kept_any_fresh = True
        cursor = resp.get("cursor") if isinstance(resp, dict) else None
        if not cursor:
            break
        if not kept_any_fresh:
            # this whole page is older than the window; newest-first -> done
            break
        if pages >= 200:
            log(f"[WARN] {series}: stopped paginating at {pages} pages (SCAN_TRUNCATED)")
            break
        time.sleep(SLEEP_BETWEEN_CALLS_S)
    return out


def pull_candles(client, series: str, ticker: str, open_ts: int, close_ts: int
                 ) -> List[dict]:
    """The full per-minute candlestick path for one market, open..close (+/- a
    minute of pad). Empty list on failure (never a fabrication)."""
    path = f"/series/{series}/markets/{ticker}/candlesticks"
    try:
        resp = client.request("GET", path,
                              params={"start_ts": open_ts - 60,
                                      "end_ts": close_ts + 60,
                                      "period_interval": 1})
    except Exception as e:  # noqa: BLE001 — read-only tool: log and skip
        print(f"[WARN] candlesticks {ticker}: {e}", file=sys.stderr)
        return []
    return resp.get("candlesticks", []) if isinstance(resp, dict) else []


# ── the per-window measurement ───────────────────────────────────

class WindowResult:
    __slots__ = ("ticker", "entry_cost", "entry_min", "mfe", "best_exit_min",
                 "target_reach", "stop_hit", "n_candles")

    def __init__(self, ticker):
        self.ticker = ticker
        self.entry_cost: Optional[int] = None
        self.entry_min: Optional[int] = None
        self.mfe: Optional[int] = None            # max favorable excursion, cents
        self.best_exit_min: Optional[int] = None  # minutes from entry to best bid
        # target -> True/False reachable (yes_bid high >= entry+target pre-endgame)
        self.target_reach: Dict[int, bool] = {t: False for t in TARGETS}
        # stop -> True/False stop would have fired first (before +8 reach)
        self.stop_hit: Dict[int, bool] = {s: False for s in STOPS}
        self.n_candles = 0


def measure_window(ticker: str, candles: List[dict], open_ts: int,
                   close_ts: int, endgame_min: int) -> Optional[WindowResult]:
    """Simulate the Lane A round-trip over one window's candlesticks.
    None when the window never offered an in-band entry early."""
    # order candles by time; keep only those inside the window with a readable
    # yes_bid (the candlestick after settle has no live bid)
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
    endgame_start = close_ts - endgame_min * 60

    # ENTRY: first minute within the early window whose bid is in band and whose
    # bid/ask straddle ~50c (both sides present)
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

    res = WindowResult(ticker)
    res.entry_cost = entry_cost
    res.entry_min = int((rows[entry_idx]["ts"] - open_ts) // 60)
    res.n_candles = len(rows)

    # walk the path AFTER entry, before the end-game, tracking the best bid
    # reached and the first stop breach (time-ordered, pessimistic tie-break)
    best_bid = entry_cost
    best_exit_ts = rows[entry_idx]["ts"]
    first_target_ts = {t: None for t in TARGETS}
    first_stop_ts = {s: None for s in STOPS}
    for r in rows[entry_idx + 1:]:
        if r["ts"] >= endgame_start:
            break
        hi = r["bid_high"]
        lo = r["bid_low"]
        if hi is not None and hi > best_bid:
            best_bid = hi
            best_exit_ts = r["ts"]
        for t in TARGETS:
            if first_target_ts[t] is None and hi is not None and hi >= entry_cost + t:
                first_target_ts[t] = r["ts"]
        for s in STOPS:
            if first_stop_ts[s] is None and lo is not None and lo <= entry_cost - s:
                first_stop_ts[s] = r["ts"]

    res.mfe = best_bid - entry_cost
    res.best_exit_min = int((best_exit_ts - rows[entry_idx]["ts"]) // 60)
    for t in TARGETS:
        res.target_reach[t] = first_target_ts[t] is not None
    # stop "first" = a stop breach strictly before (or in the same minute as) the
    # +8c target's first reach. Same-minute => stop first (pessimistic).
    ref_t = 8 if 8 in TARGETS else TARGETS[0]
    tgt_ts = first_target_ts[ref_t]
    for s in STOPS:
        st = first_stop_ts[s]
        if st is None:
            res.stop_hit[s] = False
        elif tgt_ts is None:
            res.stop_hit[s] = True
        else:
            res.stop_hit[s] = st <= tgt_ts
    return res


# ── aggregation ──────────────────────────────────────────────────

def dist_from_50(cost: int) -> str:
    d = abs(cost - 50)
    if d <= 2:
        return "|50-52| (near)"
    if d <= 5:
        return "|53-55|"
    if d <= 8:
        return "|56-58|"
    return "|59+| (far)"


def summarize(results: List[WindowResult]) -> dict:
    n = len(results)
    if n == 0:
        return {"n": 0}
    out: dict = {"n": n}
    for t in TARGETS:
        hits = sum(1 for r in results if r.target_reach[t])
        out[f"cashout_{t}c_hit_rate"] = hits / n
        out[f"cashout_{t}c_hits"] = hits
    for s in STOPS:
        hits = sum(1 for r in results if r.stop_hit[s])
        out[f"stop_{s}c_first_rate"] = hits / n
    # EV per window for the +8c cash-out vs the two stops, measured on the path:
    #   win  = +target  when +8 reached and the stop did NOT fire first
    #   loss = -stop    when the stop fired first
    #   flat = settle path otherwise (neither) -> 0 credited (conservative)
    ref_t = 8 if 8 in TARGETS else TARGETS[0]
    for s in STOPS:
        ev = 0.0
        for r in results:
            if r.stop_hit[s]:
                ev += -float(s)
            elif r.target_reach[ref_t]:
                ev += float(ref_t)
            # else 0 (no clean exit on the traded path)
        out[f"ev_cents_per_window_t{ref_t}_s{s}"] = ev / n
    out["mfe_mean_cents"] = sum(r.mfe for r in results) / n
    out["mfe_median_cents"] = sorted(r.mfe for r in results)[n // 2]
    reached = [r.best_exit_min for r in results if r.mfe > 0]
    out["time_to_best_exit_min_mean"] = (sum(reached) / len(reached)) if reached else 0.0
    out["thin"] = n < THIN_N
    return out


def bucketed(results: List[WindowResult]) -> Dict[str, dict]:
    buckets: Dict[str, List[WindowResult]] = {}
    for r in results:
        buckets.setdefault(dist_from_50(r.entry_cost), []).append(r)
    return {k: summarize(v) for k, v in sorted(buckets.items())}


# ── report ───────────────────────────────────────────────────────

def _pct(x) -> str:
    return f"{x*100:5.1f}%"


def print_report(by_series: Dict[str, List[WindowResult]], args, meta: dict) -> None:
    print("=" * 74)
    print("  LANE A REVERSION TEST — does the swing cash out before the end-game?")
    print("=" * 74)
    print(f"  series={','.join(by_series)}  days={args.days}  "
          f"endgame_min={args.endgame_min}  entry_band={ENTRY_LO}-{ENTRY_HI}c  "
          f"entry_frac={ENTRY_FRAC:.0%}")
    print(f"  targets={TARGETS}c  stops={STOPS}c  thin_flag_N<{THIN_N}")
    print(f"  markets_listed={meta['listed']}  windows_measured={meta['measured']}  "
          f"skipped_no_entry={meta['skipped']}")
    print("  FILL MODEL (conservative): a cash-out is credited ONLY when the")
    print("  yes_bid HIGH traded through the resting sell; same-minute stop+target")
    print("  counts STOP-FIRST. Hit-rates are an UPPER bound on real reachability.")
    print("=" * 74)

    all_results: List[WindowResult] = []
    for series, results in by_series.items():
        all_results.extend(results)
        s = summarize(results)
        print(f"\n── {series}  (N={s['n']}{'  ⚠THIN' if s.get('thin') else ''}) "
              + "─" * max(0, 48 - len(series)))
        if s["n"] == 0:
            print("    no measurable windows")
            continue
        for t in TARGETS:
            print(f"    cash-out +{t:2d}c reachable: {_pct(s[f'cashout_{t}c_hit_rate'])}"
                  f"  ({s[f'cashout_{t}c_hits']}/{s['n']})")
        for st in STOPS:
            print(f"    stop  -{st:2d}c fires first: {_pct(s[f'stop_{st}c_first_rate'])}")
        ref_t = 8 if 8 in TARGETS else TARGETS[0]
        for st in STOPS:
            print(f"    EV/window (+{ref_t}c vs -{st}c stop): "
                  f"{s[f'ev_cents_per_window_t{ref_t}_s{st}']:+.2f}c")
        print(f"    MFE mean {s['mfe_mean_cents']:+.2f}c  median {s['mfe_median_cents']:+d}c"
              f"  ·  time-to-best-bid ~{s['time_to_best_exit_min_mean']:.1f} min")
        print(f"    by entry distance from 50c:")
        for b, bs in bucketed(results).items():
            if bs["n"] == 0:
                continue
            ref_t = 8 if 8 in TARGETS else TARGETS[0]
            print(f"      {b:16s} N={bs['n']:4d}{'⚠' if bs.get('thin') else ' '} "
                  f"+{ref_t}c {_pct(bs[f'cashout_{ref_t}c_hit_rate'])}  "
                  f"-{STOPS[0]}c-first {_pct(bs[f'stop_{STOPS[0]}c_first_rate'])}  "
                  f"MFE {bs['mfe_mean_cents']:+.1f}c")

    print("\n" + "=" * 74)
    o = summarize(all_results)
    print(f"  OVERALL  (N={o['n']}{'  ⚠THIN' if o.get('thin') else ''})")
    if o["n"]:
        ref_t = 8 if 8 in TARGETS else TARGETS[0]
        for t in TARGETS:
            print(f"    cash-out +{t:2d}c reachable: {_pct(o[f'cashout_{t}c_hit_rate'])}")
        for st in STOPS:
            print(f"    stop  -{st:2d}c fires first: {_pct(o[f'stop_{st}c_first_rate'])}"
                  f"   EV/window(+{ref_t}c): {o[f'ev_cents_per_window_t{ref_t}_s{st}']:+.2f}c")
        print(f"    MFE mean {o['mfe_mean_cents']:+.2f}c  median {o['mfe_median_cents']:+d}c")
    print("=" * 74)
    print("  READ THIS: the hit-rate is REACHABILITY of a maker cash-out on the")
    print("  traded path, NOT a realized P&L. A real desk also pays queue risk,")
    print("  partial fills and the stop's own slippage. Treat +8c reachability")
    print("  above its break-even (stop_first_rate implied) as NECESSARY-not-")
    print("  SUFFICIENT evidence the Lane A swing is real.")
    print("=" * 74)


# ── main ─────────────────────────────────────────────────────────

def run(args) -> dict:
    from relay_engine import mac_main
    mac_main.prepare_environment()
    from relay_engine import venue
    client = venue.build_client()

    series_list = ALL_SERIES if args.series.upper() == "ALL" else [args.series.upper()]
    since_ts = time.time() - args.days * 86400
    by_series: Dict[str, List[WindowResult]] = {}
    meta = {"listed": 0, "measured": 0, "skipped": 0}

    for series in series_list:
        results: List[WindowResult] = []
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
                meta["skipped"] += 1   # not a ~15-minute window; skip
                continue
            candles = pull_candles(client, series, ticker, open_ts, close_ts)
            time.sleep(SLEEP_BETWEEN_CALLS_S)
            wr = measure_window(ticker, candles, open_ts, close_ts, args.endgame_min)
            if wr is None:
                meta["skipped"] += 1
                continue
            results.append(wr)
            meta["measured"] += 1
            if not args.json and (i + 1) % 25 == 0:
                print(f"[info] {series}: {i+1}/{len(markets)} markets, "
                      f"{len(results)} measured", file=sys.stderr)
        by_series[series] = results

    return {"by_series": by_series, "meta": meta}


def to_json(by_series: Dict[str, List[WindowResult]], meta: dict, args) -> dict:
    out = {"params": {"series": args.series, "days": args.days,
                      "endgame_min": args.endgame_min, "entry_band": [ENTRY_LO, ENTRY_HI],
                      "entry_frac": ENTRY_FRAC, "targets": list(TARGETS),
                      "stops": list(STOPS)},
           "meta": meta, "series": {}, "overall": None}
    allr: List[WindowResult] = []
    for series, results in by_series.items():
        allr.extend(results)
        out["series"][series] = {"summary": summarize(results),
                                 "by_entry_distance": bucketed(results)}
    out["overall"] = summarize(allr)
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="tools.reversion_test",
        description="Measure Lane A's oscillation/cash-out thesis on real candlesticks.")
    p.add_argument("--series", default=SERIES_DEFAULT,
                   help="KXBTC15M | KXETH15M | KXXRP15M | ALL (default KXBTC15M)")
    p.add_argument("--days", type=int, default=7, help="look-back window in days (default 7)")
    p.add_argument("--endgame-min", type=int, default=3,
                   help="final minutes excluded from the cash-out search (default 3)")
    p.add_argument("--json", action="store_true", help="emit a JSON report to stdout")
    args = p.parse_args(argv)

    data = run(args)
    if args.json:
        print(json.dumps(to_json(data["by_series"], data["meta"], args), indent=2))
    else:
        print_report(data["by_series"], args, data["meta"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
