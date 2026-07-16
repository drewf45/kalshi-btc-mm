# f_worker/pricebrain.py
# The desk's read on the tape: live spot + realized-vol regime, the delta-table gate
# constants (seeded by Deliverable 0's crossing study), and the D4 window gate that
# decides SEEKING vs SAT_OUT.
#
# The BUILD ORDER's borrow list credits k_worker/delta_table_* for the "spine"; that
# module never existed in this repo. So the spine here is: (1) the crossing-study
# output file (flipdesk_gate.json) when present, else (2) baked-in boot defaults. Both
# express the same shape — a per-vol-regime view of how likely a 15M window is to let
# a cheap bundle be built and flipped. SAT_OUT is a POSITIVE stat (§3), so the gate is
# deliberately happy to say no.

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import requests

from .config import Config
from .lib import spot as spotlib
from .lib.marketutil import parse_best_yes_no


# Boot-default gate constants. Deliverable 0 replaces these via flipdesk_gate.json.
DEFAULT_GATE = {
    # realized sigma (USD per sqrt-second) regime cutpoints
    "sigma_low": 6.0,
    "sigma_high": 18.0,
    # a window is tradeable only if sigma is in [floor, ceil]: too calm never crosses a
    # strike (no flip), too wild is a coin-flip we won't feed at rung 1.
    "sigma_floor": 5.0,
    "sigma_ceil": 30.0,
    # book quality
    "max_build_cost": 99,      # yes_bid + no_bid must leave the bundle <= this (ties W1)
    "min_flip_headroom": 2,    # 100 - build_cost must be >= this (floor must beat old math)
    "max_leg_spread": 6,       # per-side bid/ask spread ceiling
    # expected both-legs-flip rate by regime (from the study; informational at rung 1)
    "flip_rate": {"low": 0.05, "mid": 0.35, "high": 0.30},
}


@dataclass
class GateDecision:
    ok: bool
    reason: str
    yes_price: Optional[int] = None   # proposed maker bid for the YES leg
    no_price: Optional[int] = None    # proposed maker bid for the NO leg
    regime: str = "unknown"
    evidence: Dict[str, Any] = field(default_factory=dict)


class PriceBrain:
    def __init__(self, config: Config, session: Optional[requests.Session] = None,
                 gate_path: str = "flipdesk_gate.json"):
        self.cfg = config
        self.session = session or requests.Session()
        self.gate = dict(DEFAULT_GATE)
        self._sigma = spotlib.SigmaCache(floor=DEFAULT_GATE["sigma_floor"],
                                         ceil=DEFAULT_GATE["sigma_ceil"])
        self._load_gate(gate_path)

    def _load_gate(self, path: str) -> None:
        """Load crossing-study gate constants if the study has shipped them."""
        try:
            if path and os.path.exists(path):
                with open(path) as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self.gate.update(loaded)
                    print(f"[pricebrain] loaded gate constants from {path}", flush=True)
        except Exception as e:
            print(f"[pricebrain] gate load failed ({e}); using boot defaults", flush=True)

    # ---- live reads ----
    def spot(self) -> Optional[float]:
        return spotlib.fetch_btc_spot_usd(self.session)

    def sigma(self, now: Optional[float] = None) -> float:
        return self._sigma.get(self.session, now=now)

    def vol_regime(self, sigma: float) -> str:
        if sigma < self.gate["sigma_low"]:
            return "low"
        if sigma >= self.gate["sigma_high"]:
            return "high"
        return "mid"

    # ---- the D4 window gate ----
    def gate_window(self, orderbook: Dict[str, Any], sigma: Optional[float] = None) -> GateDecision:
        """Decide whether this 15M window is worth one shot. Pure given its inputs, so
        it is unit-testable with a synthetic book."""
        if sigma is None:
            sigma = self.sigma()
        regime = self.vol_regime(sigma)
        yb, ya, nb, na = parse_best_yes_no(orderbook)
        ev: Dict[str, Any] = {"sigma": round(sigma, 3), "regime": regime,
                              "yes_bid": yb, "yes_ask": ya, "no_bid": nb, "no_ask": na}

        # vol regime gate
        if sigma < self.gate["sigma_floor"]:
            return GateDecision(False, "vol too calm to cross a strike", regime=regime, evidence=ev)
        if sigma > self.gate["sigma_ceil"]:
            return GateDecision(False, "vol too wild for rung 1", regime=regime, evidence=ev)

        # need a biddable price on both sides to build a bundle as a maker
        if yb is None or nb is None:
            return GateDecision(False, "book missing a side", regime=regime, evidence=ev)

        # per-side spread sanity (depth/quality proxy)
        for tag, bid, ask in (("yes", yb, ya), ("no", nb, na)):
            if ask is not None and (ask - bid) > self.gate["max_leg_spread"]:
                return GateDecision(False, f"{tag} spread {ask - bid} > {self.gate['max_leg_spread']}",
                                    regime=regime, evidence=ev)

        # propose joining the best bid on each side; that is the bundle we would hold
        yes_price, no_price = int(yb), int(nb)
        build_cost = yes_price + no_price
        ev["build_cost"] = build_cost
        ev["floor_headroom"] = 100 - build_cost

        if build_cost > min(self.gate["max_build_cost"], self.cfg.entry_line):
            return GateDecision(False, f"build cost {build_cost} > line", regime=regime, evidence=ev)
        if (100 - build_cost) < self.gate["min_flip_headroom"]:
            return GateDecision(False, f"floor headroom {100 - build_cost} < "
                                       f"{self.gate['min_flip_headroom']}", regime=regime, evidence=ev)

        return GateDecision(True, "one shot", yes_price=yes_price, no_price=no_price,
                            regime=regime, evidence=ev)

    def flip_ask_price(self, side: str, entry_price: int) -> int:
        """Boot flip target: entry + flip_x per side (§ targets: ~entry+5..7c)."""
        return min(99, int(entry_price) + int(self.cfg.flip_x))
