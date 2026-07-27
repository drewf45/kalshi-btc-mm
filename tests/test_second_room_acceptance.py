"""WO-2026-07-26-S — THE SECOND ROOM: consolidated acceptance (the 6 criteria).

Reframed where Drew's rulings supersede the order body: #4's ghost/tuition
graduation is superseded by the live-day-one ruling — reframed as the first-day
mechanics watchlist being present. Each criterion is one test.
"""

import pytest

from relay_engine import config, lane_fh8


@pytest.fixture
def restore_roster():
    series = list(config.SERIES)
    modes = dict(config.SERIES_MODE)
    fam = set(lane_fh8.F_SERIES_ALLOWED)
    yield
    config.SERIES[:] = series
    config.SERIES_MODE.clear()
    config.SERIES_MODE.update(modes)
    lane_fh8.F_SERIES_ALLOWED = fam


def _engine(tmp_path):
    from relay_engine.shadow_runner import ShadowEngine
    from relay_engine import failures
    e = ShadowEngine(db_path=str(tmp_path / "acc.db"))
    e.boot()
    e.telegram.send = lambda m: None
    failures.configure(e.ledger, alert_fn=lambda m: None, run_mode="TEST", boot_id=1)
    return e


# ── ACCEPTANCE 1 — boot banner: per-series modes/dials, ensemble cap ──────────
def test_boot_banner_carries_rooms_ensemble_and_worst_day():
    from relay_engine import boot
    body = "\n".join(boot.boot_tape())
    assert "SERIES ROOMS (WO-S)" in body                 # per-room mode + dial
    assert "KXBTC15M LIVE F@24%" in body
    assert "ENSEMBLE (WO-S §2)" in body                   # the cap
    assert "50% of tradeable" in body
    assert "per-series halts" in body and "correlated tail" in body
    assert "THE SECOND ROOM (WO-2026-07-26-S, build 88)" in body   # the PROFILE


def test_xrp_chapter_renders_once_the_room_opens(tmp_path, restore_roster):
    from relay_engine.ops import series_chapter_lines
    e = _engine(tmp_path)
    e._cmd_series("/series xrp on")
    body = "\n".join(series_chapter_lines(e.ledger))
    assert "[KXXRP15M]" in body                            # discovered + charted


# ── ACCEPTANCE 2 — (series,lane) is a COMPUTED key: no migration, all cited ───
def test_series_is_a_computed_key_no_row_migration():
    # every existing BTC row resolves to its series with no stored column
    assert config.series_of("KXBTC15M-25JAN0210-T99") == "KXBTC15M"
    # the hardcoded-BTC sites are parameterized (F gate) or explicitly BTC-scoped (D)
    assert lane_fh8.F_SERIES_ALLOWED  # F gate is a set, not a literal
    from relay_engine import lane_d
    v = lane_d.classify("KXXRP15M-x", None, 120.0, None, None, None)
    assert v.verdict == "SKIP_WRONG_SERIES"   # D explicitly BTC-scoped, cited


# ── ACCEPTANCE 3 — ensemble cap defers-with-why; correlated once ─────────────
def test_ensemble_cap_and_correlated_loss_are_wired():
    from relay_engine.window_econ import combined_correlated_loss
    assert config.ENSEMBLE_AT_RISK_PCT == 0.50
    ev = combined_correlated_loss({"KXBTC15M": -80, "KXXRP15M": -50})
    assert ev["combined_cents"] == -130   # counted ONCE at combined size


# ── ACCEPTANCE 4 — (superseded) first-day mechanics watchlist present ─────────
def test_first_day_mechanics_watchlist_present_not_a_gate(tmp_path, restore_roster):
    """Drew's live-day-one ruling supersedes the ghost/tuition graduation; the
    protection standing where the ghost stood is the first-day watchlist +
    the room's own halt. The watchlist is a PAGE surface, never a pre-block."""
    from relay_engine.ops import series_chapter_lines
    e = _engine(tmp_path)
    e._cmd_series("/series xrp on")
    body = "\n".join(series_chapter_lines(e.ledger))
    assert "FIRST-DAY WATCHLIST (KXXRP15M)" in body
    assert "maker $0 on ITS tape" in body
    assert "never pre-blocks" in body


# ── ACCEPTANCE 5 — BTC's F path byte-identical ───────────────────────────────
def test_btc_f_path_byte_identical_dial_and_gate():
    from relay_engine import scoring
    # the BTC room dial equals the earned global constant (byte-identical sizing)
    assert config.f_notional_pct_of("KXBTC15M") == config.F_NOTIONAL_PCT
    legacy = scoring.size_order(6000, 97, 10_000, lane="F")
    roomed = scoring.size_order(6000, 97, 10_000, lane="F",
                                notional_pct=config.f_notional_pct_of("KXBTC15M"))
    assert (roomed.contracts, roomed.reason) == (legacy.contracts, legacy.reason)
    # F's wall-1 default family is BTC-only (the golden tape's world)
    assert lane_fh8.F_SERIES_ALLOWED == {"KXBTC15M"}


# ── ACCEPTANCE 6 — scrape/salvage/sentinels shared: one hwm, one owed ─────────
def test_scrape_is_singular_one_book_one_owed(tmp_path):
    e = _engine(tmp_path)
    e.ledger.baseline(5000, confirmed_by="boot")
    # owed/tradeable are ONE number for the whole book — never per-series
    assert isinstance(e.ledger.owed_cents(), int)
    assert isinstance(e.ledger.tradeable_cents(), int)
    # the scrape helpers take no series argument — the BOOK is the unit
    import inspect
    sig = inspect.signature(e.ledger.owed_cents)
    assert "series" not in sig.parameters
    sig2 = inspect.signature(e.ledger.tradeable_cents)
    assert "series" not in sig2.parameters
