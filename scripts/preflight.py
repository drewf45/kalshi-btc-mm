"""CHUNK C — THE FULL-TRUTH REGRESSION GATE. One command proves the doctrine:

    python -m scripts.preflight

Run before go-live and after ANY future deploy. Each line is a doctrine claim
backed by the named tests, executed for real (no caching, no trust): the
suite, the 01:25 replay through the deployed wiring, the golden tape, the
cash protocol's both branches, the two-strike leash across a restart, the
no-paper-in-live law, the poison→REST→resync→quarantine chain, and the ear.
This is the standing deploy gate from now on.
"""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CHECKS = [
    ("full pytest suite", ["tests"]),
    ("01:25 replay end-to-end through the deployed pipeline (P10 A4)", [
        "tests/test_p10_book_truth.py::test_0125_replay_end_to_end_zero_lying_proposals",
        "tests/test_p10_book_truth.py::test_corrupting_frame_teeth_both_directions",
    ]),
    ("golden-tape F/H8 regression", ["tests/test_golden_tape.py"]),
    ("synthetic ±$5 cash, both branches", [
        "tests/test_cash_protocol.py::test_negative_five_confirm_branch_resumes",
        "tests/test_cash_protocol.py::test_negative_five_deny_branch_stays_fatal",
        "tests/test_cash_protocol.py::test_positive_five_confirms_without_halt",
    ]),
    ("two-strike halt → restart persistence → /reset_halt", [
        "tests/test_p8_golive.py::test_two_negatives_halt_and_page",
        "tests/test_p8_golive.py::test_halt_persists_across_restart",
        "tests/test_p9_real_numbers.py::test_reset_halt_full_round_trip",
    ]),
    ("live brackets reject paper (P9 §2)", [
        "tests/test_p9_real_numbers.py::test_live_bracket_rejects_paper_source",
        "tests/test_p9_real_numbers.py::test_live_close_rejects_paper_source",
        "tests/test_p9_real_numbers.py::test_live_failed_read_never_returns_ledger_book",
    ]),
    ("crossed book: poison → REST marks → resync → quarantine ceiling", [
        "tests/test_p10_book_truth.py::test_crossed_book_poisons_pages_once_and_resyncs",
        "tests/test_p10_book_truth.py::test_custodian_marks_flip_to_rest_on_poison",
        "tests/test_p10_book_truth.py::test_clean_snapshot_clears_poison",
        "tests/test_p10_book_truth.py::test_three_episodes_quarantine_one_page",
    ]),
    ("listener round-trip (the ear is alive)", [
        "tests/test_p9_real_numbers.py::test_garbage_text_gets_the_refusal_line",
        "tests/test_p9_real_numbers.py::test_supervisor_banks_death_and_restarts",
    ]),
    ("the LIVE spine, dry-proven end-to-end (Chunk D)", [
        "tests/test_go_live_dry_run.py::test_go_live_dry_run",
    ]),
    ("proven ground: REST feed drop-in, governor, no-fabrication (P11)", [
        "tests/test_p11_proven_ground.py",
    ]),
    ("say what you did: narration, form map, orientation sentinels (P13)", [
        "tests/test_p13_narration.py",
    ]),
    ("cut only what you hold: tri-state cancel + re-derive (P14)", [
        "tests/test_p14_cut_law.py",
        "tests/test_custodian.py::test_baton_gone_exit_is_terminal_not_fatal",
    ]),
    ("ratifications: orphans, depth floor, gross wall (P15; pair→OPEN per P21)", [
        "tests/test_p15_ratifications.py",
    ]),
    ("the scalp profile, funded: db chain, rescale, deposit tape (P16)", [
        "tests/test_p16_funded.py",
    ]),
    ("show up for every market: lattice, late truths, window contract (P17)", [
        "tests/test_p17_show_up.py",
        "tests/test_attribution.py",
    ]),
    ("the detective: needle equation, two jobs, P yields the floor (P18)", [
        "tests/test_p18_detective.py",
        "tests/test_p15_ratifications.py::test_pair_formable_posts_the_favored_side",
    ]),
    ("salvage, seal, let it run: the custodian earns F (P19)", [
        "tests/test_p19_salvage.py",
    ]),
    ("the doctrine engine: netting, grain, patient hold, registry (P21)", [
        "tests/test_p21_doctrine.py",
        "tests/test_lane_flip.py",
    ]),
    ("the cell scoreboard: score every close, price every bar (P22)", [
        "tests/test_p22_scoreboard.py",
        "tests/test_sizing.py",
    ]),
    ("shield, fees, and the zero in the reversal (P24)", [
        "tests/test_p24_shield_fees_reversal.py",
        "tests/test_p19_salvage.py",
    ]),
    ("every why is a proof: one brain, the wall, OPEN's fixes (P26)", [
        "tests/test_p26_proof_law.py",
        "tests/test_p21_doctrine.py",
    ]),
    ("the interrogator: questions in code, answers on the phone (DIAG-1)", [
        "tests/test_diag1_interrogator.py",
    ]),
    ("the governor is the halt: full Kelly, one leash (P27)", [
        "tests/test_p27_governor.py",
        "tests/test_p8_golive.py",
    ]),
]


def run_check(targets):
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header", *targets],
        cwd=ROOT, capture_output=True, text=True)
    out = proc.stdout + proc.stderr
    m = re.search(r"(\d+) passed", out)
    passed = int(m.group(1)) if m else 0
    ok = proc.returncode == 0 and passed > 0 and "failed" not in out
    return ok, passed, out


def main() -> int:
    print("PREFLIGHT — the full-truth regression gate")
    print("=" * 66)
    green = 0
    failures_out = []
    for name, targets in CHECKS:
        ok, passed, out = run_check(targets)
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {name} ({passed} test{'s' if passed != 1 else ''})")
        if ok:
            green += 1
        else:
            failures_out.append((name, out))
    print("=" * 66)
    if failures_out:
        for name, out in failures_out:
            print(f"\n--- FAILED: {name} ---")
            print("\n".join(out.splitlines()[-25:]))
        print(f"\nPREFLIGHT: {green}/{len(CHECKS)} — DO NOT GO LIVE.")
        return 1
    print(f"PREFLIGHT: {green}/{len(CHECKS)} — the book cannot lie, "
          f"the money cannot lie, the ear is alive.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
