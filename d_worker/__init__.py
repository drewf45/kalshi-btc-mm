"""KAL-D: decidedness scanner + shadow engine (Phase 1: shadow only, no live orders)."""


def series_of(market: dict) -> str:
    """Derive series ticker from a market object.

    Kalshi /markets objects may not carry series_ticker. Fallback chain:
    series_ticker → event_ticker (strip date suffix like -26JUL15) →
    ticker prefix before first hyphen.
    """
    import re
    s = market.get("series_ticker") or ""
    if s:
        return s
    event = market.get("event_ticker") or ""
    if event:
        stripped = re.sub(r'-\d{2}[A-Z]{3}\d{2}$', '', event)
        if stripped != event:
            return stripped
        return event
    ticker = market.get("ticker") or ""
    if "-" in ticker:
        return ticker.split("-")[0]
    return ticker
