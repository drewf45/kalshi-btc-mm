"""The doctrine organ (P21 Part B) — docs/SEMANTICS.md, executable.

B1: the registry is one page of KNOWNs, each `LAW · CODE · TAPE`. This
module parses it.
B3: validate_registry() is the registry's own wall — every entry MUST name
an existing tape_grade line (Adversary: "the registry could rot — that
rule is the wall"). A test greps through here; the P21 tape suite re-runs
it live.
B2: drift_section() re-runs EVERY registered answer's tape test in EVERY
daily pack — registry tests never retire. A failing KNOWN auto-demotes to
QUESTION in the pack, pages `⚠ KNOWLEDGE_DRIFT [{answer}]` ONCE per
transition, and blocks nothing: reality outranks the registry; the
registry records the demotion; the Saturday retro re-proves or amends
(Answer Ledger §4.2 — findings are UNPROVEN until the tape confirms the
mechanism).
"""

import os
import re
import time
from typing import Dict, List, Optional

REGISTRY_PATH = os.path.join(os.path.dirname(__file__), "..", "docs",
                             "SEMANTICS.md")

_ENTRY_RE = re.compile(r"^### \d+\.\s+(?P<answer>.+?)\s*$")
_FIELD_RE = re.compile(r"^- (?P<key>LAW|CODE|TAPE):\s*(?P<val>.*)$")


def parse_registry(path: Optional[str] = None) -> List[dict]:
    """Every KNOWN as {answer, law, code, tape}. Field values strip the
    markdown backticks; LAW may wrap lines (continuations are folded)."""
    path = REGISTRY_PATH if path is None else path
    entries: List[dict] = []
    cur: Optional[dict] = None
    cur_key: Optional[str] = None
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if line.startswith("## QUESTION"):
                break            # questions are articulated ignorance, not KNOWNs
            m = _ENTRY_RE.match(line)
            if m:
                cur = {"answer": m.group("answer"), "law": "", "code": "",
                       "tape": ""}
                entries.append(cur)
                cur_key = None
                continue
            if cur is None:
                continue
            fm = _FIELD_RE.match(line)
            if fm:
                cur_key = fm.group("key").lower()
                cur[cur_key] = fm.group("val").strip().strip("`")
            elif cur_key and line.startswith("  ") and line.strip():
                cur[cur_key] = (cur[cur_key] + " " + line.strip()).strip("`")
    return entries


def tape_lines() -> Dict[str, object]:
    """Every tape_grade line across every suite, name -> check fn."""
    from scripts import tape_grade as tg
    lines: Dict[str, object] = {}
    for suite in (tg.CHECKS, tg.CHECKS_P16, tg.CHECKS_P17, tg.CHECKS_P18,
                  tg.CHECKS_P19, tg.CHECKS_P21, tg.CHECKS_P22,
                  tg.CHECKS_P24):
        for name, fn in suite:
            lines[name] = fn
    return lines


def validate_registry(path: Optional[str] = None) -> List[str]:
    """B3 — the wall. Returns [] when clean, else one error per rot."""
    errors: List[str] = []
    try:
        entries = parse_registry(path)
    except OSError as e:
        return [f"SEMANTICS.md unreadable: {e}"]
    if not entries:
        return ["SEMANTICS.md parsed to zero entries"]
    known = tape_lines()
    for e in entries:
        if not e["tape"]:
            errors.append(f"[{e['answer']}] has no TAPE field")
        elif e["tape"] not in known:
            errors.append(f"[{e['answer']}] names unknown tape line "
                          f"'{e['tape']}'")
        if not e["code"]:
            errors.append(f"[{e['answer']}] has no CODE citation")
        if not e["law"]:
            errors.append(f"[{e['answer']}] has no LAW")
    return errors


def drift_section(ledger, window_hours: float = 24.0, now=None) -> List[str]:
    """B2 — run every KNOWN's tape test; report, demote, page on transition.
    Returns the daily pack's DOCTRINE lines. Blocks nothing, ever."""
    now = time.time() if now is None else now
    since = now - window_hours * 3600
    lines: List[str] = []
    errs = validate_registry()
    if errs:
        # The wall itself caught rot — that is a drift of answer 12's class.
        lines.append(f"DOCTRINE: registry ROT — {'; '.join(errs[:3])}")
        return lines
    entries = parse_registry()
    known = tape_lines()
    drifted: List[str] = []
    for e in entries:
        fn = known[e["tape"]]
        try:
            ok, detail = fn(ledger.db, since)
        except Exception as exc:
            ok, detail = False, f"grader error: {exc}"
        state_key = f"drift_{re.sub(r'[^a-z0-9]+', '_', e['answer'].lower())}"
        was_drifted = ledger.get_state(state_key) == "1"
        if ok:
            if was_drifted:
                ledger.set_state(state_key, "0")
                lines.append(f"  ✓ re-proven: [{e['answer']}] — {detail}")
        else:
            drifted.append(f"  ⚠ QUESTION (demoted): [{e['answer']}] — "
                           f"tape '{e['tape']}' failed: {detail}")
            if not was_drifted:
                ledger.set_state(state_key, "1")
                from . import failures
                try:
                    failures.fail(
                        "KNOWLEDGE_DRIFT",
                        f"⚠ KNOWLEDGE_DRIFT [{e['answer']}] — its tape test "
                        f"'{e['tape']}' failed ({detail}); demoted to "
                        f"QUESTION pending Saturday re-proof. Blocks nothing.",
                        answer=e["answer"], tape=e["tape"])
                except Exception:
                    pass  # a page failure never blocks the pack
    header = (f"DOCTRINE (SEMANTICS.md): {len(entries) - len(drifted)}"
              f"/{len(entries)} KNOWN green")
    if drifted:
        return [header] + drifted + lines
    return [header + " — the machine believes what it says"] + lines
