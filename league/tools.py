"""Tools an AI Competitor's wake-up loop can call (league.tasks.run_agent_wakeup),
mirroring assistant/tools.py's build_tools(user, conversation) closure pattern:
identity (which Competitor this is) is scoped by the closure, never something
the model could set itself.

web_search is Anthropic's server-side tool, not a local @beta_tool -- it needs
wrapping as a BetaBuiltinFunctionTool (tool_runner's tools=[...] indexes every
entry by a `.name` ATTRIBUTE for dispatch, so a bare dict spec doesn't satisfy
it; confirmed against the installed anthropic==0.120.0 SDK source,
lib/tools/_tool_dispatch.py::tool_registry() and _beta_runner.py's
AnyFunctionToolT bound). Its .call() should never actually be invoked --
server-tool results come back as `server_tool_use`/`web_search_tool_result`
content blocks in the same response, a different block type than the
`tool_use` blocks the runner dispatches locally -- but this hasn't been
exercised against a live turn that actually triggers a search, only checked
against the SDK's type contract. Worth one supervised wake-up to confirm
before trusting it unattended."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal, InvalidOperation

from anthropic import beta_tool
from anthropic.lib.tools import BetaBuiltinFunctionTool, ToolError
from django.conf import settings
from google.genai import types as genai_types

from companies.models import Company
from companies.services import bar_screener, brave_search, pricing, reddit_tickers, sec_edgar, steady, technicals, tradier, wikipedia_client
from companies.services.tool_bridge import openai_tool_defs, run_openai_tool
from companies.services.presentation import build_report
from companies.services.sec_edgar import resolve_company

from .models import AgentScratchpad, AgentTickerMemory, AgentWakeup, Position, Trade
from .services import execution, valuation

# Mirrors assistant/tasks.py's MAX_TOOL_ITERATIONS -- same reasoning, a
# runaway tool-call loop shouldn't run unbounded.
LEAGUE_MAX_TOOL_ITERATIONS = 6
LEAGUE_MAX_WAKEUPS_PER_DAY = 13
LEAGUE_WEB_SEARCH_MAX_USES = 3
# get_stock_quotes' cap on tickers per call -- generous enough to cover a
# real watchlist in one round, small enough that a pathological list can't
# turn one tool call into dozens of live Steady round trips.
_BATCH_QUOTE_MAX_TICKERS = 20
# get_ticker_memories/set_ticker_memories' cap per call -- no live round
# trips here (unlike the quote batch above), the concern is a context-
# window blowout instead: get_ticker_memories returns full note bodies,
# not one line per ticker, so an unbounded batch read/write is bounded the
# same way for the same reason.
_TICKER_MEMORY_BATCH_MAX = 20
# get_price_history's tool-level cap -- tighter than technicals.py's own
# service-level _PRICE_HISTORY_MAX_DAYS (200) backstop: this bounds how
# many OHLCV rows land in the model's context in one call, not a live
# round-trip count, same reasoning as the ticker-memory caps above.
_PRICE_HISTORY_TOOL_MAX_DAYS = 90
_VALID_INTRADAY_INTERVALS = {"1min", "5min", "15min"}
# A full regular session at 1min resolution is ~390 bars -- capped to the
# most recent N (same "bound it somewhere" reasoning as
# _PRICE_HISTORY_TOOL_MAX_DAYS above and _nearest_the_money's 10-contract
# cap on an options chain) rather than dumping a whole session's worth of
# bars into the model's context every call.
_INTRADAY_MAX_BARS = 100


class _WebSearchTool(BetaBuiltinFunctionTool):
    def __init__(self, max_uses: int = LEAGUE_WEB_SEARCH_MAX_USES):
        self._max_uses = max_uses

    def to_dict(self):
        return {"type": "web_search_20250305", "name": "web_search", "max_uses": self._max_uses}

    def call(self, input):
        raise RuntimeError("web_search is executed server-side and should never be dispatched locally.")


def build_league_tools(competitor) -> list:
    @beta_tool
    def get_stock_quote(ticker: str, asset_class: str = "STOCKS") -> str:
        """Get a LIVE quote for a stock/ETF/index right now: last sale
        price, net/percent change, bid/ask, volume, market status, 52-week
        and day range. Not cached. Checking more than one or two symbols
        (e.g. your whole watchlist)? Use get_stock_quotes instead -- it's
        one round against your tool-call budget regardless of how many
        tickers you pass, where N calls here is N rounds.

        Args:
            ticker: Symbol, e.g. AAPL, SPY, IWM.
            asset_class: One of STOCKS, ETF, MUTUALFUNDS, FUTURES. Default STOCKS.
        """
        try:
            quote = pricing.get_quote(ticker, asset_class=asset_class)
        except ValueError as exc:
            raise ToolError(str(exc))
        if quote is None:
            raise ToolError("Live quote data isn't available right now -- the pricing provider's API token isn't configured.")
        if quote["last_price"] is None:
            return f"No quote data found for {ticker.upper()}."
        return json.dumps(quote)

    @beta_tool
    def get_stock_quotes(tickers: list[str], asset_class: str = "STOCKS") -> str:
        """Get LIVE quotes for MULTIPLE stocks/ETFs/indices in ONE call --
        same fields as get_stock_quote, fetched in parallel. Use this
        instead of calling get_stock_quote once per ticker whenever
        you're checking more than one or two symbols: each
        get_stock_quote call is its own round against your
        LEAGUE_MAX_TOOL_ITERATIONS budget, so working through a 5-ticker
        watchlist one at a time can burn most of a turn before you ever
        get to schedule_wakeup -- this is exactly one round no matter how
        many tickers you pass. Best-effort per ticker: one bad/unknown
        symbol doesn't fail the rest of the batch, it just gets its own
        error message in the result.

        Args:
            tickers: Symbols to look up, e.g. ["AAPL", "MSFT", "SPY"]. Capped at 20 -- split a larger watchlist across calls.
            asset_class: One of STOCKS, ETF, MUTUALFUNDS, FUTURES, applied to every ticker in this call. Default STOCKS.
        """
        if not tickers:
            raise ToolError("tickers is empty.")
        if len(tickers) > _BATCH_QUOTE_MAX_TICKERS:
            raise ToolError(f"Too many tickers ({len(tickers)}) -- capped at {_BATCH_QUOTE_MAX_TICKERS} per call.")
        if asset_class.upper() not in steady.VALID_ASSET_CLASSES:
            raise ToolError(f"asset_class must be one of {sorted(steady.VALID_ASSET_CLASSES)}, got {asset_class!r}")

        def _fetch(ticker: str):
            # Own thread, own DB connection (Django connections are
            # thread-local) -- pricing.get_quote's live call still writes an
            # ApiCallLog row (companies/services/http.py), which is a DB
            # write on this thread with nothing else to close it, same
            # reasoning as valuation.py's _fetch_job.
            from django.db import connection
            try:
                return pricing.get_quote(ticker, asset_class=asset_class)
            finally:
                connection.close()

        results = {}
        with ThreadPoolExecutor(max_workers=min(_BATCH_QUOTE_MAX_TICKERS, len(tickers))) as pool:
            futures = {pool.submit(_fetch, ticker): ticker.upper() for ticker in tickers}
            for future, symbol in futures.items():
                try:
                    quote = future.result()
                except Exception as exc:
                    results[symbol] = f"Error: {exc}"
                    continue
                if quote is None:
                    results[symbol] = "Live quote data isn't available right now -- the pricing provider's API token isn't configured."
                elif quote["last_price"] is None:
                    results[symbol] = f"No quote data found for {symbol}."
                else:
                    results[symbol] = quote
        return json.dumps(results)

    @beta_tool
    def get_options_chain(ticker: str, expiration: str = "") -> str:
        """Get a LIVE options chain snapshot: the ~10 calls and ~10 puts
        closest to the money for one expiration (nearest available, or a
        specific one if given), each with bid/ask/last, volume, open
        interest, implied volatility, delta/vega/theta/rho, and 1-year IV
        rank. Also returns every other available expiration date. Does NOT
        include the underlying's own price -- use get_stock_quote for that.

        Args:
            ticker: Symbol, e.g. AAPL, SPY, IWM, or an index like $SPX.
            expiration: Optional expiration date, format YYYY-MM-DD. Omit for the nearest expiration.
        """
        chain = pricing.get_options_chain(ticker, expiration=expiration or None)
        if chain is None:
            raise ToolError("Live options data isn't available right now -- the pricing provider's API token isn't configured.")
        if not chain["calls"] and not chain["puts"]:
            suffix = f" at expiration {expiration}" if expiration else ""
            return f"No options data found for {ticker.upper()}{suffix}."
        return json.dumps({
            "expirations": chain["expirations"],
            "calls": pricing.nearest_the_money(chain["calls"], 10),
            "puts": pricing.nearest_the_money(chain["puts"], 10),
        })

    @beta_tool
    def get_technicals(ticker: str) -> str:
        """Get last price, trailing average volume, 20-day ATR, 50-day
        SMA, 200-day SMA, 50-day high, and days to next earnings for one
        ticker in a single call -- use this if your rules require these
        fields before entering a position. Every field is independently
        null when its source is unavailable rather than omitted or
        estimated -- treat a null as "this data doesn't exist right now,"
        never infer or guess a value for it yourself.

        average_volume/atr_20d/sma_50d/sma_200d/high_50d require this
        deployment's pricing provider to be Tradier (SteadyAPI has no
        volume-average or historical price series to compute these from)
        -- if it's on Steady, those five come back null every time, not
        just for this ticker. average_volume is Tradier's own field --
        it's a genuine trailing average, but Tradier doesn't document the
        exact window, so don't assume it's precisely 20 trading days.
        next_earnings_date/days_to_earnings require an SEC-registered
        company -- ETFs/indices genuinely have no earnings date, so null
        there is correct, not missing data.

        Args:
            ticker: Symbol, e.g. AAPL, SPY.
        """
        return json.dumps(technicals.get_technicals(ticker))

    @beta_tool
    def get_price_history(ticker: str, days: int = 30) -> str:
        """Get the last N daily OHLCV bars (open/high/low/close/volume)
        for a ticker -- the raw closes get_technicals' ATR/SMA/high_50d
        are computed from, for when you want to look at the actual price
        path yourself (a chart, a specific pattern) rather than only a
        pre-computed aggregate. `days` counts trading days returned, not
        calendar days -- you may get fewer than asked for a ticker with a
        short listing history (e.g. a recent IPO), which is normal, not
        an error.

        Requires this deployment's pricing provider to be Tradier (same
        gate as get_technicals' historical fields) -- returns an error if
        it isn't. Returns a plain array of bars -- unlike
        get_intraday_candles, nothing here is ever truncated; a
        days > 90 request is rejected outright instead.

        Args:
            ticker: Symbol, e.g. AAPL, SPY.
            days: How many most-recent trading days to return. Capped at 90.
        """
        if days < 1:
            raise ToolError("days must be at least 1.")
        if days > _PRICE_HISTORY_TOOL_MAX_DAYS:
            raise ToolError(f"days={days} exceeds the cap of {_PRICE_HISTORY_TOOL_MAX_DAYS} per call.")
        bars = technicals.get_price_history(ticker, days=days)
        if bars is None:
            raise ToolError("Price history isn't available right now -- this deployment's pricing provider isn't Tradier.")
        if not bars:
            return f"No price history found for {ticker.upper()}."
        return json.dumps(bars)

    @beta_tool
    def scan_down_streaks(min_streak: int = 3, max_streak: int = 5, sma_window: int = 200) -> str:
        """Scan the ENTIRE market for tickers currently on a
        min_streak-to-max_streak-session run of lower closes (today's
        close below yesterday's, below the day before, ...) while still
        trading above their own sma_window-day SMA -- a short-term
        mean-reversion setup, not a trend-break. Unlike get_technicals/
        get_price_history, this does NOT depend on the pricing provider
        being Tradier -- it's computed from a separately-ingested full-
        market daily-bar history, so it works the same regardless of
        PRICING_PROVIDER.

        Returns a JSON array (empty if nothing matches, or if this
        deployment's daily-bar history hasn't been backfilled far enough
        yet -- a ticker needs sma_window sessions of history to be
        considered at all, so results may be sparse or empty until then).
        Each entry: {ticker, down_streak, last_close, sma, avg_volume,
        as_of}. Capped at 50 candidates, longest streak first.

        This is a pure technical scan -- it does NOT check earnings
        dates, liquidity, or anything else your strategy requires before
        actually entering. Run get_technicals on whatever this returns to
        check days_to_earnings and get a live quote before trading it.

        Args:
            min_streak: Shortest down-streak (in sessions) to match. Default 3.
            max_streak: Longest down-streak (in sessions) to match. Default 5.
            sma_window: SMA period in trading days a candidate must be trading above. Default 200.
        """
        candidates = bar_screener.scan_down_streaks(
            min_streak=min_streak, max_streak=max_streak, sma_window=sma_window,
        )
        if not candidates:
            return "No candidates found."
        return json.dumps(candidates)

    @beta_tool
    def get_intraday_candles(ticker: str, interval: str = "5min") -> str:
        """Get today's intraday OHLCV bars for a ticker at 1/5/15-minute
        resolution, REGULAR SESSION ONLY (9:30am-4pm ET -- pre/post-market
        prints are excluded) -- for reading today's actual price action
        (an opening-range breakout, an intraday reversal, volume at a
        specific time) rather than only the last daily close. Different
        from get_price_history, which is daily bars over many days; this
        is sub-day bars within the current session only.

        Returns an OBJECT, not a bare list: {"bars": [...], "bars_returned":
        N, "total_bars_this_session": M, "truncated": true/false} -- bars
        are capped to the most recent 100. At 1-minute resolution a full
        session is ~390 bars, so "truncated": true there means you're only
        seeing roughly the LAST 100 MINUTES, not the whole session -- check
        that flag before concluding something did/didn't happen earlier in
        the day (e.g. an opening-range read at 1min needs 5min/15min
        instead, or a fresh call soon after the open before 100 bars have
        accumulated).

        Requires this deployment's pricing provider to be Tradier (same
        gate as get_price_history/get_technicals' historical fields) and a
        production TRADIER_API_TOKEN for real-time bars -- under a sandbox
        token this is delayed, same caveat as get_stock_quote. Empty
        before the session's first bar has printed (e.g. checked before/
        shortly after the open) -- that's normal, not an error.

        Args:
            ticker: Symbol, e.g. AAPL, SPY.
            interval: One of 1min, 5min, 15min. Default 5min.
        """
        if interval not in _VALID_INTRADAY_INTERVALS:
            raise ToolError(f"interval must be one of {sorted(_VALID_INTRADAY_INTERVALS)}, got {interval!r}.")
        if settings.PRICING_PROVIDER != "tradier":
            raise ToolError("Intraday data isn't available right now -- this deployment's pricing provider isn't Tradier.")
        bars = tradier.get_intraday(ticker, interval=interval)
        if bars is None:
            raise ToolError("Intraday data isn't available right now -- TRADIER_API_TOKEN isn't configured.")
        if not bars:
            return f"No intraday bars found for {ticker.upper()} yet today."
        return json.dumps({
            "bars": bars[-_INTRADAY_MAX_BARS:],
            "bars_returned": min(len(bars), _INTRADAY_MAX_BARS),
            "total_bars_this_session": len(bars),
            "truncated": len(bars) > _INTRADAY_MAX_BARS,
        })

    @beta_tool
    def get_market_news(ticker: str = "", news_type: str = "ALL") -> str:
        """Get recent market news and press releases. Omit ticker for
        general market news. `news_type` (ALL/MARKET/VIDEO/PRESS-RELEASE)
        is forwarded to the API but confirmed NOT to actually filter --
        expect a mix of types regardless of what you pass. Capped to the
        20 most recent items.

        Args:
            ticker: Optional -- narrow to one company's news, e.g. AAPL.
            news_type: One of ALL, MARKET, VIDEO, PRESS-RELEASE. Default ALL (filtering unreliable, see above).
        """
        try:
            news = steady.get_news(ticker or None, news_type=news_type)
        except ValueError as exc:
            raise ToolError(str(exc))
        if news is None:
            raise ToolError("Live news isn't available right now -- STEADY_API_TOKEN isn't configured.")
        if not news:
            return "No news found."
        return json.dumps(news[:20])

    @beta_tool
    def get_reddit_sentiment(ticker: str) -> str:
        """Get recent Reddit posts mentioning a ticker -- raw content
        (title, score, upvote_ratio, comment count, a text excerpt, a
        permalink), NOT a computed sentiment score. Read what's actually
        there and judge it yourself; this hands you opinions people
        posted, not verified facts or a pre-digested number -- same
        "relayed, not verified" caveat as get_market_news. Results are
        NOT reliably scoped to any one subreddit (confirmed live:
        SteadyAPI's own subreddit-filter parameter on this search
        doesn't actually work, even against their own documented
        example) -- treat this as "what is Reddit broadly saying," never
        "what does r/wallstreetbets specifically say." A short/generic
        ticker can pull in noise from words that just happen to share
        the same letters (e.g. SLS also means NASA's Space Launch
        System rocket) -- the first several results (sorted by
        relevance) are usually the real signal; later ones increasingly
        aren't.

        Args:
            ticker: Symbol to search for, e.g. AAPL, SLS, RKLB.
        """
        try:
            posts = steady.get_reddit_search(ticker)
        except ValueError as exc:
            raise ToolError(str(exc))
        if posts is None:
            raise ToolError("Reddit data isn't available right now -- STEADY_API_TOKEN isn't configured.")
        if not posts:
            return f"No Reddit posts found mentioning {ticker.upper()}."
        return json.dumps(posts[:10])

    @beta_tool
    def scan_subreddit_for_tickers(subreddit: str, sort_type: str = "hot") -> str:
        """Scan one subreddit's current posts and return which stock
        tickers are actually being discussed -- use this to FIND ideas
        worth investigating (e.g. scan wallstreetbets for what's
        trending right now), not to check a ticker you already have in
        mind (use get_reddit_sentiment for that -- it's cheaper and more
        targeted). Ticker identification reads each post's actual
        content rather than keyword/cashtag-matching, so an unrelated
        post that happens to share letters with a ticker won't show up.
        Costs one extra small OpenAI-billed model call beyond the Reddit
        fetch itself, covering the whole batch of posts in one shot, not
        per post.

        Args:
            subreddit: Name without "r/", e.g. wallstreetbets, stocks, investing.
            sort_type: One of relevance, hot, top, new, comments. Default hot.
        """
        try:
            posts = steady.get_reddit_posts(subreddit, sort_type=sort_type)
        except ValueError as exc:
            raise ToolError(str(exc))
        if posts is None:
            raise ToolError("Reddit data isn't available right now -- STEADY_API_TOKEN isn't configured.")
        if not posts:
            return f"No posts found in r/{subreddit}."

        tickers = reddit_tickers.extract_tickers(posts)
        if tickers is None:
            raise ToolError("Ticker extraction isn't available right now -- OPENAI_API_KEY isn't configured.")
        return json.dumps({"tickers": tickers, "posts_scanned": len(posts)})

    @beta_tool
    def search_web(query: str) -> str:
        """Search the open web LIVE right now (Brave Search) and return
        matching pages -- title, url, and a short description/snippet for
        each. Use this for anything the other tools here can't answer:
        breaking news, a company's own site/investor relations page,
        analyst commentary, background on something not in SEC filings or
        Reddit. This is a SEPARATE tool from your provider's own built-in
        web_search capability, if you have one -- both exist here; use
        whichever actually returns results for you. Read the description/
        snippet to judge relevance yourself; fetching full page content
        isn't available here.

        Args:
            query: Search terms, e.g. "Rocket Lab Neutron rocket launch date".
        """
        results = brave_search.search_web(query)
        if results is None:
            raise ToolError("Web search isn't available right now -- BRAVE_API_KEY isn't configured.")
        if not results:
            return f'No web results found for "{query}".'
        return json.dumps(results)

    @beta_tool
    def list_filings(ticker: str) -> str:
        """List a company's 10-K/10-Q filings from SEC EDGAR: form, filed
        date, period, accession number, and a URL to the full filing.
        Doesn't fetch or parse the filing itself -- for the underlying
        numbers, use get_company_report; to read the filing text, follow
        the URL yourself (e.g. via web_search) or ask for it in your
        thesis reasoning.

        Args:
            ticker: Stock ticker symbol, e.g. RKLB.
        """
        try:
            company = resolve_company(ticker)
        except ValueError as exc:
            raise ToolError(str(exc))

        submissions = sec_edgar.get_submissions(company)
        filings = sec_edgar.list_filings(submissions)
        if not filings:
            return f"No 10-K/10-Q filings found for {company.ticker}."
        rows = [
            {
                "form": f["form"], "filed": f["filing_date"], "period": f["report_date"],
                "accession_number": f["accession_number"], "url": f["url"],
            }
            for f in filings
        ]
        return json.dumps(rows)

    @beta_tool
    def get_company_report(ticker: str) -> str:
        """Get the latest cached financial report for a company: price,
        market cap, P/S, cash runway, the price/shares tripwire check, and
        the multi-year metrics table (revenue, gross profit, net income,
        operating cash flow, capex, cash, debt, share count, etc). Reads
        only what's already cached from the company's last screen -- does
        not fetch fresh data, so a company that's never been screened has
        nothing to report yet.

        Args:
            ticker: Stock ticker symbol, e.g. RKLB.
        """
        try:
            company = Company.objects.get(ticker=ticker.upper())
        except Company.DoesNotExist:
            raise ToolError(f"{ticker.upper()} hasn't been screened yet -- nothing cached to report.")
        return json.dumps(build_report(company), default=str)

    @beta_tool
    def search_wikipedia(query: str) -> str:
        """Search Wikipedia LIVE right now and return short summaries of
        matching pages -- NOT persisted anywhere, nothing is saved or
        indexed. Use this for background/context on a company, its
        products, or its market, not for live financials or price data.

        Args:
            query: Search terms, e.g. "Rocket Lab Neutron rocket".
        """
        results = wikipedia_client.search_pages(query, limit=5)
        if not results:
            return f'No Wikipedia pages found for "{query}".'
        return json.dumps(results)

    @beta_tool
    def get_portfolio() -> str:
        """Your current cash, open positions, and total account value --
        the authoritative numbers. Trust this over your own scratchpad's
        memory of your positions, which may be stale."""
        positions = [
            {
                "instrument_type": p.instrument_type,
                "ticker": p.ticker,
                "strike": str(p.strike) if p.strike is not None else None,
                "expiration": p.expiration.isoformat() if p.expiration else None,
                "quantity": p.quantity,
                "is_short": p.is_short,
                "avg_cost": str(p.avg_cost),
            }
            for p in competitor.positions.all()
        ]
        return json.dumps({
            "cash_balance": str(competitor.cash_balance),
            "account_value": str(valuation.account_value(competitor)),
            "positions": positions,
        })

    @beta_tool
    def get_competition_standings() -> str:
        """Every competitor's current rank, account value, and total return
        in YOUR competition -- the same numbers the public scoreboard shows
        (GET .../scoreboard/), available to every competitor equally, human
        or AI. Use this to know whether you're leading or trailing, not
        just your own P/L in isolation -- a strategy that should play
        differently when ahead vs. behind needs this, not get_portfolio
        alone. Positions/holdings of OTHER competitors are NOT included
        here (only their aggregate account value/return) -- this is
        standings, not a way to see what anyone else is holding."""
        competition = competitor.competition
        starting = competition.starting_capital
        rows = []
        for c in competition.competitors.all():
            value = valuation.account_value(c)
            total_return_pct = (value - starting) / starting if starting else Decimal("0")
            rows.append({
                "id": c.id, "name": c.name, "kind": c.kind,
                "account_value": str(value), "total_return_pct": str(total_return_pct),
                "is_you": c.id == competitor.id,
            })
        # Same tie-break as ScoreboardView (league/views.py): id descending
        # keeps ties (e.g. everyone still at 0% early in a competition)
        # stable across repeated calls without implying anything about rank.
        rows.sort(key=lambda r: (Decimal(r["total_return_pct"]), r["id"]), reverse=True)
        for i, row in enumerate(rows, start=1):
            row["rank"] = i
        your_rank = next(row["rank"] for row in rows if row["is_you"])
        return json.dumps({
            "your_rank": your_rank, "competitor_count": len(rows), "standings": rows,
        })

    @beta_tool
    def submit_trade(
        ticker: str,
        side: str,
        instrument_type: str,
        quantity: int,
        thesis: str,
        limit_price: str = "",
        strike: str = "",
        expiration: str = "",
        target_price: str = "",
        dead_price: str = "",
    ) -> str:
        """Submit a trade. Fills immediately at the live ask (buy sides) or
        bid (sell sides) -- never mid -- or is rejected outright. EXCEPTION:
        if your system prompt's rules say this account can't trade after
        hours and the market is closed right now, this queues instead --
        you'll get a QUEUED result, not FILLED/REJECTED, and it's evaluated
        at a live quote the next time the market opens (check your prompt's
        "Orders still queued" section on a later wake-up rather than
        resubmitting). A written thesis is required for every trade (a
        sentence or two is fine).

        Args:
            ticker: Symbol, e.g. AAPL, SPY.
            side: One of buy (open/add to a long position), sell (close/reduce a long position), sell_to_open (open a covered call or cash-covered put -- the ONLY way to open a short leg; requires owning the shares or the cash collateral), buy_to_close (close/reduce a short leg opened by sell_to_open).
            instrument_type: One of stock, call, put.
            quantity: Shares (stock) or contracts (options, 100 shares each).
            thesis: Why you're making this trade -- required, graded later.
            limit_price: Optional. Reject the trade if the live fill would be worse than this (above it for a buy, below it for a sell) -- protects against the price having moved since you last looked. Leave blank for no guard.
            strike: Required for options (call/put). Numeric string, e.g. "150" or "150.00".
            expiration: Required for options. Format YYYY-MM-DD.
            target_price: Optional, only meaningful on buy/sell_to_open (opening a position). Your take-profit level -- purely informational, recorded on the resulting position, nothing auto-closes at it. Leave blank if you don't want to record one.
            dead_price: Optional, same rules as target_price -- your stop-loss/invalidation level. Both are revisable later on an already-open position via set_price_targets, without needing another trade.
        """
        try:
            limit_price_decimal = Decimal(limit_price) if limit_price else None
            strike_decimal = Decimal(strike) if strike else None
            expiration_date = date.fromisoformat(expiration) if expiration else None
            target_price_decimal = Decimal(target_price) if target_price else None
            dead_price_decimal = Decimal(dead_price) if dead_price else None
        except (InvalidOperation, ValueError) as exc:
            raise ToolError(f"Bad limit_price/strike/expiration/target_price/dead_price value: {exc}")

        trade = execution.submit_trade(
            competitor, ticker, side, instrument_type, quantity, thesis,
            limit_price=limit_price_decimal, strike=strike_decimal, expiration=expiration_date,
            target_price=target_price_decimal, dead_price=dead_price_decimal,
        )
        if trade.status == Trade.STATUS_REJECTED:
            return f"REJECTED: {trade.rejection_reason}"
        if trade.status == Trade.STATUS_PENDING:
            return (
                "QUEUED: the market is closed and this account can't trade after hours -- this order "
                "will be evaluated at a live quote the next time the market opens, not right now."
            )
        return (
            f"FILLED: {side} {quantity} {instrument_type} {ticker} @ ${trade.fill_price} "
            f"-- cash balance now ${competitor.cash_balance}."
        )

    @beta_tool
    def set_price_targets(
        ticker: str,
        instrument_type: str,
        target_price: str = "",
        dead_price: str = "",
        strike: str = "",
        expiration: str = "",
    ) -> str:
        """Update your take-profit (target_price) and/or stop-loss/
        invalidation (dead_price) level on a position you ALREADY hold --
        without submitting a trade. Use this when your thesis on an open
        position changes (e.g. "trailing my dead level up to lock in
        gains") instead of only setting these once at entry via
        submit_trade -- they're revisable anytime. Purely informational,
        same as submit_trade's target_price/dead_price: nothing auto-closes
        at either level, you still have to submit_trade yourself when you
        actually decide to act.

        Args:
            ticker: Symbol of the position to update, e.g. AAPL.
            instrument_type: One of stock, call, put -- must match the position's.
            target_price: New take-profit level. Leave blank to leave it unchanged.
            dead_price: New stop-loss/invalidation level. Leave blank to leave it unchanged.
            strike: Required for options (call/put). Numeric string, e.g. "150".
            expiration: Required for options. Format YYYY-MM-DD.
        """
        if not target_price and not dead_price:
            raise ToolError("Provide at least one of target_price/dead_price.")
        try:
            strike_decimal = Decimal(strike) if strike else None
            expiration_date = date.fromisoformat(expiration) if expiration else None
            target_price_decimal = Decimal(target_price) if target_price else None
            dead_price_decimal = Decimal(dead_price) if dead_price else None
        except (InvalidOperation, ValueError) as exc:
            raise ToolError(f"Bad strike/expiration/target_price/dead_price value: {exc}")
        if instrument_type != Position.INSTRUMENT_STOCK and (strike_decimal is None or expiration_date is None):
            raise ToolError("Options positions require both strike and expiration.")

        matches = list(Position.objects.filter(
            competitor=competitor, instrument_type=instrument_type, ticker=ticker.upper(),
            strike=strike_decimal, expiration=expiration_date,
        ))
        if not matches:
            raise ToolError(f"No open {instrument_type} position found for {ticker.upper()}.")
        if len(matches) > 1:
            raise ToolError(
                f"Found {len(matches)} matching positions for {ticker.upper()} (long and short both open) "
                "-- can't tell which one you mean."
            )
        position = matches[0]

        fields = []
        if target_price_decimal is not None:
            position.target_price = target_price_decimal
            fields.append("target_price")
        if dead_price_decimal is not None:
            position.dead_price = dead_price_decimal
            fields.append("dead_price")
        position.save(update_fields=fields)
        return (
            f"Updated {ticker.upper()}: target={position.target_price if position.target_price is not None else '(unset)'}, "
            f"dead={position.dead_price if position.dead_price is not None else '(unset)'}."
        )

    @beta_tool
    def cancel_trade(trade_id: int) -> str:
        """Cancel one of your own orders that's still QUEUED (only
        possible if your system prompt's rules say this account can't
        trade after hours -- see submit_trade -- and the market was closed
        when you placed it). Only works on a trade of yours that's still
        pending; once it's filled or rejected there's nothing left to
        cancel. Check the "Orders still queued" section of your wake-up
        prompt for the ids of anything you can cancel.

        Args:
            trade_id: The id of the pending order to cancel (shown in the "Orders still queued" section).
        """
        try:
            trade = Trade.objects.get(id=trade_id, competitor=competitor)
        except Trade.DoesNotExist:
            raise ToolError(f"No trade {trade_id} found for you.")
        try:
            execution.cancel_pending_trade(trade)
        except execution.TradeAlreadyResolved as exc:
            raise ToolError(str(exc))
        return f"CANCELLED: order {trade_id} ({trade.side} {trade.quantity} {trade.instrument_type} {trade.ticker})."

    @beta_tool
    def list_ticker_memories() -> str:
        """List every ticker you have saved research notes for (see
        set_ticker_memories), with when each was last updated and how long
        the note is. Doesn't return the note text itself -- call
        get_ticker_memories for that once you know which tickers you want.
        Use this before researching a ticker from scratch to check whether
        you already have notes on it."""
        memories = AgentTickerMemory.objects.filter(competitor=competitor)
        if not memories:
            return "No ticker research notes saved yet."
        return json.dumps([
            {"ticker": m.ticker, "updated_at": m.updated_at.isoformat(), "length": len(m.content)}
            for m in memories
        ])

    @beta_tool
    def get_ticker_memories(tickers: list[str]) -> str:
        """Pull your saved research notes for one or more tickers (see
        list_ticker_memories to see what's available first). A ticker with
        no saved note comes back with content=null, not an error, so you
        can pull a batch without checking each one exists first.

        Args:
            tickers: Ticker symbols to pull, e.g. ["AAPL", "TSLA"]. Capped at 20 per call.
        """
        if not tickers:
            raise ToolError("tickers can't be empty.")
        if len(tickers) > _TICKER_MEMORY_BATCH_MAX:
            raise ToolError(f"Too many tickers ({len(tickers)}) -- capped at {_TICKER_MEMORY_BATCH_MAX} per call.")
        wanted = [t.upper() for t in tickers]
        found = {m.ticker: m for m in AgentTickerMemory.objects.filter(competitor=competitor, ticker__in=wanted)}
        return json.dumps([
            {
                "ticker": t,
                "content": found[t].content if t in found else None,
                "updated_at": found[t].updated_at.isoformat() if t in found else None,
            }
            for t in wanted
        ])

    @beta_tool
    def set_ticker_memories(entries: list[dict[str, str]]) -> str:
        """Save/overwrite your research notes for one or more tickers in
        one call. Each entry REPLACES that ticker's whole note, it doesn't
        append -- write the complete notes you want available next time
        for that ticker, not just what changed. Use this for
        ticker-specific research (thesis, catalysts, what changed since
        last look); cross-ticker or portfolio-level thoughts still go in
        update_scratchpad instead.

        Args:
            entries: One or more notes to save, each with a "ticker" and
                "content" key, e.g. [{"ticker": "AAPL", "content": "..."},
                {"ticker": "TSLA", "content": "..."}]. Capped at 20 per call.
        """
        if not entries:
            raise ToolError("entries can't be empty.")
        if len(entries) > _TICKER_MEMORY_BATCH_MAX:
            raise ToolError(f"Too many entries ({len(entries)}) -- capped at {_TICKER_MEMORY_BATCH_MAX} per call.")
        saved = []
        for entry in entries:
            ticker = (entry.get("ticker") or "").strip().upper()
            if not ticker:
                raise ToolError(f"Missing ticker in entry: {entry}")
            AgentTickerMemory.objects.update_or_create(
                competitor=competitor, ticker=ticker, defaults={"content": entry.get("content") or ""},
            )
            saved.append(ticker)
        return f"Saved research notes for {', '.join(saved)}."

    @beta_tool
    def update_scratchpad(content: str) -> str:
        """Overwrite your persistent cross-ticker working notes -- what
        you're watching, portfolio-level thesis, what you decided last
        time and why. For notes on a SPECIFIC ticker, use
        set_ticker_memories instead -- that's pulled in on demand and
        scales across many tickers, while this scratchpad is read in full
        at the start of every wake-up, so keep it current and reasonably
        short. This REPLACES the whole thing, it doesn't
        append -- write the complete notes you want available next time,
        not just what changed. This is displayed to the operator as
        rendered Markdown, not plain text -- write valid Markdown (real
        `#`/`##` headers, `-`/`*` bullets, balanced `**bold**`/code fences,
        no ad hoc "=== SECTION ===" banners or other made-up formatting)
        so it actually renders instead of showing up as a wall of raw
        symbols.

        Args:
            content: Your full notes, replacing whatever was there before -- valid Markdown.
        """
        scratchpad, _ = AgentScratchpad.objects.get_or_create(competitor=competitor)
        scratchpad.content = content
        # "updated_at" must be listed explicitly -- update_fields skips
        # auto_now processing for any field not in the list, so without
        # this the column silently stops advancing on every real edit
        # (confirmed live: a scratchpad updated dozens of times over two
        # days still showed its very first updated_at in the admin).
        scratchpad.save(update_fields=["content", "updated_at"])
        return "Scratchpad updated."

    @beta_tool
    def schedule_wakeup(run_at: str, reason: str) -> str:
        """Schedule yourself to be woken up again at a specific future
        time to check in and decide whether to act -- e.g. "check SPY at
        9:45am tomorrow" or "check back after this week's CPI print
        Thursday 8:35am". Your system prompt's rules list names exactly
        which daily wake-ups you're guaranteed for free (the "floor",
        e.g. an 8am news scan) -- that floor may or may not include
        market open or the close. If it doesn't and you want that
        coverage, YOU have to schedule_wakeup for it yourself like
        anything else -- it is not free, and it counts against your
        daily wake-up cap exactly the same as a specific-thesis check
        would. Capped at a limited number of wake-ups per day, so make
        each one count rather than scheduling several close together.

        Args:
            run_at: When to wake up, ISO format, e.g. "2026-08-13T09:45:00-04:00". Include a timezone offset -- naive times are assumed UTC.
            reason: What you want to check/reconsider when this fires -- this is shown to you at wake-up time.
        """
        from django.utils import timezone as dj_timezone
        from django.utils.dateparse import parse_datetime

        from .services.wakeup_spacing import conflict_reason, wakeups_today_count

        run_at_dt = parse_datetime(run_at)
        if run_at_dt is None:
            raise ToolError(f"Couldn't parse run_at={run_at!r} -- use ISO format, e.g. 2026-08-13T09:45:00-04:00.")
        if dj_timezone.is_naive(run_at_dt):
            run_at_dt = dj_timezone.make_aware(run_at_dt, dj_timezone.utc)

        # Otherwise a miscomputed time (wrong day, wrong timezone offset)
        # gets apply_async(eta=<already past>), which Celery fires almost
        # immediately -- burning a wake-up slot on an unintended instant
        # right now instead of the check-in the agent actually meant to
        # schedule, and reads back as a confusing burst of turns rather
        # than the gap this whole tool exists to prevent.
        if run_at_dt <= dj_timezone.now():
            return f"Not scheduled -- {run_at_dt.isoformat()} is already in the past. Pick a future time."

        # wakeups_today_count excludes ForcedCheckin/safety-net rows and
        # counts the ET calendar day, not created_at__date's UTC day --
        # see that function's docstring for the production incident
        # counting those rows here caused.
        today_count = wakeups_today_count(competitor)
        if today_count >= LEAGUE_MAX_WAKEUPS_PER_DAY:
            return (
                f"Not scheduled -- you've already used {today_count}/{LEAGUE_MAX_WAKEUPS_PER_DAY} "
                "wake-ups today. Make your next one count, or wait until tomorrow."
            )

        conflict = conflict_reason(competitor, run_at_dt)
        if conflict is not None:
            return f"Not scheduled -- {conflict} Pick a time at least 3 minutes from either."

        wakeup = AgentWakeup.objects.create(competitor=competitor, run_at=run_at_dt, reason=reason)
        from .tasks import run_agent_wakeup
        run_agent_wakeup.apply_async(args=[wakeup.id], eta=run_at_dt)
        return f"Scheduled a wake-up for {run_at_dt.isoformat()}."

    # Provider-exclusive tools -- gated here so an unusable one is never
    # even offered to the model, rather than registered and left to raise
    # ToolError on the first (and every) call. Computed fresh on each
    # build_league_tools() call, not cached at settings/module load time,
    # so @override_settings in tests (and a real deploy-time env change)
    # both take effect immediately -- these all read live token/provider
    # settings, the same ones each tool's own inline check already used.
    # Only genuinely provider-exclusive tools are gated this way; the core
    # cross-provider ones (get_stock_quote, get_options_chain, get_technicals)
    # stay registered even with neither token configured -- that's a total
    # misconfiguration, not a capability gap, and they already surface it
    # clearly via ToolError on first call.
    _capability_gate = {
        get_intraday_candles: settings.PRICING_PROVIDER == "tradier" and bool(settings.TRADIER_API_TOKEN),
        get_market_news: bool(settings.STEADY_API_TOKEN),
        get_reddit_sentiment: bool(settings.STEADY_API_TOKEN),
        # Needs Steady (the fetch) AND OpenAI (reddit_tickers.extract_tickers)
        # -- missing either one fails on EVERY call, not just some inputs.
        scan_subreddit_for_tickers: bool(settings.STEADY_API_TOKEN) and bool(settings.OPENAI_API_KEY),
        search_web: bool(settings.BRAVE_API_KEY),
    }

    all_tools = [
        get_stock_quote, get_stock_quotes, get_options_chain, get_technicals, get_price_history,
        scan_down_streaks, get_intraday_candles, get_market_news,
        get_reddit_sentiment, scan_subreddit_for_tickers, search_web, list_filings, get_company_report, search_wikipedia,
        get_portfolio, get_competition_standings, submit_trade, set_price_targets, cancel_trade,
        list_ticker_memories, get_ticker_memories, set_ticker_memories, update_scratchpad, schedule_wakeup,
    ]
    return [tool for tool in all_tools if _capability_gate.get(tool, True)]


# openai_tool_defs/run_openai_tool now live in companies/services/
# tool_bridge.py (imported above) -- assistant/tools.py's build_tools()
# needed the exact same reshape/dispatch once the chat assistant grew
# OpenAI support (assistant/tasks.py), and neither implementation was
# league-specific to begin with. Re-imported into this module's namespace
# rather than changing anything downstream: league/services/llm_openai.py
# still does `from ..tools import ... openai_tool_defs, run_openai_tool`
# unchanged.

OPENAI_WEB_SEARCH_TOOL = {"type": "web_search"}


def gemini_tool_defs(tools: list) -> list[genai_types.FunctionDeclaration]:
    """Gemini FunctionDeclaration objects, reshaped from the same
    @beta_tool objects the Anthropic/OpenAI paths use -- .input_schema
    (real JSON Schema) is passed straight through via
    parameters_json_schema, confirmed to accept raw JSON Schema directly
    against the installed google-genai==2.17.0 SDK. Same zero-forked-logic
    principle as openai_tool_defs -- no hand-written duplicate schema."""
    return [
        genai_types.FunctionDeclaration(name=t.name, description=t.description, parameters_json_schema=t.input_schema)
        for t in tools
    ]


def run_gemini_tool(tools: list, name: str, args: dict) -> str:
    """Dispatch one Gemini FunctionCall by name to the matching @beta_tool
    object's own .call() -- same contract as run_openai_tool, except
    Gemini's FunctionCall.args is already a dict (confirmed live), unlike
    OpenAI's JSON-string arguments, so no json.loads() here. Every
    function_call needs a function_response reply or the next
    generate_content() call errors -- nothing may propagate past this
    function."""
    dispatch = {t.name: t for t in tools}
    tool = dispatch.get(name)
    if tool is None:
        return f"Unknown tool {name!r}."
    try:
        return tool.call(args or {})
    except ToolError as exc:
        return str(exc)
    except Exception as exc:
        return f"Error: {exc!r}"


GEMINI_SEARCH_TOOL = genai_types.Tool(google_search=genai_types.GoogleSearch())
