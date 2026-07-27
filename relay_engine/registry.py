"""WO-2026-07-26-P §A3 — THE DATA-QUESTION REGISTRY.

The one-lot bug was data that existed, was trusted, and validated NOTHING: a
depth number nobody was asking a question of, so nobody noticed when it lied.
This registry forces the other side of the Why Law — not "why this row?" (that
is §A1, enforced at the write chokepoint) but "why this SURFACE at all?". Every
data product the engine writes must declare, in code, the QUESTION it answers,
what a reading VALIDATES or INVALIDATES, and who CONSUMES it.

Three teeth:
  • boot asserts every declared writer is registered — WARN for the first 24h
    after this deploy, FATAL after (a graceful cutover, not a boot-day crash);
  • the daily pack prints the registry with each surface's LAST-READ timestamp;
  • a surface unread for 14 days pages DATA_WITHOUT_QUESTION — data nobody asks
    a question of is retired or explained, never left to rot into the next bug.

Seeded with 6 standing questions covering the money-and-evidence surfaces.
"""

import calendar
import time
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass(frozen=True)
class Question:
    """One surface's declared reason to exist."""
    surface: str                      # the data product / surface_rows family
    question: str                     # the question a reading answers
    validates_or_invalidates: str     # what a reading confirms or refutes
    consumer: str                     # who reads it (a real reader, named)

    def is_complete(self) -> bool:
        return bool(self.surface.strip() and self.question.strip()
                    and self.validates_or_invalidates.strip()
                    and self.consumer.strip())


_REGISTRY: Dict[str, Question] = {}

# The enforcement cutover: WARN until 24h after the WO-P deploy (2026-07-26),
# FATAL after. A fixed UTC instant so the schedule is deterministic and printed.
ENFORCE_FROM_TS: float = float(calendar.timegm((2026, 7, 27, 0, 0, 0)))
UNREAD_PAGE_DAYS: int = 14


def register(surface: str, question: str, validates_or_invalidates: str,
             consumer: str) -> None:
    """Register (or re-register) a surface's data-question. Idempotent by
    surface name — the last declaration wins, so a surface is declared exactly
    once in code."""
    _REGISTRY[surface] = Question(surface, question, validates_or_invalidates,
                                  consumer)


def get(surface: str) -> Optional[Question]:
    return _REGISTRY.get(surface)


def all_questions() -> List[Question]:
    return [_REGISTRY[s] for s in sorted(_REGISTRY)]


def registered_surfaces() -> set:
    return set(_REGISTRY)


# ── THE SEED — 6 STANDING QUESTIONS (money-and-evidence surfaces) ────────────
# Each names a real writer in the tree. These are also the DECLARED writers boot
# checks: every one must carry a complete question.
SEED_SURFACES = (
    "SIZE_DECISION", "SETTLE_AUDIT", "OPEN_SKIP",
    "SALVAGE_VERDICT", "WINDOW_ECON", "TIER_CHANGE",
    "BOOK_STALE", "CORRELATED_LOSS", "NO_COUNTERPARTY",
)


def seed() -> None:
    """Install the 6 standing questions. Called at import; idempotent."""
    register(
        "SIZE_DECISION",
        "Did sizing ever want to bet a lot the book didn't support?",
        "VALIDATES the no-fabrication rule — every live lot traces to a joining "
        "/ band depth and a notional; a DEPTH_BLIND or SIZE_ZERO_DEFER row is "
        "the honest 'no bet' the one-lot bug used to fabricate as a 1.",
        "the one-lot audit (WO-P B2) + the size row's why-terms")
    register(
        "SETTLE_AUDIT",
        "Does every booked cent trace to a fill and an exchange outcome?",
        "VALIDATES book-vs-exchange reconciliation; an unmatched-leg flag "
        "INVALIDATES the settlement's provenance and names its source.",
        "E1 provenance / standing_reconcile / the daily money line")
    register(
        "OPEN_SKIP",
        "Why did FLIP/OPEN pass this window?",
        "VALIDATES or INVALIDATES the pile-entry thresholds — a skipped window "
        "is the primary FLIP data product; a traded window never logs one.",
        "Saturday calibration of the OPEN band / skew / trend gates")
    register(
        "SALVAGE_VERDICT",
        "Does salvage recover more than the ride it forgoes?",
        "VALIDATES the salvage dial (SALVAGE_ADJ) — a DODGED_LOSS row's "
        "dodged_cents is recovered mark; a regret row recaptures nothing.",
        "scoring.salvage_recapture_cents / breakeven / the scoreboard")
    register(
        "WINDOW_ECON",
        "Did this window make or lose money, and which lanes were live?",
        "VALIDATES per-window P&L attribution against the fills-truth settle "
        "path; a divergence INVALIDATES the window bracket, not the ledger.",
        "the daily pack money curve + the CEO worst-day lens")
    register(
        "TIER_CHANGE",
        "Is a cell earning a bigger size tier, or should it shrink?",
        "VALIDATES the Wilson size ladder — a tier moves on the cell's OWN "
        "bars, never a ruling; a stale tier INVALIDATES the size it authorizes.",
        "sizing tiering / custody cut-scaling / the scoreboard")
    register(
        "BOOK_STALE",
        "How far, and when, does the market-summary endpoint lag the orderbook?",
        "VALIDATES that the post-entry watch's small offsets are endpoint LAG "
        "(freshness noise), not orientation inversion — clustering with real "
        "trouble would INVALIDATE that and earn BOOK_STALE_OFFSET_C a derived "
        "number instead of a DREW-DEFAULT.",
        "the daily pack's endpoint-lag-by-hour line (WO-R) / threshold derivation")
    register(
        "CORRELATED_LOSS",
        "How often do ≥2 rooms lose the SAME wall-clock window — the correlated tail?",
        "VALIDATES (or shrinks) the 50% ensemble cap: the cross-crypto air-pocket "
        "is the one shock that reaches every room at once; measured frequency and "
        "combined size turn ENSEMBLE_AT_RISK_PCT from a DREW-DEFAULT into a DERIVED "
        "number. Rare/small → the cap can relax; clustered/large → it tightens.",
        "the ensemble worst-day math / ENSEMBLE_AT_RISK_PCT derivation (WO-S §2)")
    register(
        "NO_COUNTERPARTY",
        "How often, and when, is a room's OPPOSITE side empty at entry — the "
        "thin-book trap?",
        "VALIDATES the counterparty gate's necessity per room: an empty opposite "
        "side means the order can't fill or (worse) can't EXIT. The by-series/hour "
        "counts are the new room's free liquidity map — a room that refuses all "
        "afternoon has no counterparties and SHOULD starve (the gate telling the "
        "truth about the room, WO-T Guard 1).",
        "the daily pack's counterparty-by-series/hour line / XRP go/no-go read")


def missing_registration(active_surfaces) -> List[str]:
    """Declared writers that are absent or carry an incomplete question."""
    out = []
    for s in active_surfaces:
        q = _REGISTRY.get(s)
        if q is None or not q.is_complete():
            out.append(s)
    return sorted(out)


def assert_writers_registered(active_surfaces=None,
                              now: Optional[float] = None,
                              fail_fn=None) -> str:
    """Boot check: every declared writer must be registered with a complete
    question. Before ENFORCE_FROM_TS this WARNs (a boot line, never a crash);
    after, it is FATAL. Returns the banner status string."""
    if now is None:
        now = time.time()
    active = list(SEED_SURFACES if active_surfaces is None else active_surfaces)
    missing = missing_registration(active)
    when = time.strftime("%Y-%m-%d %H:%MZ", time.gmtime(ENFORCE_FROM_TS))
    if not missing:
        mode = "FATAL" if now >= ENFORCE_FROM_TS else "WARN"
        return (f"DATA-QUESTION REGISTRY (WO-P A3): {len(_REGISTRY)} surfaces "
                f"registered, all complete · enforcement {mode} "
                f"(WARN→FATAL at {when}) · unread>{UNREAD_PAGE_DAYS}d pages "
                "DATA_WITHOUT_QUESTION")
    detail = ("data-question registry: declared writers with no complete "
              f"question: {', '.join(missing)} — every surface must answer a "
              "question (Why Law §A3)")
    if now >= ENFORCE_FROM_TS:
        if fail_fn is not None:
            fail_fn("DATA_WITHOUT_QUESTION", detail, fatal=True)
        else:
            from . import failures
            failures.fail("DATA_WITHOUT_QUESTION", detail, fatal=True)
        return f"DATA-QUESTION REGISTRY (WO-P A3): FATAL — {detail}"
    return (f"DATA-QUESTION REGISTRY (WO-P A3): WARN (soft until {when}) — "
            f"{detail}")


# ── LAST-READ TRACKING (persisted; feeds the 14-day page) ────────────────────
def _ensure_table(db) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS registry_reads ("
        " surface TEXT PRIMARY KEY, last_read_ts REAL)")


def record_read(db, surface: str, ts: Optional[float] = None) -> None:
    """Stamp a surface as read NOW (a real consumer touched it)."""
    _ensure_table(db)
    db.execute(
        "INSERT INTO registry_reads (surface, last_read_ts) VALUES (?,?)"
        " ON CONFLICT(surface) DO UPDATE SET last_read_ts=excluded.last_read_ts",
        (surface, ts if ts is not None else time.time()))
    db.commit()


def last_read(db, surface: str) -> Optional[float]:
    _ensure_table(db)
    row = db.execute("SELECT last_read_ts FROM registry_reads WHERE surface=?",
                     (surface,)).fetchone()
    return row[0] if row else None


def unread_surfaces(db, now: Optional[float] = None,
                    max_age_days: int = UNREAD_PAGE_DAYS) -> List[str]:
    """Registered surfaces never read, or last read > max_age_days ago."""
    if now is None:
        now = time.time()
    cutoff = now - max_age_days * 86400.0
    out = []
    for s in sorted(_REGISTRY):
        lr = last_read(db, s)
        if lr is None or lr < cutoff:
            out.append(s)
    return out


def page_unread(db, now: Optional[float] = None, fail_fn=None) -> List[str]:
    """Page DATA_WITHOUT_QUESTION for every surface unread past the window.
    Returns the paged surfaces. Non-fatal — a page, not a halt."""
    if now is None:
        now = time.time()
    stale = unread_surfaces(db, now)
    if stale:
        detail = (f"surfaces unread > {UNREAD_PAGE_DAYS}d: {', '.join(stale)} — "
                  "data nobody asks a question of; retire it or name its reader")
        if fail_fn is not None:
            fail_fn("DATA_WITHOUT_QUESTION", detail, alert=False)
        else:
            from . import failures
            failures.fail("DATA_WITHOUT_QUESTION", detail, alert=False)
    return stale


def _age(now: float, ts: Optional[float]) -> str:
    if ts is None:
        return "never read"
    days = (now - ts) / 86400.0
    if days < 1:
        return f"{(now - ts) / 3600.0:.0f}h ago"
    return f"{days:.0f}d ago"


def registry_pack_lines(db=None, now: Optional[float] = None) -> List[str]:
    """The daily-pack section: the registry with each surface's last-read."""
    if now is None:
        now = time.time()
    lines = ["=== DATA-QUESTION REGISTRY (WO-P A3) === "
             f"({len(_REGISTRY)} surfaces; unread>{UNREAD_PAGE_DAYS}d pages)"]
    for q in all_questions():
        lr = last_read(db, q.surface) if db is not None else None
        lines.append(f"  {q.surface}: {q.question}  [read {_age(now, lr)}]")
        lines.append(f"      ↳ {q.validates_or_invalidates}")
        lines.append(f"      ↳ consumer: {q.consumer}")
    if db is not None:
        stale = unread_surfaces(db, now)
        if stale:
            lines.append(f"  ⚠ DATA_WITHOUT_QUESTION (unread > "
                         f"{UNREAD_PAGE_DAYS}d): {', '.join(stale)}")
    return lines


# seed at import so the registry is populated before boot/pack read it.
seed()
