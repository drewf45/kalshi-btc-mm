"""WO-DIAG-1 "THE INTERROGATOR" — questions ship as code, answers come
back on the phone: sentinel once-only (§1.1), chunked paging, DIAG-001's
histogram + quoted table semantics + verdict hints (§0/§1.2), DIAG-002
coverage (§1.3), the env-armed proof fork with era stamps (§2), and the
permanent DIAGNOSTICS pack section (§3)."""

import time

import pytest

from relay_engine import config, delta, diagnostics, failures
from relay_engine.gateway import Order

TICKER = "KXBTC15M-02JAN251000-T99"
EVENT = TICKER.rsplit("-", 1)[0]


class _Eng:
    """The interrogator needs only ledger + telegram."""

    def __init__(self, ledger):
        self.ledger = ledger
        self.sent = []
        self.telegram = type("T", (), {})()
        self.telegram.alert = self.sent.append


@pytest.fixture
def eng(ledger, surface):
    failures.configure(ledger, alert_fn=lambda m: None, run_mode="TEST",
                       boot_id=1)
    e = _Eng(ledger)
    e.surface = surface
    yield e
    failures._ledger = None


def _seed_f_pass(surface, detail, market=TICKER, when=None):
    surface.write_row("F", market, f"w-{market}", "WATCHING", detail=detail)


# ── §1.1: sentinel once-only + chunked paging ──────────────────────────────
def test_diagnostics_run_once_ever(eng):
    n1 = diagnostics.run_boot_diagnostics(eng)
    assert n1 == len(diagnostics.REGISTRY)
    assert eng.ledger.get_state("diag_done:DIAG-001") == "1"
    pages_after_first = len(eng.sent)
    assert pages_after_first >= 2          # both diags paged something
    # a restart (second run) pages NOTHING — the sentinel holds
    assert diagnostics.run_boot_diagnostics(eng) == 0
    assert len(eng.sent) == pages_after_first


def test_chunked_paging_respects_the_limit():
    text = "\n".join(f"line {i} " + "x" * 80 for i in range(200))
    chunks = diagnostics._chunk(text)
    assert len(chunks) > 1
    assert all(len(c) <= diagnostics.TELEGRAM_CHUNK for c in chunks)
    assert "\n".join(chunks) == text       # nothing lost in the mail


def test_diagnostic_error_still_closes_and_pages(eng, monkeypatch):
    monkeypatch.setitem(diagnostics.__dict__, "REGISTRY",
                        [("DIAG-BOOM", lambda e, now=None: 1 / 0)])
    assert diagnostics.run_boot_diagnostics(eng) == 1
    assert any("DIAG-BOOM" in m and "error" in m for m in eng.sent)
    assert eng.ledger.get_state("diag_done:DIAG-BOOM") == "1"


# ── DIAG-001: the histogram that separates the hypotheses ──────────────────
def test_diag001_uniform_gaps_hint_semantics(eng, surface):
    for i, surv in enumerate((0.92, 0.93, 0.91, 0.94)):
        _seed_f_pass(surface,
                     f"TABLE_NON_REVERSAL p={surv:.2f}<bar0.96 d=120 t=540",
                     market=f"M{i}")
    out = diagnostics.diag_f_silence(eng)
    assert "🔎 DIAG F-SILENCE: 4 passes" in out
    assert "TABLE_NON_REVERSAL: 4" in out
    assert "uniform 2-6pt? yes" in out
    assert "H-SEMANTICS" in out and "F_PROOF_MODE=v2" in out
    # the quoted builder line settles the units question verbatim
    assert 'TABLE SEMANTICS: "p_cross measures ANY-TOUCH' in out
    assert "(0.92,0.96,120,540)" in out    # the sample carries (d,t)


def test_diag001_varied_gaps_hint_market(eng, surface):
    for i, (surv, bar) in enumerate(((0.50, 0.96), (0.93, 0.96),
                                     (0.20, 0.95), (0.90, 0.97))):
        _seed_f_pass(surface,
                     f"TABLE_NON_REVERSAL p={surv:.2f}<bar{bar:.2f}",
                     market=f"M{i}")
    out = diagnostics.diag_f_silence(eng)
    assert "uniform 2-6pt? no" in out
    assert "H-MARKET" in out and "Touch nothing" in out
    assert "note: (d,t) absent on pre-DIAG rows" in out  # honest gap


def test_diag001_varied_reasons_hint_discipline(eng, surface):
    _seed_f_pass(surface, "TABLE_NON_REVERSAL p=0.50<bar0.96", market="M0")
    for i, r in enumerate(("COST_IN_H8_BAND", "NO_BOOK", "ENTRY_HALT",
                           "COST_IN_H8_BAND")):
        _seed_f_pass(surface, r, market=f"M{i + 1}")
    out = diagnostics.diag_f_silence(eng)
    assert "DISCIPLINE" in out


def test_diag001_honest_when_empty(eng):
    out = diagnostics.diag_f_silence(eng)
    assert "NO F pass rows" in out and "honestly absent" in out


# ── DIAG-002: coverage ─────────────────────────────────────────────────────
def test_diag002_summarizes_misses_and_bounds(eng, monkeypatch):
    for d, t in ((2000, 900), (2000, 900), (1500, 60)):
        failures.fail("TABLE_CELL_MISS", f"grid gap: d={d} t={t}",
                      alert=False, d=d, t=t, session="ALL")
    monkeypatch.setattr(delta, "_LOADED", True)
    monkeypatch.setattr(delta, "_TABLE",
                        {(50, 60, "ALL"): {}, (2000, 900, "ALL"): {}})
    out = diagnostics.diag_table_coverage(eng)
    assert "miss d=2000 t=900" in out and "×2" in out
    assert "loaded grid: d $50-$2000 · t 60-900s · cells 2" in out


def test_diag002_honest_when_no_misses(eng, monkeypatch):
    monkeypatch.setattr(delta, "_LOADED", False)
    monkeypatch.setattr(delta, "refusal_reason", lambda: "CSV file missing")
    out = diagnostics.diag_table_coverage(eng)
    assert "zero persisted (d,t) misses" in out
    assert "table not loaded" in out


# ── §2: the fix ships blind, armed by env ──────────────────────────────────
def _gate_ctx():
    return {"spot": 118_050.0, "close_ts": 1000.0, "now": 500.0,
            "boundary_hi": 118_000.0, "book": None}


def test_proof_mode_v1_compares_price_and_stamps(monkeypatch):
    from relay_engine.lane_fh8 import EvalResult
    from relay_engine.lanes import LaneF
    monkeypatch.setattr(config, "F_PROOF_MODE", "v1")
    monkeypatch.setattr(delta, "is_loaded", lambda: True)
    monkeypatch.setattr(delta, "p_survive", lambda d, t, session="ALL": 0.93)
    shared = type("S", (), {})()
    shared.decide = lambda m, c: ("PROPOSE", res)
    shared.to_order = lambda m, r: Order(
        lane="F", event=EVENT, market=TICKER, side="yes", action="buy",
        price_cents=95, count=1, size_tier=config.TIER_PROBE,
        purpose="ENTRY", why="F tier95")
    res = type("R", (), {"lane": "F"})()
    lane = LaneF(shared)
    d = lane.evaluate(TICKER, _gate_ctx())
    # v1: surv .93 < bar .95 -> the mute, with the full cell + era stamped
    assert d.proposal is None
    assert "TABLE_NON_REVERSAL p=0.93<bar0.95" in d.pass_reason
    assert "d=50 t=500 proof=v1" in d.pass_reason


def test_proof_mode_v2_buffers_bar_and_stamps_era(monkeypatch):
    from relay_engine.lanes import LaneF
    monkeypatch.setattr(config, "F_PROOF_MODE", "v2")
    monkeypatch.setattr(delta, "is_loaded", lambda: True)
    monkeypatch.setattr(delta, "p_survive", lambda d, t, session="ALL": 0.93)
    res = type("R", (), {"lane": "F"})()
    shared = type("S", (), {})()
    shared.decide = lambda m, c: ("PROPOSE", res)
    shared.to_order = lambda m, r: Order(
        lane="F", event=EVENT, market=TICKER, side="yes", action="buy",
        price_cents=95, count=1, size_tier=config.TIER_PROBE,
        purpose="ENTRY", why="F tier95")
    lane = LaneF(shared)
    d = lane.evaluate(TICKER, _gate_ctx())
    # v2: bar = .95 − .04 = .91; surv .93 clears — F speaks, era stamped
    assert d.proposal is not None
    assert "surv0.93≥0.91 proof=v2-buffer" in d.proposal.why


def test_cell_rows_stamp_the_proof_era(ledger, monkeypatch):
    monkeypatch.setattr(config, "F_PROOF_MODE", "v2")
    ledger.record_cell_outcome("F", 95, won=True, pnl_cents=5, fees_cents=0,
                               market="M1", kind="settle")
    proof = ledger.db.execute(
        "SELECT proof FROM cell_outcomes").fetchone()[0]
    assert proof == "v2"


def test_boot_line_prints_proof_mode(monkeypatch):
    from relay_engine.boot import boot_tape
    monkeypatch.setattr(config, "F_PROOF_MODE", "v1")
    assert any(ln.startswith("F-proof: v1") for ln in boot_tape())
    monkeypatch.setattr(config, "F_PROOF_MODE", "v2")
    tape = boot_tape()
    assert any(ln.startswith("F-proof: v2") and "v2-buffer" in ln
               for ln in tape)


# ── §3: the permanent pack section ─────────────────────────────────────────
def test_pack_diagnostics_section(eng, surface, ledger):
    _seed_f_pass(surface, "TABLE_NON_REVERSAL p=0.93<bar0.95 d=120 t=540")
    surface.write_row("F", "M2", "w-M2", "ENTERED",
                      detail="fill=x @95c x1 fee=0c anchor=table")
    ledger.record_cell_outcome("F", 95, won=True, pnl_cents=5, fees_cents=0,
                               market="M3", kind="settle")
    lines = diagnostics.pack_section(ledger)
    body = "\n".join(lines)
    assert body.startswith("DIAGNOSTICS (last 24h):")
    assert "F passes: TABLE_NON_REVERSAL×1" in body
    assert "anchors: table×1 price×0" in body
    assert "F-proof:" in body and "closed risk by era" in body
    # and the daily pack carries the section
    from relay_engine.ledger import CashProtocol
    from relay_engine.ops import daily_pack
    cash = CashProtocol(ledger, alert_fn=lambda m: None)
    assert "DIAGNOSTICS (last 24h):" in daily_pack(ledger, surface, cash)
