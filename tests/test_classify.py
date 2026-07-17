"""classify.py tests: D1 crossing edge cases, unapproved series, feed issues."""

import pytest
from unittest.mock import MagicMock
from d_worker.classify import run, VerdictRow, _parse_direction, _parse_strike
from d_worker.feeds import Observation
import time


def _market(ticker="KXHIGHNY-26JUL14-B84.5", series="KXHIGHNY",
            subtitle="Will the high temperature in NYC be 84.5°F or above?",
            floor_strike=84.5):
    return {
        "ticker": ticker,
        "series_ticker": series,
        "subtitle": subtitle,
        "floor_strike": floor_strike,
        "status": "active",
    }


def _registry(approved=True, station="KNYC", source="NWS",
              rounding="0.5", units="F"):
    return {
        "series_ticker": "KXHIGHNY",
        "settle_source": source,
        "station_or_ref": station,
        "approved_ts": time.time() if approved else None,
        "version": 1,
        "units": units,
        "rounding": rounding,
        "tz": "America/New_York",
    }


def _obs(value, age=60):
    return Observation(value=value, ts=time.time() - age, source="NWS:KNYC",
                       raw={}, units="F")


def _fee():
    return {"maker_mult": 1.0, "taker_mult": 1.0}


def _book(yes_ask=97, no_ask=4, yes_bid=96, no_bid=3):
    b = MagicMock()
    b.yes_ask = yes_ask
    b.no_ask = no_ask
    b.yes_bid = yes_bid
    b.no_bid = no_bid
    return b


def _state(**overrides):
    s = {
        "close_ts": time.time() + 3600,
        "staleness_sec": 60,
        "max_staleness": 900,
        "running_value": 86.0,
        "min_net_clip_cents": 1.0,
        "batch_sizes": [1, 5, 10],
        "sim_capital_remaining": 10.0,
        "already_seeded": False,
    }
    s.update(overrides)
    return s


class TestParseDirection:
    def test_above_from_subtitle(self):
        assert _parse_direction({"subtitle": "84.5°F or above"}) == "above"

    def test_below_from_subtitle(self):
        assert _parse_direction({"subtitle": "under 70°F"}) == "below"

    def test_above_from_ticker(self):
        assert _parse_direction({"ticker": "KXHIGHNY-B84.5"}) == "above"

    def test_none_when_ambiguous(self):
        assert _parse_direction({"ticker": "FOO-123"}) is None


class TestParseStrike:
    def test_from_floor_strike(self):
        assert _parse_strike({"floor_strike": 84.5}) == 84.5

    def test_from_ticker(self):
        assert _parse_strike({"ticker": "KX-26JUL14-B84.5"}) == 84.5


class TestD1WXCrossing:
    def test_above_crossed(self):
        """Running max 86°F ≥ 84.5 + 0.5 → SEED YES."""
        v = run(_market(), _registry(), _obs(86), None, _fee(), _book(),
                _state(running_value=86.0))
        assert v.verdict == "SEED"
        assert v.side == "yes"
        assert v.dclass == "D1_WX"

    def test_exactly_at_strike_not_crossed(self):
        """84.5°F == strike 84.5 but < 84.5 + 0.5 → NOT DECIDED."""
        v = run(_market(), _registry(), _obs(84.5), None, _fee(), _book(),
                _state(running_value=84.5))
        assert v.verdict == "SKIP_NOT_DECIDED"

    def test_one_rounding_unit_above(self):
        """85.0°F == 84.5 + 0.5 → DECIDED (exactly at threshold)."""
        v = run(_market(), _registry(), _obs(85.0), None, _fee(), _book(),
                _state(running_value=85.0))
        assert v.verdict == "SEED"

    def test_below_strike_not_decidable(self):
        """Below-strike for a HIGH market: 80°F < 84.5 → NOT DECIDED."""
        v = run(_market(), _registry(), _obs(80), None, _fee(), _book(),
                _state(running_value=80))
        assert v.verdict == "SKIP_NOT_DECIDED"

    def test_below_market_running_max_undecidable(self):
        """A 'below' market with running_max variable cannot be decided early.
        Running max under the strike does NOT mean it stays under — afternoon can cross."""
        m = _market(subtitle="Will the high temperature stay under 84.5°F?")
        v = run(m, _registry(), _obs(80), None, _fee(), _book(),
                _state(running_value=80))
        assert v.verdict == "SKIP_NOT_DECIDED"

    def test_below_market_running_min_decidable(self):
        """A 'below' market with running_min variable CAN be decided when crossed."""
        m = _market(subtitle="Will the low temperature stay under 40°F?",
                    floor_strike=40.0)
        reg = _registry()
        reg["variable_kind"] = "running_min"
        v = run(m, reg, _obs(38.0), None, _fee(), _book(),
                _state(running_value=38.0))
        assert v.verdict == "SEED"
        assert v.side == "yes"


class TestUnapprovedSeries:
    def test_unapproved_returns_skip(self):
        v = run(_market(), _registry(approved=False), _obs(90), None,
                _fee(), _book(), _state())
        assert v.verdict == "SKIP_RULES_UNREVIEWED"


class TestStaleObs:
    def test_stale_obs_skipped(self):
        v = run(_market(), _registry(), _obs(90, age=1200), None,
                _fee(), _book(), _state(staleness_sec=1200, max_staleness=900))
        assert v.verdict == "SKIP_CANT_VERIFY_FAST"

    def test_no_obs_skipped(self):
        v = run(_market(), _registry(), None, None, _fee(), _book(),
                _state())
        assert v.verdict == "SKIP_CANT_VERIFY_FAST"


class TestMoneyGates:
    def test_no_fee_registry(self):
        v = run(_market(), _registry(), _obs(90), None, None, _book(),
                _state(running_value=90))
        assert v.verdict == "SKIP_FEE_FAIL"

    def test_no_book(self):
        v = run(_market(), _registry(), _obs(90), None, _fee(), None,
                _state(running_value=90))
        assert v.verdict == "SKIP_NO_BOOK"

    def test_lockup_budget_exhausted(self):
        v = run(_market(), _registry(), _obs(90), None, _fee(), _book(),
                _state(running_value=90, sim_capital_remaining=0))
        assert v.verdict == "SKIP_LOCKUP_BUDGET"

    def test_already_seeded(self):
        v = run(_market(), _registry(), _obs(90), None, _fee(), _book(),
                _state(running_value=90, already_seeded=True))
        assert v.verdict == "SKIP_ALREADY_SEEDED"
