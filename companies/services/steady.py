"""SteadyAPI client: live stock/ETF quotes and options chains (price,
greeks, IV) for a ticker/ETF/index. Unlike fmp.py, deliberately NOT cached
via CompanySnapshot -- the entire value of these endpoints is that they're
live, so every call hits the network. http.get() still writes an
ApiCallLog row per call, so there's an audit trail without a stale-data
model to manage.

Also deliberately does NOT go through companies.services.sec_edgar's
resolve_company(): SteadyAPI serves tickers with no SEC-derived Company row
(index symbols like $SPX, ETFs), so this takes a bare ticker string and logs
with company=None rather than gating on a Company existing first."""

from django.conf import settings

from . import http

OPTIONS_URL = "https://api.steadyapi.com/v3/markets/options"
QUOTE_URL = "https://api.steadyapi.com/v1/markets/quote"
NEWS_URL = "https://api.steadyapi.com/v2/markets/news"
REDDIT_SEARCH_URL = "https://api.steadyapi.com/v1/reddit/search"
REDDIT_POSTS_URL = "https://api.steadyapi.com/v1/reddit/posts"

_NON_NUMERIC = {"n/a", "unch", "--", ""}


def _parse_number(raw):
    """SteadyAPI returns numeric fields as formatted strings ('$194.83',
    '+63.92%', '39,945') and marks "no value" three different ways
    ('N/A', 'unch', '--') depending on the field -- confirmed against a
    live response, not just the docs (which under-document this)."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        return raw
    cleaned = raw.strip()
    if cleaned.lower() in _NON_NUMERIC:
        return None
    cleaned = cleaned.replace("$", "").replace(",", "").replace("%", "").replace("+", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _none_if_na(raw: str | None) -> str | None:
    if raw is None or raw.strip().lower() in _NON_NUMERIC:
        return None
    return raw


def _normalize_contract(raw: dict) -> dict:
    return {
        "symbol": raw.get("symbol"),
        "base_symbol": raw.get("baseSymbol"),
        "strike_price": _parse_number(raw.get("strikePrice")),
        "expiration_date": raw.get("expirationDate"),
        "expiration_type": raw.get("expirationType"),
        "moneyness_pct": _parse_number(raw.get("moneyness")),
        "bid": _parse_number(raw.get("bidPrice")),
        "mid": _parse_number(raw.get("midpoint")),
        "ask": _parse_number(raw.get("askPrice")),
        "last": _parse_number(raw.get("lastPrice")),
        "price_change": _parse_number(raw.get("priceChange")),
        "percent_change_pct": _parse_number(raw.get("percentChange")),
        "volume": _parse_number(raw.get("volume")),
        "open_interest": _parse_number(raw.get("openInterest")),
        "open_interest_change": _parse_number(raw.get("openInterestChange")),
        "implied_volatility_pct": _parse_number(raw.get("volatility")),
        "delta": _parse_number(raw.get("delta")),
        "vega": _parse_number(raw.get("vega")),
        "theta": _parse_number(raw.get("theta")),
        "rho": _parse_number(raw.get("rho")),
        "option_type": raw.get("optionType"),
        "days_to_expiration": _parse_number(raw.get("daysToExpiration")),
        "average_iv_pct": _parse_number(raw.get("averageVolatility")),
        "historic_volatility_30d_pct": _parse_number(raw.get("historicVolatility30d")),
        "next_earnings_date": _none_if_na(raw.get("baseNextEarningsDate")),
        "implied_volatility_rank_1y_pct": _parse_number(raw.get("impliedVolatilityRank1y")),
    }


def get_options_chain(ticker: str, expiration: str | None = None) -> dict | None:
    """Live options chain for `ticker` (stock/ETF/index symbol, e.g. AAPL,
    SPY, $SPX). `expiration` (YYYY-MM-DD) picks a specific expiration date;
    omitted, SteadyAPI defaults to the nearest one. Returns None if
    STEADY_API_TOKEN isn't configured -- callers (assistant/tools.py)
    translate that into a user-facing message. A ticker/expiration with no
    matching contracts returns 200 with an empty body (confirmed live, not
    an error) -- reflected here as empty calls/puts lists, not an exception.
    """
    if not settings.STEADY_API_TOKEN:
        return None

    ticker = ticker.upper()
    params = {"ticker": ticker}
    if expiration:
        params["expiration"] = expiration
    query = "&".join(f"{key}={value}" for key, value in params.items())

    resp = http.get(
        f"{OPTIONS_URL}?{query}",
        "options",
        provider="steady",
        headers={"Authorization": f"Bearer {settings.STEADY_API_TOKEN}"},
        ticker=ticker,
        tag=expiration,
    )
    payload = resp.json()
    body = payload.get("body", [])
    calls = body.get("Call", []) if isinstance(body, dict) else []
    puts = body.get("Put", []) if isinstance(body, dict) else []

    return {
        "expirations": payload.get("meta", {}).get("expirations", {}),
        "calls": [_normalize_contract(c) for c in calls],
        "puts": [_normalize_contract(c) for c in puts],
    }


VALID_ASSET_CLASSES = {"STOCKS", "ETF", "MUTUALFUNDS", "FUTURES"}


def _parse_quote(body: dict) -> dict:
    primary = body.get("primaryData") or {}
    secondary = body.get("secondaryData") or {}
    key_stats = body.get("keyStats") or {}
    return {
        "symbol": body.get("symbol"),
        "company_name": body.get("companyName"),
        "exchange": body.get("exchange"),
        "market_status": body.get("marketStatus"),
        "last_price": _parse_number(primary.get("lastSalePrice")),
        "net_change": _parse_number(primary.get("netChange")),
        "percent_change_pct": _parse_number(primary.get("percentageChange")),
        "is_real_time": primary.get("isRealTime"),
        "last_trade_timestamp": primary.get("lastTradeTimestamp"),
        "bid": _parse_number(primary.get("bidPrice")),
        "ask": _parse_number(primary.get("askPrice")),
        "volume": _parse_number(primary.get("volume")),
        # SteadyAPI's quote has no trailing-average-volume or distinct
        # prior-session-close field at all (confirmed against a live
        # response) -- always None here so pricing.py's normalized quote
        # shape has these keys regardless of provider; only Tradier
        # actually populates them.
        "average_volume": None,
        "close": None,
        "after_hours": {
            "last_price": _parse_number(secondary.get("lastSalePrice")),
            "net_change": _parse_number(secondary.get("netChange")),
            "percent_change_pct": _parse_number(secondary.get("percentageChange")),
            "last_trade_timestamp": secondary.get("lastTradeTimestamp"),
        } if secondary else None,
        "fifty_two_week_range": (key_stats.get("fiftyTwoWeekHighLow") or {}).get("value"),
        "day_range": (key_stats.get("dayrange") or {}).get("value"),
    }


def get_quote(ticker: str, asset_class: str = "STOCKS") -> dict | None:
    """Live quote for `ticker`: last sale price, net/percent change, bid/ask,
    volume, market status (Open/Closed/After-Hours), and 52-week/day range.
    `asset_class` is SteadyAPI's own required param -- one of STOCKS, ETF,
    MUTUALFUNDS, FUTURES. Returns None if STEADY_API_TOKEN isn't configured,
    same convention as get_options_chain."""
    if not settings.STEADY_API_TOKEN:
        return None

    ticker = ticker.upper()
    asset_class = asset_class.upper()
    if asset_class not in VALID_ASSET_CLASSES:
        raise ValueError(f"asset_class must be one of {sorted(VALID_ASSET_CLASSES)}, got {asset_class!r}")

    resp = http.get(
        f"{QUOTE_URL}?ticker={ticker}&type={asset_class}",
        "quote",
        provider="steady",
        headers={"Authorization": f"Bearer {settings.STEADY_API_TOKEN}"},
        ticker=ticker,
    )
    payload = resp.json()
    return _parse_quote(payload.get("body") or {})


NEWS_TYPES = {"ALL", "MARKET", "VIDEO", "PRESS-RELEASE"}


def _normalize_news_item(raw: dict) -> dict:
    return {
        "title": raw.get("title"),
        "text": raw.get("text"),
        "source": raw.get("source"),
        "type": raw.get("type"),
        "url": raw.get("url"),
        "tickers": raw.get("tickers", []),
        "time": raw.get("time"),
        "ago": raw.get("ago"),
    }


def get_news(ticker: str | None = None, news_type: str = "ALL") -> list[dict] | None:
    """Live market news and press releases. `ticker` narrows to one company
    (omit for general market news -- those items have no `tickers` field).
    `news_type` is SteadyAPI's own param (ALL/MARKET/VIDEO/PRESS-RELEASE)
    but confirmed live NOT to actually filter -- a `type=VIDEO` request
    returned a mix of Video and Article items. Forwarded anyway in case
    it's fixed server-side later; callers should not rely on it narrowing
    results. `time` is a raw string in two different formats depending on
    the item (ISO timestamp or a human-readable "Mon D, YYYY, H:MM AM/PM
    TZ" string) -- passed through as-is, not parsed into a datetime.
    Returns None if STEADY_API_TOKEN isn't configured, same convention as
    get_options_chain/get_quote. A ticker with no news returns a
    completely different shape than options/quote's empty-list pattern --
    `{"success": false, "message": "No data returned"}`, no `meta`/`body`
    keys at all (confirmed live) -- reflected here as an empty list, not
    an exception."""
    if not settings.STEADY_API_TOKEN:
        return None

    news_type = news_type.upper()
    if news_type not in NEWS_TYPES:
        raise ValueError(f"news_type must be one of {sorted(NEWS_TYPES)}, got {news_type!r}")

    params = {"type": news_type}
    if ticker:
        params["ticker"] = ticker.upper()
    query = "&".join(f"{key}={value}" for key, value in params.items())

    resp = http.get(
        f"{NEWS_URL}?{query}",
        "news",
        provider="steady",
        headers={"Authorization": f"Bearer {settings.STEADY_API_TOKEN}"},
        ticker=ticker.upper() if ticker else None,
    )
    payload = resp.json()
    if payload.get("success") is False:
        return []
    return [_normalize_news_item(item) for item in payload.get("body", [])]


# Reddit posts are a pass-through of Reddit's own submission object
# (~90-105 raw fields confirmed live: moderation flags, award/gilding
# metadata, thumbnail dimensions, wls/pwls whitelist codes, etc.) --
# unlike quote/options/news, Steady does NOT normalize these, so this is
# a field ALLOWLIST, not a parse/clean step.
_REDDIT_POST_FIELDS = (
    "id", "title", "selftext", "author", "subreddit", "score", "upvote_ratio",
    "num_comments", "created_utc", "permalink", "url",
)


def _normalize_reddit_post(raw: dict) -> dict:
    return {field: raw.get(field) for field in _REDDIT_POST_FIELDS}


REDDIT_SORT_TYPES = {"relevance", "hot", "top", "new", "comments"}
REDDIT_TIME_FILTERS = {"hour", "day", "week", "month", "year", "all"}


def get_reddit_search(
    query: str, subreddit: str | None = None, sort_type: str = "relevance", time_filter: str = "all",
) -> list[dict] | None:
    """Reddit-wide keyword search via SteadyAPI's proxy (works around
    Reddit closing self-service API registration). Returns the first page
    only (25 results, Steady's own cap) --
    there's no pagination support here, `meta.cursor` is dropped.

    CONFIRMED LIVE (2026-08-20, against SteadyAPI's own documented
    example): `subreddit` is accepted with no error but silently does
    NOT filter results -- ran the identical documented example request
    with and without it and got byte-identical results, same 25 post IDs
    in the same order, only ~3/25 actually from the named subreddit. This
    is a SteadyAPI bug, not a param-naming mistake (the docs' own example
    uses exactly this parameter name) -- reported, not yet fixed as of
    this writing. `subreddit` is still accepted and forwarded below in
    case it gets fixed server-side later, but callers must NOT treat
    results as actually scoped to it -- use get_reddit_posts for
    genuine one-subreddit results, which IS confirmed to work.
    Returns None if STEADY_API_TOKEN isn't configured, same convention
    as get_quote/get_options_chain/get_news."""
    if not settings.STEADY_API_TOKEN:
        return None

    sort_type = sort_type.lower()
    if sort_type not in REDDIT_SORT_TYPES:
        raise ValueError(f"sort_type must be one of {sorted(REDDIT_SORT_TYPES)}, got {sort_type!r}")
    time_filter = time_filter.lower()
    if time_filter not in REDDIT_TIME_FILTERS:
        raise ValueError(f"time_filter must be one of {sorted(REDDIT_TIME_FILTERS)}, got {time_filter!r}")

    params = {"search": query, "filter": "posts", "timeFilter": time_filter, "sortType": sort_type}
    if subreddit:
        params["subreddit"] = subreddit
    query_string = "&".join(f"{key}={value}" for key, value in params.items())

    resp = http.get(
        f"{REDDIT_SEARCH_URL}?{query_string}",
        "reddit_search",
        provider="steady",
        headers={"Authorization": f"Bearer {settings.STEADY_API_TOKEN}"},
        tag=subreddit,
    )
    payload = resp.json()
    return [_normalize_reddit_post(item) for item in payload.get("body", [])]


def get_reddit_posts(subreddit: str, sort_type: str = "hot") -> list[dict] | None:
    """Hot/new/top posts from ONE subreddit -- genuinely scoped, unlike
    get_reddit_search's broken subreddit param above (confirmed live:
    every result actually came back from the named subreddit). Returns
    the first page only (25 posts). `sort_type` (Steady's own `sortType`
    param -- NOT `sort`, confirmed live via a 422 body: `{"filter": [...
    "required when sort type is not present"], "sortType": [...
    "required when filter is not present"]}`) defaults to "hot"; "new"
    also confirmed live. Returns None if STEADY_API_TOKEN isn't
    configured, same convention as the rest of this module."""
    if not settings.STEADY_API_TOKEN:
        return None

    sort_type = sort_type.lower()
    if sort_type not in REDDIT_SORT_TYPES:
        raise ValueError(f"sort_type must be one of {sorted(REDDIT_SORT_TYPES)}, got {sort_type!r}")

    resp = http.get(
        f"{REDDIT_POSTS_URL}?subreddit={subreddit}&sortType={sort_type}",
        "reddit_posts",
        provider="steady",
        headers={"Authorization": f"Bearer {settings.STEADY_API_TOKEN}"},
        tag=subreddit,
    )
    payload = resp.json()
    return [_normalize_reddit_post(item) for item in payload.get("body", [])]
