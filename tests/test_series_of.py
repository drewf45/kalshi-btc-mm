"""series_of tests: derive series from market objects with various field combos."""

from d_worker import series_of


class TestSeriesOf:
    def test_series_ticker_present(self):
        assert series_of({"series_ticker": "KXHIGHNY"}) == "KXHIGHNY"

    def test_event_ticker_with_date_suffix(self):
        assert series_of({"event_ticker": "KXHIGHNY-26JUL15"}) == "KXHIGHNY"

    def test_event_ticker_no_date_suffix(self):
        assert series_of({"event_ticker": "KXHIGHNY"}) == "KXHIGHNY"

    def test_ticker_only(self):
        assert series_of({"ticker": "KXHIGHNY-26JUL15-B97"}) == "KXHIGHNY"

    def test_empty_market(self):
        assert series_of({}) == ""

    def test_crypto_event_ticker(self):
        assert series_of({"event_ticker": "KXBTC15M-26JUL15"}) == "KXBTC15M"

    def test_series_ticker_takes_priority(self):
        m = {"series_ticker": "KXHIGHNY", "event_ticker": "KXOTHER-26JUL15",
             "ticker": "KXWRONG-26JUL15-B97"}
        assert series_of(m) == "KXHIGHNY"

    def test_event_ticker_takes_priority_over_ticker(self):
        m = {"event_ticker": "KXHIGHNY-26JUL15", "ticker": "KXHIGHNY-26JUL15-B97"}
        assert series_of(m) == "KXHIGHNY"

    def test_realistic_market_no_series_ticker(self):
        """Simulates a real Kalshi /markets object that lacks series_ticker."""
        m = {
            "ticker": "KXHIGHNY-26JUL15-B97",
            "event_ticker": "KXHIGHNY-26JUL15",
            "title": "NYC High Temp",
            "subtitle": "Will the high temperature be 97°F or above?",
            "status": "active",
            "floor_strike": 97.0,
        }
        assert series_of(m) == "KXHIGHNY"

    def test_date_format_variations(self):
        assert series_of({"event_ticker": "KXLOWCHI-01JAN26"}) == "KXLOWCHI"
        assert series_of({"event_ticker": "KXHIGHLAX-15DEC25"}) == "KXHIGHLAX"
