"""Tools the chat assistant can call, bound to a specific user/conversation
via build_tools() (a closure factory, not module-level functions) so the
model-facing schema only exposes arguments Claude should actually supply
(ticker, query, ...) -- user/conversation identity comes from the closure,
never from something the model could set itself.

Most tools execute immediately when Claude calls them, including writes --
linking/unlinking a wiki page is cheap and reversible, unlike ingesting a
filing (a real fetch + Docling parse + embedding call). Only
propose_filing_ingestion is deliberately not immediate: it doesn't perform
the ingestion itself, just proposes it -- see PendingAction's docstring for
why."""

import json

from anthropic import beta_tool
from anthropic.lib.tools import ToolError
from django.conf import settings
from django.db import IntegrityError
from django.db.models import Count

from companies.models import Company, ResearchClip, SubredditMention, TrackedCompany, TrackedSubreddit, WikiSource
from companies.services import bar_screener, brave_search, embeddings, filing_ingestion, pricing, reddit_tickers, sec_edgar, steady, technicals, tradier, vector_store, wiki_ingestion, wikipedia_client
from companies.services.presentation import build_report
from companies.services.sec_edgar import resolve_company
from companies.tasks import sync_wiki_source
from realtime.utils import send_to_user

from .models import Conversation, PendingAction

# get_price_history's tool-level cap -- tighter than technicals.py's own
# service-level _PRICE_HISTORY_MAX_DAYS (200) backstop: this bounds how
# many OHLCV rows land in the model's context in one call, not a live
# round-trip count. Same value as league/tools.py's identical constant.
_PRICE_HISTORY_TOOL_MAX_DAYS = 90
_VALID_INTRADAY_INTERVALS = {"1min", "5min", "15min"}
# Same value/reasoning as league/tools.py's identical constant -- a full
# regular session at 1min resolution is ~390 bars, capped to the most
# recent N rather than dumping a whole session into the model's context.
_INTRADAY_MAX_BARS = 100


def build_tools(user, conversation: Conversation) -> list:
    @beta_tool
    def list_filings(ticker: str) -> str:
        """List a company's 10-K/10-Q filings from SEC EDGAR (form, filed
        date, period, accession number, URL), each flagged with whether
        it's already been ingested into semantic search for this user.
        "Ingested" means saved as searchable ResearchClips (see
        search_research_clips) -- not ingested just means we have the EDGAR
        link but haven't parsed/embedded it yet.

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

        tracked, _ = TrackedCompany.objects.get_or_create(
            user=user, company=company, defaults={"origin": TrackedCompany.ORIGIN_USER}
        )
        rows = [
            {
                "form": f["form"],
                "filed": f["filing_date"],
                "period": f["report_date"],
                "accession_number": f["accession_number"],
                "url": f["url"],
                "ingested": filing_ingestion.is_ingested(tracked, f["accession_number"]),
            }
            for f in filings
        ]
        return json.dumps(rows)

    @beta_tool
    def get_company_report(ticker: str) -> str:
        """Get the latest cached financial report for a company: price,
        market cap, P/S, cash runway, the price/shares tripwire check, and
        the multi-year metrics table (revenue, gross profit, net income,
        operating cash flow, capex, cash, debt, share count, etc). Same
        data as this app's Screener dashboard. Reads only what's already
        cached from the company's last screen -- does not fetch fresh data.

        Args:
            ticker: Stock ticker symbol, e.g. RKLB.
        """
        try:
            company = Company.objects.get(ticker=ticker.upper())
        except Company.DoesNotExist:
            raise ToolError(f"{ticker.upper()} hasn't been screened yet -- nothing cached to report.")
        return json.dumps(build_report(company), default=str)

    @beta_tool
    def get_stock_quote(ticker: str, asset_class: str = "STOCKS") -> str:
        """Get a LIVE quote for a stock/ETF/index right now -- NOT cached,
        NOT persisted. Returns last sale price, net/percent change, bid/ask,
        volume, market status (Open/Closed/After-Hours), and 52-week/day
        range. This is the actual current price -- get_options_chain does
        NOT include the underlying's price (only strikes/moneyness), so use
        this tool for "what's it trading at" questions, and use
        get_options_chain separately for option contract data.

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
    def get_options_chain(ticker: str, expiration: str = "") -> str:
        """Get a LIVE options chain snapshot straight from the market right
        now -- NOT cached, NOT persisted, nothing saved (unlike
        get_company_report, which only reads what's already cached from a
        screen). Works for stocks, ETFs, and indices (e.g. AAPL, SPY, or an
        index like $SPX) -- these don't need to be a tracked/screened
        company first. Returns the ~10 calls and ~10 puts closest to the
        money for one expiration (nearest available, or a specific one if
        given), each with bid/ask/last, volume, open interest, implied
        volatility, delta/vega/theta/rho, and 1-year IV rank. Also returns
        every other available weekly/monthly expiration date -- call again
        with one of those in `expiration` to see a different date. Does NOT
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
        ticker in a single call. Every field is independently null when
        its source is unavailable rather than omitted or estimated --
        treat a null as "this data doesn't exist right now," never infer
        or guess a value for it.

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
        are computed from, for when you want the actual price path (a
        chart, a specific pattern) rather than only a pre-computed
        aggregate. `days` counts trading days returned, not calendar
        days -- you may get fewer than asked for a ticker with a short
        listing history (e.g. a recent IPO), which is normal, not an
        error. Requires this deployment's pricing provider to be Tradier
        (same gate as get_technicals' historical fields). Returns a plain
        array of bars -- unlike get_intraday_candles, nothing here is
        ever truncated; a days > 90 request is rejected outright instead.

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
        market daily-bar history (companies.models.DailyBar, backed by
        Polygon), so it works the same regardless of PRICING_PROVIDER.

        Returns a JSON array (empty if nothing matches, or if this
        deployment's daily-bar history hasn't been backfilled far enough
        yet -- a ticker needs sma_window sessions of history to be
        considered at all). Each entry: {ticker, down_streak, last_close,
        sma, avg_volume, as_of}. Capped at 50 candidates, longest streak
        first.

        This is a pure technical scan -- it does NOT check earnings
        dates, liquidity, or anything else you'd want before actually
        recommending a trade. Follow up with get_technicals on whatever
        this returns to check days_to_earnings and get a live quote.

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
        rather than only the last daily close. Different from
        get_price_history, which is daily bars over many days; this is
        sub-day bars within the current session only.

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
        before the session's first bar has printed -- that's normal, not
        an error.

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
    def get_reddit_sentiment(ticker: str) -> str:
        """Get recent Reddit posts mentioning a ticker -- raw content
        (title, score, upvote_ratio, comment count, a text excerpt, a
        permalink), NOT a computed sentiment score. Read what's actually
        there and judge it yourself; this hands you opinions people
        posted, not verified facts. Results are NOT reliably scoped to
        any one subreddit (SteadyAPI's own subreddit-filter parameter on
        this search doesn't actually work) -- treat this as "what is
        Reddit broadly saying," never "what does r/wallstreetbets
        specifically say" (use scan_subreddit_for_tickers for that). A
        short/generic ticker can pull in noise from words that just
        happen to share the same letters -- the first several results
        (sorted by relevance) are usually the real signal.

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
        """Scan one subreddit's CURRENT posts right now and return which
        stock tickers are actually being discussed -- a live, one-off
        look, not persisted anywhere. Use this to FIND ideas worth
        investigating (e.g. scan wallstreetbets for what's trending right
        now), not to check a ticker you already have in mind (use
        get_reddit_sentiment for that -- cheaper and more targeted). This
        is different from track_subreddit: that adds the subreddit to the
        user's watch list for an ongoing twice-daily automated scan (see
        get_subreddit_mentions for those results); this tool is a single
        live snapshot. Ticker identification reads each post's actual
        content rather than keyword/cashtag-matching, so an unrelated
        post that happens to share letters with a ticker won't show up.
        Costs one extra small OpenAI-billed model call beyond the Reddit
        fetch itself, covering the whole batch of posts in one shot.

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
        each. NOT persisted anywhere. Use this for anything the other
        tools here can't answer: current events, a company's own website,
        news outside Reddit, background research on something not in SEC
        filings/Wikipedia. Different from search_wikipedia (Wikipedia
        only) and get_reddit_sentiment (Reddit only) -- this is the
        general "look it up" tool. Read the description/snippet to judge
        relevance yourself; fetching the actual page content isn't
        available here.

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
    def track_subreddit(subreddit: str) -> str:
        """Add a subreddit to the user's watch list for ongoing ticker
        discovery. Runs immediately -- no confirmation prompt, fully
        reversible via untrack_subreddit -- but does NOT scan it right
        now: companies.tasks.scan_tracked_subreddits picks up every
        watched subreddit twice a day (roughly 00:00 and 12:00 UTC), so
        results won't show up in get_subreddit_mentions until the next
        scheduled run. For an immediate look, use
        scan_subreddit_for_tickers instead (or in addition).

        Args:
            subreddit: Name without "r/", e.g. wallstreetbets, stocks, investing.
        """
        name = subreddit.strip()
        if name.lower().startswith("r/"):
            name = name[2:]
        name = name.strip("/").strip()
        if not name:
            raise ToolError("Subreddit name can't be empty.")
        try:
            watch, created = TrackedSubreddit.objects.get_or_create(user=user, name=name)
        except IntegrityError as exc:
            raise ToolError(str(exc))
        if created:
            return f"Now watching r/{watch.name} -- it'll be scanned on the next twice-daily run."
        return f"r/{watch.name} is already on the watch list."

    @beta_tool
    def list_tracked_subreddits() -> str:
        """List every subreddit currently on the user's watch list (see
        track_subreddit), with when each was added."""
        watches = TrackedSubreddit.objects.filter(user=user)
        if not watches:
            return "No subreddits are being watched yet."
        return json.dumps([
            {"id": w.id, "name": w.name, "added_at": w.added_at} for w in watches
        ], default=str)

    @beta_tool
    def untrack_subreddit(subreddit: str) -> str:
        """Remove a subreddit from the user's watch list -- stops future
        twice-daily scans. Already-collected SubredditMention rows for it
        are left in place, not deleted. Fully reversible via
        track_subreddit again later.

        Args:
            subreddit: The watched subreddit's name, as shown by list_tracked_subreddits (without "r/").
        """
        name = subreddit.strip()
        if name.lower().startswith("r/"):
            name = name[2:]
        name = name.strip("/").strip()
        watch = TrackedSubreddit.objects.filter(user=user, name=name).first()
        if watch is None:
            raise ToolError(f'"{name}" isn\'t on the watch list.')
        watch.delete()
        return f"Stopped watching r/{name}."

    @beta_tool
    def get_subreddit_mentions(ticker: str = "") -> str:
        """Read results already collected by the twice-daily watch-list
        scan (companies.tasks.scan_tracked_subreddits) -- this does NOT
        trigger a new scan. With no ticker: aggregated {subreddit,
        ticker, count} rows across every watched subreddit, most-
        mentioned first -- good for "what's trending across my watch
        list." With a ticker: every individual post mention of it
        (title, excerpt, permalink, score, comment count, when scanned)
        for the "show me the actual posts" drill-down.

        Args:
            ticker: Optional -- narrow to individual post mentions of one ticker, e.g. RKLB.
        """
        if ticker:
            mentions = SubredditMention.objects.filter(user=user, ticker=ticker.upper())
            if not mentions:
                return f"No collected mentions of {ticker.upper()} yet."
            return json.dumps([
                {
                    "subreddit": m.subreddit, "ticker": m.ticker, "title": m.title,
                    "permalink": m.permalink, "excerpt": m.excerpt, "score": m.score,
                    "num_comments": m.num_comments, "scanned_at": m.scanned_at,
                }
                for m in mentions
            ], default=str)

        rows = (
            SubredditMention.objects.filter(user=user)
            .values("subreddit", "ticker")
            .annotate(count=Count("id"))
            .order_by("-count")
        )
        if not rows:
            return "No mentions collected yet -- either no subreddits are watched, or the next scheduled scan hasn't run."
        return json.dumps(list(rows))

    @beta_tool
    def search_research_clips(query: str, ticker: str = "") -> str:
        """Semantic search over this user's own saved research clips
        (pasted filing excerpts, notes, articles -- see list_filings for
        turning a filing into searchable clips in the first place).
        Finds passages relevant to the query, ranked nearest-first.

        Args:
            query: What to search for, e.g. "customer concentration risk".
            ticker: Optional -- narrow results to one company's clips.
        """
        vector = embeddings.embed_text(query)
        if vector is None:
            raise ToolError("Semantic search isn't available right now -- OPENAI_API_KEY isn't configured.")

        hits = vector_store.search_clips(
            vector, user_id=str(user.id), ticker=ticker.upper() if ticker else None, limit=10
        )
        if not hits:
            return "No matching clips found."

        clips_by_id = {
            c.id: c
            for c in ResearchClip.objects.filter(id__in=[h["clip_id"] for h in hits]).select_related(
                "tracked_company__company"
            )
        }
        results = []
        for hit in hits:
            clip = clips_by_id.get(hit["clip_id"])
            if clip is None:
                continue
            results.append(
                {
                    "label": clip.label,
                    "ticker": clip.tracked_company.company.ticker,
                    "category": clip.category,
                    "text": clip.text,
                    "distance": hit["distance"],
                }
            )
        return json.dumps(results)

    @beta_tool
    def search_media_transcripts(query: str, ticker: str = "") -> str:
        """Semantic search over this user's uploaded audio/video transcripts
        (earnings calls, interviews, etc. -- diarized, so each result knows
        who said it and when). Finds passages relevant to the query,
        ranked nearest-first, with speaker and timestamp so you can cite
        exactly who said what and point to where in the recording it was.

        Args:
            query: What to search for, e.g. "guidance for next quarter".
            ticker: Optional -- narrow results to one company's media.
        """
        # Imported here, not at module level: the media app is opt-in
        # (settings.ENABLE_MEDIA) and isn't in INSTALLED_APPS at all when
        # off -- importing its model at module level would break this
        # entire module's import, not just this one tool, on a deployment
        # that never enabled media. This tool is itself gated on
        # ENABLE_MEDIA below (the _capability_gate dict), so in practice
        # this only ever runs when the app is actually installed.
        from media.models import MediaSegment
        from media.services import vector_store as media_vector_store

        vector = embeddings.embed_text(query)
        if vector is None:
            raise ToolError("Semantic search isn't available right now -- OPENAI_API_KEY isn't configured.")

        hits = media_vector_store.search_segments(
            vector, user_id=str(user.id), ticker=ticker.upper() if ticker else None, limit=10
        )
        if not hits:
            return "No matching media transcripts found."

        segments_by_id = {
            s.id: s
            for s in MediaSegment.objects.filter(id__in=[h["segment_id"] for h in hits]).select_related(
                "media_file__company"
            )
        }
        results = []
        for hit in hits:
            segment = segments_by_id.get(hit["segment_id"])
            if segment is None:
                continue
            results.append(
                {
                    "media_file": segment.media_file.original_filename,
                    "ticker": segment.media_file.company.ticker if segment.media_file.company_id else None,
                    "speaker": segment.speaker,
                    "start_seconds": segment.start_seconds,
                    "end_seconds": segment.end_seconds,
                    "text": segment.text,
                    "distance": hit["distance"],
                }
            )
        return json.dumps(results)

    @beta_tool
    def propose_filing_ingestion(ticker: str, accession_number: str) -> str:
        """Propose ingesting one SEC filing into semantic search (fetches
        it, parses and chunks it with Docling, embeds each chunk -- a real
        action with real latency and cost). This does NOT run immediately:
        it shows the user a confirmation prompt and returns without
        ingesting anything. Only call this after list_filings has
        confirmed the filing exists and isn't already ingested -- don't
        call it speculatively, and don't call it again for the same filing
        while waiting for the user to respond.

        Args:
            ticker: Stock ticker symbol, e.g. RKLB.
            accession_number: The filing's accession number from list_filings, e.g. 0001390478-26-000003.
        """
        try:
            company = resolve_company(ticker)
        except ValueError as exc:
            raise ToolError(str(exc))

        tracked, _ = TrackedCompany.objects.get_or_create(
            user=user, company=company, defaults={"origin": TrackedCompany.ORIGIN_USER}
        )
        if filing_ingestion.is_ingested(tracked, accession_number):
            raise ToolError(f"Accession {accession_number} is already ingested for {company.ticker}.")

        submissions = sec_edgar.get_submissions(company)
        filing = next(
            (f for f in sec_edgar.list_filings(submissions) if f["accession_number"] == accession_number), None
        )
        if filing is None:
            raise ToolError(f"No filing with accession {accession_number} found for {company.ticker} -- check list_filings.")

        pending = PendingAction.objects.create(
            conversation=conversation,
            action_type=PendingAction.TYPE_INGEST_FILING,
            params={"ticker": company.ticker, "accession_number": accession_number},
            description=f"Ingest {filing['form']} filed {filing['filing_date']} for {company.ticker} into semantic search",
        )
        send_to_user(
            user.id,
            "assistant.confirmation_required",
            conversation_id=conversation.id,
            pending_action_id=pending.id,
            description=pending.description,
            ticker=company.ticker,
            form=filing["form"],
            filing_date=filing["filing_date"],
        )
        return (
            f"Proposed ingesting the {filing['form']} filed {filing['filing_date']} for {company.ticker} -- "
            "a confirmation prompt has been shown to the user. It has NOT been ingested yet. Do not call this "
            "again for the same filing; tell the user you're waiting on their confirmation."
        )

    @beta_tool
    def search_wikipedia(query: str) -> str:
        """Search Wikipedia LIVE right now and return short summaries of
        matching pages -- NOT persisted anywhere, nothing is saved or
        indexed. Use this to find or confirm a page exists/what it's about
        before linking it (see link_wiki_page), or to answer a general
        background question directly. This is DIFFERENT from
        search_research_clips / search_media_transcripts: those search this
        user's own ALREADY-INGESTED material; this queries Wikipedia's live
        search and returns pages that have NOT been ingested. Don't use this
        to search a company's already-linked wiki content -- for that, use
        search_research_clips (linked pages become searchable clips
        automatically once linked).

        Args:
            query: Search terms, e.g. "Rocket Lab Neutron rocket".
        """
        results = wikipedia_client.search_pages(query, limit=5)
        if not results:
            return f'No Wikipedia pages found for "{query}".'
        return json.dumps(results)

    @beta_tool
    def link_wiki_page(ticker: str, page_title: str) -> str:
        """Link a Wikipedia page to a tracked company for ongoing
        monitoring: fetches the full article now, saves it as searchable
        ResearchClips, and schedules a daily check for future edits (an
        edit triggers a notification and a re-index of the searchable
        content to match the latest version). Runs immediately -- no
        confirmation prompt, unlike propose_filing_ingestion, since this is
        cheap and fully reversible (unlink_wiki_page). A company can have
        multiple linked pages (e.g. a parent company and a specific
        product). Accepts either a plain title or a full en.wikipedia.org
        URL. Resolves redirects and rejects disambiguation pages -- link
        the specific article, not a disambiguation index.

        Args:
            ticker: Stock ticker symbol, e.g. RKLB.
            page_title: Wikipedia page title (exact or approximate, e.g. "Rocket Lab") or a full en.wikipedia.org URL.
        """
        try:
            company = resolve_company(ticker)
        except ValueError as exc:
            raise ToolError(str(exc))
        tracked, _ = TrackedCompany.objects.get_or_create(
            user=user, company=company, defaults={"origin": TrackedCompany.ORIGIN_USER}
        )
        try:
            title = wikipedia_client.resolve_title_input(page_title)
            page = wikipedia_client.fetch_full_page(title)
        except ValueError as exc:
            raise ToolError(str(exc))
        if page["is_disambiguation"]:
            raise ToolError(f'"{title}" is a disambiguation page -- specify which article you mean.')

        source, created = WikiSource.objects.get_or_create(
            tracked_company=tracked, page_title=page["canonical_title"],
        )
        if created:
            sync_wiki_source.delay(source.id)
            return f'Linked "{source.page_title}" to {company.ticker} -- fetching and indexing it now.'
        return f'"{source.page_title}" is already linked to {company.ticker}.'

    @beta_tool
    def list_linked_wiki_pages(ticker: str) -> str:
        """List every Wikipedia page currently linked to a tracked company,
        with when each was last checked for changes.

        Args:
            ticker: Stock ticker symbol, e.g. RKLB.
        """
        try:
            company = resolve_company(ticker)
        except ValueError as exc:
            raise ToolError(str(exc))
        tracked = TrackedCompany.objects.filter(user=user, company=company).first()
        if tracked is None:
            return f"{company.ticker} isn't tracked yet, so no wiki pages are linked."
        sources = tracked.wiki_sources.all()
        if not sources:
            return f"No Wikipedia pages linked to {company.ticker} yet."
        return json.dumps([
            {
                "id": s.id,
                "page_title": s.page_title,
                "last_checked_at": s.last_checked_at,
                "last_changed_at": s.last_changed_at,
            }
            for s in sources
        ], default=str)

    @beta_tool
    def unlink_wiki_page(ticker: str, page_title: str) -> str:
        """Unlink a Wikipedia page from a tracked company: stops future
        daily checks and removes its content from semantic search (deletes
        the ResearchClips and their Weaviate vectors). Immediate, no
        confirmation -- fully reversible via link_wiki_page again later.

        Args:
            ticker: Stock ticker symbol, e.g. RKLB.
            page_title: The linked page's title, as shown by list_linked_wiki_pages.
        """
        try:
            company = resolve_company(ticker)
        except ValueError as exc:
            raise ToolError(str(exc))
        tracked = TrackedCompany.objects.filter(user=user, company=company).first()
        source = tracked.wiki_sources.filter(page_title=page_title).first() if tracked else None
        if source is None:
            raise ToolError(f'"{page_title}" isn\'t linked to {company.ticker}.')

        clips = tracked.clips.filter(source=wiki_ingestion.clip_source_key(source))
        for clip in clips:
            vector_store.delete_clip(clip.id)
        clips.delete()
        source.delete()
        return f'Unlinked "{page_title}" from {company.ticker}.'

    @beta_tool
    def get_wiki_page_changes(ticker: str, page_title: str) -> str:
        """Show what changed in a linked Wikipedia page between its two
        most recent snapshots (paragraph-level, size-capped) -- for "did
        something notable happen" questions (e.g. did a rocket explode, was
        a launch scrubbed, did leadership change). Returns "not enough
        history yet" if fewer than two snapshots exist (e.g. right after
        first linking, before the first change is detected).

        Args:
            ticker: Stock ticker symbol, e.g. RKLB.
            page_title: The linked page's title, as shown by list_linked_wiki_pages.
        """
        try:
            company = resolve_company(ticker)
        except ValueError as exc:
            raise ToolError(str(exc))
        tracked = TrackedCompany.objects.filter(user=user, company=company).first()
        source = tracked.wiki_sources.filter(page_title=page_title).first() if tracked else None
        if source is None:
            raise ToolError(f'"{page_title}" isn\'t linked to {company.ticker}.')

        result = wiki_ingestion.diff_latest(source)
        if not result["available"]:
            return f'Not enough history yet for "{page_title}" -- only one snapshot has been captured so far.'
        if not result["diff"]:
            return f'No paragraph-level differences detected for "{page_title}" between its last two snapshots.'
        return json.dumps(result)

    # Provider-exclusive tools -- gated here so an unusable one is never
    # even offered to the model, rather than registered and left to raise
    # ToolError on the first (and every) call. Computed fresh on each
    # build_tools() call (not cached at settings/module load time) so
    # @override_settings in tests and a real deploy-time env change both
    # take effect immediately. Mirrors league/tools.py's build_league_
    # tools() gate -- same capabilities, same reasoning, this app just
    # doesn't have a get_market_news tool to gate on "news".
    _capability_gate = {
        get_intraday_candles: settings.PRICING_PROVIDER == "tradier" and bool(settings.TRADIER_API_TOKEN),
        get_reddit_sentiment: bool(settings.STEADY_API_TOKEN),
        # Needs Steady (the fetch) AND OpenAI (reddit_tickers.extract_tickers)
        # -- missing either one fails on EVERY call, not just some inputs.
        scan_subreddit_for_tickers: bool(settings.STEADY_API_TOKEN) and bool(settings.OPENAI_API_KEY),
        search_web: bool(settings.BRAVE_API_KEY),
        # Needs OpenAI (the query embedding) AND Weaviate (the actual
        # vector search) -- embeddings.embed_text only checks the former,
        # so gating on just that would still register a tool that fails
        # on every call whenever Weaviate alone is unconfigured.
        search_research_clips: bool(settings.WEAVIATE_URL) and bool(settings.OPENAI_API_KEY),
        # Also requires ENABLE_MEDIA -- the media app (MediaSegment,
        # media_vector_store) isn't in INSTALLED_APPS at all when that's
        # off, so this tool can't function regardless of Weaviate/OpenAI.
        search_media_transcripts: (
            settings.ENABLE_MEDIA and bool(settings.WEAVIATE_URL) and bool(settings.OPENAI_API_KEY)
        ),
    }

    all_tools = [
        list_filings, get_company_report, get_stock_quote, get_options_chain, get_technicals,
        get_price_history, scan_down_streaks, get_intraday_candles,
        get_reddit_sentiment, scan_subreddit_for_tickers, search_web,
        track_subreddit, list_tracked_subreddits, untrack_subreddit, get_subreddit_mentions,
        search_research_clips,
        search_media_transcripts, propose_filing_ingestion,
        search_wikipedia, link_wiki_page, list_linked_wiki_pages,
        unlink_wiki_page, get_wiki_page_changes,
    ]
    return [tool for tool in all_tools if _capability_gate.get(tool, True)]
