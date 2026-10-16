"""Technical/entry-gate data for one ticker in a single call: last price,
20-day ATR, 50/200-day SMA, 50-day high, and days to next earnings --
built for AI trading agents whose system prompt requires these fields
before entering a position (league/tools.py's get_technicals).
get_price_history exposes the same underlying daily bars raw (the last N
closes), rather than only the aggregates derived from them.

ATR/SMA/N-day-high need a real daily OHLC series. SteadyAPI has NO
historical-bars endpoint at all (confirmed live -- it only ever offered
quote/options/news/reddit), so those four fields come from Tradier's own history
endpoint (companies/services/tradier.py::get_history) and are unavailable
outright when PRICING_PROVIDER isn't "tradier" -- this is a real gap for
Steady-only deployments, not a bug, and not something this module works
around. last_price and days_to_earnings are NOT gated on PRICING_PROVIDER:
last_price goes through pricing.py's normal provider dispatch, and
earnings dates come from FMP (companies/services/fmp.py), a third source
independent of either pricing provider."""

from datetime import date

from django.conf import settings

from . import fmp, pricing, tradier
from .sec_edgar import resolve_company

ATR_PERIOD = 20
SMA_SHORT_PERIOD = 50
SMA_LONG_PERIOD = 200
HIGH_LOOKBACK_PERIOD = 50

# Calendar-day lookback wide enough to cover SMA_LONG_PERIOD trading days
# even across weekends/holidays (200 trading days is ~280-290 calendar
# days in a normal year) -- with margin.
_HISTORY_LOOKBACK_DAYS = 400

# get_price_history's hard cap on trading days returned, regardless of
# what's asked for -- same "bound it somewhere" reasoning as league/
# tools.py's _nearest_the_money capping an options chain to the 10
# nearest-the-money contracts. This is a service-level backstop; the
# AI-facing tool wrapping this (league/tools.py, assistant/tools.py)
# enforces its own tighter, LLM-context-sized cap and rejects instead of
# silently truncating.
_PRICE_HISTORY_MAX_DAYS = 200
# Requesting N trading days of history needs MORE than N calendar days of
# lookback (weekends/holidays) -- same margin reasoning as
# _HISTORY_LOOKBACK_DAYS above, scaled to whatever N is actually asked
# for instead of always fetching 400 days.
_CALENDAR_DAYS_PER_TRADING_DAY = 1.6


def _atr(bars: list[dict], period: int) -> float | None:
    if len(bars) < period + 1:
        return None
    window = bars[-(period + 1):]
    true_ranges = []
    for i in range(1, len(window)):
        high, low, prev_close = window[i]["high"], window[i]["low"], window[i - 1]["close"]
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return sum(true_ranges) / period


def _sma(bars: list[dict], period: int) -> float | None:
    if len(bars) < period:
        return None
    closes = [b["close"] for b in bars[-period:]]
    return sum(closes) / period


def _n_day_high(bars: list[dict], period: int) -> float | None:
    if len(bars) < period:
        return None
    return max(b["high"] for b in bars[-period:])


def _price_technicals(ticker: str) -> dict:
    """{as_of, atr_20d, sma_50d, sma_200d, high_50d} -- all None if
    PRICING_PROVIDER isn't "tradier" or TRADIER_API_TOKEN isn't set."""
    empty = {"as_of": None, "atr_20d": None, "sma_50d": None, "sma_200d": None, "high_50d": None}
    if settings.PRICING_PROVIDER != "tradier":
        return empty

    bars = tradier.get_history(ticker, lookback_days=_HISTORY_LOOKBACK_DAYS)
    if not bars:
        return empty
    bars = [b for b in bars if b.get("close") is not None and b.get("high") is not None and b.get("low") is not None]
    if not bars:
        return empty

    return {
        "as_of": bars[-1]["date"],
        "atr_20d": _atr(bars, ATR_PERIOD),
        "sma_50d": _sma(bars, SMA_SHORT_PERIOD),
        "sma_200d": _sma(bars, SMA_LONG_PERIOD),
        "high_50d": _n_day_high(bars, HIGH_LOOKBACK_PERIOD),
    }


def get_price_history(ticker: str, days: int = 30) -> list[dict] | None:
    """The last `days` daily OHLCV bars for `ticker` -- the same raw
    series _price_technicals derives ATR/SMA/high_50d from, exposed
    directly for "what did the last N closes actually look like" instead
    of only the derived aggregates. `days` counts TRADING days (bars
    returned), not calendar days -- fetches a wider calendar-day window
    internally to cover weekends/holidays, then returns exactly the last
    `days` bars (fewer if the ticker doesn't have that much history yet,
    e.g. a recent IPO -- never an error for that).

    None if PRICING_PROVIDER isn't "tradier" or TRADIER_API_TOKEN isn't
    set, same gate as _price_technicals (see module docstring) -- []  if
    Tradier has no history at all for the ticker. Silently clamps `days`
    to _PRICE_HISTORY_MAX_DAYS as a backstop; the AI-facing tool wrapping
    this enforces its own tighter cap and rejects instead."""
    if settings.PRICING_PROVIDER != "tradier":
        return None

    days = max(1, min(days, _PRICE_HISTORY_MAX_DAYS))
    lookback_days = int(days * _CALENDAR_DAYS_PER_TRADING_DAY) + 5
    bars = tradier.get_history(ticker, lookback_days=lookback_days)
    if bars is None:
        return None
    bars = [b for b in bars if b.get("close") is not None]
    return [
        {
            "date": b["date"], "open": b.get("open"), "high": b.get("high"),
            "low": b.get("low"), "close": b.get("close"), "volume": b.get("volume"),
        }
        for b in bars[-days:]
    ]


def _days_to_earnings(ticker: str) -> tuple[str | None, int | None]:
    """(next_earnings_date, days_to_earnings) -- (None, None) if the
    ticker isn't an SEC-registered company (ETFs/indices genuinely have no
    earnings date, correctly reported this way, not an error), FMP_API_KEY
    isn't set, or FMP's plan doesn't cover the earnings endpoint."""
    try:
        company = resolve_company(ticker)
    except ValueError:
        return None, None
    next_date = fmp.get_next_earnings_date(company)
    if next_date is None:
        return None, None
    return next_date, (date.fromisoformat(next_date) - date.today()).days


def get_technicals(ticker: str) -> dict:
    """last_price, average_volume, as_of, atr_20d, sma_50d, sma_200d,
    high_50d, next_earnings_date, days_to_earnings for `ticker` -- see
    module docstring for which fields are provider-gated. average_volume
    is Tradier's own trailing-average field (always None under Steady --
    see pricing.py's _tradier_quote_to_steady_shape -- and Tradier doesn't
    document its exact averaging window, so don't treat it as precisely
    "20-day"). Every field is None independently when its underlying
    source is unavailable; this never raises for "no data," only for a
    genuinely invalid ticker upstream."""
    quote = pricing.get_quote(ticker)
    last_price = quote.get("last_price") if quote else None
    average_volume = quote.get("average_volume") if quote else None

    price_technicals = _price_technicals(ticker)
    next_earnings_date, days_to_earnings = _days_to_earnings(ticker)

    return {
        "ticker": ticker.upper(),
        "last_price": last_price,
        "average_volume": average_volume,
        **price_technicals,
        "next_earnings_date": next_earnings_date,
        "days_to_earnings": days_to_earnings,
    }
