"""Provider-agnostic live pricing: dispatches get_quote/get_options_chain to
either steady.py or tradier.py based on settings.PRICING_PROVIDER, always
returning steady.py's normalized shape (see its _parse_quote/
_normalize_contract) so every caller -- league.services.execution's fills,
league.services.valuation's mark-to-market, and both AI agent tool surfaces
(assistant/tools.py, league/tools.py) -- works unchanged regardless of which
provider is active. This is the ONLY module those callers should import for
live quote/chain data; steady.py/tradier.py stay as the raw per-provider
clients underneath it.

get_news/get_reddit_* have no Tradier equivalent and aren't part of this
switch -- callers that need those still import steady directly.

Tradier's chain endpoint has no SteadyAPI-style "moneyness" field, so it's
computed here from the underlying's last price (one extra live quote call
per chain fetch) rather than left blank -- assistant/tools.py and
league/tools.py both rank contracts by it. Tradier also requires an
explicit expiration (no "nearest" default like Steady) -- resolved here via
get_expirations() when the caller omits one."""

from datetime import date

from django.conf import settings

from . import steady, tradier

_PROVIDERS = {"steady", "tradier"}


def _provider() -> str:
    provider = settings.PRICING_PROVIDER
    if provider not in _PROVIDERS:
        raise ValueError(f"PRICING_PROVIDER must be one of {sorted(_PROVIDERS)}, got {provider!r}")
    return provider


def _tradier_quote_to_steady_shape(raw: dict) -> dict:
    week_low, week_high = raw.get("week_52_low"), raw.get("week_52_high")
    day_low, day_high = raw.get("low"), raw.get("high")
    return {
        "symbol": raw.get("symbol"),
        "company_name": raw.get("description"),
        "exchange": raw.get("exch"),
        "market_status": None,
        "last_price": raw.get("last"),
        "net_change": raw.get("change"),
        "percent_change_pct": raw.get("change_percentage"),
        "is_real_time": None,
        "last_trade_timestamp": raw.get("trade_date"),
        "bid": raw.get("bid"),
        "ask": raw.get("ask"),
        "volume": raw.get("volume"),
        # Tradier's own trailing average -- confirmed present on a live
        # quote, but Tradier does not document the exact window (not
        # verified to be exactly 20 trading days), unlike every other
        # field here which is this exact session's own numbers.
        "average_volume": raw.get("average_volume"),
        # The most recently COMPLETED session's settled close -- distinct
        # from "last" (the most recent print, which during extended hours
        # can be a post-market trade, not the 4pm print) and specifically
        # what league.services.execution.settle_expired_position needs for
        # an ITM/OTM decision at expiration.
        "close": raw.get("close"),
        "after_hours": None,
        "fifty_two_week_range": f"{week_low} - {week_high}" if week_low is not None and week_high is not None else None,
        "day_range": f"{day_low} - {day_high}" if day_low is not None and day_high is not None else None,
    }


def get_quote(ticker: str, asset_class: str = "STOCKS") -> dict | None:
    """Same contract as steady.get_quote: None if the provider's token isn't
    configured, a normalized quote dict otherwise (last_price None if the
    ticker isn't recognized, never an exception). `asset_class` is
    Tradier-ignored -- its quote endpoint auto-detects stock/ETF/index, no
    equivalent required param -- kept for API parity with steady.get_quote."""
    if _provider() == "steady":
        return steady.get_quote(ticker, asset_class=asset_class)

    try:
        raw = tradier.get_quote(ticker)
    except ValueError:
        return None
    if raw is None:
        return None
    return _tradier_quote_to_steady_shape(raw)


def _tradier_contract_to_steady_shape(raw: dict, underlying_last: float | None) -> dict:
    greeks = raw.get("greeks") or {}
    bid, ask, strike = raw.get("bid"), raw.get("ask"), raw.get("strike")
    mid_iv = greeks.get("mid_iv")
    moneyness_pct = (strike - underlying_last) / underlying_last * 100 if underlying_last and strike is not None else None
    return {
        "symbol": raw.get("symbol"),
        "base_symbol": raw.get("underlying") or raw.get("root_symbol"),
        "strike_price": strike,
        "expiration_date": raw.get("expiration_date"),
        "expiration_type": raw.get("expiration_type"),
        "moneyness_pct": moneyness_pct,
        "bid": bid,
        "mid": (bid + ask) / 2 if bid is not None and ask is not None else None,
        "ask": ask,
        "last": raw.get("last"),
        "price_change": raw.get("change"),
        "percent_change_pct": raw.get("change_percentage"),
        "volume": raw.get("volume"),
        "open_interest": raw.get("open_interest"),
        "open_interest_change": None,
        "implied_volatility_pct": mid_iv * 100 if mid_iv is not None else None,
        "delta": greeks.get("delta"),
        "vega": greeks.get("vega"),
        "theta": greeks.get("theta"),
        "rho": greeks.get("rho"),
        "option_type": raw.get("option_type"),
        "days_to_expiration": None,
        "average_iv_pct": None,
        "historic_volatility_30d_pct": None,
        "next_earnings_date": None,
        "implied_volatility_rank_1y_pct": None,
    }


def nearest_the_money(contracts: list[dict], n: int) -> list[dict]:
    """The `n` contracts closest to the money by |moneyness_pct|, ascending
    -- contracts with unknown moneyness sort last rather than being dropped.
    Shared by the options-chain HTTP endpoint (companies/views.py, caller-
    supplied `n`) and both AI agent tool surfaces (assistant/tools.py,
    league/tools.py, which each hardcode n=10 as part of their tool
    description's contract with the model)."""
    return sorted(
        contracts,
        key=lambda c: abs(c["moneyness_pct"]) if c["moneyness_pct"] is not None else float("inf"),
    )[:n]


def get_expirations(ticker: str) -> list[str] | None:
    """Just the available expiration dates for `ticker`'s options, for
    populating a date picker without paying for a full chain fetch. Cheap
    dedicated call on Tradier (tradier.get_expirations). SteadyAPI has no
    lighter endpoint -- every chain response carries the full expirations
    list in its `meta` regardless of which expiration was requested, so
    this costs the same as get_options_chain there; also normalized here
    from Steady's `{"weekly": [...], "monthly": [...]}` shape into one
    flat sorted list, matching Tradier's shape, since a caller populating a
    single date picker needs one flat list either way. None if the
    provider's token isn't configured, same convention as every other
    function here."""
    ticker = ticker.upper()
    if _provider() == "tradier":
        return tradier.get_expirations(ticker)

    chain = steady.get_options_chain(ticker)
    if chain is None:
        return None
    raw = chain["expirations"]
    if isinstance(raw, dict):
        return sorted({exp_date for dates in raw.values() for exp_date in dates})
    return raw


def get_options_chain(ticker: str, expiration: str | None = None) -> dict | None:
    """Same contract as steady.get_options_chain: None if the provider's
    token isn't configured, {"expirations": ..., "calls": [...], "puts":
    [...]} otherwise (empty lists if there are no contracts, never an
    exception)."""
    if _provider() == "steady":
        return steady.get_options_chain(ticker, expiration=expiration)

    ticker = ticker.upper()
    expirations_list = tradier.get_expirations(ticker)
    if expirations_list is None:
        return None

    resolved_expiration = expiration
    if not resolved_expiration:
        today = date.today().isoformat()
        resolved_expiration = next((d for d in expirations_list if d >= today), None)
    if not resolved_expiration:
        return {"expirations": expirations_list, "calls": [], "puts": []}

    contracts = tradier.get_options_chain(ticker, resolved_expiration, greeks=True)
    if contracts is None:
        return None

    quote = get_quote(ticker)
    underlying_last = quote.get("last_price") if quote else None

    calls, puts = [], []
    for raw in contracts:
        contract = _tradier_contract_to_steady_shape(raw, underlying_last)
        (calls if raw.get("option_type") == "call" else puts).append(contract)

    return {"expirations": expirations_list, "calls": calls, "puts": puts}
