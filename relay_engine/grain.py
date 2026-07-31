"""Grain — the herd's compass (P21 A3).

SOURCE (Drew, voice, 0718): "if the last three markets have been down and
it's 49/49, you buy no — that's what everyone already thinks." Gamblers bet
continuation and the app shows them the last windows; we read the herd's own
screen, not BTC's future.

Data: OUR window_outcomes table (written wherever settled_yes is learned —
the settlement sweep's lineage, P8/P13). Traded windows populate it today;
that partial view is itself part of the grain QUESTION in the registry.
"""

from typing import Optional

from . import config


def grain(ledger, k: Optional[int] = None) -> Optional[dict]:
    """The last-K window outcomes → streak direction/length, or None when the
    screen is empty. direction is the most recent outcome's side; length is
    how many consecutive windows agree with it (from most recent backwards)."""
    k = config.GRAIN_K if k is None else k
    rows = ledger.db.execute(
        "SELECT settled_yes FROM window_outcomes ORDER BY ts DESC, id DESC"
        " LIMIT ?", (k,)).fetchall()
    if not rows:
        return None
    latest = bool(rows[0][0])
    length = 0
    for (s,) in rows:
        if bool(s) == latest:
            length += 1
        else:
            break
    return {"direction": "yes" if latest else "no", "length": length, "k": k}
