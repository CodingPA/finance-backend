"""Read/write API for the paper trading league. Plain APIView + manual
dict-building, matching companies/views.py's convention (no DRF
serializers used anywhere else in this codebase)."""

from datetime import date, datetime, time, timezone as dt_timezone
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError
from django.db.models import Count, Max, Sum
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework import permissions, status
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from assistant.models import AssistantModel

from .models import (
    AccountValueSnapshot, AgentTurn, AgentWakeup, Competition, Competitor, DailyBrief, Position, Trade,
    create_default_forced_checkins,
)
from .services import budget, execution, valuation, wakeup_spacing
from .tasks import (
    EASTERN, next_occurrence, refresh_competition_quotes, refresh_competitor_quotes, run_agent_wakeup,
)


def _position_row(p: Position) -> dict:
    current_price, current_value, priced_at = valuation.mark_position_detail(p)
    return {
        "id": p.id,
        "instrument_type": p.instrument_type,
        "ticker": p.ticker,
        "strike": str(p.strike) if p.strike is not None else None,
        "expiration": p.expiration.isoformat() if p.expiration else None,
        "quantity": p.quantity,
        "is_short": p.is_short,
        "avg_cost": str(p.avg_cost),
        "current_price": str(current_price),
        "current_value": str(current_value),
        "priced_at": priced_at,
        "target_price": str(p.target_price) if p.target_price is not None else None,
        "dead_price": str(p.dead_price) if p.dead_price is not None else None,
        "opened_at": p.opened_at,
    }


def _trade_row(t: Trade) -> dict:
    return {
        "id": t.id,
        "side": t.side,
        "instrument_type": t.instrument_type,
        "ticker": t.ticker,
        "strike": str(t.strike) if t.strike is not None else None,
        "expiration": t.expiration.isoformat() if t.expiration else None,
        "quantity": t.quantity,
        "limit_price": str(t.limit_price) if t.limit_price is not None else None,
        "fill_price": str(t.fill_price) if t.fill_price is not None else None,
        "thesis": t.thesis,
        "status": t.status,
        "rejection_reason": t.rejection_reason,
        "rejection_code": t.rejection_code,
        "created_at": t.created_at,
    }


def _league_trade_row(t: Trade) -> dict:
    """Same shape as _trade_row, plus the competitor identity fields a
    cross-competitor feed needs that a single-competitor endpoint (called
    from a URL that already names the competitor) doesn't -- see
    CompetitionTradeFeedView."""
    row = _trade_row(t)
    row["competitor_id"] = t.competitor_id
    row["competitor_name"] = t.competitor.name
    row["competitor_kind"] = t.competitor.kind
    return row


def _parse_date_boundary(value: str, end_of_day: bool = False):
    """Parse a `since`/`until` query param into an aware datetime -- either
    an ISO datetime (used as-is, any offset) or a bare date (start-of-day
    for `since`, end-of-day for `until`, so `?since=2026-08-01&until=
    2026-08-13` reads as the intuitive whole-days-inclusive range rather
    than midnight-to-midnight excluding most of the last day). A bare date
    is anchored to America/New_York, not TIME_ZONE (UTC) -- this is a
    trading log, and "trades from 2026-08-13" means that ET trading day,
    matching every other day/market-hours notion in league.tasks, not an
    arbitrary UTC slice that would cut ~4-5pm ET trades into the next
    day's bucket."""
    parsed = parse_datetime(value)
    if parsed is not None:
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed, dt_timezone.utc)
        return parsed

    parsed_date = parse_date(value)
    if parsed_date is None:
        raise ValidationError(f"Bad date/datetime {value!r} -- use YYYY-MM-DD or ISO 8601.")
    naive = datetime.combine(parsed_date, time.max if end_of_day else time.min)
    return naive.replace(tzinfo=EASTERN)


_UNSET = object()  # sentinel distinct from None: None is a legit "zero turns" value for api_cost_usd/last_woke_at


def _competitor_stats(competition: Competition) -> tuple[dict, dict]:
    """Bulk per-competitor aggregates for a whole competition -- three
    queries total regardless of competitor count, feeding
    _competitor_summary's filled_trades_count/api_cost_usd/last_woke_at so
    ScoreboardView/CompetitionCompetitorsView (and their refresh-task
    duplicates, league/tasks.py) don't do it per-row (N+1)."""
    trade_counts = dict(
        Trade.objects.filter(competitor__competition=competition, status=Trade.STATUS_FILLED)
        .values_list("competitor_id").annotate(n=Count("id")).values_list("competitor_id", "n")
    )
    turn_stats = {
        row["competitor_id"]: (row["cost"], row["last"])
        for row in AgentTurn.objects.filter(competitor__competition=competition)
        .values("competitor_id").annotate(cost=Sum("cost_usd"), last=Max("created_at"))
    }
    return trade_counts, turn_stats


def _competitor_budget_stats(competition: Competition, now=None) -> tuple[dict, dict]:
    """Bulk per-competitor week/today AgentTurn.cost_usd for a whole
    competition -- two queries total regardless of competitor count, same
    "no N+1 across a scoreboard" reasoning as _competitor_stats above.
    league.services.budget.snapshot() does the equivalent single-competitor
    query for CompetitorDetailView, where an extra O(1) query per request is
    fine; ScoreboardView/CompetitionCompetitorsView reuse this instead since
    they render every competitor in a competition at once."""
    now = now or timezone.now()
    week_start, _week_end = wakeup_spacing.et_week_bounds(now)
    day_start, _day_end = wakeup_spacing.et_day_bounds(now)
    week_stats = dict(
        AgentTurn.objects.filter(competitor__competition=competition, created_at__gte=week_start)
        .values_list("competitor_id").annotate(s=Sum("cost_usd")).values_list("competitor_id", "s")
    )
    today_stats = dict(
        AgentTurn.objects.filter(competitor__competition=competition, created_at__gte=day_start)
        .values_list("competitor_id").annotate(s=Sum("cost_usd")).values_list("competitor_id", "s")
    )
    return week_stats, today_stats


def _budget_row_fields(competitor: Competitor, week_spent: Decimal | None, today_spent: Decimal | None) -> dict:
    """AI-only budget fields shared by every competitor-row builder
    (ScoreboardView, CompetitionCompetitorsView, CompetitorDetailView) --
    one place so the three endpoints can't drift on field names/shape."""
    return {
        "total_budget_usd": str(competitor.total_budget_usd) if competitor.total_budget_usd is not None else None,
        "weekly_budget_usd": str(competitor.weekly_budget_usd) if competitor.weekly_budget_usd is not None else None,
        "week_spent_usd": str(week_spent or Decimal("0")),
        "today_spent_usd": str(today_spent or Decimal("0")),
    }


def _rejected_trade_breakdown(competition: Competition) -> list[dict]:
    """Competition-wide rejected-trade counts by Trade.rejection_code --
    pre-migration rows (rejection_code="") are excluded rather than folded
    into a misleading bucket, see Trade.rejection_code's docstring."""
    rows = (
        Trade.objects.filter(competitor__competition=competition, status=Trade.STATUS_REJECTED)
        .exclude(rejection_code="")
        .values("rejection_code").annotate(n=Count("id")).order_by("-n")
    )
    return [{"reason": row["rejection_code"], "count": row["n"]} for row in rows]


def _competitor_summary(
    competitor: Competitor,
    account_value: Decimal | None = None,
    filled_trades_count=_UNSET,
    api_cost_usd=_UNSET,
    last_woke_at=_UNSET,
) -> dict:
    """account_value: pass the already-computed total when the caller
    (CompetitorDetailView) has to mark every position individually anyway
    for its own positions list, so this doesn't mark them all a second
    time -- omit it (the common case, Scoreboard/CompetitionCompetitors,
    which only ever need the total) to have this compute it itself.

    filled_trades_count/api_cost_usd/last_woke_at: pass the already-looked-
    up values (from _competitor_stats) when the caller is iterating a whole
    competition, to avoid a query per competitor -- omit all three (the
    single-competitor case, e.g. CompetitorDetailView) to have this compute
    them itself; each is O(1) queries for one competitor, so no N+1 risk
    there. api_cost_usd/last_woke_at are None (not the sentinel) for a
    competitor with zero AgentTurn rows -- a human, or an AI never woken."""
    starting = competitor.competition.starting_capital
    value = account_value if account_value is not None else valuation.account_value(competitor)
    if filled_trades_count is _UNSET:
        filled_trades_count = competitor.trades.filter(status=Trade.STATUS_FILLED).count()
    if api_cost_usd is _UNSET or last_woke_at is _UNSET:
        turn_agg = competitor.turns.aggregate(cost=Sum("cost_usd"), last=Max("created_at"))
        api_cost_usd, last_woke_at = turn_agg["cost"], turn_agg["last"]
    return {
        "id": competitor.id,
        "name": competitor.name,
        "color": competitor.color,
        "avatar": competitor.avatar,
        "kind": competitor.kind,
        "cash_balance": str(competitor.cash_balance),
        "account_value": str(value),
        "total_return_pct": str((value - starting) / starting) if starting else "0",
        "filled_trades_count": filled_trades_count,
        "api_cost_usd": str(api_cost_usd) if api_cost_usd is not None else None,
        "last_woke_at": last_woke_at,
    }


def _max_drawdown_pct(competitor: Competitor) -> Decimal:
    """Largest peak-to-trough decline across this competitor's weekly
    marks (the rules doc's scoring rule 5) -- from WeeklyMark history, not
    the live value, since drawdown is specifically about the recorded
    weekly series, not an instantaneous number. Uses .all() rather than an
    explicit .order_by("week_ending") so a prefetch_related("weekly_marks")
    upstream (ScoreboardView) actually gets reused instead of triggering a
    fresh per-competitor query -- WeeklyMark.Meta.ordering is already
    ["competitor", "week_ending"], so .all() is already in the right order."""
    peak = None
    worst = Decimal("0")
    for mark in competitor.weekly_marks.all():
        if peak is None or mark.account_value > peak:
            peak = mark.account_value
        if peak:
            drawdown = (peak - mark.account_value) / peak
            worst = max(worst, drawdown)
    return worst


def _get_competitor_or_404(competitor_id) -> Competitor:
    try:
        return Competitor.objects.select_related("competition").get(id=competitor_id)
    except Competitor.DoesNotExist:
        raise NotFound(f"No competitor with id {competitor_id}.")


def _get_competition_or_404(competition_id) -> Competition:
    try:
        return Competition.objects.get(id=competition_id)
    except Competition.DoesNotExist:
        raise NotFound(f"No competition with id {competition_id}.")


def _competition_row(c: Competition) -> dict:
    return {
        "id": c.id, "name": c.name, "status": c.status,
        "starting_capital": str(c.starting_capital),
        "max_position_pct": str(c.max_position_pct),
        "max_index_pct": str(c.max_index_pct),
        "cash_apy": str(c.cash_apy),
        "index_tickers": c.index_tickers,
        "start_date": c.start_date.isoformat(),
        "end_date": c.end_date.isoformat(),
        "competitor_count": c.competitors.count(),
    }


def _competitor_detail_row(
    c: Competitor,
    account_value: Decimal | None = None,
    filled_trades_count=_UNSET,
    api_cost_usd=_UNSET,
    last_woke_at=_UNSET,
    week_spent_usd=_UNSET,
    today_spent_usd=_UNSET,
) -> dict:
    """week_spent_usd/today_spent_usd: pass the already bulk-fetched values
    (league.services.budget's per-competition equivalent... actually
    views._competitor_budget_stats) when the caller is iterating a whole
    competition (CompetitionCompetitorsView), same N+1-avoidance reasoning
    as filled_trades_count/api_cost_usd/last_woke_at above. Omit (the
    single-competitor case, CompetitorDetailView/the POST create response)
    to have this compute them itself via budget.snapshot() -- O(1) queries,
    no N+1 risk for one competitor."""
    row = _competitor_summary(
        c, account_value=account_value, filled_trades_count=filled_trades_count,
        api_cost_usd=api_cost_usd, last_woke_at=last_woke_at,
    )
    row["user_email"] = c.user.email if c.user else None
    if c.kind == Competitor.KIND_AI:
        row["assistant_model"] = c.assistant_model.model_id if c.assistant_model else None
        row["provider"] = c.assistant_model.provider if c.assistant_model else None
        row["system_prompt"] = c.system_prompt
        if week_spent_usd is _UNSET or today_spent_usd is _UNSET:
            snap = budget.snapshot(c)
            week_spent_usd, today_spent_usd = snap.week_spent, snap.today_spent
        row.update(_budget_row_fields(c, week_spent_usd, today_spent_usd))
    return row


def _wakeup_row(w: AgentWakeup) -> dict:
    return {
        "id": w.id, "run_at": w.run_at, "reason": w.reason,
        "status": w.status, "created_at": w.created_at, "completed_at": w.completed_at,
    }


def _turn_summary_row(t: AgentTurn) -> dict:
    return {
        "id": t.id, "trigger": t.trigger, "wakeup_id": t.wakeup_id, "tool_calls": t.tool_calls,
        "input_tokens": t.input_tokens, "output_tokens": t.output_tokens,
        "cost_usd": str(t.cost_usd) if t.cost_usd is not None else None,
        "created_at": t.created_at,
    }


class ScoreboardView(APIView):
    """GET every competitor in a Competition: current account value, total
    return vs. starting capital, and max drawdown from weekly marks --
    sorted best return first."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, competition_id):
        try:
            competition = Competition.objects.get(id=competition_id)
        except Competition.DoesNotExist:
            raise NotFound(f"No competition with id {competition_id}.")

        trade_counts, turn_stats = _competitor_stats(competition)
        week_stats, today_stats = _competitor_budget_stats(competition)
        rows = []
        # prefetch_related: without it, competitor.positions.all() (inside
        # valuation.account_value) and competitor.weekly_marks.order_by(...)
        # (inside _max_drawdown_pct) each re-query per competitor -- N+1 on
        # top of the live-quote N+1 the Steady cache above already fixes.
        for competitor in competition.competitors.prefetch_related("positions", "weekly_marks"):
            cost, last_woke_at = turn_stats.get(competitor.id, (None, None))
            row = _competitor_summary(
                competitor, filled_trades_count=trade_counts.get(competitor.id, 0),
                api_cost_usd=cost, last_woke_at=last_woke_at,
            )
            row["max_drawdown_pct"] = str(_max_drawdown_pct(competitor))
            if competitor.kind == Competitor.KIND_AI:
                row.update(_budget_row_fields(
                    competitor, week_stats.get(competitor.id), today_stats.get(competitor.id),
                ))
            rows.append(row)
        # -id secondary key: total_return_pct alone lets tied rows (e.g.
        # everyone still at 0.0%/starting_capital early in a competition)
        # reorder arbitrarily on every refresh -- id descending keeps ties
        # stable without meaning anything about rank.
        rows.sort(key=lambda r: (Decimal(r["total_return_pct"]), r["id"]), reverse=True)
        return Response({
            "competition": competition.name, "competitors": rows,
            "rejected_trades": _rejected_trade_breakdown(competition),
        })


class CompetitionRefreshView(APIView):
    """POST to force a live re-quote of every open position across every
    competitor in a competition -- one click updates everyone's scoreboard,
    not just the caller's own (see CompetitorRefreshView for the
    single-competitor version). Fire-and-forget over Celery: this returns
    202 immediately rather than blocking on Steady, same as
    CompetitorWakeupsView.post; the refreshed scoreboard arrives over
    ws://.../ws/league/<competition_id>/ as league.scoreboard_refreshed
    (docs/websockets.md), not the HTTP response."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, competition_id):
        competition = _get_competition_or_404(competition_id)
        refresh_competition_quotes.delay(competition.id)
        return Response({"status": "queued", "competition_id": competition.id}, status=status.HTTP_202_ACCEPTED)


class CompetitionTradeFeedView(APIView):
    """GET every trade (filled and rejected) across every competitor in a
    competition -- human and AI alike -- newest first, for a "what is
    everyone doing" monitoring view rather than CompetitorTradesView's
    single-competitor history. Optional `?since=`/`?until=` (YYYY-MM-DD or
    full ISO 8601) narrow to a date range; `?competitor_kind=ai` or
    `=human` narrows to one side. Live updates for this same data arrive
    over ws://.../ws/league/<competition_id>/ (realtime.consumers.
    LeagueFeedConsumer, broadcast from league.services.execution.
    submit_trade) -- this endpoint is the initial page load / anything-
    missed-while-disconnected source of truth, not meant to be polled."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, competition_id):
        competition = _get_competition_or_404(competition_id)
        trades = Trade.objects.filter(competitor__competition=competition).select_related("competitor")

        since = request.query_params.get("since")
        if since:
            trades = trades.filter(created_at__gte=_parse_date_boundary(since))
        until = request.query_params.get("until")
        if until:
            trades = trades.filter(created_at__lte=_parse_date_boundary(until, end_of_day=True))

        competitor_kind = request.query_params.get("competitor_kind")
        if competitor_kind:
            if competitor_kind not in (Competitor.KIND_AI, Competitor.KIND_HUMAN):
                raise ValidationError({"competitor_kind": [f"Must be one of {[Competitor.KIND_AI, Competitor.KIND_HUMAN]}."]})
            trades = trades.filter(competitor__kind=competitor_kind)

        return Response([_league_trade_row(t) for t in trades])


class EquityCurveView(APIView):
    """GET intraday account-value history for every competitor in a
    competition, one series per competitor, in a single call -- feeds both
    the league page's hero equity chart and its per-row standings
    sparklines (same payload, no separate coarse endpoint; downsampling for
    the sparklines is left to the frontend). Points come from
    AccountValueSnapshot, written every ~10 minutes during market hours by
    league.tasks.refresh_open_position_quotes -- so there are gaps
    overnight/weekends (no snapshots taken), which the frontend should
    connect across rather than treat as missing data. A competitor's series
    starts at whenever this snapshot job first ran for them, not
    competition start -- there's nothing to backfill from (TickerSnapshot
    only ever holds the LATEST quote per ticker, no history) -- and a
    competitor added mid-competition simply has a shorter series starting
    at its own first snapshot, not padded flat to competition start.
    Optional `?since=`/`?until=` (YYYY-MM-DD or full ISO 8601, same
    semantics as CompetitionTradeFeedView's) narrow the range; no
    `?interval=` downsampling param -- v1 always returns full resolution
    (~39 points/day/competitor, trivial volume even across a full 84-day
    competition)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, competition_id):
        competition = _get_competition_or_404(competition_id)
        snapshots = AccountValueSnapshot.objects.filter(
            competitor__competition=competition,
        ).order_by("competitor_id", "at")

        since = request.query_params.get("since")
        if since:
            snapshots = snapshots.filter(at__gte=_parse_date_boundary(since))
        until = request.query_params.get("until")
        if until:
            snapshots = snapshots.filter(at__lte=_parse_date_boundary(until, end_of_day=True))

        # Every competitor gets an entry, even one with no snapshots yet
        # (job hasn't run since they were added) -- an empty points list,
        # not an absent series, so the frontend never has to special-case
        # "this competitor is missing from the response entirely."
        series = {c.id: {"competitor_id": c.id, "name": c.name, "points": []} for c in competition.competitors.all()}
        for snap in snapshots:
            series[snap.competitor_id]["points"].append({"at": snap.at, "account_value": str(snap.account_value)})
        return Response(list(series.values()))


class LeagueModelListView(APIView):
    """GET the active AssistantModel rows a league AI competitor can be
    created with. Deliberately separate from GET /api/assistant/models/
    (which is scoped to provider=anthropic, since that app's own chat loop
    is Anthropic-only) -- league supports multiple providers
    (league/services/llm_openai.py's Responses-API branch), so this
    returns every active model regardless of provider."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        models = AssistantModel.objects.filter(is_active=True)
        return Response([
            {"model_id": m.model_id, "display_name": m.display_name, "provider": m.provider, "is_default": m.is_default}
            for m in models
        ])


class DailyBriefListView(APIView):
    """GET generated market briefs (headlines + broad-market snapshot,
    league.tasks.get_or_create_daily_brief) -- newest first. Global, not
    scoped to a competition (see DailyBrief's model docstring): one row per
    (date, slot), shared across every competition whose ForcedCheckin rows
    inject_news with that news_slot. Optional `?slot=` narrows to one slot
    (e.g. "morning", "noon"); `?since=`/`?until=` (YYYY-MM-DD) narrow the
    date range -- unlike CompetitionTradeFeedView's since/until, these are
    plain dates (DailyBrief.date has no time component), not ISO
    datetimes. `?limit=` caps how many rows come back (default 30, newest
    first per DailyBrief.Meta.ordering) -- there's no separate "latest"
    endpoint: `?slot=morning&limit=1` (or just reading the first item
    client-side) IS the latest brief for that slot."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        briefs = DailyBrief.objects.all()

        slot = request.query_params.get("slot")
        if slot:
            briefs = briefs.filter(slot=slot)

        since = request.query_params.get("since")
        if since:
            parsed = parse_date(since)
            if parsed is None:
                raise ValidationError({"since": ["Use YYYY-MM-DD."]})
            briefs = briefs.filter(date__gte=parsed)
        until = request.query_params.get("until")
        if until:
            parsed = parse_date(until)
            if parsed is None:
                raise ValidationError({"until": ["Use YYYY-MM-DD."]})
            briefs = briefs.filter(date__lte=parsed)

        limit_param = request.query_params.get("limit")
        if limit_param:
            try:
                limit = int(limit_param)
            except ValueError:
                raise ValidationError({"limit": ["Must be an integer."]})
        else:
            limit = 30

        return Response([
            {"id": b.id, "date": b.date.isoformat(), "slot": b.slot, "content": b.content, "generated_at": b.generated_at}
            for b in briefs[:limit]
        ])


class CompetitionListCreateView(APIView):
    """GET every competition, POST to create a new one. Rules
    (position/index caps, cash APY, index_tickers) are data on the row,
    not code constants -- set once here, editable afterward via Django
    admin if they need tuning. Same for the guaranteed daily floor
    wake-up schedule (league.models.ForcedCheckin) -- POST seeds the new
    default (one 8am news scan) automatically; add/remove/retime rows
    afterward via the ForcedCheckin inline on CompetitionAdmin, there's
    no API for it (this is operator/cost-tuning, not a player-facing
    concept)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response([_competition_row(c) for c in Competition.objects.all()])

    def post(self, request):
        data = request.data
        if not data.get("name"):
            raise ValidationError({"name": ["This field is required."]})
        if not data.get("start_date") or not data.get("end_date"):
            raise ValidationError({"start_date": ["start_date and end_date are both required."]})
        try:
            start_date = date.fromisoformat(data["start_date"])
            end_date = date.fromisoformat(data["end_date"])
        except ValueError as exc:
            raise ValidationError(f"Bad start_date/end_date: {exc}")

        try:
            competition = Competition.objects.create(
                name=data["name"],
                starting_capital=Decimal(str(data.get("starting_capital", "5000"))),
                max_position_pct=Decimal(str(data.get("max_position_pct", "0.60"))),
                max_index_pct=Decimal(str(data.get("max_index_pct", "0.30"))),
                cash_apy=Decimal(str(data.get("cash_apy", "0.033"))),
                index_tickers=data.get("index_tickers", []),
                start_date=start_date,
                end_date=end_date,
            )
        except (InvalidOperation, IntegrityError) as exc:
            raise ValidationError(str(exc))
        create_default_forced_checkins(competition)
        return Response(_competition_row(competition), status=status.HTTP_201_CREATED)


class CompetitionCompetitorsView(APIView):
    """GET every competitor in a competition (fuller detail than the
    scoreboard -- persona/model for AI competitors, not just returns).
    POST adds a new one. Both AI and human competitors are attached to
    the requesting user: AI so it has an owner to notify
    (league/tasks.py::run_agent_wakeup), human because this app has
    exactly one human operator/player right now, not a general signup
    flow (see this feature's plan) -- cash_balance is always seeded from
    competition.starting_capital, never entered by hand."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, competition_id):
        competition = _get_competition_or_404(competition_id)
        competitors = competition.competitors.select_related("user", "assistant_model").prefetch_related("positions")
        trade_counts, turn_stats = _competitor_stats(competition)
        week_stats, today_stats = _competitor_budget_stats(competition)
        rows = []
        for c in competitors:
            cost, last_woke_at = turn_stats.get(c.id, (None, None))
            rows.append(_competitor_detail_row(
                c, filled_trades_count=trade_counts.get(c.id, 0), api_cost_usd=cost, last_woke_at=last_woke_at,
                week_spent_usd=week_stats.get(c.id), today_spent_usd=today_stats.get(c.id),
            ))
        return Response(rows)

    def post(self, request, competition_id):
        competition = _get_competition_or_404(competition_id)
        data = request.data
        name = data.get("name")
        kind = data.get("kind")
        if not name:
            raise ValidationError({"name": ["This field is required."]})
        if kind not in (Competitor.KIND_AI, Competitor.KIND_HUMAN):
            raise ValidationError({"kind": [f"Must be one of {[Competitor.KIND_AI, Competitor.KIND_HUMAN]}."]})

        competitor_kwargs = dict(
            competition=competition, name=name, kind=kind,
            user=request.user, cash_balance=competition.starting_capital,
            color=data.get("color", ""), avatar=data.get("avatar", ""),
        )
        if kind == Competitor.KIND_AI:
            model_id = data.get("model")
            if model_id:
                try:
                    model = AssistantModel.objects.get(model_id=model_id, is_active=True)
                except AssistantModel.DoesNotExist:
                    raise ValidationError({"model": [f"No active model {model_id!r}."]})
            else:
                model = AssistantModel.get_default()
            competitor_kwargs["assistant_model"] = model
            competitor_kwargs["system_prompt"] = data.get("system_prompt", "")

        try:
            competitor = Competitor.objects.create(**competitor_kwargs)
        except IntegrityError:
            raise ValidationError({"name": [f"{name!r} already exists in this competition."]})
        return Response(_competitor_detail_row(competitor), status=status.HTTP_201_CREATED)


class CompetitorDetailView(APIView):
    """GET one competitor's current cash, positions, and account value --
    plus, for an AI competitor, its model/provider/persona (same fields
    CompetitionCompetitorsView's list already returns; this single-row
    view had been built on the bare _competitor_summary instead of
    _competitor_detail_row, so a dashboard detail panel showing model tag/
    persona/provider-aware transcript rendering had nothing to read here
    -- not a crash, just a silent omission, same graceful-degradation
    shape as everywhere else in this API)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, competitor_id):
        competitor = _get_competitor_or_404(competitor_id)
        # Mark every position exactly once (previously: once inside
        # _competitor_detail_row's account_value() call for the total,
        # then AGAIN here per-position for the rows -- every open Position
        # was live-quoted twice in a single request).
        position_rows = [_position_row(p) for p in competitor.positions.all()]
        total_value = competitor.cash_balance + sum(
            (Decimal(row["current_value"]) for row in position_rows), Decimal("0"),
        )
        detail = _competitor_detail_row(competitor, account_value=total_value)
        detail["positions"] = position_rows
        # The OLDEST (most stale) priced_at among shown positions, not the
        # newest -- "prices as of X" should describe the least-fresh number
        # actually contributing to this page, not flatter it with whichever
        # ticker happened to refresh most recently. None (omitted from the
        # min) for any position still on avg_cost fallback -- those aren't
        # priced at all, so they can't set an "as of" time; None overall
        # only if EVERY position lacks a snapshot (or there are no positions).
        priced_ats = [row["priced_at"] for row in position_rows if row["priced_at"] is not None]
        detail["prices_as_of"] = min(priced_ats) if priced_ats else None
        return Response(detail)


class CompetitorRefreshView(APIView):
    """POST to force a live re-quote of just this competitor's held
    tickers/contracts -- see CompetitionRefreshView for the
    whole-competition version. Same fire-and-forget/202/websocket-delivery
    shape: the refreshed row arrives over ws://.../ws/league/
    <competition_id>/ as league.competitor_refreshed."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, competitor_id):
        competitor = _get_competitor_or_404(competitor_id)
        refresh_competitor_quotes.delay(competitor.id)
        return Response({"status": "queued", "competitor_id": competitor.id}, status=status.HTTP_202_ACCEPTED)


class CompetitorTradesView(APIView):
    """GET a competitor's trade history (filled and rejected -- the full
    audit trail). POST submits a new trade -- reuses
    league.services.execution.submit_trade(), the exact same rules engine
    an AI competitor's submit_trade tool call goes through (full parity,
    no kind-specific code path). Only the competitor's own linked user may
    POST; AI competitors reject POST entirely -- they only ever trade
    through their own Celery wake-up loop, never this endpoint.

    Optional `target_price`/`dead_price` on an opening trade (buy/
    sell_to_open) record a take-profit/invalidation level on the
    resulting Position -- purely informational (see that field's
    docstring), and revisable afterward without a new trade via
    PositionPriceTargetsView below."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, competitor_id):
        competitor = _get_competitor_or_404(competitor_id)
        return Response([_trade_row(t) for t in competitor.trades.all()])

    def post(self, request, competitor_id):
        competitor = _get_competitor_or_404(competitor_id)
        if competitor.kind != Competitor.KIND_HUMAN or competitor.user_id != request.user.id:
            raise PermissionDenied("You can only submit trades for your own competitor.")

        data = request.data
        required = ["ticker", "side", "instrument_type", "quantity", "thesis"]
        missing = [f for f in required if not data.get(f)]
        if missing:
            raise ValidationError({f: ["This field is required."] for f in missing})

        try:
            quantity = int(data["quantity"])
            limit_price = Decimal(str(data["limit_price"])) if data.get("limit_price") else None
            strike = Decimal(str(data["strike"])) if data.get("strike") else None
            expiration = date.fromisoformat(data["expiration"]) if data.get("expiration") else None
            target_price = Decimal(str(data["target_price"])) if data.get("target_price") else None
            dead_price = Decimal(str(data["dead_price"])) if data.get("dead_price") else None
        except (InvalidOperation, ValueError) as exc:
            raise ValidationError(f"Bad quantity/limit_price/strike/expiration/target_price/dead_price: {exc}")

        trade = execution.submit_trade(
            competitor, data["ticker"], data["side"], data["instrument_type"], quantity, data["thesis"],
            limit_price=limit_price, strike=strike, expiration=expiration,
            target_price=target_price, dead_price=dead_price,
        )
        # PENDING (queued -- this competition restricts after-hours trading
        # and the market's closed, see execution.submit_trade) reads as
        # "accepted," not "failed" -- only an actual REJECTED outcome is a 400.
        response_status = status.HTTP_400_BAD_REQUEST if trade.status == Trade.STATUS_REJECTED else status.HTTP_201_CREATED
        return Response(_trade_row(trade), status=response_status)


class CompetitorTradeCancelView(APIView):
    """POST to cancel one of the competitor's own still-STATUS_PENDING
    trades (queued because Competition.restrict_after_hours_trading was set
    and the market was closed when it was submitted -- see
    execution.submit_trade). Same ownership rule as CompetitorTradesView.
    post/PositionPriceTargetsView -- only the competitor's own linked human
    user may call this; AI competitors do the equivalent via their own
    cancel_trade tool (league/tools.py), never this endpoint.

    A cancel that loses its race with league.tasks.process_pending_trades
    (the order resolved to filled/rejected microseconds before this hit the
    database -- see execution.TradeAlreadyResolved/Trade's own docstring on
    the race) comes back as 409 Conflict carrying the trade's actual
    current state (_trade_row, same shape as the 200 success case) rather
    than a bare error with no trade body -- lets the frontend render "this
    already resolved" with the real outcome (fill_price/rejection_reason)
    directly from the response, no extra fetch needed."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, competitor_id, trade_id):
        competitor = _get_competitor_or_404(competitor_id)
        if competitor.kind != Competitor.KIND_HUMAN or competitor.user_id != request.user.id:
            raise PermissionDenied("You can only cancel trades for your own competitor.")
        try:
            trade = competitor.trades.get(id=trade_id)
        except Trade.DoesNotExist:
            raise NotFound(f"No trade {trade_id} for this competitor.")
        try:
            trade = execution.cancel_pending_trade(trade)
        except execution.TradeAlreadyResolved:
            # Full refresh (not the status-only one cancel_pending_trade
            # already did internally) -- this response needs fill_price/
            # rejection_reason too, not just the status, to actually show
            # "the real outcome" the frontend asked for.
            trade.refresh_from_db()
            return Response(_trade_row(trade), status=status.HTTP_409_CONFLICT)
        return Response(_trade_row(trade))


class PositionPriceTargetsView(APIView):
    """POST to set/revise the take-profit (target_price) and/or
    invalidation (dead_price) level on an already-open Position, without
    submitting a new trade -- for "I'm moving my stop up" without
    actually trading. Purely informational, exactly like setting these at
    trade time (see Position.target_price's docstring): nothing here is
    enforced, nothing auto-closes. Same ownership rule as
    CompetitorTradesView.post -- only the competitor's own linked human
    user may call this; AI competitors do the equivalent via their own
    set_price_targets tool (league/tools.py), never this endpoint."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, competitor_id, position_id):
        competitor = _get_competitor_or_404(competitor_id)
        if competitor.kind != Competitor.KIND_HUMAN or competitor.user_id != request.user.id:
            raise PermissionDenied("You can only edit price targets for your own competitor.")
        try:
            position = competitor.positions.get(id=position_id)
        except Position.DoesNotExist:
            raise NotFound(f"No open position {position_id} for this competitor.")

        data = request.data
        if "target_price" not in data and "dead_price" not in data:
            raise ValidationError("Provide at least one of target_price/dead_price.")
        try:
            fields = []
            if "target_price" in data:
                position.target_price = Decimal(str(data["target_price"])) if data["target_price"] else None
                fields.append("target_price")
            if "dead_price" in data:
                position.dead_price = Decimal(str(data["dead_price"])) if data["dead_price"] else None
                fields.append("dead_price")
        except InvalidOperation as exc:
            raise ValidationError(f"Bad target_price/dead_price: {exc}")
        position.save(update_fields=fields)
        return Response(_position_row(position))


class CompetitorWakeupsView(APIView):
    """GET a competitor's wake-up history (scheduled by itself via the
    schedule_wakeup tool, or manually triggered here). POST fires one
    right now -- e.g. kicking off an AI competitor's initial research
    after creating it, or telling it to look at something specific
    on-demand rather than waiting for its next scheduled check.
    Dispatched asynchronously (same pattern as
    companies/views.py::ScreenView) -- returns 202 immediately; the
    actual result (trades, scratchpad update) arrives via the existing
    notify()/WebSocket path (league.tasks.run_agent_wakeup's
    league.trade_activity / league.wakeup_complete / league.wakeup_failed
    events) and is visible afterward through this endpoint and
    CompetitorTurnsView. Human competitors have no AI loop to trigger, and
    neither does a paused one (Competitor.is_paused) -- rejected outright
    here rather than accepted and silently no-op'd by run_agent_wakeup's
    own pause check, which would look like a bug from the caller's side."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, competitor_id):
        competitor = _get_competitor_or_404(competitor_id)
        return Response([_wakeup_row(w) for w in competitor.wakeups.all()])

    def post(self, request, competitor_id):
        competitor = _get_competitor_or_404(competitor_id)
        if competitor.kind != Competitor.KIND_AI:
            raise ValidationError("Only AI competitors can be woken up -- humans submit trades directly.")
        if competitor.is_paused:
            raise ValidationError(f"{competitor.name} is paused -- unpause it in the admin panel first.")

        reason = request.data.get("reason") or "Manual trigger from the competition operator."
        wakeup = AgentWakeup.objects.create(competitor=competitor, run_at=timezone.now(), reason=reason)
        run_agent_wakeup.delay(wakeup.id, AgentTurn.TRIGGER_MANUAL)
        return Response(_wakeup_row(wakeup), status=status.HTTP_202_ACCEPTED)


class CompetitorNextWakeupsView(APIView):
    """GET what's coming up next for one AI competitor -- two different
    kinds of "scheduled", both worth surfacing separately since only one
    of them has a row anywhere: this competitor's competition's
    guaranteed daily floor wake-ups (league.models.ForcedCheckin,
    admin-editable, count/times vary per competition -- see
    dispatch_forced_checkins; these never get an AgentWakeup row until
    the moment they actually fire, so there's nothing in the DB to list;
    computed live here instead) and whatever the competitor has
    self-scheduled via schedule_wakeup (real AgentWakeup rows,
    status=scheduled -- same data CompetitorWakeupsView shows, just
    pre-filtered to "still pending" and sorted soonest-first, rather than
    the full history). Empty lists for a human competitor -- neither
    concept applies to them."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, competitor_id):
        competitor = _get_competitor_or_404(competitor_id)
        if competitor.kind != Competitor.KIND_AI:
            return Response({"forced_checkins": [], "self_scheduled": []})

        now_et = timezone.now().astimezone(EASTERN)
        forced = sorted(
            (
                {"label": c.label, "next_run_at": next_occurrence(c.hour, c.minute, now_et)}
                for c in competitor.competition.forced_checkins.all()
            ),
            key=lambda row: row["next_run_at"],
        )
        self_scheduled = competitor.wakeups.filter(
            status=AgentWakeup.STATUS_SCHEDULED, run_at__gt=timezone.now(),
        ).order_by("run_at")

        return Response({
            "forced_checkins": [{"label": f["label"], "next_run_at": f["next_run_at"]} for f in forced],
            "self_scheduled": [_wakeup_row(w) for w in self_scheduled],
        })


class CompetitorTurnsView(APIView):
    """GET a competitor's AI reasoning history -- lightweight summaries
    (which tools it called, tokens, cost) without the full transcript,
    which can be large. Fetch TurnDetailView for one turn's full
    play-by-play."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, competitor_id):
        competitor = _get_competitor_or_404(competitor_id)
        return Response([_turn_summary_row(t) for t in competitor.turns.all()])


class TurnDetailView(APIView):
    """GET one AgentTurn's full transcript: every round's text/tool-use/
    server-tool-use/web-search-result blocks verbatim -- the "peek into
    AI logic" view. league/tasks.py::_run_wakeup builds this with
    block.model_dump() rather than hand-picked fields, so it's complete
    (confirmed live: a plain text/tool_use-only capture silently dropped
    web_search's server_tool_use/web_search_tool_result blocks)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, competitor_id, turn_id):
        competitor = _get_competitor_or_404(competitor_id)
        try:
            turn = competitor.turns.get(id=turn_id)
        except AgentTurn.DoesNotExist:
            raise NotFound(f"No turn {turn_id} for competitor {competitor_id}.")
        row = _turn_summary_row(turn)
        row["transcript"] = turn.transcript
        return Response(row)


class CompetitorScratchpadView(APIView):
    """GET an AI competitor's current working notes -- what it's watching
    right now and what it decided last time, the durable state each
    wake-up is rebuilt from (see CompetitorTurnsView for turn-by-turn
    history instead)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, competitor_id):
        competitor = _get_competitor_or_404(competitor_id)
        scratchpad = getattr(competitor, "scratchpad", None)
        return Response({
            "content": scratchpad.content if scratchpad else "",
            "updated_at": scratchpad.updated_at if scratchpad else None,
        })


class CompetitorTickerMemoriesView(APIView):
    """GET every ticker an AI competitor has saved research notes for
    (AgentTickerMemory) -- the per-ticker counterpart to
    CompetitorScratchpadView's single blob. Unlike the scratchpad, this is
    a list: one entry per ticker the agent has actually researched.

    No ownership check, same as CompetitorScratchpadView/CompetitorTurnsView
    -- any authenticated user can view any competitor's notes, which is
    fine here specifically because AgentTickerMemory rows can only ever
    belong to an AI competitor (set_ticker_memories is reachable only from
    inside league.tools.build_league_tools, which only runs in the AI
    wake-up loop) -- there is no human-owned note this could leak.

    Ordered most-recently-updated first for a human skimming this in the
    UI -- deliberately NOT AgentTickerMemory's own Meta.ordering
    (alphabetical by ticker, what list_ticker_memories the AI-facing tool
    uses instead, where stable order across calls matters more than
    recency)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, competitor_id):
        competitor = _get_competitor_or_404(competitor_id)
        memories = competitor.ticker_memories.order_by("-updated_at")
        return Response([
            {"ticker": m.ticker, "content": m.content, "updated_at": m.updated_at}
            for m in memories
        ])
