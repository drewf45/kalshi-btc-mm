"""WO-2026-07-27-W P1+P4 — BROKER CASH IS THE ONLY SIZING TRUTH + THE CASH RACE.

The sizing base is the venue's live CASH (what is spendable, which shrinks as
rooms deploy), never the ledger's book and never the portfolio balance. The
ensemble ceiling reads cash + at-risk (total capital). All LIVE-gated — SHADOW
sizes off the paper book, byte-identical.
"""

import pytest

from relay_engine import config
from relay_engine.ledger import Ledger


def test_p1_shadow_tradeable_is_the_paper_book(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "live_submit_enabled", lambda: False)
    led = Ledger(str(tmp_path / "s.db"))
    led.baseline(5000, confirmed_by="test")
    # even with a (stale) venue cash stamped, SHADOW ignores it — paper book base
    led.set_state("last_venue_cash_cents", "999")
    assert led.tradeable_cents() == 5000            # book − owed(0), byte-identical


def test_p1_live_tradeable_is_venue_cash_minus_owed(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "live_submit_enabled", lambda: True)
    led = Ledger(str(tmp_path / "l.db"))
    led.baseline(9700, confirmed_by="test")         # phantom book $97
    led.set_state("last_venue_cash_cents", "2070")  # the venue's real cash $20.70
    # broker cash is the only truth: sizing base is 2070, NOT the 9700 book
    assert led.tradeable_cents() == 2070
    assert led.book_cents() == 9700                 # book survives as reporting only


def test_p1_live_falls_back_to_book_before_first_venue_read(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "live_submit_enabled", lambda: True)
    led = Ledger(str(tmp_path / "f.db"))
    led.baseline(3000, confirmed_by="boot")         # no venue cash stamped yet
    assert led.tradeable_cents() == 3000            # boot has a base until reconcile


def test_p4_ensemble_base_is_cash_plus_at_risk_in_cash_regime(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "live_submit_enabled", lambda: True)
    led = Ledger(str(tmp_path / "e.db"))
    led.baseline(10_000, confirmed_by="test")
    led.set_state("last_venue_cash_cents", "3000")  # $30 cash left…
    led.record_fill("KXBTC15M-A", "F", "yes", "ENTRY", 96, 20, config.TIER_PROBE)  # …$19.20 deployed
    deployed = led.deployed_cents()
    assert deployed > 0
    # tradeable = cash (excludes deployed); ensemble base = cash + at-risk
    assert led.tradeable_cents() == 3000
    assert led.ensemble_base_cents() == 3000 + deployed


def test_p4_ensemble_base_equals_tradeable_in_book_regime(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "live_submit_enabled", lambda: False)
    led = Ledger(str(tmp_path / "b.db"))
    led.baseline(8000, confirmed_by="test")
    # book already reflects deployed — ensemble base is just tradeable (no double add)
    assert led.ensemble_base_cents() == led.tradeable_cents() == 8000


def test_p4_the_cash_race_later_proposals_size_off_less(tmp_path, monkeypatch):
    """P4: deployed cash leaves the balance, so a later proposal in the window
    races for what remains — self-limiting by arithmetic. Modeled by the venue
    cash dropping between reads (the -V reconcile stamps it)."""
    monkeypatch.setattr(config, "live_submit_enabled", lambda: True)
    from relay_engine import scoring
    led = Ledger(str(tmp_path / "race.db"))
    led.baseline(10_000, confirmed_by="test")
    led.set_state("last_venue_cash_cents", "10000")     # window start: $100 cash
    first = scoring.size_order(led.tradeable_cents(), 97, 10_000, lane="F",
                               notional_pct=config.f_notional_pct_of("KXBTC15M"))
    led.set_state("last_venue_cash_cents", "4000")      # after a room deployed: $40 left
    second = scoring.size_order(led.tradeable_cents(), 97, 10_000, lane="F",
                                notional_pct=config.f_notional_pct_of("KXBTC15M"))
    assert second.contracts < first.contracts           # the race: less cash, smaller clip


def test_p1_no_book_or_portfolio_number_in_the_sizing_base():
    """Grep artifact (acceptance #1): tradeable_cents is the ONE sizing base and
    it reads venue CASH or the paper book — never a portfolio/pv total. Checked
    on the EXECUTABLE code only (the docstring names 'portfolio' to say it is
    excluded — that prose is the ruling, not a read)."""
    import ast
    import inspect
    src = inspect.getsource(Ledger.tradeable_cents)
    assert "last_venue_cash_cents" in src           # cash is the live truth
    # strip the docstring: keep only the statements that actually run
    fn = ast.parse(src.lstrip()).body[0]
    body = fn.body[1:] if (fn.body and isinstance(fn.body[0], ast.Expr)
                           and isinstance(fn.body[0].value, ast.Constant)) else fn.body
    code = "\n".join(ast.unparse(n) for n in body).lower()
    assert "portfolio" not in code                  # never the portfolio balance
    assert "pv" not in code.split()                 # never a marked-position total
    assert "last_venue_cash_cents" in code          # …and the cash read IS in code
