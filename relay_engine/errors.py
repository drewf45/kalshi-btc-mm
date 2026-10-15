"""Fail-loud error types (§F: FATAL on integrity violations)."""


class FatalIntegrityError(RuntimeError):
    """An integrity violation. The engine halts and STAYS halted; no auto-recovery."""


class WallRejection(Exception):
    """An order rejected by a gateway wall. Carries the wall name for the surface row."""

    def __init__(self, wall: str, detail: str = ""):
        self.wall = wall
        self.detail = detail
        super().__init__(f"WALL_REJECT[{wall}] {detail}")


class GatedOnMissingInput(RuntimeError):
    """A code path that requires an input Drew has not yet provided (B1/B2)."""


class CountRejected(WallRejection):
    """WO-ROT-1 Chunk 0: a contract count the venue boundary refuses.

    Replaces the silent max-of-one count bump (venue.py place/amend): a
    count is never truncated, floored or bumped at the venue. `wall` carries the
    skip code — SKIP_BELOW_ONE_LOT (under one lot step) or COUNT_INVALID (not a
    finite positive number on the 0.01 grid) — so the gateway surfaces it as an
    ordinary wall reject with its why, and nothing is placed."""
