"""Account valuation: cash + mark-to-market of every open Position. Single
source of truth for both the 60/30% entry caps (execution.py) and the
weekly scoreboard mark (tasks.mark_weekly_values). Options are marked at
BID (the conservative, actually-exitable price -- matches the rules doc's
own fill convention), stock/ETF/mutual-fund shares at last sale.

Display/mark-to-market prices are read from TickerSnapshot (companies.
models) -- a persisted "last known" quote/chain per ticker, NOT a live
pricing-provider call. This is what makes ScoreboardView/CompetitionCompetitorsView/
CompetitorDetailView fast: they used to each be an N+1 of live provider round
trips (one per open Position, every page load), which is why they were
routinely taking 13-14s in production. The snapshot table is kept warm by
three writers instead:
  1. league.tasks.refresh_open_position_quotes -- Celery Beat, every 10
     minutes during market hours, refreshes every ticker/contract actually
     held across running competitions.
  2. execution.py's _fill_price -- persists the live quote/chain it fetches
     for actual fill pricing, so a brand-new position is fresh immediately
     rather than stale until the next Beat tick.
  3. league.tasks.refresh_competitor_quotes / refresh_competition_quotes --
     operator-triggered manual refresh (POST .../refresh/), pushed back
     over the league WebSocket group once done.

Falls back to avg_cost whenever no snapshot exists yet for a ticker/contract
(brand new to the app, never fetched) rather than raising -- a stale mark is
better than an exception blocking every trade or the whole weekly scoreboard
run over one missing quote."""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal

from django.core.cache import cache

from companies.models import TickerSnapshot
from companies.services import pricing

from . import market_hours
from ..models import Position

logger = logging.getLogger(__name__)

# steady.get_quote requires an explicit asset_class and returns None for
# the wrong one -- confirmed live: SPY (ETF) returns nothing under STOCKS,
# FXAIX (mutual fund) returns nothing under STOCKS or ETF (Tradier ignores
# asset_class entirely, so this loop exits after one call when
# PRICING_PROVIDER=tradier). Position doesn't track which bucket a ticker
# falls into, so this tries all three in order -- up to 3 live calls for a
# non-plain-stock ticker on Steady.
_QUOTE_ASSET_CLASSES = ["STOCKS", "ETF", "MUTUALFUNDS"]

# Which asset class actually has data for a ticker doesn't change day to
# day, so remembering it (once, per ticker) skips get_stock_quote's
# up-to-3-sequential-call STOCKS/ETF/MUTUALFUNDS probe on every
# subsequent lookup for a known ETF/mutual fund. This is the one piece of
# Redis TTL caching left in this module -- unlike the old per-quote display
# cache, TickerSnapshot has fully replaced that.
_ASSET_CLASS_CACHE_TTL = 60 * 60 * 24

# How many live Steady calls to run at once from refresh_quotes_for_positions
# -- these are I/O-bound network round trips (requests.get), so a thread
# pool actually helps despite the GIL. Bounded well under Steady's 15 req/s
# hard limit (companies/services/http.py's PROVIDER_MIN_INTERVAL still
# serializes the actual wire sends across threads via its own lock-free
# min-interval check).
_REFRESH_MAX_WORKERS = 8


def get_stock_quote(ticker: str) -> dict | None:
    """Live quote via whichever provider PRICING_PROVIDER selects, never
    cached/persisted by itself. Used both by refresh_quote() (which persists
    what this returns) and,
    directly, by execution.py's actual trade-fill pricing, which must
    always see a live price."""
    ticker = ticker.upper()
    asset_class_cache_key = f"league:quote-asset-class:{ticker}"
    known_class = cache.get(asset_class_cache_key)
    classes = [known_class, *[c for c in _QUOTE_ASSET_CLASSES if c != known_class]] if known_class else _QUOTE_ASSET_CLASSES

    for asset_class in classes:
        quote = pricing.get_quote(ticker, asset_class=asset_class)
        if quote and quote.get("last_price") is not None:
            cache.set(asset_class_cache_key, asset_class, _ASSET_CLASS_CACHE_TTL)
            return quote
    return None


def _chain_data_type(expiration: str | None) -> str:
    return f"chain:{expiration or 'nearest'}"


def save_quote_snapshot(ticker: str, quote: dict) -> None:
    """Persist an already-fetched live quote as the display/mark-to-market
    snapshot for `ticker` -- does NOT itself call Steady. Used by
    refresh_quote() (which fetches then calls this) and by execution.py's
    _fill_price (which already has a live quote in hand for the fill
    itself, so this reuses it instead of a second live call)."""
    TickerSnapshot.objects.update_or_create(
        ticker=ticker.upper(), data_type="quote", defaults={"raw_json": quote},
    )


def save_chain_snapshot(ticker: str, expiration: str | None, chain: dict) -> None:
    """Same as save_quote_snapshot, for an options chain."""
    TickerSnapshot.objects.update_or_create(
        ticker=ticker.upper(), data_type=_chain_data_type(expiration), defaults={"raw_json": chain},
    )


def refresh_quote(ticker: str) -> dict | None:
    """Live-refetch `ticker`'s stock/ETF/mutual-fund quote and persist it.
    The write side of mark_position_detail's read; called by the Beat task
    and the manual-refresh tasks (league/tasks.py), never by execution.py
    (which persists the quote it already has via save_quote_snapshot
    instead of fetching twice)."""
    quote = get_stock_quote(ticker)
    if quote is not None:
        save_quote_snapshot(ticker, quote)
    return quote


def refresh_chain(ticker: str, expiration: str | None) -> dict | None:
    """Live-refetch an options chain and persist it. Same role as
    refresh_quote(), for options."""
    chain = pricing.get_options_chain(ticker, expiration=expiration)
    if chain is not None:
        save_chain_snapshot(ticker, expiration, chain)
    return chain


def _fetch_job(job: tuple[str, str, str | None]):
    """job is ("quote", ticker, None) or ("chain", ticker, expiration) --
    the live-network half of one refresh_quotes_for_positions unit of
    work. Deliberately does NOT call save_quote_snapshot/save_chain_snapshot
    itself (see that function's docstring for why the actual TickerSnapshot
    write is kept off the worker threads) -- but the live Steady call still
    writes an ApiCallLog row per call (companies/services/http.py), which
    IS a DB write happening on this thread. Each pool thread gets its own
    connection (Django connections are thread-local) that nothing would
    otherwise close -- there's no request/task cycle here to do it for us
    -- so this closes it explicitly rather than leaking one per thread per
    refresh."""
    from django.db import connection

    kind, ticker, expiration = job
    try:
        if kind == "quote":
            return get_stock_quote(ticker)
        return pricing.get_options_chain(ticker, expiration=expiration)
    finally:
        connection.close()


def refresh_quotes_for_positions(positions) -> None:
    """Live-refetch and persist a TickerSnapshot for every distinct
    ticker (stock) / (ticker, expiration) (option chain) held across
    `positions`. The shared implementation behind:
      - league.tasks.refresh_open_position_quotes (Beat, every open
        position in a running competition)
      - league.tasks.refresh_competitor_quotes (one competitor's holdings)
      - league.tasks.refresh_competition_quotes (one competition's, i.e.
        every competitor's, holdings)

    Only the live Steady round trips run in a thread pool -- the actual
    slow part this function exists to parallelize. The database writes
    happen afterward, serially, back on the calling thread: several
    threads each opening a DB connection just to hammer
    update_or_create() concurrently buys nothing (the writes themselves
    are fast) and SQLite in particular serializes/locks writers at the
    file level, so doing it there risked "database is locked" under
    exactly the concurrency this function adds -- confirmed by this
    module's own tests, not a hypothetical.

    Best-effort per ticker/contract -- one bad/rate-limited lookup must
    not block the rest of the batch, matching mark_position_detail's own
    fall-back-to-avg_cost tolerance for a missing quote."""
    quote_tickers = set()
    chain_keys = set()
    for p in positions:
        if p.instrument_type == Position.INSTRUMENT_STOCK:
            quote_tickers.add(p.ticker)
        else:
            chain_keys.add((p.ticker, p.expiration.isoformat() if p.expiration else None))

    jobs = [("quote", ticker, None) for ticker in quote_tickers]
    jobs += [("chain", ticker, expiration) for ticker, expiration in chain_keys]
    if not jobs:
        return

    results = {}
    with ThreadPoolExecutor(max_workers=min(_REFRESH_MAX_WORKERS, len(jobs))) as pool:
        futures = {pool.submit(_fetch_job, job): job for job in jobs}
        for future, job in futures.items():
            try:
                results[job] = future.result()
            except Exception:
                logger.exception("refresh_quotes_for_positions: failed to fetch %s", job)

    for (kind, ticker, expiration), payload in results.items():
        if payload is None:
            continue
        if kind == "quote":
            save_quote_snapshot(ticker, payload)
        else:
            save_chain_snapshot(ticker, expiration, payload)


def _find_contract(chain: dict | None, instrument_type: str, strike: Decimal) -> dict | None:
    if not chain:
        return None
    contracts = chain["calls"] if instrument_type == "call" else chain["puts"]
    for contract in contracts:
        if contract["strike_price"] is not None and Decimal(str(contract["strike_price"])) == strike:
            return contract
    return None


def mark_position_detail(position) -> tuple[Decimal, Decimal, datetime | None]:
    """(current unit price, current signed market value, priced_at) for
    one open Position -- unit price is last sale for stock, bid for
    options (matching the rules doc's own fill convention); value is
    sign-aware, since a short position (the option leg of a covered call
    / cash-covered put) is a liability -- what it would cost to buy back
    right now -- so it subtracts from account value rather than adding to
    it. `priced_at` is the backing TickerSnapshot's fetched_at (how stale
    this specific mark actually is -- see module docstring for who keeps
    it warm), or None when there's no snapshot at all and price/value are
    just avg_cost -- that fallback isn't "priced" in any live sense, so
    it gets no timestamp rather than a misleading one. Split from
    mark_position so callers that need the live per-unit price for
    display (e.g. a position row in the API) don't have to look it up a
    second time to get what mark_position already read.

    Pure DB read -- see module docstring for who keeps TickerSnapshot warm."""
    if position.instrument_type == position.INSTRUMENT_STOCK:
        snapshot = TickerSnapshot.objects.filter(ticker=position.ticker.upper(), data_type="quote").first()
        quote = snapshot.raw_json if snapshot else None
        if quote and quote.get("last_price") is not None:
            return Decimal(str(quote["last_price"])), Decimal(str(quote["last_price"])) * position.quantity, snapshot.fetched_at
        return position.avg_cost, position.avg_cost * position.quantity, None

    expiration = position.expiration.isoformat() if position.expiration else None
    snapshot = TickerSnapshot.objects.filter(
        ticker=position.ticker.upper(), data_type=_chain_data_type(expiration),
    ).first()
    chain = snapshot.raw_json if snapshot else None
    contract = _find_contract(chain, position.instrument_type, position.strike)
    if contract is not None and contract["bid"] is not None:
        price = Decimal(str(contract["bid"]))
        priced_at = snapshot.fetched_at
    elif position.expiration is not None and market_hours.option_expired(position.expiration):
        # avg_cost is what was PAID/collected at open -- economically
        # meaningless for a contract that has stopped trading, and
        # league.tasks.settle_expired_options should force-close this
        # position at tonight's close regardless. Zeroing it here closes
        # the gap between "it expired" and "the nightly settlement task
        # actually ran" (a same-day 0DTE position between its own 4pm
        # close and the ~21:35 UTC settlement task, or any expired
        # position on a day the task hasn't run yet) -- without this, an
        # expired long call kept counting as its full avg_cost toward
        # account value indefinitely, inflating both the scoreboard and
        # the 60%/30% entry caps for every trade after it.
        price = Decimal("0")
        priced_at = None
    else:
        price = position.avg_cost
        priced_at = None
    value = price * position.quantity * 100
    return price, (-value if position.is_short else value), priced_at


def mark_position(position) -> Decimal:
    """Current market value of one open Position -- see
    mark_position_detail for the sign/pricing convention."""
    return mark_position_detail(position)[1]


def account_value(competitor) -> Decimal:
    total = competitor.cash_balance
    for position in competitor.positions.all():
        total += mark_position(position)
    return total
