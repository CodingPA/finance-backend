from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator
from django.db import models


class Competition(models.Model):
    """One paper-trading league run (docs/paper-trading-league-rules.md) --
    a real model rather than a hardcoded singleton, since more competitions
    with different players/dates are expected later. Rules are data on this
    row (not code constants) so a future competition can tune them without
    a deploy -- `league.services.execution` reads these fields, never a
    hardcoded 0.60/0.30.

    `index_tickers` is specifically the list of broad-market index FUNDS
    the 30% share cap applies to (SPY/VOO/QQQ/FXAIX, etc.) -- an option on
    one of these tickers is a directional bet with theta working against
    it, not the passive parking the cap exists to discourage, so it is
    deliberately NOT counted against this cap (see execution.py)."""

    STATUS_UPCOMING = "upcoming"
    STATUS_ACTIVE = "active"
    STATUS_COMPLETED = "completed"
    STATUS_CHOICES = [
        (STATUS_UPCOMING, "Upcoming"),
        (STATUS_ACTIVE, "Active"),
        (STATUS_COMPLETED, "Completed"),
    ]

    name = models.CharField(max_length=255)
    starting_capital = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("5000"))
    max_position_pct = models.DecimalField(max_digits=5, decimal_places=4, default=Decimal("0.60"))
    max_index_pct = models.DecimalField(max_digits=5, decimal_places=4, default=Decimal("0.30"))
    cash_apy = models.DecimalField(max_digits=5, decimal_places=4, default=Decimal("0.033"))
    index_tickers = models.JSONField(default=list, blank=True)
    start_date = models.DateField()
    end_date = models.DateField()
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_UPCOMING)
    restrict_after_hours_trading = models.BooleanField(
        default=False,
        help_text=(
            "Simulate a broker that doesn't support extended-hours trading (e.g. Fidelity) "
            "instead of this app's default 'trades can be posted any time' (rules doc Rule 6). "
            "When set, a trade submitted while the market is closed (league.services."
            "market_hours.is_market_open) queues as Trade.STATUS_PENDING instead of filling "
            "immediately, and is resolved at a live quote the next time the market is open "
            "(league.tasks.process_pending_trades, ticks every 5 minutes). Off by default -- "
            "existing/other competitions are unaffected."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-start_date"]

    def __str__(self):
        return self.name


# NOT auto-seeded onto new competitions (unlike DEFAULT_FORCED_CHECKINS
# below) -- an operator loads this into a specific Competition's
# index_tickers via the "Load default index tickers" button on its admin
# change form (league/admin.py's CompetitionAdmin), then edits/trims
# before saving, since not every competition wants every one of these
# funds exempted from the 30% index cap. Broad-market/sector/bond/
# commodity index funds and ETFs, common brokerage money-market/index
# mutual funds -- deliberately NOT individual stocks or leveraged/inverse
# funds, which are directional bets the cap exists to catch, not passive
# parking.
DEFAULT_INDEX_TICKERS = [
    "SPY", "VOO", "IVV", "SPLG", "SPTM", "VTI", "ITOT", "SCHB", "VXF", "VB", "VBR", "VBK",
    "IJR", "IJH", "IJS", "IJT", "VO", "VOE", "VOT", "MDY", "SCHM", "SCHA", "VV", "VUG", "VTV",
    "IWB", "IWD", "IWF", "IWM", "IWN", "IWO", "IWP", "IWR", "IWS", "IWV", "RSP",
    "QQQ", "QQQM", "QQQE", "ONEQ", "DIA", "OEF", "SCHX", "SCHG", "SCHV", "SCHD", "VYM", "VIG",
    "DVY", "SDY", "NOBL", "HDV", "DGRO", "SPYD", "SPYG", "SPYV", "SPHQ", "USMV", "SPLV",
    "MTUM", "QUAL", "VLUE", "SIZE",
    "EFA", "IEFA", "VEA", "SCHF", "SPDW", "IDEV", "EEM", "IEMG", "VWO", "SCHE", "SPEM",
    "VXUS", "IXUS", "VEU", "ACWI", "ACWX", "VT", "URTH", "VSS", "SCZ",
    "VGK", "IEUR", "EZU", "VPL", "EWJ", "EWU", "EWG", "EWQ", "EWA", "EWC", "EWY", "EWT",
    "EWZ", "EWW", "EWH", "EWS", "EWL", "EWD", "EWN", "EWP", "EWI", "EIS",
    "INDA", "MCHI", "FXI", "KWEB", "ASHR", "EPI", "TUR", "EZA",
    "AGG", "BND", "SCHZ", "SPAB", "BIV", "BSV", "BLV", "VGIT", "VGSH", "VGLT", "GOVT",
    "SHY", "IEF", "TLT", "TLH", "SHV", "BIL", "SGOV",
    "VCIT", "VCSH", "VCLT", "LQD", "IGSB", "IGIB", "HYG", "JNK", "SHYG", "USHY",
    "MUB", "VTEB", "TFI", "SUB", "TIP", "VTIP", "SCHP", "STIP", "BNDX", "BWX", "IAGG",
    "EMB", "VWOB",
    "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC",
    "VGT", "VFH", "VDE", "VHT", "VIS", "VCR", "VDC", "VPU", "VAW",
    "VNQ", "VOX", "IYW", "IYF", "IYE", "IYH", "IYJ", "IYC", "IYK", "IDU", "IYM", "IYR", "IYZ", "IYT",
    "SMH", "SOXX", "IBB", "XBI", "ITA", "XAR", "KRE", "KBE", "XOP", "OIH", "XME", "XHB",
    "ITB", "XRT", "JETS", "TAN", "ICLN", "GDX", "GDXJ", "SCHH", "RWR", "REET", "VNQI",
    "GLD", "IAU", "SLV", "GLDM", "DBC", "PDBC", "USO", "UNG", "VXX",
    "FXAIX", "FSKAX", "FTIHX", "FXNAX", "FNILX", "FZROX", "FZILX", "FSPSX", "FSMDX", "FSSNX",
    "VFIAX", "VTSAX", "VTIAX", "VBTLX", "VSMAX", "VIMAX", "VFWAX", "VTMGX", "VEMAX",
    "VGSLX", "VBIAX", "VTWAX", "SWPPX", "SWTSX", "SWISX", "SWAGX", "SWSSX",
]


# Seeded onto every new Competition (CompetitionListCreateView.post,
# management.commands.seed_league) -- the 2026-08-13 strategy change:
# only a pre-market news read is a guaranteed floor wake-up now; every
# other check-in (market open, pre-close, anything else) is the AI's own
# schedule_wakeup call instead of an operator-mandated slot. Editable/
# removable per competition afterward via the admin inline on
# CompetitionAdmin (league/admin.py) -- this is just the starting point,
# not a hardcoded floor. A noon "midday pulse" slot isn't included here
# deliberately: it would double every future competition's guaranteed LLM-
# call floor by default, which is a cost decision an operator should opt
# into per competition (admin inline, news_slot="noon" to share the same
# generated brief this default's "morning" slot already gets), not
# something baked into every new competition's starting point.
DEFAULT_FORCED_CHECKINS = [
    dict(
        hour=8, minute=0, label="8am news scan",
        prompt=(
            "Nothing else is scheduled before the open. Read overnight/premarket news and macro "
            "data and flag anything that changes your thesis on open positions or your watchlist."
        ),
        inject_news=True, news_slot="morning",
    ),
]


def create_default_forced_checkins(competition: "Competition") -> None:
    ForcedCheckin.objects.bulk_create(
        ForcedCheckin(competition=competition, **kwargs) for kwargs in DEFAULT_FORCED_CHECKINS
    )


class ForcedCheckin(models.Model):
    """One guaranteed daily wake-up slot every AI competitor in a
    Competition gets regardless of its own schedule_wakeup calls -- data
    on the row (same "rules belong on Competition, not code constants"
    reasoning as max_position_pct/cash_apy/index_tickers above), so an
    operator can add, remove, retime, or reword these from the admin
    panel without a deploy. Dispatched by league.tasks.
    dispatch_forced_checkins, which runs every 5 minutes and fires every
    row whose (hour, minute) matches America/New_York wall-clock time
    right now, Mon-Fri only (market holidays aren't filtered -- a holiday
    tick just finds stale/closed quotes, harmless).

    Replaces the old hardcoded FORCED_CHECKINS list in league/tasks.py
    (three fixed slots: 8am news, 9:35am open, 3:15pm pre-close) --
    that list applied identically to every competition; this doesn't,
    which is the actual point of the change (a market-open/pre-close
    check now costs a self-scheduled wake-up like anything else, rather
    than being free, so the operator can tune how many guaranteed LLM
    calls per day a competition's cost budget actually wants)."""

    competition = models.ForeignKey(Competition, on_delete=models.CASCADE, related_name="forced_checkins")
    hour = models.PositiveSmallIntegerField(validators=[MaxValueValidator(23)])
    minute = models.PositiveSmallIntegerField(validators=[MaxValueValidator(59)])
    label = models.CharField(max_length=100)
    prompt = models.TextField()
    inject_news = models.BooleanField(
        default=False,
        help_text=(
            "Force-feed a market brief (headlines + broad-market snapshot, see "
            "league.tasks.get_or_create_daily_brief) directly into the wake-up prompt instead "
            "of leaving news-gathering to the agent's own get_market_news tool call -- for a "
            "slot that specifically wants a guaranteed, can't-skip news read, not just an "
            "opportunity to ask for one."
        ),
    )
    news_slot = models.CharField(
        max_length=32, blank=True,
        help_text=(
            "Cache key for the shared DailyBrief this row injects (only meaningful when "
            "inject_news is set). Rows across DIFFERENT competitions that share a news_slot "
            "(e.g. every competition's 8am row using 'morning') share one generated brief for "
            "that day instead of each independently hitting Steady -- this is the actual API-"
            "call savings, not just the per-competitor pre-fetch inject_news already gives you. "
            "Blank falls back to this row's own hour:minute as the slot key (still shared across "
            "competitions whose forced check-in happens to land on the exact same time)."
        ),
    )

    class Meta:
        ordering = ["competition", "hour", "minute"]

    def __str__(self):
        return f"{self.competition.name}: {self.hour:02d}:{self.minute:02d} ET -- {self.label}"


class DailyBrief(models.Model):
    """A market brief (headlines + broad-market snapshot) generated once per
    (date, slot) and shared across every competitor, in every competition,
    whose ForcedCheckin.news_slot matches -- see league.tasks.
    get_or_create_daily_brief and _generate_brief_content. Deliberately NOT
    scoped to a Competition: market headlines and index levels aren't
    competition-specific, so one shared row is what turns N competitors'
    worth of Steady API calls into one. Not cached indefinitely either --
    get_or_create_daily_brief creates a fresh row (never reuses a prior
    day's) the first time a given (date, slot) is needed, so a stale brief
    can never carry over into the next trading day."""

    date = models.DateField()
    slot = models.CharField(max_length=32)
    content = models.TextField(blank=True)
    generated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["date", "slot"], name="unique_daily_brief_date_slot"),
        ]
        ordering = ["-date", "slot"]

    def __str__(self):
        return f"{self.date}:{self.slot}"


class Competitor(models.Model):
    """One entrant in a Competition -- human or AI, same ledger either way
    (full parity: both submit trades through league.services.execution,
    nothing kind-specific in the rules engine). `assistant_model` and
    `system_prompt` are AI-only (nullable for humans); `user` is the
    reverse (nullable for AI competitors, since they aren't a login) and is
    what an API trade-submission view checks ownership against for a human
    competitor.

    `cash_balance` starts at competition.starting_capital and is the one
    field mutated outside a migration -- every change to it goes through
    execution.submit_trade() (a fill) or the weekly SPAXX accrual
    (tasks.mark_weekly_values), never set ad hoc.

    `is_paused` is an operator kill switch for an AI competitor's own
    LLM-driven wake-up loop -- the actual cost center (every wake-up is a
    real API call to Anthropic/OpenAI/Gemini), not the paper trades
    themselves. Toggle it from the admin list view (list_editable, see
    league/admin.py) rather than needing a code change or deleting
    anything. Also set automatically by league.tasks.run_agent_wakeup on a
    terminal provider error (a revoked API key, an account with no
    credits left -- see league.services.provider_errors.is_terminal) so a
    dead credential doesn't keep failing silently every day at the
    ForcedCheckin floor until an operator happens to notice; an operator
    still has to flip it back off once the credential is fixed. Checked in
    three places so pausing is airtight, not just "stops new ones from
    being scheduled": league.tasks.dispatch_forced_checkins (won't create
    a forced wake-up for a paused competitor), league.tasks.
    run_agent_wakeup (no-ops -- STATUS_CANCELLED -- if a wake-up fires
    anyway, e.g. one already queued via apply_async(eta=...) before the
    pause), and league.views.CompetitorWakeupsView.post (rejects a manual
    trigger outright rather than accepting it and then silently
    no-op'ing, which would look like a bug from the caller's side).
    Existing open Positions are untouched by pausing -- see the
    sell_all_positions management command to flatten those too."""

    KIND_HUMAN = "human"
    KIND_AI = "ai"
    KIND_CHOICES = [
        (KIND_HUMAN, "Human"),
        (KIND_AI, "AI"),
    ]

    competition = models.ForeignKey(Competition, on_delete=models.CASCADE, related_name="competitors")
    name = models.CharField(max_length=100)
    color = models.CharField(
        max_length=7, blank=True,
        help_text="Hex color (e.g. #FF5733) for this competitor's badge/line on the frontend. Cosmetic only.",
    )
    avatar = models.CharField(
        max_length=8, blank=True,
        help_text="A single emoji identifying this competitor on the frontend. Cosmetic only.",
    )
    kind = models.CharField(max_length=8, choices=KIND_CHOICES)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="league_competitors"
    )
    assistant_model = models.ForeignKey(
        "assistant.AssistantModel", on_delete=models.SET_NULL, null=True, blank=True, related_name="league_competitors"
    )
    system_prompt = models.TextField(blank=True)
    cash_balance = models.DecimalField(max_digits=12, decimal_places=2)
    is_paused = models.BooleanField(default=False)
    total_budget_usd = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text=(
            "Hard cap on this competitor's cumulative AgentTurn.cost_usd for the whole "
            "competition. Blank means no cap. Enforced in league.tasks.run_agent_wakeup "
            "(league.services.budget.is_over_total_budget), which sets is_paused=True and "
            "cancels the wakeup instead of running -- same shape as an is_paused check, so "
            "an operator has to raise the cap or leave it paused, there's no self-un-pause. "
            "Checked between wake-ups, not mid-call, so a single turn's own cost can still "
            "carry the total slightly past this number before the next wake-up gets blocked."
        ),
    )
    weekly_budget_usd = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text=(
            "Advisory-only pacing guide for this competitor's spend in the current trading "
            "week (Monday 00:00 ET through the next Monday 00:00 ET -- see "
            "league.services.wakeup_spacing.et_week_bounds). Blank means no guide. Unlike "
            "total_budget_usd this is never enforced -- it's only fed into the wake-up "
            "prompt (league.tasks._budget_block) so the competitor can judge its own pace, "
            "deliberately not a hard cap so an overspent week doesn't strand it dark until "
            "the week rolls over (same failure class LEAGUE_MAX_WAKEUPS_PER_DAY's safety net "
            "exists to avoid for the count-based cap)."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["competition", "name"], name="unique_competition_competitor_name"),
        ]
        ordering = ["competition", "name"]

    def __str__(self):
        return f"{self.name} ({self.competition.name})"


class Position(models.Model):
    """One open holding for a Competitor. `is_short` is only ever True for
    the option leg of a covered call or cash-covered put (this league
    disallows naked shorting/margin entirely -- see the rules doc); `covers`
    links that short-call Position to the specific long-stock Position
    collateralizing it, so execution.py can check
    `stock.quantity - SUM(100 * short_call.quantity for short calls
    covering this stock)` instead of trusting a single point-in-time check
    -- the FK makes double-covering the same 100 shares structurally
    checkable, not just validated once at entry and then forgotten.
    Cash-covered puts don't need a `covers` FK -- their collateral is cash,
    checked against cash_balance directly in execution.py."""

    INSTRUMENT_STOCK = "stock"
    INSTRUMENT_CALL = "call"
    INSTRUMENT_PUT = "put"
    INSTRUMENT_CHOICES = [
        (INSTRUMENT_STOCK, "Stock"),
        (INSTRUMENT_CALL, "Call"),
        (INSTRUMENT_PUT, "Put"),
    ]

    competitor = models.ForeignKey(Competitor, on_delete=models.CASCADE, related_name="positions")
    instrument_type = models.CharField(max_length=8, choices=INSTRUMENT_CHOICES)
    ticker = models.CharField(max_length=16)
    strike = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    expiration = models.DateField(null=True, blank=True)
    quantity = models.PositiveIntegerField()
    is_short = models.BooleanField(default=False)
    avg_cost = models.DecimalField(max_digits=12, decimal_places=4)
    covers = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="covered_by"
    )
    # Purely informational thesis levels -- nothing in execution.py watches
    # or enforces these, no auto-close, no alerting. Set optionally at open
    # (submit_trade's target_price/dead_price kwargs) and revisable anytime
    # on an already-open position without a new trade (the set_price_targets
    # tool / POST .../positions/{id}/price-targets/) -- deliberately NOT on
    # Trade, because the competitor (AI or human) routinely revises these on
    # a position it already holds ("moving my dead level up to lock in
    # gains"), not just at entry; a Trade-level field would go stale the
    # first time that happened, since a Trade row is never edited after
    # the fact.
    target_price = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    dead_price = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    opened_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["competitor", "-opened_at"]

    def __str__(self):
        suffix = f" {self.strike}{'C' if self.instrument_type == self.INSTRUMENT_CALL else 'P'} {self.expiration}" if self.instrument_type != self.INSTRUMENT_STOCK else ""
        return f"{self.competitor.name}:{'-' if self.is_short else '+'}{self.quantity} {self.ticker}{suffix}"


class Trade(models.Model):
    """Record of every submit_trade() call, filled or rejected -- the audit
    trail the rules doc's "written thesis... so it can be graded honestly"
    requirement points at. Instrument fields are denormalized from Position
    rather than read through the FK, so this row stays a truthful record of
    what was submitted even if the resulting Position is later modified/
    closed by further trades.

    Not strictly append-only: a STATUS_PENDING row (Competition.
    restrict_after_hours_trading queued it because the market was closed --
    see league.services.execution.submit_trade/resolve_pending_trade) is
    mutated in place to FILLED/REJECTED once league.tasks.
    process_pending_trades resolves it, or to CANCELLED if the competitor
    cancels it first (execution.cancel_pending_trade) -- same row/id
    throughout its lifecycle rather than a new row per transition, mirrors
    how AgentWakeup moves through its own STATUS_* states. Whichever of
    resolve/cancel actually reaches the database first wins a race between
    the two (both do a conditional UPDATE ... WHERE status='pending'); the
    loser sees 0 rows affected (cancel) or a no-longer-pending status on a
    fresh read (resolve) and backs off instead of overwriting the other."""

    SIDE_BUY = "buy"
    SIDE_SELL = "sell"
    SIDE_SELL_TO_OPEN = "sell_to_open"
    SIDE_BUY_TO_CLOSE = "buy_to_close"
    SIDE_CHOICES = [
        (SIDE_BUY, "Buy"),
        (SIDE_SELL, "Sell"),
        (SIDE_SELL_TO_OPEN, "Sell to open"),
        (SIDE_BUY_TO_CLOSE, "Buy to close"),
    ]

    STATUS_PENDING = "pending"
    STATUS_FILLED = "filled"
    STATUS_REJECTED = "rejected"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_FILLED, "Filled"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    competitor = models.ForeignKey(Competitor, on_delete=models.CASCADE, related_name="trades")
    position = models.ForeignKey(Position, on_delete=models.SET_NULL, null=True, blank=True, related_name="trades")
    side = models.CharField(max_length=16, choices=SIDE_CHOICES)
    instrument_type = models.CharField(max_length=8, choices=Position.INSTRUMENT_CHOICES)
    ticker = models.CharField(max_length=16)
    strike = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    expiration = models.DateField(null=True, blank=True)
    quantity = models.PositiveIntegerField()
    limit_price = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    fill_price = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    thesis = models.TextField()
    status = models.CharField(max_length=16, choices=STATUS_CHOICES)
    rejection_reason = models.CharField(max_length=500, blank=True)
    REJECTION_PRICE_LIMIT = "price_limit"
    REJECTION_POSITION_CAP = "position_cap"
    REJECTION_INDEX_CAP = "index_cap"
    REJECTION_INSUFFICIENT_CASH = "insufficient_cash"
    REJECTION_INSUFFICIENT_HOLDINGS = "insufficient_holdings"
    REJECTION_INSUFFICIENT_COLLATERAL = "insufficient_collateral"
    REJECTION_MARKET_UNAVAILABLE = "market_unavailable"
    REJECTION_INVALID_REQUEST = "invalid_request"
    REJECTION_CODE_CHOICES = [
        (REJECTION_PRICE_LIMIT, "Price limit crossed"),
        (REJECTION_POSITION_CAP, "Single-position cap exceeded"),
        (REJECTION_INDEX_CAP, "Index-fund cap exceeded"),
        (REJECTION_INSUFFICIENT_CASH, "Insufficient cash"),
        (REJECTION_INSUFFICIENT_HOLDINGS, "Insufficient holdings"),
        (REJECTION_INSUFFICIENT_COLLATERAL, "Insufficient collateral"),
        (REJECTION_MARKET_UNAVAILABLE, "Market data unavailable"),
        (REJECTION_INVALID_REQUEST, "Invalid request"),
    ]
    # Blank ("") means either a filled trade (no rejection at all) or a
    # rejected row created before this field existed -- deliberately not
    # backfilled from the old free-text rejection_reason (string-matching
    # old messages is fragile and rots the moment a message gets reworded),
    # so pre-migration rejections are excluded from rejection_code
    # breakdowns rather than folded into a misleading bucket.
    rejection_code = models.CharField(max_length=32, choices=REJECTION_CODE_CHOICES, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.competitor.name}:{self.side}:{self.ticker}:{self.status}"


class AgentScratchpad(models.Model):
    """An AI competitor's mutable working memory -- read in full and
    rewritten every wake-up (league.tasks.run_agent_wakeup), deliberately
    kept small and current rather than an append-only log. Curated by the
    agent itself via the update_scratchpad tool, the same shape as this
    Claude Code session's own memory files: durable state reconstructed
    fresh each time, not a growing transcript replayed forever."""

    competitor = models.OneToOneField(Competitor, on_delete=models.CASCADE, related_name="scratchpad")
    content = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.competitor.name}:scratchpad"


class AgentTickerMemory(models.Model):
    """One AI competitor's persistent research notes for a single ticker --
    same "durable state reconstructed fresh each time, not an append-only
    log" idea as AgentScratchpad, just partitioned per ticker instead of
    one blob for everything. Curated by the agent itself via the
    set_ticker_memories tool, which overwrites a row wholesale (never
    appends), same as update_scratchpad.

    Deliberately NOT auto-injected into every wake-up's prompt the way the
    scratchpad is (see league.tasks._scratchpad_content) -- meant to scale
    across many tickers an agent has looked at over time, most of which
    aren't relevant to any given wake-up, so it's pull-based instead:
    list_ticker_memories to see what's saved, get_ticker_memories to pull
    specific ones in. Cross-ticker/portfolio-level thoughts still belong in
    the scratchpad, not here."""

    competitor = models.ForeignKey(Competitor, on_delete=models.CASCADE, related_name="ticker_memories")
    ticker = models.CharField(max_length=16)
    content = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["competitor", "ticker"], name="unique_competitor_ticker_memory"),
        ]
        ordering = ["ticker"]

    def __str__(self):
        return f"{self.competitor.name}:{self.ticker}"


class AgentWakeup(models.Model):
    """A future check-in an AI competitor scheduled for itself via the
    schedule_wakeup tool. The row exists for durability/audit; the actual
    timing is Celery's native apply_async(eta=run_at) against the Redis
    broker already backing this project -- no Beat polling loop involved
    (see league.tools.schedule_wakeup / league.tasks.run_agent_wakeup)."""

    STATUS_SCHEDULED = "scheduled"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_SCHEDULED, "Scheduled"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    competitor = models.ForeignKey(Competitor, on_delete=models.CASCADE, related_name="wakeups")
    run_at = models.DateTimeField()
    reason = models.TextField(blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_SCHEDULED)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    # Set only on STATUS_FAILED, by league.services.provider_errors.
    # describe_error -- provider + status + the API's own message, so the
    # admin and the operator notification (league.tasks.run_agent_wakeup)
    # say something more useful than "Check server logs for details.",
    # which was the entire notification body before this field existed.
    error = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["run_at"]

    def __str__(self):
        return f"{self.competitor.name}:{self.run_at}:{self.status}"


class AgentTurn(models.Model):
    """Audit log of one actual LLM call for an AI competitor -- the
    transcript is for human review only and is NEVER read back into a
    future wake-up's context (see run_agent_wakeup's docstring: every
    wake-up rebuilds a fresh, bounded prompt from AgentScratchpad +
    live portfolio state, not from this history). Token/cost fields mirror
    assistant.Message's shape, reusing AssistantModel.cost_for()."""

    TRIGGER_SCHEDULED = "scheduled_wakeup"
    TRIGGER_MANUAL = "manual"
    TRIGGER_FORCED = "forced_checkin"
    TRIGGER_CHOICES = [
        (TRIGGER_SCHEDULED, "Scheduled wakeup"),
        (TRIGGER_MANUAL, "Manual"),
        (TRIGGER_FORCED, "Forced daily check-in"),
    ]

    competitor = models.ForeignKey(Competitor, on_delete=models.CASCADE, related_name="turns")
    wakeup = models.ForeignKey(AgentWakeup, on_delete=models.SET_NULL, null=True, blank=True, related_name="turns")
    trigger = models.CharField(max_length=20, choices=TRIGGER_CHOICES)
    transcript = models.JSONField(default=list, blank=True)
    input_tokens = models.PositiveIntegerField(null=True, blank=True)
    output_tokens = models.PositiveIntegerField(null=True, blank=True)
    cache_creation_input_tokens = models.PositiveIntegerField(null=True, blank=True)
    cache_read_input_tokens = models.PositiveIntegerField(null=True, blank=True)
    cost_usd = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True)
    tool_calls = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.competitor.name}:{self.trigger}:{self.pk}"


class WeeklyMark(models.Model):
    """One weekly account-value snapshot for a Competitor -- written by
    league.tasks.mark_weekly_values (the one recurring Beat job this
    feature adds, alongside refresh-prices-daily/poll-wiki-sources-daily
    in core/settings.py's CELERY_BEAT_SCHEDULE). Feeds the scoreboard:
    total return vs. competition.starting_capital, and max drawdown as the
    largest peak-to-trough decline across consecutive marks."""

    competitor = models.ForeignKey(Competitor, on_delete=models.CASCADE, related_name="weekly_marks")
    week_ending = models.DateField()
    account_value = models.DecimalField(max_digits=12, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["competitor", "week_ending"], name="unique_competitor_week"),
        ]
        ordering = ["competitor", "week_ending"]

    def __str__(self):
        return f"{self.competitor.name}:{self.week_ending}:{self.account_value}"


class AccountValueSnapshot(models.Model):
    """One intraday mark of a Competitor's total account value -- feeds the
    league page's equity-curve chart/sparklines (league.views.
    EquityCurveView). Written every ~10 minutes during market hours by
    league.tasks.refresh_open_position_quotes, for EVERY competitor in a
    currently-running competition (not just ones with open positions -- an
    all-cash competitor still needs a flat line, not a gap). Unlike
    WeeklyMark (once a week, scoring-rule-specific), this is purely a
    charting time series -- no uniqueness constraint, a snapshot every tick
    is expected."""

    competitor = models.ForeignKey(Competitor, on_delete=models.CASCADE, related_name="account_value_snapshots")
    at = models.DateTimeField()
    account_value = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        indexes = [models.Index(fields=["competitor", "at"])]
        ordering = ["competitor", "at"]

    def __str__(self):
        return f"{self.competitor.name}:{self.at}:{self.account_value}"
