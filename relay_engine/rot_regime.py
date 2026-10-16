"""WO-ROT-REGIME-1 — the ROT lane's turbulence gate (entries only).

The RULED finding this exists to defend: lane ROT's mean-reversion edge is REAL
in quiet/low-vol tape and INVERTS into adverse selection on high-volatility
trending macro days — the #1 risk is resting liquidity into a big directional
move and carrying the loser into a total-loss binary settlement. The fix is to
STAND ASIDE on new entries while the underlying is turbulent and resume when it
is calm again. EXITS ARE NEVER GATED — cash-outs, the soft/time/late walks and
the hard-stop CUT fire regardless of regime, always. This file only reads spot
and names a regime; the gate itself lives on the ENTRY path in lane_rot.py.

THE SIGNAL (no new data source, no numpy/scipy — stdlib math only):
  * ROT already hands the desk spot_fn(underlying) -> Optional[float] (the same
    feed the soft-stop confirm reads). Once per cycle the desk calls
    observe(underlying, spot, now) for every underlying it is about to price.
  * Per underlying a small time-ordered ring buffer keeps the last
    ROT_REGIME_WINDOW_S of samples. The vol metric is the MEAN ABSOLUTE
    per-sample move expressed in BASIS POINTS of spot (|Δ| / prev, in bps) —
    the simpler robust choice over a returns stdev: one outlier print does not
    dominate, and bps is comparable across BTC (~$100k) and XRP (~$2) without a
    per-underlying scale knob.
  * regime(underlying) classifies that metric: >= ROT_REGIME_TURBULENT_BPS is
    TURBULENT; <= ROT_REGIME_QUIET_BPS is QUIET; between is NORMAL. Until there
    are ROT_REGIME_MIN_SAMPLES fresh samples the regime is UNKNOWN and the gate
    does NOT fire (no proof of calm is not proof of turbulence; the desk still
    trades, exactly as it does today with no regime logic at all).

OFF BY DEFAULT. ROT_REGIME_ENABLED defaults False. When False the tracker is a
no-op: observe() records nothing and regime() returns None, so the entry gate
is byte-identical to the desk with no regime code at all (a live desk runs this
— the default MUST change nothing).
"""

import math
from collections import deque
from typing import Deque, Dict, Optional, Tuple

from . import config

# ── regime labels ────────────────────────────────────────────────
QUIET = "QUIET"
NORMAL = "NORMAL"
TURBULENT = "TURBULENT"
UNKNOWN = "UNKNOWN"     # not enough fresh samples yet — the gate never fires


class RotRegime:
    """Per-underlying realized-volatility tracker and QUIET/NORMAL/TURBULENT
    classifier for the ROT entry gate. Pure of I/O: the desk feeds it spot.

    Config is read live on every call (never snapshotted) so a dial change —
    ROT_REGIME_ENABLED flipped on, a threshold moved — takes on the next cycle,
    the same way every other ROT knob is read fresh each cycle."""

    def __init__(self) -> None:
        # underlying -> deque[(ts, spot)], oldest first, pruned to the window
        self._samples: Dict[str, Deque[Tuple[float, float]]] = {}

    # ── sampling ─────────────────────────────────────────────────
    def observe(self, underlying: str, spot: Optional[float], now: float) -> None:
        """Record one spot sample for the underlying, then prune anything older
        than the window. A None spot (no fresh feed) is NOT recorded — a gap in
        the feed is not a price move. A no-op while disabled."""
        if not config.ROT_REGIME_ENABLED:
            return
        if spot is None or not math.isfinite(spot) or spot <= 0:
            return
        buf = self._samples.get(underlying)
        if buf is None:
            buf = self._samples[underlying] = deque()
        buf.append((float(now), float(spot)))
        self._prune(buf, now)

    def _prune(self, buf: "Deque[Tuple[float, float]]", now: float) -> None:
        cutoff = now - config.ROT_REGIME_WINDOW_S
        while buf and buf[0][0] < cutoff:
            buf.popleft()

    # ── the metric ───────────────────────────────────────────────
    def vol_bps(self, underlying: str, now: Optional[float] = None) -> Optional[float]:
        """Mean absolute per-sample move over the window, in basis points of
        spot. None until there are ROT_REGIME_MIN_SAMPLES fresh samples (so the
        caller knows the regime is UNKNOWN, not calm). A no-op while disabled."""
        if not config.ROT_REGIME_ENABLED:
            return None
        buf = self._samples.get(underlying)
        if not buf:
            return None
        if now is not None:
            self._prune(buf, now)
        if len(buf) < config.ROT_REGIME_MIN_SAMPLES:
            return None
        moves = []
        prev = buf[0][1]
        for _ts, px in list(buf)[1:]:
            if prev > 0:
                moves.append(abs(px - prev) / prev * 10_000.0)
            prev = px
        if not moves:
            return None
        return sum(moves) / len(moves)

    # ── the classification ───────────────────────────────────────
    def regime(self, underlying: str, now: Optional[float] = None) -> Optional[str]:
        """QUIET / NORMAL / TURBULENT for the underlying, UNKNOWN when there is
        not yet enough tape to say. None while disabled — the one value the
        entry gate treats as 'no opinion, trade as before'."""
        if not config.ROT_REGIME_ENABLED:
            return None
        v = self.vol_bps(underlying, now)
        if v is None:
            return UNKNOWN
        if v >= config.ROT_REGIME_TURBULENT_BPS:
            return TURBULENT
        if v <= config.ROT_REGIME_QUIET_BPS:
            return QUIET
        return NORMAL

    def blocks_entry(self, underlying: str, now: Optional[float] = None) -> bool:
        """The whole contract of the gate in one predicate: True ONLY when the
        underlying is classified TURBULENT. Disabled, UNKNOWN, QUIET and NORMAL
        all return False (trade). This never considers exits — the desk only
        ever calls it on the entry path."""
        if not config.ROT_REGIME_ENABLED:
            return False
        return self.regime(underlying, now) == TURBULENT
