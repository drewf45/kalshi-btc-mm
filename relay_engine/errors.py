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
