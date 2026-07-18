"""Replay harness — the recorder's named reader, now real (P7).

Drives recorded (or synthetic real-shaped) WS frames through a fresh Feed and
reports, per frame, what lane F would see: the favorite side and its cost from
THE book — the same OrderBook every lane reads (one book, one truth). The
regression test replays a live-shaped tape (fp-dollars snapshot + deltas) and
asserts F tracks the favorite; `replay_from_db` replays the book_snapshots
table itself, closing the streams-name-readers loop.
"""

from typing import List, Optional, Tuple

from .book import touch_view
from .feed import DegradeLadder, Feed
from .lane_fh8 import favorite_side


def replay_frames(frames, now_start: float = 0.0) -> List[Tuple[str, Optional[str], Optional[float]]]:
    """Feed raw frames through a fresh Feed. Returns per-frame
    (market, favorite_side, favorite_cost_cents) — None side = F is blind."""
    feed = Feed(DegradeLadder())
    out = []
    for i, raw in enumerate(frames):
        try:
            feed.handle_frame(raw, now=now_start + i)
        except Exception:
            out.append(("<frame-error>", None, None))
            continue
        for market, book in feed.books.items():
            side, cost_d, _yq, _fp = favorite_side(touch_view(book))
            out.append((market, side, float(cost_d) if cost_d is not None else None))
    return out


def replay_from_db(ledger, limit: int = 10_000):
    """Replay the recorder's own tape (book_snapshots) — the reader the stream
    named at birth."""
    rows = ledger.db.execute(
        "SELECT snapshot FROM book_snapshots ORDER BY id LIMIT ?", (limit,)).fetchall()
    return replay_frames([r[0] for r in rows])
