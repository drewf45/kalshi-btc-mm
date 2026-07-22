"""§A4 grep-proof: no waterfall-era accounting code EVER EXISTS in this tree.

The forbidden tokens are assembled from fragments so this test file itself
doesn't trip its own scan. reference/ is excluded — it is quarantined, not code.
docs/ is excluded — the canon documents record the ruling that kills the thing
they name.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = ["water" + "fall", "scr" + "ape", "mark_" + "paid", "owed"]
SCAN_DIRS = ["relay_engine"]


def test_no_waterfall_era_tokens_in_engine():
    hits = []
    for d in SCAN_DIRS:
        for path in (ROOT / d).rglob("*.py"):
            text = path.read_text().lower()
            for token in FORBIDDEN:
                if re.search(rf"\b{token}\b", text):
                    hits.append(f"{path.relative_to(ROOT)}: {token}")
    assert hits == [], f"waterfall-era tokens found: {hits}"


def test_monolith_quarantined_and_never_imported():
    assert (ROOT / "reference" / "legacy_dump_bot.py").exists()
    assert not (ROOT / "bot.py").exists()
    for path in (ROOT / "relay_engine").rglob("*.py"):
        text = path.read_text()
        assert "legacy_dump_bot" not in text.replace("reference/legacy_dump_bot.py", ""), \
            f"{path} imports from the quarantined monolith"
        assert "import bot" not in text


def test_no_paid_command():
    """/paid is retired. The surface is the accounting pair plus /reset_halt
    (P8 §2.3) plus /scoreboard (P22 §5 — read-only) plus /clear_cash_fatal
    (P-CASH-FATAL-1 §4.4) plus /daily (WO-2026-07-22-K — the read-only day
    export). Still no order-shaped command."""
    from relay_engine.ops import Telegram
    assert Telegram.COMMANDS == ("/confirm_cash", "/deny_cash", "/reset_halt",
                                 "/scoreboard", "/clear_cash_fatal", "/daily")
    assert "/paid" not in Telegram.COMMANDS
