"""Minimum spacing between an AI competitor's wake-ups -- shared by
league.tools.schedule_wakeup (rejects a new conflicting wakeup up front)
and league.management.commands.enforce_wakeup_spacing (cleans up ones
that landed within the window some other way, e.g. scheduled before this
check existed). One definition of "too close" so the two never drift
apart.

"Too close" means within MIN_WAKEUP_SPACING of either another
STATUS_SCHEDULED AgentWakeup for the same competitor, or one of that
competitor's own competition's ForcedCheckin floor slots (league.models)
-- those aren't AgentWakeup rows in the database ahead of time
(dispatch_forced_checkins creates the row only when the slot actually
fires), so they're checked as wall-clock ET times instead, same
weekday-only gate dispatch_forced_checkins itself uses (holidays not
accounted for, same known gap noted throughout league.tasks)."""

import zoneinfo
from datetime import datetime, time, timedelta

from django.utils import timezone as dj_timezone

from .. import models as league_models

MIN_WAKEUP_SPACING = timedelta(minutes=3)
EASTERN = zoneinfo.ZoneInfo("America/New_York")

# Reason-text prefixes written by league.tasks.dispatch_forced_checkins and
# _ensure_future_wakeup_scheduled respectively -- the only way to tell an
# operator/system-mandated AgentWakeup apart from the agent's own
# schedule_wakeup call, since AgentWakeup has no `source` field. Defined
# here (not duplicated in tasks.py/tools.py) so the two writers and every
# reader (wakeups_today_count below, league/tasks.py's dispatch_forced_
# checkins, the wakeup_diagnostics/export_league_data management commands)
# can never drift out of sync with each other.
FORCED_CHECKIN_REASON_PREFIX = "[Forced: "
SAFETY_NET_REASON_PREFIX = "Automatic safety-net check-in:"


def is_exempt_reason(reason: str) -> bool:
    """True for a ForcedCheckin or safety-net AgentWakeup -- both
    operator/system-mandated, not the agent's own scheduling discretion,
    so both are meant to sit outside LEAGUE_MAX_WAKEUPS_PER_DAY (see each
    creator's own docstring). See wakeups_today_count for why that
    distinction actually has to be enforced, not just documented."""
    reason = reason or ""
    return reason.startswith(FORCED_CHECKIN_REASON_PREFIX) or reason.startswith(SAFETY_NET_REASON_PREFIX)


def et_day_bounds(now: datetime) -> tuple[datetime, datetime]:
    """[start, end) of now's America/New_York calendar day, both tz-aware.
    Used everywhere "today" has to mean the ET trading day rather than
    created_at__date's UTC day (TIME_ZONE="UTC" -- a UTC-day boundary
    lands at 8pm ET, mid-session for anyone still trading into the
    close)."""
    start = now.astimezone(EASTERN).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def et_week_bounds(now: datetime) -> tuple[datetime, datetime]:
    """[start, end) of now's America/New_York trading week -- Monday 00:00 ET
    through the following Monday 00:00 ET. Same "one shared definition of a
    calendar boundary" reasoning as et_day_bounds above -- used by
    league.services.budget for the weekly (advisory, not enforced -- see
    Competitor.weekly_budget_usd) spend figure fed into the wake-up prompt."""
    et = now.astimezone(EASTERN)
    start_date = et.date() - timedelta(days=et.weekday())  # Monday == 0
    start = datetime.combine(start_date, time.min, tzinfo=EASTERN)
    return start, start + timedelta(days=7)


def wakeups_today_count(competitor, now: datetime | None = None) -> int:
    """How many of competitor's OWN AgentWakeup rows -- excluding
    ForcedCheckin and safety-net rows, see is_exempt_reason -- were
    created so far in today's ET calendar day. This is what
    LEAGUE_MAX_WAKEUPS_PER_DAY is actually meant to budget.

    Counting the exempt rows too (the bug this replaced) is what turned a
    routine forced 8am check-in plus a couple of iteration-budget safety
    nets into a real production incident on 2026-08-25: two competitors
    each fired 17 consecutive safety-net wake-ups, one every 15 minutes,
    because every one of the 13 "free" rows counted against the cap,
    schedule_wakeup then refused the competitor's own next attempt, the
    turn ended with nothing scheduled, the safety net fired again 15
    minutes later, and *that* row counted too -- a closed loop that only
    broke when the UTC calendar day rolled over at 8pm ET and reset the
    old (buggy) count."""
    now = now or dj_timezone.now()
    start, end = et_day_bounds(now)
    reasons = league_models.AgentWakeup.objects.filter(
        competitor=competitor, created_at__gte=start, created_at__lt=end,
    ).values_list("reason", flat=True)
    return sum(1 for reason in reasons if not is_exempt_reason(reason))


def next_cap_reset(now: datetime) -> datetime:
    """First moment wakeups_today_count reads 0 again for a wake-up
    scheduled at/after it -- the next America/New_York midnight, plus a
    1-minute buffer so the new ET day has actually started by the time
    this fires. Used by league.tasks._ensure_future_wakeup_scheduled when
    a turn ended with nothing scheduled BECAUSE it was cap-refused: the
    usual 15-minute safety-net delay would just get refused again (see
    wakeups_today_count's docstring for the incident this caused), so a
    cap-exhausted competitor instead gets its safety net timed for just
    after the reset rather than 15 minutes out."""
    _, day_end = et_day_bounds(now)
    return day_end + timedelta(minutes=1)


def _forced_checkin_times(competition) -> list[time]:
    return [time(c.hour, c.minute) for c in competition.forced_checkins.all()]


def nearest_forced_checkin_gap(competitor, run_at: datetime) -> timedelta | None:
    """Smallest gap between run_at and any of competitor's competition's
    ForcedCheckin slots on its own (ET) day -- None on a weekend (no
    forced check-ins fire at all) or if the competition has no
    ForcedCheckin rows configured."""
    et = run_at.astimezone(EASTERN)
    if et.weekday() >= 5:
        return None
    times = _forced_checkin_times(competitor.competition)
    if not times:
        return None
    return min(
        abs(et - et.replace(hour=slot.hour, minute=slot.minute, second=0, microsecond=0))
        for slot in times
    )


def conflict_reason(competitor, run_at: datetime, exclude_id=None) -> str | None:
    """None if run_at is clear to schedule for competitor, otherwise a
    human-readable reason it isn't. exclude_id skips one AgentWakeup (the
    row being re-evaluated itself) out of the "other scheduled wake-ups"
    check."""
    forced_gap = nearest_forced_checkin_gap(competitor, run_at)
    if forced_gap is not None and forced_gap <= MIN_WAKEUP_SPACING:
        et = run_at.astimezone(EASTERN)
        return f"{et.strftime('%I:%M%p ET').lstrip('0')} is within 3 minutes of a guaranteed floor wake-up."

    nearby = league_models.AgentWakeup.objects.filter(
        competitor=competitor,
        status=league_models.AgentWakeup.STATUS_SCHEDULED,
        run_at__gte=run_at - MIN_WAKEUP_SPACING,
        run_at__lte=run_at + MIN_WAKEUP_SPACING,
    )
    if exclude_id is not None:
        nearby = nearby.exclude(id=exclude_id)
    other = nearby.order_by("run_at").first()
    if other is not None:
        other_et = other.run_at.astimezone(EASTERN).strftime('%I:%M%p ET').lstrip('0')
        return f"within 3 minutes of another scheduled wake-up at {other_et} ({other.reason or 'no reason given'})."

    return None
