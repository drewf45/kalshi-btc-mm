"""WO-2026-07-28-X X4 — THE VENUE SPEAKS LAST at the reporting surface.

Every HEADLINE that answers "what is the account worth" prints the venue's own
account VALUE (cash + portfolio value = the Kalshi-app number), age-stamped,
fetched not derived. `book_cents` is demoted to internal attribution and never
prints as an account value again. SHADOW papers the money; LIVE before the first
venue read falls back to book; then the venue value with its age.
"""

import inspect

import pytest

from relay_engine import config, ops
from relay_engine.ledger import Ledger


def test_shadow_account_value_is_the_paper_book(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "live_submit_enabled", lambda: False)
    led = Ledger(str(tmp_path / "s.db"))
    led.baseline(5000, confirmed_by="test")
    led.set_state("last_venue_value_cents", "9999")   # ignored in SHADOW
    cents, src, age = led.account_value_display()
    assert (cents, src) == (5000, "paper")


def test_live_account_value_is_the_venue_number_age_stamped(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "live_submit_enabled", lambda: True)
    led = Ledger(str(tmp_path / "l.db"))
    led.baseline(9700, confirmed_by="test")           # phantom book $97
    led.set_state("last_venue_value_cents", "3291")   # the app shows $32.91
    led.set_state("last_venue_value_ts", "1000.0")
    cents, src, age = led.account_value_display(now=1012.0)
    assert (cents, src) == (3291, "venue")            # venue truth, NOT the 9700 book
    assert age == pytest.approx(12.0)                 # age-stamped
    assert led.book_cents() == 9700                   # book survives as attribution


def test_live_falls_back_to_book_before_the_first_venue_read(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "live_submit_enabled", lambda: True)
    led = Ledger(str(tmp_path / "f.db"))
    led.baseline(3000, confirmed_by="boot")           # no venue value stamped yet
    cents, src, age = led.account_value_display()
    assert (cents, src, age) == (3000, "book", None)  # boot has a number until read


def test_account_headline_formats_each_source(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "live_submit_enabled", lambda: True)
    led = Ledger(str(tmp_path / "h.db"))
    led.baseline(3291, confirmed_by="test")
    led.set_state("last_venue_value_cents", "3291")
    led.set_state("last_venue_value_ts", "1000.0")
    h = ops.account_headline(led, now=1005.0)
    assert "$32.91" in h and "venue" in h and "5s ago" in h
    # SHADOW paper
    monkeypatch.setattr(config, "live_submit_enabled", lambda: False)
    assert "paper book" in ops.account_headline(led)


def test_owed_line_prints_the_venue_account_not_book(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "live_submit_enabled", lambda: False)
    led = Ledger(str(tmp_path / "o.db"))
    led.baseline(5000, confirmed_by="test")
    led.seed_scrape()
    line = ops.owed_line(led)
    assert "account" in line and "paper book" in line     # venue-headline shape
    assert "book $" not in line                            # book demoted


# ── the grep artifact (Acceptance #1): no user-facing 'book $'/'book=' account value ──
def test_no_user_facing_book_as_account_value():
    """The account-value HEADLINE functions must present the venue number, never
    `book $X`/`book=Xc` as the account. Checked on the source of each surface."""
    for fn in (ops.owed_line, ops.restated_money_lines, ops.account_headline):
        src = inspect.getsource(fn)
        assert "book $" not in src, fn.__name__       # never 'book $X' as account
        assert "book=" not in src, fn.__name__        # never 'book=Xc' as account
    # the account headline reads account_value_display (venue), never book_cents,
    # for ITS NUMBER (the code below the docstring)
    body = inspect.getsource(ops.account_headline).split('"""')[-1]
    assert "account_value_display" in body and "book_cents" not in body


def test_pack_money_and_hourly_carry_venue_account():
    # the pack MONEY line and the hourly both route through account_headline
    from relay_engine import shadow_runner
    money_src = inspect.getsource(ops.restated_money_lines)
    assert "account_headline" in money_src and "venue truth" in money_src
    # the hourly Telegram alert reads the account headline, not book_cents
    sr_src = inspect.getsource(shadow_runner)
    assert 'account={account_headline' in sr_src
