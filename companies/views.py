from datetime import timedelta

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError
from django.db.models import Count
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Company, Journal, ResearchClip, SubredditMention, TrackedCompany, TrackedSubreddit, WikiSource
from .services import embeddings, fmp, pricing, vector_store, wiki_ingestion, wikipedia_client
from .services.presentation import build_report, build_summary
from .services.sec_edgar import resolve_company
from .tasks import index_clip, refresh_tracked_subreddit, screen_ticker, sync_wiki_source


def _row(tracked: TrackedCompany) -> dict:
    return {
        **build_summary(tracked.company),
        "status": tracked.status,
        "origin": tracked.origin,
        "added_at": tracked.added_at,
    }


class CompanyListView(APIView):
    """GET the user's dashboard: every company they track (system defaults,
    auto-provisioned into their own row on first load here, plus anything
    they've added themselves), each with an independently-owned status.
    POST adds a ticker to their list without triggering a screen -- that's
    a separate action (POST .../screen/), matching "Add" and "Run screen"
    being distinct buttons in the UI."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        _provision_system_defaults(request.user)
        tracked = (
            TrackedCompany.objects.filter(user=request.user)
            .select_related("company")
            .order_by("company__ticker")
        )
        return Response([_row(t) for t in tracked])

    def post(self, request):
        ticker = request.data.get("ticker")
        if not ticker:
            raise ValidationError({"ticker": ["This field is required."]})

        try:
            company = resolve_company(ticker)
        except ValueError as exc:
            raise ValidationError(str(exc))

        tracked, _ = TrackedCompany.objects.get_or_create(
            user=request.user, company=company, defaults={"origin": TrackedCompany.ORIGIN_USER}
        )
        return Response(_row(tracked), status=status.HTTP_201_CREATED)


def _provision_system_defaults(user) -> None:
    existing = set(
        TrackedCompany.objects.filter(user=user, company__is_system_default=True).values_list("company_id", flat=True)
    )
    missing = Company.objects.filter(is_system_default=True).exclude(id__in=existing)
    TrackedCompany.objects.bulk_create(
        [TrackedCompany(user=user, company=c, origin=TrackedCompany.ORIGIN_SYSTEM) for c in missing]
    )


def _get_tracked_or_404(user, ticker: str) -> TrackedCompany:
    try:
        return TrackedCompany.objects.select_related("company").get(user=user, company__ticker=ticker.upper())
    except TrackedCompany.DoesNotExist:
        raise NotFound(f"You aren't tracking {ticker.upper()} -- POST to /api/companies/ to add it first.")


class TrackedCompanyStatusView(APIView):
    """PATCH a user's own status (pending/shortlisted/killed) for a ticker
    they're tracking. 404 if they aren't tracking it -- add it first."""

    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, ticker):
        new_status = request.data.get("status")
        valid_statuses = [choice[0] for choice in TrackedCompany.STATUS_CHOICES]
        if new_status not in valid_statuses:
            raise ValidationError({"status": [f"Must be one of {valid_statuses}."]})

        tracked = _get_tracked_or_404(request.user, ticker)
        tracked.status = new_status
        tracked.save(update_fields=["status"])
        return Response(_row(tracked))


class ScreenView(APIView):
    """POST to fetch fresh EDGAR/FMP data for a ticker (creating the Company
    on first sight). Validates the ticker synchronously (so a bad ticker
    400s immediately) then hands the actual fetching off to a Celery task --
    the result arrives over the user's WebSocket group (`screen.progress` /
    `screen.complete` / `screen.error`, see docs/websockets.md) instead of
    the HTTP response, so the frontend isn't blocked on SEC's throttling."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, ticker):
        max_age_days = float(request.data.get("max_age_days", 1.0))
        try:
            resolve_company(ticker)
        except ValueError as exc:
            raise ValidationError(str(exc))
        screen_ticker.delay(str(request.user.id), ticker.upper(), max_age_days)
        return Response({"status": "queued", "ticker": ticker.upper()}, status=status.HTTP_202_ACCEPTED)


class CompanyDetailView(APIView):
    """GET the latest cached report for a ticker without triggering a fetch.
    404 if it's never been screened."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, ticker):
        try:
            company = Company.objects.get(ticker=ticker.upper())
        except Company.DoesNotExist:
            raise NotFound(
                f"{ticker.upper()} hasn't been screened yet -- POST to /api/companies/{ticker.upper()}/screen/ first."
            )
        return Response(build_report(company))


class PriceHistoryView(APIView):
    """GET the full daily EOD price series for a ticker, for charting.
    Fetch-or-cache-through FMP like every other data source in this app --
    no separate "run" step like /screen/, since this is just data a chart
    asks for, not a deliberate multi-provider action. Repeat loads on the
    same day are served from the cache, so no extra FMP calls."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, ticker):
        try:
            company = Company.objects.get(ticker=ticker.upper())
        except Company.DoesNotExist:
            raise NotFound(
                f"{ticker.upper()} hasn't been screened yet -- POST to /api/companies/{ticker.upper()}/screen/ first."
            )
        history = fmp.get_price_history(company)
        return Response({"ticker": company.ticker, "history": history})


class QuoteView(APIView):
    """GET a live quote for any ticker -- no tracking/screening required,
    unlike CompanyDetailView's cached FMP price. Meant for a trade ticket to
    show "here's the current price" before the user commits, so the frontend
    polls this every few seconds while the ticket is open rather than this
    pushing over a websocket (a pre-trade price glance doesn't need
    tick-by-tick streaming, and Steady's own chain data only refreshes
    ~every 10 minutes server-side anyway -- see companies/services/tradier.py)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, ticker):
        asset_class = request.query_params.get("asset_class", "STOCKS")
        try:
            quote = pricing.get_quote(ticker.upper(), asset_class=asset_class)
        except ValueError as exc:
            raise ValidationError(str(exc))
        if quote is None:
            raise NotFound("No live pricing provider is configured on the backend.")
        return Response(quote)


class OptionsExpirationsView(APIView):
    """GET just the expiration dates for a ticker's options chain -- meant
    to populate a date picker up front, before the frontend commits to
    fetching a full chain via OptionsChainView. Cheap on Tradier (a
    dedicated endpoint); on SteadyAPI it costs the same as a full chain
    fetch since Steady has no lighter endpoint -- see
    pricing.get_expirations."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, ticker):
        expirations = pricing.get_expirations(ticker.upper())
        if expirations is None:
            raise NotFound("No live pricing provider is configured on the backend.")
        return Response({"expirations": expirations})


class OptionsChainView(APIView):
    """GET the live options chain for a ticker -- same "no tracking/screening
    required" contract as QuoteView, for pricing an option before a trade.
    `?expiration=YYYY-MM-DD` picks one expiration; omitted, the provider
    picks its own default (nearest expiration for Steady, Tradier resolved
    to the soonest date not already in the past). `?strikes=N` trims calls
    and puts down to the N contracts on each side closest to the money
    (same "closest by |moneyness_pct|" ranking the AI agent tools use,
    see pricing.nearest_the_money) instead of returning the full chain --
    a full SPY chain is 1000+ contracts, most of it useless for a trade
    ticket's "pick a nearby strike" table. Omit `strikes` to get the
    unfiltered chain (the original contract, kept for any caller that
    wants everything)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, ticker):
        expiration = request.query_params.get("expiration")
        chain = pricing.get_options_chain(ticker.upper(), expiration=expiration)
        if chain is None:
            raise NotFound("No live pricing provider is configured on the backend.")

        strikes_param = request.query_params.get("strikes")
        if strikes_param is not None:
            try:
                strikes = int(strikes_param)
            except ValueError:
                raise ValidationError({"strikes": ["Must be an integer."]})
            if strikes <= 0:
                raise ValidationError({"strikes": ["Must be a positive integer."]})
            chain = {
                "expirations": chain["expirations"],
                "calls": pricing.nearest_the_money(chain["calls"], strikes),
                "puts": pricing.nearest_the_money(chain["puts"], strikes),
            }
        return Response(chain)


def _clip_row(clip: ResearchClip) -> dict:
    return {
        "id": clip.id,
        "category": clip.category,
        "source_type": clip.source_type,
        "label": clip.label,
        "source": clip.source,
        "text": clip.text,
        "created_at": clip.created_at,
    }


class ResearchClipListView(APIView):
    """Mode 3 (Deep dive) paste-in: GET every clip a user has saved against
    a company they track, POST a new one. Deliberately no auto-fetch here --
    the point of a clip is that it's something a screen can't fetch (a
    filing excerpt, a wiki article, a news story, an operational KPI); this
    is the paste-in surface for it. 404 if the user isn't tracking the
    ticker -- same as the status endpoint, since deep-diving something not
    on your dashboard doesn't happen in the real workflow."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, ticker):
        tracked = _get_tracked_or_404(request.user, ticker)
        clips = tracked.clips.all()
        return Response([_clip_row(c) for c in clips])

    def post(self, request, ticker):
        tracked = _get_tracked_or_404(request.user, ticker)

        label = request.data.get("label")
        text = request.data.get("text")
        if not label:
            raise ValidationError({"label": ["This field is required."]})
        if not text:
            raise ValidationError({"text": ["This field is required."]})

        category = request.data.get("category", ResearchClip.CATEGORY_CONTEXT)
        valid_categories = [choice[0] for choice in ResearchClip.CATEGORY_CHOICES]
        if category not in valid_categories:
            raise ValidationError({"category": [f"Must be one of {valid_categories}."]})

        source_type = request.data.get("source_type", ResearchClip.SOURCE_TYPE_OTHER)
        valid_source_types = [choice[0] for choice in ResearchClip.SOURCE_TYPE_CHOICES]
        if source_type not in valid_source_types:
            raise ValidationError({"source_type": [f"Must be one of {valid_source_types}."]})

        clip = ResearchClip.objects.create(
            tracked_company=tracked,
            category=category,
            source_type=source_type,
            label=label,
            source=request.data.get("source", ""),
            text=text,
        )
        index_clip.delay(clip.id)
        return Response(_clip_row(clip), status=status.HTTP_201_CREATED)


class ResearchClipDetailView(APIView):
    """DELETE a single clip. 404 if the user isn't tracking the ticker, or
    the clip doesn't belong to them."""

    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, ticker, clip_id):
        tracked = _get_tracked_or_404(request.user, ticker)
        try:
            clip = tracked.clips.get(id=clip_id)
        except ResearchClip.DoesNotExist:
            raise NotFound(f"No clip {clip_id} for {ticker.upper()}.")
        clip.delete()
        # Synchronous, not a Celery task -- one cheap call, and we want the
        # Weaviate side gone immediately rather than racing a background
        # worker (a deleted clip briefly still turning up in search).
        vector_store.delete_clip(clip_id)
        return Response(status=status.HTTP_204_NO_CONTENT)


def _wiki_source_row(source: WikiSource) -> dict:
    return {
        "id": source.id,
        "page_title": source.page_title,
        "label": source.label,
        "last_checked_at": source.last_checked_at,
        "last_changed_at": source.last_changed_at,
        "created_at": source.created_at,
    }


class WikiSourceListView(APIView):
    """GET every Wikipedia page linked to a tracked company; POST links a
    new one. `page_title` accepts either a plain title or a full
    en.wikipedia.org URL (the expected frontend input -- a user pastes a
    link, not a title; see wikipedia_client.resolve_title_input for the
    parsing rules and why other-language Wikipedia URLs are rejected
    rather than silently mishandled). POST resolves+validates synchronously
    against Wikipedia (so a bad URL/title/disambiguation page 400s
    immediately) then queues the actual fetch+chunk+embed via
    sync_wiki_source.delay -- an HTTP request cycle shouldn't block on a
    full-article ingest. get_or_create on (tracked_company, canonical_title)
    so re-linking an already-linked page (even under a different raw input
    that resolves to the same canonical title) returns the existing row
    rather than 500ing on the UniqueConstraint."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, ticker):
        tracked = _get_tracked_or_404(request.user, ticker)
        return Response([_wiki_source_row(s) for s in tracked.wiki_sources.all()])

    def post(self, request, ticker):
        tracked = _get_tracked_or_404(request.user, ticker)
        raw_title = request.data.get("page_title")
        if not raw_title:
            raise ValidationError({"page_title": ["This field is required."]})

        try:
            title = wikipedia_client.resolve_title_input(raw_title)
            page = wikipedia_client.fetch_full_page(title)
        except ValueError as exc:
            raise ValidationError({"page_title": [str(exc)]})
        if page["is_disambiguation"]:
            raise ValidationError({"page_title": [f'"{title}" is a disambiguation page -- link the specific article.']})

        source, created = WikiSource.objects.get_or_create(
            tracked_company=tracked, page_title=page["canonical_title"],
            defaults={"label": request.data.get("label", "")},
        )
        if created:
            sync_wiki_source.delay(source.id)
        return Response(_wiki_source_row(source), status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class WikiSourceDetailView(APIView):
    """DELETE unlinks a page -- removes the WikiSource (cascades snapshots)
    and its ResearchClips (Postgres + Weaviate, per-clip, same pattern as
    ResearchClipDetailView / filing_ingestion's force path)."""

    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, ticker, source_id):
        tracked = _get_tracked_or_404(request.user, ticker)
        try:
            source = tracked.wiki_sources.get(id=source_id)
        except WikiSource.DoesNotExist:
            raise NotFound(f"No linked Wikipedia page {source_id} for {ticker.upper()}.")

        clips = tracked.clips.filter(source=wiki_ingestion.clip_source_key(source))
        for clip in clips:
            vector_store.delete_clip(clip.id)
        clips.delete()
        source.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ClipSearchView(APIView):
    """Semantic search over ResearchClip text -- Phase 6 built the
    embeddings + Weaviate ingestion pipeline but deliberately left this
    endpoint unbuilt (scope, per-ticker vs. global, was an open decision).
    Both won: this view serves both routes. Under `/clips/search/` (no
    ticker in the URL) it's global -- every clip the user has across their
    whole tracked portfolio, optionally narrowed with `?ticker=`. Under
    `/{ticker}/clips/search/` it's pinned to that one company, matching the
    nested clips/journal convention (and 404s the same way if the user
    isn't tracking it). Always scoped to the requesting user regardless of
    route -- the Weaviate object carries user_id precisely so search can't
    surface someone else's clips.

    Weaviate only carries clip_id + distance, not the clip text, so results
    are joined back to Postgres for the full row; `distance` is cosine
    distance (lower = closer) since that's this collection's default."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, ticker=None):
        query = request.query_params.get("q")
        if not query:
            raise ValidationError({"q": ["This field is required."]})

        if ticker is not None:
            _get_tracked_or_404(request.user, ticker)
            ticker = ticker.upper()
        else:
            ticker = request.query_params.get("ticker")
            if ticker:
                ticker = ticker.upper()

        limit = int(request.query_params.get("limit", 10))

        vector = embeddings.embed_text(query)
        if vector is None:
            return Response(
                {"detail": "Search isn't available -- OPENAI_API_KEY isn't configured."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        hits = vector_store.search_clips(vector, user_id=str(request.user.id), ticker=ticker, limit=limit)
        clips_by_id = {
            clip.id: clip
            for clip in ResearchClip.objects.filter(
                id__in=[hit["clip_id"] for hit in hits]
            ).select_related("tracked_company__company")
        }
        results = []
        for hit in hits:
            clip = clips_by_id.get(hit["clip_id"])
            if clip is None:
                continue  # stale Weaviate object outliving its Postgres row
            results.append(
                {
                    **_clip_row(clip),
                    "ticker": clip.tracked_company.company.ticker,
                    "distance": hit["distance"],
                }
            )
        return Response(results)


def _journal_row(journal: Journal) -> dict:
    return {
        "sells": journal.sells,
        "wins": journal.wins,
        "kills": journal.kills,
        "checkpoint": journal.checkpoint,
        "checkpoint_date": journal.checkpoint_date,
        "updated_at": journal.updated_at,
    }


class JournalView(APIView):
    """The Mode 3 deliverable (docs/overview.md): what they sell / why they
    win / what kills them, plus one falsifiable checkpoint with a date.
    GET fetches-or-creates the (initially blank) row for a tracked company;
    PATCH updates whichever fields are present in the body. 404 if the user
    isn't tracking the ticker."""

    permission_classes = [permissions.IsAuthenticated]

    FIELDS = ["sells", "wins", "kills", "checkpoint", "checkpoint_date"]

    def get(self, request, ticker):
        tracked = _get_tracked_or_404(request.user, ticker)
        journal, _ = Journal.objects.get_or_create(tracked_company=tracked)
        return Response(_journal_row(journal))

    def patch(self, request, ticker):
        tracked = _get_tracked_or_404(request.user, ticker)
        journal, _ = Journal.objects.get_or_create(tracked_company=tracked)

        update_fields = []
        for field in self.FIELDS:
            if field in request.data:
                setattr(journal, field, request.data[field])
                update_fields.append(field)

        if update_fields:
            try:
                journal.full_clean(validate_unique=False)
            except DjangoValidationError as exc:
                raise ValidationError(exc.message_dict)
            journal.save()
        return Response(_journal_row(journal))


def _subreddit_row(watch: TrackedSubreddit) -> dict:
    return {
        "id": watch.id, "name": watch.name, "added_at": watch.added_at,
        "last_scanned_at": watch.last_scanned_at,
    }


class SubredditListView(APIView):
    """GET every subreddit a user has asked to have scanned; POST adds
    one. Independent of TrackedCompany -- this is the "Social" discovery
    watch list (companies.tasks.scan_tracked_subreddits, twice daily),
    not tied to any one ticker. No screener-style auto-provisioning of
    defaults here -- unlike Company's system-default tickers, there's no
    sensible universal default subreddit list to seed."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        watches = TrackedSubreddit.objects.filter(user=request.user)
        return Response([_subreddit_row(w) for w in watches])

    def post(self, request):
        # Strip a literal "r/" prefix if present -- NOT .lstrip("r/"),
        # which strips leading 'r'/'/' CHARACTERS individually and would
        # mangle a subreddit that genuinely starts with 'r' (e.g.
        # "robinhood" -> "obinhood").
        name = (request.data.get("name") or "").strip()
        if name.lower().startswith("r/"):
            name = name[2:]
        name = name.strip("/").strip()
        if not name:
            raise ValidationError({"name": ["This field is required."]})
        try:
            watch, _ = TrackedSubreddit.objects.get_or_create(user=request.user, name=name)
        except IntegrityError as exc:
            raise ValidationError(str(exc))
        return Response(_subreddit_row(watch), status=status.HTTP_201_CREATED)


class SubredditDetailView(APIView):
    """DELETE removes a watched subreddit. Unlike TrackedCompany (never
    deleted, only status-flipped), a subreddit watch is genuinely
    removable -- nothing else references a TrackedSubreddit row by FK
    the way ResearchClip/Journal reference TrackedCompany, so there's no
    "orphaned children" concern a status flip would otherwise avoid."""

    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, subreddit_id):
        try:
            watch = TrackedSubreddit.objects.get(id=subreddit_id, user=request.user)
        except TrackedSubreddit.DoesNotExist:
            raise NotFound(f"No tracked subreddit {subreddit_id}.")
        watch.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class SubredditRefreshView(APIView):
    """POST triggers an immediate, single-subreddit re-scan instead of
    waiting for the next twice-daily Beat tick (companies.tasks.
    scan_tracked_subreddits) -- same 202-then-WebSocket shape as
    POST .../screen/ (ScreenView above): dispatched asynchronously, the
    actual result (tickers found, how many were genuinely new) arrives as
    subreddit.progress / subreddit.complete / subreddit.error
    (docs/websockets.md), not in this response."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, subreddit_id):
        try:
            watch = TrackedSubreddit.objects.get(id=subreddit_id, user=request.user)
        except TrackedSubreddit.DoesNotExist:
            raise NotFound(f"No tracked subreddit {subreddit_id}.")
        refresh_tracked_subreddit.delay(watch.id)
        return Response({"status": "queued", "id": watch.id, "name": watch.name}, status=status.HTTP_202_ACCEPTED)


class SubredditRefreshAllView(APIView):
    """POST triggers an immediate re-scan of EVERY subreddit this user
    watches -- the "refresh now" button for the whole list rather than
    one row at a time. Queues one refresh_tracked_subreddit task per
    TrackedSubreddit (same task SubredditRefreshView uses for a single
    row), so the frontend gets one independent subreddit.progress/
    complete/error triple per subreddit rather than a single bulk event
    -- a slow or failing subreddit doesn't block the others from
    reporting back, same per-item isolation reasoning as the Beat sweep's
    own per-subreddit try/except."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        watches = list(TrackedSubreddit.objects.filter(user=request.user))
        for watch in watches:
            refresh_tracked_subreddit.delay(watch.id)
        return Response({"status": "queued", "count": len(watches)}, status=status.HTTP_202_ACCEPTED)


class SubredditMentionListView(APIView):
    """GET ticker mentions found by companies.tasks.scan_tracked_subreddits.
    Default: aggregated {subreddit, ticker, count} rows -- a Count query
    over the raw SubredditMention rows at read time, not a stored
    aggregate (matches run_screen's "derived values computed at read
    time, not duplicated" philosophy, screening.py's module docstring).
    `?ticker=` switches to drill-down mode: every individual mention
    (post title/excerpt/permalink/score) for that one ticker, for the
    "click a ticker to see context" UI need. `?subreddit=` narrows either
    mode to one subreddit -- the aggregated counts to "tickers trending
    in just r/X," or combined with `?ticker=` to "posts mentioning this
    ticker in just r/X" (useful once a ticker shows up across more than
    one watched subreddit and the drill-down would otherwise mix them).
    `?days=` narrows either mode to mentions first scanned in the last N
    days -- without it, counts are all-time, which starts padding out
    with old/dead threads the longer this feature runs; combinable with
    both of the above."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        ticker = request.query_params.get("ticker")
        subreddit = request.query_params.get("subreddit")
        days_param = request.query_params.get("days")
        since = None
        if days_param is not None:
            try:
                days = int(days_param)
                if days <= 0:
                    raise ValueError
            except ValueError:
                raise ValidationError({"days": ["Must be a positive integer."]})
            since = timezone.now() - timedelta(days=days)

        if ticker:
            mentions = SubredditMention.objects.filter(user=request.user, ticker=ticker.upper())
            if subreddit:
                mentions = mentions.filter(subreddit=subreddit)
            if since:
                mentions = mentions.filter(scanned_at__gte=since)
            return Response([
                {
                    "id": m.id, "subreddit": m.subreddit, "ticker": m.ticker, "title": m.title,
                    "permalink": m.permalink, "excerpt": m.excerpt, "score": m.score,
                    "num_comments": m.num_comments, "scanned_at": m.scanned_at,
                }
                for m in mentions
            ])

        rows = SubredditMention.objects.filter(user=request.user)
        if subreddit:
            rows = rows.filter(subreddit=subreddit)
        if since:
            rows = rows.filter(scanned_at__gte=since)
        rows = rows.values("subreddit", "ticker").annotate(count=Count("id")).order_by("-count")
        return Response(list(rows))
