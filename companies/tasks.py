import json
import logging
from zoneinfo import ZoneInfo

from celery import shared_task
from django.utils import timezone

from realtime.utils import send_to_user

from .models import Company, DailyBar, ResearchClip, SubredditMention, TrackedCompany, TrackedSubreddit, WikiSource
from .services import embeddings, fmp, reddit_tickers, steady, vector_store
from .services.presentation import build_report
from .services.screening import run_screen

logger = logging.getLogger(__name__)


@shared_task
def ping():
    """Trivial task to verify the worker <-> broker <-> Django wiring end to
    end. Not meant to stick around once real tasks exist."""
    return "pong"


@shared_task
def screen_ticker(user_id, ticker, max_age_days=1.0):
    """Mode 1 (Screen), off the request/response cycle. ScreenView.post()
    enqueues this and returns 202 immediately; the result arrives over the
    user's WebSocket group instead of the HTTP response (docs/websockets.md).
    user_id travels as a plain string (Celery args are JSON) -- matches
    user_group_name()'s f-string formatting either way, so group names still
    line up with the WebSocket connection opened from the JWT's user_id."""
    send_to_user(user_id, "screen.progress", ticker=ticker, status="running")
    try:
        result = run_screen(ticker, max_age_days=max_age_days)
    except ValueError as exc:
        send_to_user(user_id, "screen.error", ticker=ticker, message=str(exc))
        return
    report = build_report(result.company)
    # build_report() has real int dict keys (fiscal years, e.g. {2023: ...}) --
    # DRF's JSON renderer stringifies those for the normal HTTP response, but
    # this goes straight through the channel layer instead, which serializes
    # with msgpack. msgpack's strict unpacking rejects non-string map keys,
    # so round-trip through json first to match the exact shape the HTTP API
    # already documents (string keys throughout).
    report = json.loads(json.dumps(report))
    send_to_user(user_id, "screen.complete", ticker=ticker, report=report)


@shared_task
def refresh_prices():
    """Daily Celery Beat job (settings.CELERY_BEAT_SCHEDULE): force-refresh
    just the FMP price/market-cap snapshot for every tracked company, once,
    after market close -- deliberately narrower than a full re-screen, since
    EDGAR fundamentals only change when a filing lands, not daily, and
    re-pulling those every day would burn rate-limit budget for no reason.
    Runs for every company anyone tracks regardless of status -- a killed
    company is a status flip, not a delete (matches Phase 4's clips/journal
    precedent)."""
    company_ids = TrackedCompany.objects.values_list("company_id", flat=True).distinct()
    for company in Company.objects.filter(id__in=company_ids):
        fmp.get_profile(company, max_age_days=0)


@shared_task
def ingest_daily_bars():
    """Daily Celery Beat job (settings.CELERY_BEAT_SCHEDULE): pull one
    session's full-market grouped-daily bars from Polygon
    (companies/services/polygon.py) and bulk-upsert them into DailyBar.
    Scheduled after market close, same "after today's close" placement as
    refresh_prices/scan_reddit_for_tracked_tickers_daily, since Polygon's
    grouped-daily payload for a session starts appearing once that
    session's tape closes.

    No holiday calendar -- same known/accepted gap as
    league.services.market_hours (weekday-only, not exchange-calendar-
    aware). A market holiday just gets Polygon's resultsCount: 0 response,
    logged and skipped, not treated as an error. No-ops the same way if
    POLYGON_API_KEY isn't configured (polygon.get_grouped_daily returns []
    either way, so this task can't tell the two apart -- both are
    "nothing to ingest today," not a failure)."""
    from .services import polygon

    today = timezone.localdate(timezone=ZoneInfo("America/New_York"))
    if today.weekday() >= 5:
        return

    day = today.isoformat()
    bars = polygon.get_grouped_daily(day)
    if not bars:
        logger.info("ingest_daily_bars: no results for %s (holiday, or POLYGON_API_KEY unset)", day)
        return

    objs = [
        DailyBar(
            ticker=bar["T"],
            date=today,
            open=bar["o"],
            high=bar["h"],
            low=bar["l"],
            close=bar["c"],
            volume=bar["v"],
        )
        for bar in bars
        if "T" in bar
    ]
    DailyBar.objects.bulk_create(
        objs,
        update_conflicts=True,
        unique_fields=["ticker", "date"],
        update_fields=["open", "high", "low", "close", "volume"],
    )
    logger.info("ingest_daily_bars: upserted %d bars for %s", len(objs), day)


@shared_task
def index_clip(clip_id):
    """Embed a ResearchClip's text and upsert it into Weaviate. Fired right
    after a clip is created (ResearchClipListView.post()). No-ops if
    OPENAI_API_KEY isn't set -- nothing downstream depends on this
    succeeding yet, since no search endpoint exists yet either."""
    clip = ResearchClip.objects.select_related("tracked_company__user", "tracked_company__company").get(id=clip_id)
    vector = embeddings.embed_text(clip.text)
    if vector is None:
        return
    vector_store.upsert_clip(clip, vector)


@shared_task
def sync_wiki_source(source_id):
    """On-demand sync for one WikiSource, fired right after a page is
    linked (WikiSourceListView.post or the link_wiki_page assistant tool)
    so the initial fetch+chunk+embed doesn't block the request/tool-call
    cycle -- mirrors index_clip's shape (fire-and-forget after creation)."""
    from .services import wiki_ingestion

    source = WikiSource.objects.select_related("tracked_company__user", "tracked_company__company").get(id=source_id)
    wiki_ingestion.sync_source(source)


@shared_task
def poll_wiki_sources():
    """Daily Celery Beat job (settings.CELERY_BEAT_SCHEDULE): check every
    linked WikiSource for content changes. Per-source try/except --
    unlike refresh_prices (one provider, cached-fallback assumption), this
    loop hits N independent Wikipedia pages, and one 404/renamed/deleted
    page must not abort the rest of the batch."""
    from .services import wiki_ingestion

    for source in WikiSource.objects.select_related("tracked_company__user", "tracked_company__company"):
        try:
            wiki_ingestion.sync_source(source)
        except Exception:
            logger.exception("poll_wiki_sources: failed for WikiSource %s (%s)", source.id, source.page_title)


def _scan_one_subreddit(watch: TrackedSubreddit) -> tuple[list[str], int]:
    """The actual fetch/extract/attribute/store body for one
    TrackedSubreddit -- factored out so the twice-daily Beat sweep
    (scan_tracked_subreddits) and the on-demand single-subreddit refresh
    (refresh_tracked_subreddit, below) share the exact same scan logic
    instead of two copies drifting apart. Returns (tickers_found,
    new_mentions_created) -- tickers_found is reddit_tickers.
    extract_tickers' full result (every ticker genuinely being discussed
    right now, whether or not this particular scan happened to create any
    NEW SubredditMention rows for it -- a ticker discussed in posts
    already seen on a prior scan is still "found" this run, just not
    "new"), new_mentions_created counts only get_or_create calls that
    actually inserted a row, for a refresh's "here's what's new" signal.
    Raises on a Steady/OpenAI failure -- unlike the Beat sweep, which
    swallows this per-subreddit (one bad subreddit must not abort the
    whole batch), a single on-demand refresh has exactly one subreddit to
    report back to, so the caller decides how to surface it (push
    subreddit.error) rather than this function deciding to swallow it.

    Stamps watch.last_scanned_at = now() FIRST, before the fetch even
    runs -- "last attempted," not "last successful" (see that field's own
    docstring): a Steady/OpenAI failure below still leaves an honest
    record that the scan loop reached this subreddit just now, rather
    than leaving it looking identical to Beat never having run at all."""
    watch.last_scanned_at = timezone.now()
    watch.save(update_fields=["last_scanned_at"])

    posts = steady.get_reddit_posts(watch.name, sort_type="hot")
    if not posts:
        return [], 0
    tickers = reddit_tickers.extract_tickers(posts)
    if not tickers:
        return [], 0
    mentions = reddit_tickers.attribute_mentions(posts, tickers)
    new_mentions = 0
    for ticker, matched_posts in mentions.items():
        for post in matched_posts:
            post_id = post.get("id")
            if not post_id:
                continue
            _, created = SubredditMention.objects.get_or_create(
                user=watch.user, subreddit=watch.name, ticker=ticker, post_id=post_id,
                defaults={
                    "title": post.get("title") or "",
                    "permalink": post.get("permalink") or "",
                    "excerpt": (post.get("selftext") or "")[:1000],
                    "score": post.get("score"),
                    "num_comments": post.get("num_comments"),
                },
            )
            if created:
                new_mentions += 1
    return tickers, new_mentions


@shared_task
def scan_tracked_subreddits():
    """Twice-daily Celery Beat job (settings.CELERY_BEAT_SCHEDULE): runs
    _scan_one_subreddit for every TrackedSubreddit across every user.
    get_or_create on the model's unique constraint means re-scanning a
    post already seen in an earlier run is a no-op, not a duplicate --
    "how many times was ticker X mentioned" stays a Count query over
    these rows, not a counter that could double-count across scans.

    Per-subreddit try/except -- the poll_wiki_sources shape above, not
    refresh_prices's (no per-item isolation there, for a different,
    documented reason) -- one subreddit being unreachable, empty, or
    hitting a Steady/OpenAI error must not abort the rest of the
    batch."""
    for watch in TrackedSubreddit.objects.select_related("user"):
        try:
            _scan_one_subreddit(watch)
        except Exception:
            logger.exception("scan_tracked_subreddits: failed for r/%s (user %s)", watch.name, watch.user_id)


@shared_task
def refresh_tracked_subreddit(subreddit_id):
    """POST /api/companies/subreddits/{id}/refresh/ (SubredditRefreshView)
    enqueues this and returns 202 immediately -- an on-demand,
    single-subreddit version of scan_tracked_subreddits' twice-daily
    sweep, for "I don't want to wait up to 12 hours for the next Beat
    tick" (e.g. right after adding a new watch, or checking whether the
    scan pipeline is even working). Result arrives over the user's
    WebSocket group (subreddit.progress / subreddit.complete /
    subreddit.error, see docs/websockets.md), same shape as
    screen_ticker's screen.* events above -- not notify(), same
    fire-and-forget reasoning: a missed push just means the frontend
    re-fetches GET .../subreddit-mentions/ on its own.

    Skips quietly (no push -- nothing to report to, and SubredditRefreshView
    already 404s on a bad id at request time) if the row was deleted
    between the request and this task actually running."""
    try:
        watch = TrackedSubreddit.objects.select_related("user").get(id=subreddit_id)
    except TrackedSubreddit.DoesNotExist:
        logger.info("refresh_tracked_subreddit: skipping %s, row no longer exists", subreddit_id)
        return

    send_to_user(watch.user_id, "subreddit.progress", subreddit_id=watch.id, name=watch.name, status="running")
    try:
        tickers, new_mentions = _scan_one_subreddit(watch)
    except Exception as exc:
        logger.exception("refresh_tracked_subreddit: failed for r/%s (user %s)", watch.name, watch.user_id)
        send_to_user(
            watch.user_id, "subreddit.error", subreddit_id=watch.id, name=watch.name, message=str(exc),
        )
        return
    send_to_user(
        watch.user_id, "subreddit.complete", subreddit_id=watch.id, name=watch.name,
        tickers=tickers, new_mentions=new_mentions,
    )


@shared_task
def scan_reddit_for_tracked_tickers():
    """Daily Celery Beat job: for every ticker anyone tracks in their
    screener (TrackedCompany), pull global Reddit search results for it
    and feed them into the existing ResearchClip -> Weaviate pipeline
    (source_type=SOURCE_TYPE_REDDIT) -- the same "paste-in deep-dive
    material" flow ResearchClipListView.post() already uses, reusing
    index_clip.delay() unchanged rather than building parallel indexing
    infrastructure. Capped to the first 10 (relevance-sorted) results
    per ticker per run -- confirmed live that relevance sort front-loads
    the real signal, so this
    avoids flooding a popular ticker's clip list with 25 low-relevance
    results every single day.

    Global search, not restricted to any user's TrackedSubreddit list --
    deliberate: "what does Reddit broadly say about a stock I'm
    tracking" is a different signal than "what's trending in the
    specific subreddits I watch" (that's scan_tracked_subreddits' job),
    and get_reddit_search's subreddit filter is confirmed broken
    server-side anyway.

    Dedup via get_or_create on (tracked_company, source=permalink) --
    deliberately application-level only, no new DB UniqueConstraint on
    ResearchClip, since `source` is blank for plenty of other clip types
    (manually pasted material) and a DB constraint on that pair would
    reject those. Per-tracked-company try/except, same poll_wiki_sources-
    style isolation as scan_tracked_subreddits above."""
    for tracked in TrackedCompany.objects.select_related("user", "company"):
        try:
            posts = steady.get_reddit_search(tracked.company.ticker)
            if not posts:
                continue
            for post in posts[:10]:
                permalink = post.get("permalink") or ""
                if not permalink:
                    continue
                clip, created = ResearchClip.objects.get_or_create(
                    tracked_company=tracked, source=permalink,
                    defaults={
                        "category": ResearchClip.CATEGORY_CONTEXT,
                        "source_type": ResearchClip.SOURCE_TYPE_REDDIT,
                        "label": post.get("title") or "Reddit post",
                        "text": post.get("selftext") or post.get("title") or "",
                    },
                )
                if created:
                    index_clip.delay(clip.id)
        except Exception:
            logger.exception(
                "scan_reddit_for_tracked_tickers: failed for %s (tracked_company %s)",
                tracked.company.ticker, tracked.id,
            )
