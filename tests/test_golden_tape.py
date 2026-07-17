"""VERIFY GATE 5: golden-tape regression — the vendored LIVE F/H8 code and the
relay port replay the same tapes and must produce identical proposals/passes.
Corpus and comparison rules in tests/golden_harness.py; committed diff report
in docs/GOLDEN_TAPE_REPORT.md (regenerate: python tests/golden_harness.py)."""

from golden_harness import compare_all


def test_golden_tape_zero_mismatches():
    result = compare_all()
    assert result["ladder_total"] >= 26
    assert result["eval_total"] >= 2900
    assert result["mismatches"] == [], (
        f"{len(result['mismatches'])} golden-tape mismatches: "
        f"{[m[0] for m in result['mismatches'][:10]]}")
