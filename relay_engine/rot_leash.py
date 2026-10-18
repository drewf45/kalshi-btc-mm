"""WO-ROT-LEASH-1 — the odds-driven, time-decaying leash for Lane A (ROT).

RULED 2026-10-16 (Drew). The stop is not a fixed number of cents. The leash is
WIDE when the market is genuinely UNDETERMINED — a coin-flip you must not sell
into, because a dip there is noise, not a loss — and TIGHTENS as the market
DECIDES against the held side. Odds-driven, never clock-driven: a market that
decides early tightens early even with time left; one that stays a coin-flip
keeps its long leash. (The clock still ends the market via the late window and
settlement — that machinery is untouched.)

HOW "DECIDED" IS MEASURED, stdlib only, no new feed
  The market's own price IS its probability estimate: a held side quoted at
  `bid` cents is the market pricing that side at bid% to win. So the held
  side's LOSE probability is simply (100 - bid)/100. bid near 50 = undetermined
  (wide leash); bid falling toward 0 = the market deciding against us (tight
  leash). This is the purest "how decided is this?" signal and needs nothing
  the inside loop does not already read every cycle.

  Optionally, when a strike + running-60s-average feed is available (a caller's
  own odds engine; Lane END no longer ships one — cost is its signal), a model p_flip
  for the held side and we take the MORE PESSIMISTIC of the two (max lose_prob)
  — the model catches a near-expiry average sitting on the line that the quote
  has not yet repriced. Absent that feed, the quote alone drives the leash.

THE MAP (config dials)
  lose_prob in [0,1] (held side's probability of losing)
  below ROT_LEASH_UNDETERMINED_P  -> pinned WIDE  (truly undetermined)
  at/above ROT_LEASH_DECIDED_P    -> pinned TIGHT  (decided against us)
  linear between:
     t = (lose_prob - UNDETERMINED_P) / (DECIDED_P - UNDETERMINED_P)
     stop_cents = round(WIDE + t * (TIGHT - WIDE))     # WIDE >= TIGHT, so shrinks
  The fixed ROT_HARD_STOP_CENTS stays a HARD backstop: the leash never permits a
  loss deeper than it (stop_cents = min(stop_cents, ROT_HARD_STOP_CENTS)).

OFF BY DEFAULT: ROT_LEASH_ENABLED False => leash_stop_cents returns None and the
caller uses the fixed dials unchanged — byte-identical to the pre-leash desk.
"""

from typing import Optional

from . import config


def lose_prob_from_quote(bid: int) -> float:
    """The held side's probability of LOSING, implied by its own bid price.
    A side quoted at `bid`c is the market pricing it bid% to win, so its lose
    probability is (100 - bid)/100. Clamped to [0,1]."""
    p = (100.0 - float(bid)) / 100.0
    return max(0.0, min(1.0, p))


def leash_stop_cents(bid: int,
                     model_lose_prob: Optional[float] = None) -> Optional[int]:
    """The odds-driven hard-stop distance in cents for a held position, given
    its current `bid` (YES-terms cost of the held side) and, optionally, a model
    lose-probability from the p_flip odds engine. Returns None when the leash is
    disabled (caller falls back to the fixed ROT_HARD_STOP_CENTS).

    Wide when undetermined, tight when decided against; never deeper than the
    fixed hard-stop backstop. The MORE PESSIMISTIC of quote-implied and model
    lose-prob drives it, so a near-expiry average on the line tightens the leash
    even if the quote lags."""
    if not config.ROT_LEASH_ENABLED:
        return None

    lose_prob = lose_prob_from_quote(bid)
    if model_lose_prob is not None:
        lose_prob = max(lose_prob, max(0.0, min(1.0, model_lose_prob)))

    wide = int(config.ROT_LEASH_WIDE_CENTS)
    tight = int(config.ROT_LEASH_TIGHT_CENTS)
    lo = float(config.ROT_LEASH_UNDETERMINED_P)
    hi = float(config.ROT_LEASH_DECIDED_P)

    if lose_prob <= lo:
        stop = wide                       # truly undetermined: widest leash
    elif lose_prob >= hi or hi <= lo:
        stop = tight                      # decided against us: tightest leash
    else:
        t = (lose_prob - lo) / (hi - lo)
        stop = round(wide + t * (tight - wide))

    # WIDE is the absolute backstop: the leash's own widest rung is the
    # deepest loss ever allowed. (The fixed ROT_HARD_STOP_CENTS governs ONLY
    # when the leash is disabled — not here, or it would clamp the wide early
    # leash away. Drew 2026-10-16: the backstop must be the WIDEST the leash
    # can be, not the tightest.)
    stop = min(int(stop), wide)
    return max(1, int(stop))


def leash_describe(bid: int, model_lose_prob: Optional[float] = None) -> str:
    """A one-line tape of the leash decision (for why_log / events)."""
    if not config.ROT_LEASH_ENABLED:
        return "leash OFF (fixed dials)"
    lp = lose_prob_from_quote(bid)
    src = "quote"
    if model_lose_prob is not None and model_lose_prob > lp:
        lp = max(0.0, min(1.0, model_lose_prob))
        src = "model"
    s = leash_stop_cents(bid, model_lose_prob)
    phase = ("UNDETERMINED" if lp <= config.ROT_LEASH_UNDETERMINED_P
             else "DECIDED" if lp >= config.ROT_LEASH_DECIDED_P
             else "DECIDING")
    return (f"leash {s}c ({phase}: lose_prob {lp:.2f} via {src}, "
            f"bid {bid}c, backstop {config.ROT_LEASH_WIDE_CENTS}c)")
