"""AI competitor cost-budget aggregation and pacing projections --
Competitor.total_budget_usd (hard cap, enforced in league.tasks.
run_agent_wakeup) and Competitor.weekly_budget_usd (advisory-only, fed into
the wake-up prompt via league.tasks._budget_block). One module for both
concerns since they're computed from the same three AgentTurn.cost_usd
aggregates -- see snapshot() below.

Deliberately NOT a rolling 7-day window for "this week" -- et_week_bounds
(wakeup_spacing.py) anchors it to the trading week (Monday 00:00 ET through
the next Monday), same reasoning et_day_bounds already applies to "today":
a UTC-day/rolling-window slice doesn't match how a human (or an agent
reasoning about its own weekly pace) actually thinks about "this week."

Because weekly_budget_usd is advisory only, there's no banked-carryover
ledger here -- an underspent week isn't tracked as credit anywhere, it just
stops counting the moment et_week_bounds rolls over to the next Monday.
"Carryover" happens implicitly, not through bookkeeping."""

from dataclasses import dataclass
from decimal import Decimal

from django.db.models import Count, Sum
from django.utils import timezone as dj_timezone

from .. import models as league_models
from .wakeup_spacing import et_day_bounds, et_week_bounds

# Floor for elapsed-time-this-week when projecting a full-week total --
# without this, a wake-up in the first few minutes after Monday midnight
# would divide by a near-zero denominator and project an absurd number
# (a single $0.05 call at 00:01 Monday "projecting" to hundreds of dollars
# by Sunday). Not a precision knob, just a sanity floor.
_MIN_ELAPSED_DAYS_FOR_PROJECTION = Decimal("0.1")


@dataclass
class BudgetSnapshot:
    total_spent: Decimal
    total_budget: Decimal | None
    total_remaining: Decimal | None
    week_spent: Decimal
    weekly_budget: Decimal | None
    today_spent: Decimal
    projected_week_total: Decimal | None
    daily_avg_overall: Decimal
    projected_runway_days: Decimal | None
    competition_days_left: int
    avg_cost_per_turn: Decimal | None
    remaining_calls_estimate: int | None


def _spent_since(turns_qs, since) -> Decimal:
    return turns_qs.filter(created_at__gte=since).aggregate(s=Sum("cost_usd"))["s"] or Decimal("0")


def snapshot(competitor, now=None) -> BudgetSnapshot:
    now = now or dj_timezone.now()
    competition = competitor.competition

    turns = league_models.AgentTurn.objects.filter(competitor=competitor)
    agg = turns.aggregate(total=Sum("cost_usd"), count=Count("id"))
    total_spent = agg["total"] or Decimal("0")
    turn_count = agg["count"] or 0

    week_start, _week_end = et_week_bounds(now)
    week_spent = _spent_since(turns, week_start)

    day_start, _day_end = et_day_bounds(now)
    today_spent = _spent_since(turns, day_start)

    total_budget = competitor.total_budget_usd
    total_remaining = (total_budget - total_spent) if total_budget is not None else None

    weekly_budget = competitor.weekly_budget_usd

    days_elapsed_in_competition = max((now.date() - competition.start_date).days + 1, 1)
    daily_avg_overall = total_spent / Decimal(days_elapsed_in_competition)

    projected_runway_days = None
    if total_remaining is not None and daily_avg_overall > 0:
        projected_runway_days = total_remaining / daily_avg_overall

    elapsed_seconds_this_week = (now - week_start).total_seconds()
    elapsed_days_this_week = max(Decimal(elapsed_seconds_this_week) / Decimal(86400), _MIN_ELAPSED_DAYS_FOR_PROJECTION)
    projected_week_total = None
    if weekly_budget is not None:
        projected_week_total = (week_spent / elapsed_days_this_week) * 7

    competition_days_left = max((competition.end_date - now.date()).days, 0)

    avg_cost_per_turn = (total_spent / turn_count) if turn_count else None
    remaining_calls_estimate = None
    if total_remaining is not None and avg_cost_per_turn is not None and avg_cost_per_turn > 0:
        remaining_calls_estimate = int(total_remaining / avg_cost_per_turn)

    return BudgetSnapshot(
        total_spent=total_spent, total_budget=total_budget, total_remaining=total_remaining,
        week_spent=week_spent, weekly_budget=weekly_budget, today_spent=today_spent,
        projected_week_total=projected_week_total, daily_avg_overall=daily_avg_overall,
        projected_runway_days=projected_runway_days, competition_days_left=competition_days_left,
        avg_cost_per_turn=avg_cost_per_turn, remaining_calls_estimate=remaining_calls_estimate,
    )


def is_over_total_budget(competitor, now=None) -> bool:
    """True once competitor's all-time AgentTurn.cost_usd has met or passed
    total_budget_usd. False (never over) when total_budget_usd isn't set --
    callers still need their own `is not None` check before treating False
    as "under budget" vs "no budget configured at all"."""
    if competitor.total_budget_usd is None:
        return False
    total_spent = league_models.AgentTurn.objects.filter(competitor=competitor).aggregate(
        s=Sum("cost_usd"),
    )["s"] or Decimal("0")
    return total_spent >= competitor.total_budget_usd
