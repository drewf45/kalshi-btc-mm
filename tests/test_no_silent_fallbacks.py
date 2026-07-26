"""WO-2026-07-26-P §A2 — THE NO-SILENT-FALLBACKS LINT.

The one-lot bug was two silent fallbacks in the money path: a depth read
coerced to 0 (`book.visible_depth(side, price) or 0`) and a lot count floored
up to 1 (`max(1, contracts)`). Each turned an honest "I don't know / nothing
there" into a number, and a number nobody could trace bet real money.

This lint greps the sizing / depth / mark modules for that pattern FAMILY and
FAILS the build on any new one. A line that genuinely needs a floor (RULING-3's
depth-driven 1-lot, the REST feed's touch-parity guess) carries an inline
`fallback-audited: <reason>` marker naming why it is honest — the marker is the
audit trail, and an UNMARKED hit is a bug. The companion test plants a raw
`or 0` in a source snippet and proves the scanner catches it.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The money path: where a fabricated size / depth / mark becomes a real order.
MONEY_MODULES = ["sizing.py", "book.py", "scoring.py", "shadow_runner.py"]

# A line is HONEST-BY-AUDIT only if it carries this inline marker plus a reason.
EXEMPT_MARKER = "fallback-audited:"

# The banned pattern FAMILY. Each entry: (compiled regex, human name). The
# regexes are deliberately narrow — they target depth/mark/size operands so
# price clamps (`max(1, price - w)`), division guards (`max(1, price_cents)`)
# and coordinate math never trip them.
BANNED = [
    (re.compile(
        r"\.(visible_depth|joining_depth|band_depth|total_bid_depth|"
        r"book_cents|tradeable_cents|owed_cents|low_mark)\s*\([^)]*\)\s*or\b"),
     "depth/mark accessor result silently defaulted with `or` "
     "(the exact one-lot bug: a blind/empty read becomes a number)"),
    (re.compile(r"\b\w*(?:depth|_qty|mark)\w*\s+or\s+\d"),
     "depth/qty/mark value silently defaulted to a literal "
     "(a fabricated wall the book never showed)"),
    (re.compile(
        r"max\(\s*1\s*,[^)]*\b(contracts|count|lots|depth_max|notional_max|"
        r"size_lots)\b"),
     "a lot/depth count floored UP to 1 — fabricates a lot the math "
     "didn't produce (defer instead: SIZE_ZERO_DEFER)"),
]


def _scan_text(text: str):
    """Return [(lineno, line, why)] for every banned, unexempted hit."""
    hits = []
    for i, line in enumerate(text.splitlines(), start=1):
        if EXEMPT_MARKER in line:
            continue
        for rx, why in BANNED:
            if rx.search(line):
                hits.append((i, line.strip(), why))
    return hits


def test_no_silent_fallbacks_in_money_path():
    """No unaudited depth/mark/size fallback exists in the money modules."""
    findings = []
    for mod in MONEY_MODULES:
        path = ROOT / "relay_engine" / mod
        for lineno, line, why in _scan_text(path.read_text()):
            findings.append(f"{mod}:{lineno}: {why}\n    {line}")
    assert findings == [], (
        "silent-fallback family found in the money path — defer or read "
        "honestly, or mark the line `fallback-audited: <reason>` if it is a "
        "ruled honest floor:\n" + "\n".join(findings))


def test_lint_catches_a_planted_or_zero():
    """Proof the scanner has teeth: a raw `or 0` on a depth read is caught."""
    planted = (
        "def _score_and_size(self, proposal, book):\n"
        "    depth = book.visible_depth(side, price) or 0\n"  # the bug, revived
        "    contracts = max(1, contracts)\n"
    )
    hits = _scan_text(planted)
    assert len(hits) == 2, f"scanner missed a planted fallback: {hits}"
    joined = " ".join(w for _, _, w in hits)
    assert "one-lot bug" in joined
    assert "floored UP to 1" in joined


def test_marker_exempts_a_ruled_honest_floor():
    """An audited line (RULING-3's depth-driven floor) is NOT flagged."""
    honest = (
        "        depth_max = max(1, depth_max)  # fallback-audited: RULING-3 — "
        "a real book with >=1 visible lot admits 1 lot; depth-DRIVEN, not "
        "fabricated (a blind book already deferred as DEPTH_BLIND above)\n"
    )
    assert _scan_text(honest) == []
