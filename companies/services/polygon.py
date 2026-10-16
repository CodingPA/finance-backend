"""Polygon.io (rebranded "Massive" in 2026; API still served from
api.polygon.io) daily-bar data -- the companies app's only source of
PERSISTED historical OHLCV. Distinct from tradier.py's get_history:
Tradier serves history per-symbol on demand and stores nothing; this
module backs the DailyBar table (models.py) via one full-market call per
session instead of one call per ticker, so a market-wide screener doesn't
need N serialized per-ticker calls against Tradier's rate limit.

Confirmed live against the free "Stocks Basic" tier (2026-09-20):
get_grouped_daily returned the entire US market (12,592 tickers that
session) in one 200, and get_active_common_stock_tickers paginates at
1000/page via Polygon's cursor-based next_url. Free tier is 5 req/min and
2 years of history -- fine here since actual call volume never approaches
that (see PROVIDER_MIN_INTERVAL's comment in http.py)."""

from django.conf import settings

from . import http

BASE_URL = "https://api.polygon.io"


def get_grouped_daily(day: str) -> list[dict]:
    """Full US stock market OHLCV for one session (day: "YYYY-MM-DD"),
    split/dividend-adjusted (adjusted=true). Returns [] if
    POLYGON_API_KEY isn't configured, same "unconfigured" convention as
    steady.py/tradier.py/brave_search.py -- callers treat this as
    "unavailable," not a crash. Also [] (not an error) for a date with no
    session (weekend/holiday): Polygon answers those with
    resultsCount: 0 on a normal 200, not a non-2xx.

    Each dict uses Polygon's own field names, passed through unchanged:
    T (ticker), o/h/l/c, v (volume), vw (volume-weighted average price),
    t (session start, epoch ms UTC), n (trade count)."""
    if not settings.POLYGON_API_KEY:
        return []

    resp = http.get(
        f"{BASE_URL}/v2/aggs/grouped/locale/us/market/stocks/{day}?adjusted=true"
        f"&apiKey={settings.POLYGON_API_KEY}",
        "grouped_daily",
        provider="polygon",
        headers={"Accept": "application/json"},
        tag=day,
    )
    return resp.json().get("results") or []


def get_active_common_stock_tickers() -> list[dict]:
    """Every active US common-stock ticker (type=CS), Polygon's own field
    names passed through unchanged (ticker, name, primary_exchange, ...).
    Paginates at 1000/page via Polygon's next_url until exhausted --
    ~5-8 calls for the full universe. Returns [] if POLYGON_API_KEY isn't
    configured. Meant to be called far less often than get_grouped_daily
    (the active-ticker universe barely changes day to day) -- not wired
    into the nightly Beat schedule, call this ad hoc or add a weekly/
    monthly Beat entry if a screener ends up needing it."""
    if not settings.POLYGON_API_KEY:
        return []

    results: list[dict] = []
    url = (
        f"{BASE_URL}/v3/reference/tickers?market=stocks&type=CS&active=true"
        f"&limit=1000&apiKey={settings.POLYGON_API_KEY}"
    )
    while url:
        resp = http.get(url, "tickers_reference", provider="polygon", headers={"Accept": "application/json"})
        body = resp.json()
        results.extend(body.get("results") or [])
        next_url = body.get("next_url")
        url = f"{next_url}&apiKey={settings.POLYGON_API_KEY}" if next_url else None
    return results
