from django.conf import settings
from django.db import models


class TickerDirectory(models.Model):
    """SEC's full ticker -> CIK directory (www.sec.gov/files/company_tickers.json),
    ~10k rows, refreshed periodically. Not per-user data and not really "the
    app's" data either, but it lives in the same DB rather than a second
    SQLite file or an external cache -- it's small, and a lookup table like
    this is exactly what a database is for."""

    ticker = models.CharField(max_length=16, unique=True)
    cik = models.PositiveIntegerField()
    title = models.CharField(max_length=255, blank=True)
    refreshed_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.ticker


class Company(models.Model):
    """A ticker/CIK we've screened at least once. Shared across all users --
    the financials are the same fact regardless of who's looking, so this is
    not per-account data."""

    ticker = models.CharField(max_length=16, unique=True)
    cik = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=255, blank=True)
    # Suggested to every user's dashboard, auto-provisioned into their own
    # TrackedCompany row on first load (see companies/views.py) rather than
    # shown as a shared/read-only row -- each user still gets an independent
    # status on it, same as anything they add themselves.
    is_system_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["ticker"]

    def __str__(self):
        return self.ticker


class CompanySnapshot(models.Model):
    """Latest cached response for one (company, data_type) pair -- an EDGAR
    concept tag (e.g. "concept:us-gaap:Revenues"), an EDGAR bulk fetch
    ("submissions", "facts"), or an FMP endpoint ("profile:fmp",
    "shares_float:fmp"). One row per pair; a re-fetch overwrites in place --
    history isn't kept, only the latest value and when it was fetched.
    Replaces the old scripts/db.py company_data table."""

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="snapshots")
    data_type = models.CharField(max_length=128)
    raw_json = models.JSONField()
    fetched_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["company", "data_type"], name="unique_company_data_type"),
        ]

    def __str__(self):
        return f"{self.company.ticker}:{self.data_type}"


class TickerSnapshot(models.Model):
    """Latest cached SteadyAPI response for one (ticker, data_type) pair --
    "quote" (last sale/bid/ask) or "chain:<expiration|nearest>" (an options
    chain). Same shape/overwrite-in-place semantics as CompanySnapshot, but
    keyed by a bare ticker string instead of a Company FK -- SteadyAPI
    serves tickers with no SEC-derived Company row (index symbols like
    $SPX, ETFs), same reasoning as companies/services/steady.py's own
    module docstring for why it doesn't use CompanySnapshot directly.

    Written by league/services/valuation.py's display/mark-to-market path
    (a live fill in execution.py, the market-hours Beat task, or a manual
    refresh) -- never by steady.py's own trade-fill callers, which read
    live and skip this table entirely."""

    ticker = models.CharField(max_length=16)
    data_type = models.CharField(max_length=32)
    raw_json = models.JSONField()
    fetched_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["ticker", "data_type"], name="unique_ticker_data_type"),
        ]

    def __str__(self):
        return f"{self.ticker}:{self.data_type}"


class DailyBar(models.Model):
    """One ticker's split/dividend-adjusted daily OHLCV for one session --
    the persisted history CompanySnapshot/TickerSnapshot don't provide
    (both are "latest value only" caches, not a time series). Backed by
    companies/services/polygon.py's grouped-daily endpoint: one full-market
    API call per session populates every ticker's row for that day in one
    bulk upsert, rather than one row write per per-ticker API call.

    Exists for signal logic that needs several sessions of history at once
    (an N-consecutive-down-closes streak, a rolling SMA) -- computed from
    these stored rows, not from a live per-call fetch like
    technicals.py's Tradier-backed SMA fields."""

    ticker = models.CharField(max_length=16)
    date = models.DateField()
    open = models.FloatField()
    high = models.FloatField()
    low = models.FloatField()
    close = models.FloatField()
    volume = models.FloatField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["ticker", "date"], name="unique_ticker_date_bar"),
        ]
        indexes = [
            models.Index(fields=["ticker", "-date"]),
        ]
        ordering = ["ticker", "date"]

    def __str__(self):
        return f"{self.ticker}:{self.date}"


class ApiCallLog(models.Model):
    """Append-only audit log of every network call made to SEC EDGAR or FMP.
    Replaces the old scripts/db.py api_calls table -- same purpose, same
    shape."""

    ts = models.DateTimeField(auto_now_add=True)
    url = models.TextField()
    endpoint_type = models.CharField(max_length=64)
    provider = models.CharField(max_length=32, default="sec")
    ticker = models.CharField(max_length=16, null=True, blank=True)
    company = models.ForeignKey(Company, on_delete=models.SET_NULL, null=True, blank=True, related_name="api_calls")
    tag = models.CharField(max_length=128, null=True, blank=True)
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    from_cache = models.BooleanField(default=False)

    class Meta:
        ordering = ["-ts"]

    def __str__(self):
        return f"{self.ts} {self.provider} {self.endpoint_type} {self.ticker or ''}"


class TrackedCompany(models.Model):
    """One user's row for one company on their dashboard -- the mockup's
    table. Always user-scoped, even for system defaults: origin just records
    where the suggestion came from for display ("SYS" vs "MINE" tag);
    status is independently owned by each user from the moment their row is
    created."""

    STATUS_PENDING = "pending"
    STATUS_SHORTLISTED = "shortlisted"
    STATUS_KILLED = "killed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_SHORTLISTED, "Shortlisted"),
        (STATUS_KILLED, "Killed"),
    ]

    ORIGIN_SYSTEM = "system"
    ORIGIN_USER = "user"
    ORIGIN_CHOICES = [
        (ORIGIN_SYSTEM, "System"),
        (ORIGIN_USER, "User"),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="tracked_companies")
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="tracked_by")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING)
    origin = models.CharField(max_length=8, choices=ORIGIN_CHOICES, default=ORIGIN_USER)
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "company"], name="unique_user_company_tracking"),
        ]
        ordering = ["company__ticker"]

    def __str__(self):
        return f"{self.user_id}:{self.company.ticker}"


class ResearchClip(models.Model):
    """One pasted piece of deep-dive material (Mode 3) for a company on a
    user's dashboard -- a 10-K/10-Q excerpt, a wiki article, a news story,
    an operational KPI, whatever moved the verdict. Scoped to
    `TrackedCompany` (not a fresh user+company pair) since that's already
    the per-user join row, and deep-diving something you aren't tracking
    doesn't happen in the real workflow.

    `category` is the six deep-dive questions from docs/overview.md -- the
    things a screen can't fetch and that actually move a verdict -- plus
    "context" for background material that doesn't map to one of the six
    (wiki articles, launch cadence, general KPIs)."""

    CATEGORY_REVENUE_QUALITY = "revenue_quality"
    CATEGORY_PROFITABILITY_PATH = "profitability_path"
    CATEGORY_CATALYST = "catalyst"
    CATEGORY_CONCENTRATION = "concentration"
    CATEGORY_DILUTION = "dilution"
    CATEGORY_INSIDER_ACTIVITY = "insider_activity"
    CATEGORY_CONTEXT = "context"
    CATEGORY_CHOICES = [
        (CATEGORY_REVENUE_QUALITY, "Revenue quality/durability (backlog, book-to-bill)"),
        (CATEGORY_PROFITABILITY_PATH, "Path to profitability"),
        (CATEGORY_CATALYST, "The one falsifiable catalyst"),
        (CATEGORY_CONCENTRATION, "Customer/revenue concentration"),
        (CATEGORY_DILUTION, "Dilution trajectory"),
        (CATEGORY_INSIDER_ACTIVITY, "Insider behavior"),
        (CATEGORY_CONTEXT, "Context (background, not one of the six)"),
    ]

    SOURCE_TYPE_FILING = "filing"
    SOURCE_TYPE_NEWS = "news"
    SOURCE_TYPE_WIKI = "wiki"
    SOURCE_TYPE_PRESS_RELEASE = "press_release"
    SOURCE_TYPE_REDDIT = "reddit"
    SOURCE_TYPE_OTHER = "other"
    SOURCE_TYPE_CHOICES = [
        (SOURCE_TYPE_FILING, "SEC filing (10-K/10-Q excerpt)"),
        (SOURCE_TYPE_NEWS, "News article"),
        (SOURCE_TYPE_WIKI, "Wikipedia/reference article"),
        (SOURCE_TYPE_PRESS_RELEASE, "Press release/IR"),
        (SOURCE_TYPE_REDDIT, "Reddit post"),
        (SOURCE_TYPE_OTHER, "Other"),
    ]

    tracked_company = models.ForeignKey(TrackedCompany, on_delete=models.CASCADE, related_name="clips")
    category = models.CharField(max_length=32, choices=CATEGORY_CHOICES, default=CATEGORY_CONTEXT)
    source_type = models.CharField(max_length=16, choices=SOURCE_TYPE_CHOICES, default=SOURCE_TYPE_OTHER)
    label = models.CharField(max_length=255)
    source = models.CharField(max_length=500, blank=True)
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.tracked_company}:{self.label}"


class Journal(models.Model):
    """The Mode 3 deliverable (docs/overview.md): three sentences -- what
    they sell, why they win, what kills them -- plus one falsifiable
    checkpoint with a date. One per `TrackedCompany`, matching the
    mockup's Journal panel exactly."""

    tracked_company = models.OneToOneField(TrackedCompany, on_delete=models.CASCADE, related_name="journal")
    sells = models.TextField(blank=True)
    wins = models.TextField(blank=True)
    kills = models.TextField(blank=True)
    checkpoint = models.TextField(blank=True)
    checkpoint_date = models.DateField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.tracked_company}:journal"


class WikiSource(models.Model):
    """One Wikipedia page linked to a TrackedCompany for ongoing monitoring
    (e.g. RKLB -> "Rocket Lab" and, separately, "Neutron (rocket)" -- two
    rows). `page_title` is always the resolved CANONICAL title (MediaWiki's
    redirects=1) -- never the user's raw input -- since every poll re-fetches
    by this stored value and it's also the exact string wiki_ingestion.py
    uses to build ResearchClip.source for dedup; a redirect resolving
    differently across runs would otherwise fork the clip set."""

    tracked_company = models.ForeignKey(TrackedCompany, on_delete=models.CASCADE, related_name="wiki_sources")
    page_title = models.CharField(max_length=300)
    label = models.CharField(max_length=255, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_changed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tracked_company", "page_title"], name="unique_tracked_company_wiki_page"),
        ]
        ordering = ["page_title"]

    def __str__(self):
        return f"{self.tracked_company}:{self.page_title}"


class WikiSnapshot(models.Model):
    """One full-text capture of a WikiSource at a point in time. Retention:
    only the 3 most recent per source are kept (wiki_ingestion.prune_snapshots)
    -- enough to diff "what changed" between the two latest without keeping
    unbounded history of an article that might churn indefinitely."""

    source = models.ForeignKey(WikiSource, on_delete=models.CASCADE, related_name="snapshots")
    content_hash = models.CharField(max_length=64)
    text = models.TextField()
    fetched_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-fetched_at", "-id"]

    def __str__(self):
        return f"{self.source_id}@{self.fetched_at}"


class TrackedSubreddit(models.Model):
    """One subreddit a user wants scanned for ticker mentions -- the
    "Social" discovery feature (league/tools.py's AI-only
    scan_subreddit_for_tickers tool covers the same ground for an AI
    competitor; this is the human-facing, scheduled equivalent).
    Deliberately independent of TrackedCompany: a subreddit watch isn't
    about any one ticker, it's "keep an eye on r/X and tell me what
    comes up" -- companies.tasks.scan_tracked_subreddits is what turns
    this into SubredditMention rows twice daily. Unlike TrackedCompany
    (never deleted, only status-flipped), a subreddit watch is genuinely
    removable -- there's no "killed" concept here, nothing else
    references a row by FK the way ResearchClip/Journal reference
    TrackedCompany."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="tracked_subreddits")
    name = models.CharField(max_length=100, help_text='Without "r/", e.g. "wallstreetbets".')
    added_at = models.DateTimeField(auto_now_add=True)
    # Set by companies.tasks._scan_one_subreddit at the START of every
    # attempt (Beat sweep or on-demand refresh alike), success or
    # failure -- deliberately "last attempted" not "last successful", so a
    # Steady/OpenAI outage still advances it (the scan loop DID reach this
    # row) rather than looking identical to Beat not running at all. NULL
    # means never scanned since being added. The one field this app answers
    # "is the scan job actually running" with, in the admin panel or
    # GET .../subreddits/ -- SubredditMention.scanned_at only exists for a
    # HIT, so a subreddit with zero mentions ever looked, before this field
    # existed, was indistinguishable from one that had never been checked.
    last_scanned_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "name"], name="unique_user_subreddit"),
        ]
        ordering = ["name"]

    def __str__(self):
        return f"{self.user_id}:r/{self.name}"


class SubredditMention(models.Model):
    """One (ticker, post) pair found while scanning a TrackedSubreddit --
    raw, per-post rows, not a stored aggregate. "How many times was
    ticker X mentioned in subreddit Y" is a Count query over these at
    read time (companies/views.py's SubredditMentionListView), matching
    this app's existing philosophy (screening.py's run_screen docstring:
    fetch and cache raw data, don't store/duplicate derived values that
    can go stale). Ticker identification is companies.services.
    reddit_tickers.extract_tickers (contextual, one LLM call for the
    whole batch of posts) + attribute_mentions (a second, purely local
    pass -- no extra LLM call -- that matches each already-validated
    ticker back to the specific post(s) that mention it, for the
    "click a ticker to see context" drill-down).

    Not indexed into Weaviate -- this is the lightweight discovery feed;
    permalink + excerpt stored directly are enough for the context
    drill-down without semantic search. Compare companies.tasks.
    scan_reddit_for_tracked_tickers, which DOES flow through ResearchClip
    -> Weaviate, for the separate "Reddit context on a ticker I'm
    already tracking in my screener" use case."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="subreddit_mentions")
    subreddit = models.CharField(max_length=100)
    ticker = models.CharField(max_length=16)
    post_id = models.CharField(max_length=32, help_text="Reddit's own post id -- dedup key across repeat scans.")
    title = models.CharField(max_length=500)
    permalink = models.CharField(max_length=500)
    excerpt = models.TextField(blank=True)
    score = models.IntegerField(null=True, blank=True)
    num_comments = models.IntegerField(null=True, blank=True)
    scanned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "subreddit", "ticker", "post_id"], name="unique_subreddit_mention"),
        ]
        ordering = ["-scanned_at"]

    def __str__(self):
        return f"{self.user_id}:r/{self.subreddit}:{self.ticker}:{self.post_id}"
