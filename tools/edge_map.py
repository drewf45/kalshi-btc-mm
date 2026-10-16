"""edge_map — an UNBIASED edge map of real Kalshi trading history.

Purpose
-------
Read the account's ACTUAL fills from the Kalshi portfolio API, pair them into
realized round-trips, and report realized EV bucketed by ENTRY PRICE RANGE
(in YES-terms cents). This feeds a downstream P&L optimizer.

NO-CONFIRMATION-BIAS MANDATE  (the #1 rule, load-bearing)
---------------------------------------------------------
This tool exists to tell the truth about where money is made and LOST. It must
never massage the data toward a hoped-for conclusion:

  * Price ranges that LOSE money are reported, in full, and flagged LOSING when
    realized win-rate falls below the break-even win-rate for that bucket.
  * Only REALIZED outcomes count. A position held to settlement is a win ONLY
    if the venue settled it in our favor — never because we hoped it would.
    When settlement cannot be determined, the round-trip is bucketed SEPARATELY
    as "open/unresolved" and counted as UNKNOWN, never as a win.
  * Buckets with too few samples (N < MIN_SAMPLES) are labelled THIN and no
    conclusion is drawn from them.
  * Truncation is never silent: if the all-time pull hits the cap, the report
    says so and prints how many were pulled and whether more remain.

READ-ONLY. This module performs GET requests only. It never places, amends, or
cancels an order, never imports a submit/gateway path, and never writes to
kal.db or any engine state. It is deliberately OUTSIDE relay_engine/ so it can
never be mistaken for part of the trading engine.

Usage
-----
    .venv/bin/python -m tools.edge_map                 # full all-time pull + text report
    .venv/bin/python -m tools.edge_map --limit 500     # cap fills pulled
    .venv/bin/python -m tools.edge_map --series KXBTC15M   # filter one market series
    .venv/bin/python -m tools.edge_map --json          # structured JSON for the optimizer

Caveats on settlement-outcome inference are printed inline in the report and
documented at _infer_settlement_basis() below.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict, deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

# ── Config ───────────────────────────────────────────────────────────────────

# Hard cap on the all-time pull. NO SILENT TRUNCATION: if we hit this, the
# report says how far we got and whether the venue had more.
MAX_FILLS = 10_000
PAGE_LIMIT = 200          # per-request page size for cursor pagination
MIN_SAMPLES = 5           # buckets with fewer resolved trades are THIN

# Entry-price buckets, in YES-terms cents. The 40-54 band is the desk's entry
# zone, deliberately split at 50 (40-49 and 50-54) so the two halves stay
# distinguishable. Buckets are [lo, hi] INCLUSIVE in integer cents.
BUCKETS: List[Tuple[int, int]] = [
    (1, 9), (10, 19), (20, 29), (30, 39), (40, 49),
    (50, 54), (55, 59), (60, 69), (70, 79), (80, 89),
    (90, 94), (95, 99),
]


def _bucket_label(lo: int, hi: int) -> str:
    return f"{lo}-{hi}"


def _bucket_for(entry_cents: float) -> Optional[str]:
    """Return the bucket label for an entry price in cents, or None if it falls
    outside every defined bucket (e.g. a 0c or 100c degenerate fill)."""
    c = round(entry_cents)
    for lo, hi in BUCKETS:
        if lo <= c <= hi:
            return _bucket_label(lo, hi)
    return None


# ── Fill normalization ───────────────────────────────────────────────────────

@dataclass
class NormFill:
    """One fill, normalized to YES-terms cents. action opens (buy) or closes
    (sell) exposure on `side` of `ticker`."""
    ticker: str
    series: str
    side: str            # 'yes' | 'no'
    action: str          # 'buy' | 'sell'
    entry_cents: float   # YES-terms cost of ONE contract on this side, in cents
    count: Decimal
    fee_cents: float
    is_taker: bool
    created_time: str
    ts: int


def _series_of(ticker: str) -> str:
    """Market SERIES = the prefix before the first dash-digit group, e.g.
    'KXBTC15M-26OCT151300-00' -> 'KXBTC15M', 'KXETHD-...' -> 'KXETHD'.
    The series is the token up to (but not including) the first dash whose
    following segment begins with a digit. Falls back to the whole first
    dash-segment when nothing matches."""
    parts = ticker.split("-")
    if not parts:
        return ticker
    out = [parts[0]]
    for seg in parts[1:]:
        if seg and seg[0].isdigit():
            break
        out.append(seg)
    return "-".join(out)


def _dollars_to_cents(raw) -> Optional[float]:
    """Kalshi price strings are DOLLARS ('0.8500' = 85c). Exact via Decimal."""
    if raw is None:
        return None
    try:
        return float(Decimal(str(raw)) * 100)
    except Exception:
        return None


def _exact_count(raw) -> Optional[Decimal]:
    """count_fp is a fractional-contract string. Keep it EXACT as Decimal.
    None when unreadable or negative (we never fabricate a count)."""
    if raw is None:
        return None
    try:
        d = Decimal(str(raw))
    except Exception:
        return None
    if not d.is_finite() or d < 0:
        return None
    return d


def normalize_fill(fill: dict) -> Optional[NormFill]:
    """Normalize one raw fill dict into YES-terms cents, or None if it cannot
    be parsed (unknown side, unreadable price/count). Unparseable fills are
    dropped loudly by the caller, never silently reinterpreted.

    Normalization rule (per spec):
      side == 'yes' -> entry cost = yes_price_dollars * 100
      side == 'no'  -> entry cost = no_price_dollars  * 100
    i.e. what we actually PAID per contract on the side we took, in cents.
    """
    ticker = str(fill.get("market_ticker") or fill.get("ticker") or "").strip()
    if not ticker:
        return None

    side = str(fill.get("side") or fill.get("outcome_side") or "").strip().lower()
    if side not in ("yes", "no"):
        return None

    action = str(fill.get("action") or "").strip().lower()
    if action not in ("buy", "sell"):
        return None

    price_key = "yes_price_dollars" if side == "yes" else "no_price_dollars"
    entry_cents = _dollars_to_cents(fill.get(price_key))
    if entry_cents is None:
        return None

    count = _exact_count(fill.get("count_fp") or fill.get("count"))
    if count is None or count <= 0:
        return None

    fee_cents = _dollars_to_cents(fill.get("fee_cost"))
    if fee_cents is None:
        fee_cents = 0.0

    ts = fill.get("ts")
    try:
        ts = int(ts) if ts is not None else 0
    except Exception:
        ts = 0

    return NormFill(
        ticker=ticker,
        series=_series_of(ticker),
        side=side,
        action=action,
        entry_cents=entry_cents,
        count=count,
        fee_cents=float(fee_cents),
        is_taker=bool(fill.get("is_taker")),
        created_time=str(fill.get("created_time") or ""),
        ts=ts,
    )


# ── Round-trip pairing (FIFO) ──────────────────────────────────────────────────

@dataclass
class RoundTrip:
    """One contract-unit of realized (or unresolved) exposure.

    net_cents is PER CONTRACT, realized, AFTER fees, in YES-terms cents:
      resolved closed round-trip: (close_price - entry_price) - fees
      held-to-settlement (resolved via venue): (100 or 0) - entry - fees
    For 'open/unresolved' round-trips net_cents is None (unknown — NOT a win)."""
    ticker: str
    series: str
    side: str
    entry_cents: float          # YES-terms entry price (bucket key)
    count: Decimal
    net_cents: Optional[float]  # per-contract realized net, None if unresolved
    fee_cents: float            # total fees attributed to this round-trip unit
    is_taker: bool
    resolved: bool
    outcome: str                # 'win' | 'loss' | 'scratch' | 'unresolved'


@dataclass
class _OpenLot:
    entry_cents: float
    count: Decimal
    fee_per_contract: float
    is_taker: bool


def _pair_market_side(fills: List[NormFill],
                      settlement_basis: Optional[float]) -> List[RoundTrip]:
    """FIFO-pair buys (opens) against sells (closes) within one ticker+side.

    A 'buy' adds an open lot at its entry price. A 'sell' closes the oldest open
    lot(s) first, realizing (sell_price - entry_price) per contract. Fees from
    both legs are attributed per contract.

    Whatever remains open after all fills is held-to-settlement. If
    settlement_basis is known (100c for a side that WON, 0c for a side that
    LOST), each remaining contract realizes (basis - entry). If settlement is
    unknown, the remainder is bucketed as 'open/unresolved' with net=None.
    """
    fills = sorted(fills, key=lambda f: (f.ts, f.created_time))
    open_lots: deque[_OpenLot] = deque()
    trips: List[RoundTrip] = []

    for f in fills:
        fee_pc = (f.fee_cents / float(f.count)) if f.count else 0.0
        if f.action == "buy":
            open_lots.append(_OpenLot(f.entry_cents, f.count, fee_pc, f.is_taker))
            continue

        # sell = close against oldest open lots (FIFO)
        remaining = f.count
        while remaining > 0 and open_lots:
            lot = open_lots[0]
            matched = min(remaining, lot.count)
            net_pc = (f.entry_cents - lot.entry_cents) - lot.fee_per_contract - fee_pc
            trips.append(_mk_trip(f, lot, matched, net_pc, resolved=True))
            lot.count -= matched
            remaining -= matched
            if lot.count <= 0:
                open_lots.popleft()
        # A sell with no matching open lot is a naked close (short open). We do
        # NOT invent a basis for it — it becomes an unresolved short remainder.
        if remaining > 0:
            open_lots.append(_OpenLot(f.entry_cents, -remaining, fee_pc, f.is_taker))

    # Leftover open lots -> held to settlement.
    for lot in open_lots:
        qty = abs(lot.count)
        if qty <= 0:
            continue
        if settlement_basis is not None and lot.count > 0:
            # long held to settlement: basis - entry
            net_pc = (settlement_basis - lot.entry_cents) - lot.fee_per_contract
            trips.append(RoundTrip(
                ticker=fills[0].ticker, series=fills[0].series, side=fills[0].side,
                entry_cents=lot.entry_cents, count=qty, net_cents=net_pc,
                fee_cents=lot.fee_per_contract * float(qty), is_taker=lot.is_taker,
                resolved=True, outcome=_outcome_of(net_pc)))
        else:
            # Unknown settlement (or a short remainder) -> UNRESOLVED. Never a win.
            trips.append(RoundTrip(
                ticker=fills[0].ticker, series=fills[0].series, side=fills[0].side,
                entry_cents=lot.entry_cents, count=qty, net_cents=None,
                fee_cents=lot.fee_per_contract * float(qty), is_taker=lot.is_taker,
                resolved=False, outcome="unresolved"))
    return trips


def _mk_trip(sell: NormFill, lot: _OpenLot, matched: Decimal,
             net_pc: float, resolved: bool) -> RoundTrip:
    return RoundTrip(
        ticker=sell.ticker, series=sell.series, side=sell.side,
        entry_cents=lot.entry_cents, count=matched, net_cents=net_pc,
        fee_cents=(lot.fee_per_contract + (sell.fee_cents / float(sell.count)
                                           if sell.count else 0.0)) * float(matched),
        is_taker=lot.is_taker, resolved=resolved, outcome=_outcome_of(net_pc))


def _outcome_of(net_cents: float) -> str:
    if net_cents > 0:
        return "win"
    if net_cents < 0:
        return "loss"
    return "scratch"


def _infer_settlement_basis(side: str, result: Optional[str]) -> Optional[float]:
    """Translate a venue settlement result into a YES-terms basis in cents for
    a held-to-settlement position on `side`.

    CAVEAT (honest inference, no guessing): we only return a basis when the
    venue itself reports result == 'yes' or 'no'. A YES contract whose market
    settled 'yes' pays 100c; settled 'no' pays 0c. A NO contract is the mirror.
    If the venue has not settled (result is None), we return None and the
    remainder is bucketed as open/unresolved — we NEVER assume an outcome.
    """
    if result not in ("yes", "no"):
        return None
    won = (result == side)
    return 100.0 if won else 0.0


# ── Fills pull (cursor pagination) ─────────────────────────────────────────────

@dataclass
class PullResult:
    fills: List[dict]
    pulled: int
    truncated: bool          # hit MAX_FILLS cap with more available
    more_available: bool     # venue still had a cursor when we stopped
    pages: int
    error: Optional[str]     # non-fatal: what we got before a raise, if any


def pull_all_fills(client, venue, limit: Optional[int] = None) -> PullResult:
    """Paginate GET <fills_route> via the cursor param to pull full history.

    How pagination works (per the Kalshi fills endpoint): each request takes a
    `limit` and an optional `cursor`; the response is {"fills": [...],
    "cursor": "<next>"}. A non-empty cursor means there is a next page; an empty
    or missing cursor means we have reached the end. We pass the returned cursor
    into the next request until it is empty, the fill list is empty, or we reach
    the requested `limit` / MAX_FILLS cap.

    NO SILENT TRUNCATION: PullResult records exactly how many were pulled, how
    many pages, whether we truncated at the cap, and whether the venue still had
    more. API errors are caught — we return what we got so far plus the error.
    """
    route = venue._fills_route
    hard_cap = MAX_FILLS if limit is None else min(limit, MAX_FILLS)

    collected: List[dict] = []
    cursor: Optional[str] = None
    pages = 0
    error: Optional[str] = None
    more_available = False

    while len(collected) < hard_cap:
        page_size = min(PAGE_LIMIT, hard_cap - len(collected))
        params: Dict[str, object] = {"limit": page_size}
        if cursor:
            params["cursor"] = cursor
        try:
            resp = client.request("GET", route, params=params)
        except Exception as e:  # pull can raise — report how far we got
            error = f"{type(e).__name__}: {e}"
            break

        if isinstance(resp, dict):
            page = resp.get("fills") or []
            next_cursor = resp.get("cursor") or ""
        elif isinstance(resp, list):
            page, next_cursor = resp, ""
        else:
            page, next_cursor = [], ""

        pages += 1
        collected.extend(page)

        if not page:
            break
        if not next_cursor:
            break
        cursor = next_cursor
        # If we are about to stop purely because of the cap, the venue still had
        # a cursor -> more available.
        if len(collected) >= hard_cap:
            more_available = True
            break

    truncated = (len(collected) >= MAX_FILLS) and more_available
    return PullResult(
        fills=collected, pulled=len(collected), truncated=truncated,
        more_available=more_available, pages=pages, error=error)


# ── Aggregation ────────────────────────────────────────────────────────────────

@dataclass
class BucketStat:
    label: str
    n_trades: int = 0                 # resolved round-trip units
    contracts: Decimal = Decimal(0)   # resolved contracts
    wins: int = 0
    losses: int = 0
    scratches: int = 0
    win_cents_sum: float = 0.0        # sum of per-contract net on winning units
    loss_cents_sum: float = 0.0       # sum of ABS per-contract net on losing units
    realized_net_dollars: float = 0.0
    fees_dollars: float = 0.0
    # Unresolved (held, settlement unknown) tracked separately — NEVER as wins.
    unresolved_trades: int = 0
    unresolved_contracts: Decimal = Decimal(0)

    def add(self, t: RoundTrip) -> None:
        if not t.resolved or t.net_cents is None:
            self.unresolved_trades += 1
            self.unresolved_contracts += t.count
            self.fees_dollars += t.fee_cents / 100.0
            return
        self.n_trades += 1
        self.contracts += t.count
        self.realized_net_dollars += (t.net_cents * float(t.count)) / 100.0
        self.fees_dollars += t.fee_cents / 100.0
        if t.outcome == "win":
            self.wins += 1
            self.win_cents_sum += t.net_cents
        elif t.outcome == "loss":
            self.losses += 1
            self.loss_cents_sum += abs(t.net_cents)
        else:
            self.scratches += 1

    @property
    def resolved(self) -> int:
        return self.wins + self.losses + self.scratches

    @property
    def win_rate(self) -> Optional[float]:
        return (self.wins / self.resolved) if self.resolved else None

    @property
    def mean_win(self) -> Optional[float]:
        return (self.win_cents_sum / self.wins) if self.wins else None

    @property
    def mean_loss(self) -> Optional[float]:
        return (self.loss_cents_sum / self.losses) if self.losses else None

    @property
    def breakeven_wr(self) -> Optional[float]:
        mw, ml = self.mean_win, self.mean_loss
        if mw is None or ml is None:
            return None
        denom = mw + ml
        return (ml / denom) if denom > 0 else None

    @property
    def ev_per_contract(self) -> Optional[float]:
        """EV = winrate*meanWin - (1-winrate)*meanLoss, in cents. Needs both a
        win and a loss sample to be meaningful; None otherwise."""
        wr, mw, ml = self.win_rate, self.mean_win, self.mean_loss
        if wr is None or mw is None or ml is None:
            return None
        return wr * mw - (1 - wr) * ml

    @property
    def thin(self) -> bool:
        return self.resolved < MIN_SAMPLES

    @property
    def losing(self) -> Optional[bool]:
        """True when realized win-rate is below the break-even win-rate — i.e.
        this entry-price range LOSES money. None when undeterminable."""
        wr, be = self.win_rate, self.breakeven_wr
        if wr is None or be is None:
            return None
        return wr < be

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "n_trades": self.n_trades,
            "contracts": float(self.contracts),
            "resolved": self.resolved,
            "wins": self.wins,
            "losses": self.losses,
            "scratches": self.scratches,
            "win_rate": self.win_rate,
            "mean_win_cents": self.mean_win,
            "mean_loss_cents": self.mean_loss,
            "breakeven_win_rate": self.breakeven_wr,
            "ev_per_contract_cents": self.ev_per_contract,
            "realized_net_dollars": round(self.realized_net_dollars, 4),
            "fees_dollars": round(self.fees_dollars, 4),
            "thin": self.thin,
            "losing": self.losing,
            "unresolved_trades": self.unresolved_trades,
            "unresolved_contracts": float(self.unresolved_contracts),
        }


def aggregate(trips: List[RoundTrip], keyfn) -> Dict[str, BucketStat]:
    out: Dict[str, BucketStat] = {}
    for t in trips:
        key = keyfn(t)
        if key is None:
            continue
        if key not in out:
            out[key] = BucketStat(label=key)
        out[key].add(t)
    return out


# ── Report rendering ───────────────────────────────────────────────────────────

def _fmt_pct(x: Optional[float]) -> str:
    return f"{x*100:5.1f}%" if x is not None else "   —  "


def _fmt_cents(x: Optional[float]) -> str:
    return f"{x:7.2f}" if x is not None else "    —  "


def _bucket_table(title: str, stats: Dict[str, BucketStat],
                  order: Optional[List[str]] = None) -> List[str]:
    lines = [title, "-" * len(title)]
    header = (f"{'bucket':<10} {'N':>5} {'ctr':>9} {'win%':>7} {'be%':>7} "
              f"{'mWin':>8} {'mLoss':>8} {'EV/ct':>8} {'net$':>10} {'fee$':>8} flag")
    lines.append(header)
    keys = order if order is not None else sorted(stats.keys())
    for k in keys:
        s = stats.get(k)
        if s is None or (s.resolved == 0 and s.unresolved_trades == 0):
            continue
        flags = []
        if s.thin:
            flags.append("THIN")
        if s.losing is True:
            flags.append("LOSING")
        elif s.losing is False and not s.thin:
            flags.append("edge+")
        flag = " ".join(flags)
        lines.append(
            f"{s.label:<10} {s.n_trades:>5} {float(s.contracts):>9.2f} "
            f"{_fmt_pct(s.win_rate)} {_fmt_pct(s.breakeven_wr)} "
            f"{_fmt_cents(s.mean_win)} {_fmt_cents(s.mean_loss)} "
            f"{_fmt_cents(s.ev_per_contract)} {s.realized_net_dollars:>10.2f} "
            f"{s.fees_dollars:>8.2f} {flag}")
        if s.unresolved_trades:
            lines.append(
                f"{'':<10} {'':>5} {float(s.unresolved_contracts):>9.2f} "
                f"{'(unresolved/held — settlement unknown, counted UNKNOWN not win)':<}")
    return lines


def render_text(pull: PullResult, by_bucket: Dict[str, BucketStat],
                by_series: Dict[str, BucketStat],
                by_takermaker: Dict[str, BucketStat],
                series_filter: Optional[str],
                dropped: int, resolved_total: int,
                unresolved_total: int) -> str:
    out: List[str] = []
    out.append("=" * 84)
    out.append("KALSHI EDGE MAP — realized EV by entry-price bucket (UNBIASED)")
    out.append("=" * 84)
    out.append("")
    out.append("MANDATE: realized outcomes only. Losing buckets are flagged LOSING. Held")
    out.append("positions with unknown settlement are UNRESOLVED (counted unknown, not wins).")
    out.append("Buckets with N<%d resolved trades are THIN — draw no conclusions." % MIN_SAMPLES)
    out.append("")

    # Pull provenance — no silent truncation.
    out.append(f"Fills pulled: {pull.pulled}  (pages: {pull.pages})")
    if series_filter:
        out.append(f"Series filter: {series_filter}")
    if dropped:
        out.append(f"Unparseable fills dropped (loud): {dropped}")
    if pull.truncated:
        out.append(f"!! TRUNCATED at MAX_FILLS={MAX_FILLS}: the venue HAD MORE history. "
                   f"Report is PARTIAL.")
    elif pull.more_available:
        out.append(f"!! Stopped at --limit cap with more fills available at the venue. "
                   f"Report is PARTIAL.")
    if pull.error:
        out.append(f"!! API error during pull (reporting what was retrieved): {pull.error}")
    out.append(f"Resolved round-trip units: {resolved_total}   "
               f"Unresolved (held, unknown): {unresolved_total}")
    out.append("")

    out.extend(_bucket_table("BY ENTRY-PRICE BUCKET (YES-terms cents)", by_bucket,
                             order=[_bucket_label(lo, hi) for lo, hi in BUCKETS]))
    out.append("")
    out.extend(_bucket_table("BY MARKET SERIES", by_series))
    out.append("")
    out.extend(_bucket_table("BY TAKER vs MAKER", by_takermaker,
                             order=["taker", "maker"]))
    out.append("")

    # Honest bottom line.
    net = sum(s.realized_net_dollars for s in by_bucket.values())
    fees = sum(s.fees_dollars for s in by_bucket.values())
    losing = [s.label for s in by_bucket.values() if s.losing is True and not s.thin]
    winning = [s.label for s in by_bucket.values() if s.losing is False and not s.thin]
    out.append("-" * 84)
    out.append(f"TOTAL realized net: ${net:,.2f}   (fees paid: ${fees:,.2f})")
    out.append(f"LOSING entry buckets (realized wr < breakeven, N>=%d): "
               f"{', '.join(losing) if losing else 'none'}" % MIN_SAMPLES)
    out.append(f"Profitable entry buckets (N>=%d): "
               f"{', '.join(winning) if winning else 'none'}" % MIN_SAMPLES)
    out.append("-" * 84)
    return "\n".join(out)


def build_json(pull: PullResult, by_bucket, by_series, by_takermaker,
               series_filter, dropped, resolved_total, unresolved_total) -> dict:
    return {
        "mandate": "unbiased realized edge map; losing buckets flagged; "
                   "unresolved held positions counted unknown not wins",
        "pull": {
            "pulled": pull.pulled,
            "pages": pull.pages,
            "truncated_at_cap": pull.truncated,
            "more_available": pull.more_available,
            "max_fills": MAX_FILLS,
            "error": pull.error,
        },
        "series_filter": series_filter,
        "unparseable_dropped": dropped,
        "resolved_round_trip_units": resolved_total,
        "unresolved_round_trip_units": unresolved_total,
        "min_samples_for_conclusion": MIN_SAMPLES,
        "by_bucket": [by_bucket[_bucket_label(lo, hi)].to_dict()
                      for lo, hi in BUCKETS if _bucket_label(lo, hi) in by_bucket],
        "by_series": [s.to_dict() for s in by_series.values()],
        "by_taker_maker": [by_takermaker[k].to_dict()
                           for k in ("taker", "maker") if k in by_takermaker],
    }


# ── Orchestration ──────────────────────────────────────────────────────────────

def run(limit: Optional[int], series_filter: Optional[str],
        as_json: bool) -> int:
    # Import the engine's READ-ONLY plumbing only. No submit/gateway path.
    from relay_engine import mac_main, venue
    mac_main.prepare_environment()

    client = venue.build_client()
    try:
        venue.probe_fills_route(client)
    except Exception as e:
        print(f"FATAL: could not probe fills route: {e}", file=sys.stderr)
        return 2

    pull = pull_all_fills(client, venue, limit=limit)
    if pull.error and not pull.fills:
        print(f"FATAL: fills pull failed with no data retrieved: {pull.error}",
              file=sys.stderr)
        return 2

    # Normalize. Drop unparseable fills loudly (counted, never reinterpreted).
    norm: List[NormFill] = []
    dropped = 0
    for raw in pull.fills:
        nf = normalize_fill(raw)
        if nf is None:
            dropped += 1
            continue
        if series_filter and nf.series != series_filter:
            continue
        norm.append(nf)

    # Group by ticker+side for FIFO pairing.
    groups: Dict[Tuple[str, str], List[NormFill]] = defaultdict(list)
    for nf in norm:
        groups[(nf.ticker, nf.side)].append(nf)

    # Determine whether any group has a leftover position needing settlement.
    # We only query settlement for tickers that actually have an open remainder,
    # and we cache per-ticker so we never hammer the API.
    settle_cache: Dict[str, Optional[str]] = {}

    def settlement_basis(ticker: str, side: str, has_remainder: bool) -> Optional[float]:
        if not has_remainder:
            return None
        if ticker not in settle_cache:
            try:
                settle_cache[ticker] = venue.get_settlement_result(client, ticker)
            except Exception:
                settle_cache[ticker] = None  # unknown -> unresolved, never a guess
        return _infer_settlement_basis(side, settle_cache[ticker])

    trips: List[RoundTrip] = []
    for (ticker, side), fills in groups.items():
        bought = sum((f.count for f in fills if f.action == "buy"), Decimal(0))
        sold = sum((f.count for f in fills if f.action == "sell"), Decimal(0))
        has_remainder = bought != sold
        basis = settlement_basis(ticker, side, has_remainder)
        trips.extend(_pair_market_side(fills, basis))

    by_bucket = aggregate(trips, lambda t: _bucket_for(t.entry_cents))
    by_series = aggregate(trips, lambda t: t.series)
    by_takermaker = aggregate(trips, lambda t: "taker" if t.is_taker else "maker")

    resolved_total = sum(1 for t in trips if t.resolved and t.net_cents is not None)
    unresolved_total = sum(1 for t in trips if not (t.resolved and t.net_cents is not None))

    if as_json:
        print(json.dumps(build_json(pull, by_bucket, by_series, by_takermaker,
                                     series_filter, dropped, resolved_total,
                                     unresolved_total), indent=2))
    else:
        print(render_text(pull, by_bucket, by_series, by_takermaker,
                          series_filter, dropped, resolved_total, unresolved_total))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="edge_map",
        description="Unbiased realized edge map of Kalshi trading history "
                    "(READ-ONLY).")
    p.add_argument("--limit", type=int, default=None,
                   help="Cap total fills pulled (default: full all-time pull up "
                        f"to MAX_FILLS={MAX_FILLS}).")
    p.add_argument("--series", type=str, default=None,
                   help="Filter to one market series, e.g. KXBTC15M.")
    p.add_argument("--json", action="store_true", dest="as_json",
                   help="Emit structured JSON (for the P&L optimizer).")
    args = p.parse_args(argv)
    try:
        return run(args.limit, args.series, args.as_json)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
