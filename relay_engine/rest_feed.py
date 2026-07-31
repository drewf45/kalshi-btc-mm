"""RestFeed — P11 "PROVEN GROUND" §B1 + P11.1-a: the FeedLike drop-in.

The reversion borrows nothing new: a 1s poll fetches the venue's own
orderbook (venue.fetch_orderbook_raw — the fp-dollars parser certified in the
truth audit), synthesizes the snapshot frame the WS would have sent, and
pipes it through the INHERITED Feed.handle_frame. Everything the engine
learned the hard way rides along for free and unchanged:

  - the first-frame unit assertion (P5 §3)
  - true-touch fp retention (the parts' law)
  - the coherence invariant with its debounce (P10 §2 / CHUNK B)
  - the recorder — every synthesized snapshot IS the P11.1-c format, banked
    through the same record() path, replay-compatible by construction
  - the FeedLike surface the runner touches (books / book / drop_book /
    resync_needed / on_poison / frames_seen): the runner does not rewire.

Poison and resync are near-no-ops by construction: the next 1s poll IS the
resync (a fresh snapshot clears the poison exactly as P10 §2.1 wrote).

P11.1-d — the no-defaults law extended to the books the engine breathes:
a FAILED fetch means that market has NO book this cycle. The failure is
tagged (BOOK_FETCH_FAILED) and counted; the previous book is left untouched
with its old timestamp so the staleness bound — not a fabricated freshness —
governs what custody may still read. Never serve yesterday's book as today's.
"""

import json
import logging
import time
from typing import Optional

from . import failures
from .feed import Feed

log = logging.getLogger("relay.rest_feed")


class RestFeed(Feed):
    def __init__(self, ladder, recorder=None, on_poison=None):
        super().__init__(ladder, recorder=recorder, on_poison=on_poison)
        self.fetch_failed: set = set()   # markets whose LAST poll failed
        self.failed_fetches = 0          # lifetime counter (hourly line)

    def poll_market(self, client, market: str, now: Optional[float] = None) -> bool:
        """One REST poll → one synthesized snapshot through the frame
        pipeline. Returns True when the market has a fresh book this cycle."""
        from . import venue
        now = time.time() if now is None else now
        raw_ob = venue.fetch_orderbook_raw(client, market)
        if raw_ob is None:
            self.fetch_failed.add(market)
            self.failed_fetches += 1
            failures.fail("BOOK_FETCH_FAILED",
                          f"{market}: REST book fetch failed — NO book this "
                          f"cycle; staleness bounds custody's grace "
                          f"(never a stale fabrication)",
                          market=market, alert=False)
            return False
        self.fetch_failed.discard(market)
        frame = json.dumps({"type": "orderbook_snapshot",
                            "msg": {"market_ticker": market,
                                    "yes": raw_ob["yes"],
                                    "no": raw_ob["no"]}})
        self.handle_frame(frame, now=now)
        return True
