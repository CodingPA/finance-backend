"""Tradier client: stock/ETF quotes, option expirations, and option
chains. Built to check whether Tradier is a fresher alternative to
SteadyAPI for options pricing -- see `python manage.py tradier_itm_quote`
(league/management/commands/) for the freshness problem this exists to
check: SteadyAPI's options chain
only updates ~every 10 minutes, so a day-trade fill can be priced off a
snapshot that's minutes stale, and the first price a trader sees after
the open can still be the prior session's close.

Real-time data requires a *production* Tradier token tied to a funded
brokerage account (or their paid market-data add-on) --
https://docs.tradier.com/docs/endpoints. The sandbox environment
(TRADIER_BASE_URL=https://sandbox.tradier.com) takes the same API shape
but serves DELAYED data -- fine for exercising this code, but it will not
answer "is this actually real-time" and should not be mistaken for a fix.

Like steady.py: no CompanySnapshot caching (the entire point is being
live) and no resolve_company() gate (Tradier serves plain ticker symbols
with no SEC-derived Company row). Unlike steady.py, Tradier's JSON fields
are already native numbers/None, not formatted strings -- no
_parse_number() equivalent needed here."""

from datetime import date, timedelta

from django.conf import settings

from . import http


def _headers() -> dict:
    return {"Accept": "application/json", "Authorization": f"Bearer {settings.TRADIER_API_TOKEN}"}


def _base_url() -> str:
    return settings.TRADIER_BASE_URL.rstrip("/")


def get_quote(symbol: str) -> dict | None:
    """Live (production token) or delayed (sandbox token) quote for one
    symbol -- last/bid/ask/change/volume plus `trade_date` (epoch ms of
    the last trade, 0 if the symbol hasn't traded), which is how callers
    tell a fresh price from a stale one. Returns None if
    TRADIER_API_TOKEN isn't configured. Raises ValueError if Tradier
    doesn't recognize the symbol -- a bad ticker is a normal input error
    here, not a transport failure."""
    if not settings.TRADIER_API_TOKEN:
        return None

    symbol = symbol.upper()
    resp = http.get(
        f"{_base_url()}/v1/markets/quotes?symbols={symbol}&greeks=false",
        "quote",
        provider="tradier",
        headers=_headers(),
        ticker=symbol,
    )
    quotes = resp.json().get("quotes") or {}
    if quotes.get("unmatched_symbols"):
        raise ValueError(f"Tradier has no quote for {symbol!r} -- check the ticker.")
    quote = quotes.get("quote")
    if isinstance(quote, list):
        quote = quote[0] if quote else None
    if not quote:
        raise ValueError(f"Tradier returned no quote for {symbol!r}.")
    return quote


def get_expirations(symbol: str) -> list[str] | None:
    """Available option expiration dates (YYYY-MM-DD) for `symbol`, in
    whatever order Tradier returns them. Returns None if
    TRADIER_API_TOKEN isn't configured, [] if the symbol has no listed
    options."""
    if not settings.TRADIER_API_TOKEN:
        return None

    symbol = symbol.upper()
    resp = http.get(
        f"{_base_url()}/v1/markets/options/expirations?symbol={symbol}&includeAllRoots=true",
        "options_expirations",
        provider="tradier",
        headers=_headers(),
        ticker=symbol,
    )
    dates = (resp.json().get("expirations") or {}).get("date") or []
    return dates if isinstance(dates, list) else [dates]


def get_options_chain(symbol: str, expiration: str, greeks: bool = False) -> list[dict] | None:
    """Full option chain (calls and puts together, distinguished by each
    contract's `option_type`) for `symbol` at one `expiration`
    (YYYY-MM-DD). Returns None if TRADIER_API_TOKEN isn't configured, []
    if there are no contracts for that expiration. Tradier collapses a
    single-contract result to a bare object instead of a one-item list --
    normalized here so callers always get a list, same shape either way."""
    if not settings.TRADIER_API_TOKEN:
        return None

    symbol = symbol.upper()
    resp = http.get(
        f"{_base_url()}/v1/markets/options/chains?symbol={symbol}&expiration={expiration}"
        f"&greeks={'true' if greeks else 'false'}",
        "options_chain",
        provider="tradier",
        headers=_headers(),
        ticker=symbol,
        tag=expiration,
    )
    options = (resp.json().get("options") or {}).get("option") or []
    if isinstance(options, dict):
        options = [options]
    return options


def get_history(symbol: str, lookback_days: int = 400, interval: str = "daily") -> list[dict] | None:
    """Daily/weekly/monthly OHLCV bars for `symbol` over the last
    `lookback_days` calendar days -- companies/services/technicals.py's
    only source of the historical series ATR/SMA/N-day-high need (there is
    no SteadyAPI equivalent -- see technicals.py's module docstring).
    Unlike get_quote/get_options_chain, this works the same on a sandbox
    token as a production one: past sessions' closed bars aren't "delayed"
    the way today's live quote is, so this doesn't need a funded brokerage
    account. Returns None if TRADIER_API_TOKEN isn't configured, [] if
    there's no history for the symbol/range. Tradier collapses a
    single-day result to a bare object instead of a one-item list, same
    quirk as get_options_chain -- normalized here so callers always get a
    list."""
    if not settings.TRADIER_API_TOKEN:
        return None

    symbol = symbol.upper()
    end = date.today()
    start = end - timedelta(days=lookback_days)
    resp = http.get(
        f"{_base_url()}/v1/markets/history?symbol={symbol}&interval={interval}"
        f"&start={start.isoformat()}&end={end.isoformat()}",
        "history",
        provider="tradier",
        headers=_headers(),
        ticker=symbol,
    )
    days = (resp.json().get("history") or {}).get("day") or []
    if isinstance(days, dict):
        days = [days]
    return days


def get_intraday(
    symbol: str, interval: str = "5min", start: date | None = None, end: date | None = None,
    session_filter: str = "open",
) -> list[dict] | None:
    """Intraday OHLCV bars for `symbol` via Tradier's timesales endpoint --
    a different product from get_history above, which only ever serves
    daily/weekly/monthly bars; timesales is the only Tradier endpoint that
    goes below a full session. interval is one of 1min/5min/15min. start/
    end (calendar dates, inclusive) default to Tradier's own "today" when
    omitted -- pass both for a specific past session, since intraday data
    ages out of the free/sandbox tier faster than daily history does.
    Internally expanded to a full-day datetime range (`{date} 00:00` to
    `{date} 23:59`) before being sent -- CONFIRMED LIVE that passing a
    bare date (no time-of-day) makes Tradier treat it as a near-instant
    point rather than a whole day, silently returning ~1 bar instead of a
    real session; this expansion is required, not defensive. session_filter
    defaults to "open" (regular trading hours only) -- passed explicitly
    rather than left to Tradier's own default ("all", which includes pre/
    post-market prints), since a caller reading this for regular-session
    price action (an opening-range breakout, an intraday reversal) would
    get a silently wrong answer with 4am prints mixed into the series;
    pass "all" explicitly for extended hours. Returns None if
    TRADIER_API_TOKEN isn't configured, [] if there's no intraday data for
    the range (e.g. queried before the session's first bar has printed, a
    non-trading day, a symbol with no intraday trades, or a range too far
    in the past for the account tier). Needs a *production* token for
    real-time bars, same caveat as get_quote -- sandbox serves this
    delayed too.

    CONFIRMED LIVE against a production account (2026-08-28, AAPL,
    5min): session_filter="open" with an explicit full-day start/end
    returns exactly the 78 bars spanning 09:30-15:55 ET, none outside
    it -- the regular-session cutoff is exact, not approximate.
    session_filter="open" with start/end OMITTED on a non-trading day
    (a Saturday) correctly returned [] -- consistent with "today" having
    no session at all, though this wasn't checked against an actual live
    trading day (worth confirming start/end-omitted still resolves to
    the current session mid-day, not just that it's correctly empty when
    there's nothing to find). A single-bar result on this account came
    back as a genuine one-item LIST, not Tradier's bare-object-instead-
    of-single-item-list quirk seen on get_history/get_options_chain --
    the isinstance(dict) guard below is kept as a harmless defensive
    no-op rather than removed, since one observation isn't proof it can
    never happen."""
    if not settings.TRADIER_API_TOKEN:
        return None

    symbol = symbol.upper()
    url = (
        f"{_base_url()}/v1/markets/timesales?symbol={symbol}&interval={interval}"
        f"&session_filter={session_filter}"
    )
    if start is not None:
        url += f"&start={start.isoformat()} 00:00"
    if end is not None:
        url += f"&end={end.isoformat()} 23:59"
    resp = http.get(url, "timesales", provider="tradier", headers=_headers(), ticker=symbol)
    bars = (resp.json().get("series") or {}).get("data") or []
    if isinstance(bars, dict):
        bars = [bars]
    return bars
