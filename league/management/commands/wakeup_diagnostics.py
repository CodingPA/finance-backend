"""Read-only health report for the AI competitor wake-up loop -- built to
answer "why do the agents sometimes just not check in?" with evidence
rather than a guess.

Safe to run on production: it only reads. No .save(), no task dispatch, no
LLM calls.

    python manage.py wakeup_diagnostics --days 14
    python manage.py wakeup_diagnostics --days 14 --json /tmp/wakeups.json

The summary printed to stdout is deliberately small enough to copy/paste
somewhere else; --json dumps the same findings in full (including every
row id behind each number) for deeper digging.

Each section isolates ONE candidate explanation, so a nonzero count points
at a specific mechanism instead of a vague "it's flaky":

 1. COVERAGE -- the symptom itself. Per competitor, per trading day, the
    biggest gap between AgentTurns inside the 8am-4pm ET session, plus
    days with no turn at all. Everything below is only interesting to the
    extent it lines up with a blackout here, so this is reported first.

 2. ORPHANED WAKE-UPS -- rows still STATUS_SCHEDULED whose run_at is in
    the past. As of 2026-08-29 these get automatically recovered by
    league.tasks.sweep_overdue_wakeups (a Beat task, every 10 minutes,
    re-dispatching anything overdue past its own grace window) -- so this
    section should read ~0 going forward; a nonzero count now means
    either the sweep task itself isn't running (check `journalctl -u
    finance-celery-beat | grep sweep_overdue_wakeups`) or a row is less
    than OVERDUE_SWEEP_GRACE (10 min) overdue and just hasn't been swept
    yet. Before the fix, nothing in this codebase ever revisited these,
    and run_agent_wakeup only ever fired from an apply_async(eta=...)
    handed to Redis -- an orphan meant the broker lost the task, most
    plausibly a worker restart (deploy.sh restarts finance-celery-worker
    every deploy) dropping ETA tasks it was holding in memory. Cross-
    reference these run_at times against `journalctl -u finance-celery-
    worker | grep -i "warm shutdown\\|ready"`. Note
    CELERY_BROKER_TRANSPORT_OPTIONS sets visibility_timeout to 14 days,
    so a genuinely unacked message won't be redelivered for two weeks --
    effectively never, for a wake-up, which is exactly why the sweep task
    exists rather than relying on that.

 3. FORCED CHECK-IN COVERAGE -- did each ForcedCheckin row actually fire
    on every weekday it should have? As of 2026-08-29,
    dispatch_forced_checkins fires any not-yet-fired-today slot whose
    time has passed (an ET-calendar-day "already fired" check, not a
    rolling window) AND is within CHECKIN_CATCH_UP_WINDOW (2 hours) of
    when it was due, so a genuinely MISSING day now means one of: the
    competitor was paused, the Beat task wasn't running at all that day,
    the row was created/edited after the slot time had already passed for
    that day (picks up the next day), OR a Beat/worker outage lasted
    longer than the 2-hour catch-up window (check league.tasks.
    dispatch_forced_checkins's own WARNING-level log line for "beyond the
    ... catch-up window" around the missing date -- this is now a
    deliberate, logged skip, not a silent one). Before the fix, matching
    was hour/minute EXACT equality against wall-clock ET at the moment
    the task executed, so (a) any row whose minute wasn't a multiple of
    Beat's own 5-minute tick could never fire at all, and (b) a tick that
    executed even 60s late (worker backlog, autoscale spin-up) silently
    skipped the slot with no catch-up.

 4. DAILY CAP -- LEAGUE_MAX_WAKEUPS_PER_DAY, reported using the CURRENT
    (fixed, 2026-08-29) counting rule: league.services.wakeup_spacing.
    wakeups_today_count's own ET-calendar-day window, excluding
    ForcedCheckin/safety-net rows. Before the fix, schedule_wakeup counted
    every AgentWakeup row created for the competitor that UTC day
    (resetting the cap at 8pm ET, mid-session) including forced/safety-net
    rows the code's own docstrings said were exempt -- that bug produced a
    real 17-wakeup runaway loop for two competitors on 2026-08-25 (see
    section 4's own per-day breakdown for any historical day it happened
    on). A nonzero "capped" count under the CURRENT rule means an agent is
    genuinely burning through its own scheduling budget, not an artifact
    of the old bug.

 5. FAILED TURNS -- as of 2026-08-29, run_agent_wakeup only skips
    _ensure_future_wakeup_scheduled for a TERMINAL provider error
    (league.services.provider_errors.is_terminal -- a revoked API key, an
    exhausted account), and pauses the competitor instead (Competitor.
    is_paused) so the failure is visible in the admin/competitor list
    rather than silent. A non-terminal failure now gets the same 15-minute
    safety-net retry a budget-exhausted COMPLETED turn does. Before the
    fix, EVERY failure -- transient or not -- broke the self-scheduling
    chain until the next forced check-in, which is what let two dead
    provider credentials produce 5 competitor-days of blackout. This
    section still reports the real elapsed time to each competitor's next
    turn, which is now mostly useful for confirming a TERMINAL failure's
    pause actually took (elapsed time should run until an operator
    intervenes) versus a transient one recovering within ~15 minutes.

 6. PAST-DATED SCHEDULES -- as of 2026-08-29, schedule_wakeup rejects a
    run_at that's already in the past outright (returns "Not scheduled"
    rather than accepting it), so this section should read 0 going
    forward for anything created after the fix; a nonzero count here now
    localizes to rows created before it shipped. Before the fix, an agent
    that miscomputed a time got apply_async with an eta already past,
    which fired almost immediately -- a burst of turns rather than a gap.
"""

import json
import zoneinfo
from collections import defaultdict
from datetime import datetime, time, timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from league.models import AgentTurn, AgentWakeup, Competitor, ForcedCheckin
from league.services import wakeup_spacing
from league.tools import LEAGUE_MAX_WAKEUPS_PER_DAY

EASTERN = zoneinfo.ZoneInfo("America/New_York")

# The window a competitor is expected to be awake in at least occasionally
# -- 8am (the default forced news scan) through the 4pm close. Gaps are
# measured inside this only; an overnight gap is expected, not a symptom.
SESSION_START = time(8, 0)
SESSION_END = time(16, 0)

# A gap this long inside one session is worth surfacing -- roughly "missed
# either the open or the close entirely."
GAP_ALERT = timedelta(hours=3)

# Reason-text prefixes written by league.tasks.dispatch_forced_checkins and
# _ensure_future_wakeup_scheduled respectively -- imported from
# league.services.wakeup_spacing (the single definition both writers and
# every reader, including LEAGUE_MAX_WAKEUPS_PER_DAY's own counting logic,
# now share) rather than duplicated here. Matching on the text is how
# those two sources are told apart from the agent's own schedule_wakeup
# calls -- AgentWakeup has no `source` field to filter on (AgentTurn.trigger
# records it, but only after the turn runs, and never for a wake-up that
# was lost before firing, which is exactly the case section 2 is about).
FORCED_REASON_PREFIX = wakeup_spacing.FORCED_CHECKIN_REASON_PREFIX
SAFETY_NET_REASON_PREFIX = wakeup_spacing.SAFETY_NET_REASON_PREFIX

# core/settings.py: CELERY_BEAT_SCHEDULE["league-forced-checkins"]. No
# longer load-bearing for section 3's own matching logic (dispatch_forced_
# checkins fires any not-yet-fired-today slot whose time has passed, not
# an exact tick match, since the 2026-08-29 fix) -- kept only to label the
# "fired late" threshold in that section's output.
BEAT_TICK_MINUTES = 5

MAX_ROWS_PRINTED = 25


def _et(dt):
    return dt.astimezone(EASTERN)


def _fmt(dt):
    return _et(dt).strftime("%Y-%m-%d %I:%M%p").replace(" 0", " ") + " ET"


def _fmt_td(td):
    total = int(td.total_seconds() // 60)
    hours, minutes = divmod(max(0, total), 60)
    if hours and minutes:
        return f"{hours}h{minutes:02d}m"
    return f"{hours}h" if hours else f"{minutes}m"


def _weekdays(start_date, end_date):
    day = start_date
    while day <= end_date:
        if day.weekday() < 5:
            yield day
        day += timedelta(days=1)


def _day_bounds(day):
    """UTC-aware [start, end) covering one America/New_York calendar day --
    the DB stores UTC, and every "per day" question here is an ET-day
    question (the cap in schedule_wakeup notably is NOT, which is itself
    reported in section 4)."""
    start = datetime.combine(day, time(0, 0), tzinfo=EASTERN)
    return start, start + timedelta(days=1)


class Command(BaseCommand):
    help = "Read-only report on AI competitor wake-up reliability (gaps, orphans, missed forced check-ins, cap pressure)."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=14, help="How many days back to analyze (default 14).")
        parser.add_argument("--json", dest="json_path", default="", help="Also write the full findings to this path.")

    def handle(self, *args, **options):
        days = options["days"]
        now = timezone.now()
        now_et = _et(now)
        window_start = now - timedelta(days=days)
        first_day = _et(window_start).date()
        today = now_et.date()

        competitors = list(
            Competitor.objects.filter(kind=Competitor.KIND_AI).select_related("competition").order_by("name")
        )

        self.stdout.write("=" * 72)
        self.stdout.write(f"WAKE-UP DIAGNOSTICS -- last {days} days, as of {_fmt(now)}")
        self.stdout.write(f"{len(competitors)} AI competitor(s)")
        self.stdout.write("=" * 72)

        report = {
            "generated_at": now.isoformat(),
            "window_days": days,
            "competitors": [
                {
                    "id": c.id, "name": c.name, "competition": c.competition.name,
                    "is_paused": c.is_paused, "model": getattr(c.assistant_model, "model_id", None),
                }
                for c in competitors
            ],
        }

        report["status_counts"] = self._section_status_counts(window_start)
        report["coverage"] = self._section_coverage(competitors, first_day, today, now_et)
        report["orphans"] = self._section_orphans(now)
        report["forced_checkins"] = self._section_forced_checkins(first_day, today, now_et)
        report["daily_cap"] = self._section_daily_cap(competitors, first_day, today)
        report["failures"] = self._section_failures(window_start)
        report["past_dated"] = self._section_past_dated(window_start)

        if options["json_path"]:
            with open(options["json_path"], "w", encoding="utf-8") as fh:
                json.dump(report, fh, indent=2, default=str)
            self.stdout.write(f"\nFull findings written to {options['json_path']}")

    # ------------------------------------------------------------------ 0

    def _section_status_counts(self, window_start):
        counts = {}
        for status, _label in AgentWakeup.STATUS_CHOICES:
            counts[status] = AgentWakeup.objects.filter(created_at__gte=window_start, status=status).count()
        turns = AgentTurn.objects.filter(created_at__gte=window_start).count()
        self.stdout.write("\n--- 0. TOTALS (rows CREATED in window) ---")
        self.stdout.write("  AgentWakeup: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
        self.stdout.write(f"  AgentTurn:   {turns}")
        return {"wakeups": counts, "turns": turns}

    # ------------------------------------------------------------------ 1

    def _section_coverage(self, competitors, first_day, today, now_et):
        """The symptom, measured directly: per competitor per trading day,
        blackouts and the largest in-session gap between turns. Read this
        first -- a bug in a later section only matters if its dates line up
        with a blackout here."""
        self.stdout.write("\n--- 1. SESSION COVERAGE (8am-4pm ET, weekdays) ---")
        findings = []
        for competitor in competitors:
            comp = competitor.competition
            turns_by_day = defaultdict(list)
            for created_at in AgentTurn.objects.filter(
                competitor=competitor, created_at__gte=datetime.combine(first_day, time(0, 0), tzinfo=EASTERN),
            ).values_list("created_at", flat=True):
                turns_by_day[_et(created_at).date()].append(_et(created_at))

            blackouts, wide_gaps, day_rows = [], [], []
            for day in _weekdays(first_day, today):
                if day < comp.start_date or day > comp.end_date:
                    continue
                open_at = datetime.combine(day, SESSION_START, tzinfo=EASTERN)
                close_at = datetime.combine(day, SESSION_END, tzinfo=EASTERN)
                if open_at > now_et:
                    continue
                close_at = min(close_at, now_et)
                if close_at - open_at < timedelta(hours=1):
                    continue  # today, before the session has meaningfully started

                in_session = sorted(t for t in turns_by_day.get(day, []) if open_at <= t <= close_at)
                points = [open_at, *in_session, close_at]
                max_gap = max(b - a for a, b in zip(points, points[1:]))
                day_rows.append({"date": str(day), "turns": len(in_session), "max_gap_minutes": int(max_gap.total_seconds() // 60)})
                if not in_session:
                    blackouts.append(str(day))
                elif max_gap >= GAP_ALERT:
                    wide_gaps.append((str(day), max_gap))

            findings.append({
                "competitor": competitor.name, "competitor_id": competitor.id,
                "is_paused": competitor.is_paused, "blackout_days": blackouts,
                "wide_gap_days": [{"date": d, "max_gap_minutes": int(g.total_seconds() // 60)} for d, g in wide_gaps],
                "days": day_rows,
            })

            paused = " [PAUSED]" if competitor.is_paused else ""
            sessions = len(day_rows)
            self.stdout.write(f"  {competitor.name}{paused} -- {sessions} session(s) analyzed")
            if blackouts:
                self.stdout.write(self.style.ERROR(f"      NO TURNS AT ALL on {len(blackouts)} day(s): {', '.join(blackouts)}"))
            if wide_gaps:
                shown = ", ".join(f"{d} ({_fmt_td(g)})" for d, g in wide_gaps[:6])
                more = f" (+{len(wide_gaps) - 6} more)" if len(wide_gaps) > 6 else ""
                self.stdout.write(self.style.WARNING(f"      gap >= {_fmt_td(GAP_ALERT)} on {len(wide_gaps)} day(s): {shown}{more}"))
            if not blackouts and not wide_gaps and day_rows:
                self.stdout.write(self.style.SUCCESS("      no blackouts, no wide gaps"))
        return findings

    # ------------------------------------------------------------------ 2

    def _section_orphans(self, now):
        """STATUS_SCHEDULED with run_at already past. As of 2026-08-29,
        league.tasks.sweep_overdue_wakeups (a Beat task, every 10 minutes)
        recovers anything overdue past its own grace window -- so a row
        showing up here for more than ~20 minutes means either that Beat
        task isn't running, or something is wrong with the row itself
        (e.g. its competitor was deleted). Before the fix, nothing ever
        picked these back up at all."""
        self.stdout.write("\n--- 2. ORPHANED WAKE-UPS (scheduled, run_at in the past) ---")
        orphans = list(
            AgentWakeup.objects.filter(status=AgentWakeup.STATUS_SCHEDULED, run_at__lt=now)
            .select_related("competitor").order_by("run_at")
        )
        if not orphans:
            self.stdout.write(self.style.SUCCESS("  none -- every past-due wake-up ran or was swept"))
            return []

        self.stdout.write(self.style.ERROR(f"  {len(orphans)} orphaned wake-up(s)"))
        buckets = defaultdict(int)
        for w in orphans:
            overdue = now - w.run_at
            buckets["<1h" if overdue < timedelta(hours=1) else "1-24h" if overdue < timedelta(days=1) else ">24h"] += 1
        self.stdout.write("  overdue by: " + ", ".join(f"{k}={v}" for k, v in buckets.items()))
        self.stdout.write(
            "  (rows overdue >20min mean sweep_overdue_wakeups isn't running: "
            "journalctl -u finance-celery-beat | grep sweep_overdue_wakeups. "
            "Also compare against worker restart times: journalctl -u finance-celery-worker | grep -i 'warm shutdown\\|ready')"
        )
        for w in orphans[:MAX_ROWS_PRINTED]:
            kind = self._reason_kind(w.reason)
            self.stdout.write(f"      #{w.id} {w.competitor.name}: due {_fmt(w.run_at)} [{kind}] {(w.reason or '')[:70]}")
        if len(orphans) > MAX_ROWS_PRINTED:
            self.stdout.write(f"      ... +{len(orphans) - MAX_ROWS_PRINTED} more (see --json)")

        return [
            {
                "id": w.id, "competitor": w.competitor.name, "run_at": w.run_at.isoformat(),
                "run_at_et": _fmt(w.run_at), "created_at": w.created_at.isoformat(),
                "kind": self._reason_kind(w.reason), "reason": w.reason,
            }
            for w in orphans
        ]

    # ------------------------------------------------------------------ 3

    def _section_forced_checkins(self, first_day, today, now_et):
        """Did each guaranteed floor slot actually fire every weekday? Also
        flags rows Beat's */5 tick can never match at all."""
        self.stdout.write("\n--- 3. FORCED CHECK-IN COVERAGE (the guaranteed floor) ---")
        rows = list(ForcedCheckin.objects.select_related("competition").order_by("competition", "hour", "minute"))
        if not rows:
            self.stdout.write(self.style.WARNING("  no ForcedCheckin rows configured -- agents have NO floor at all"))
            return []

        findings = []
        for fc in rows:
            comp = fc.competition
            unreachable = fc.minute % BEAT_TICK_MINUTES != 0
            reason = f"[Forced: {fc.label}] {fc.prompt}"
            expected_competitors = Competitor.objects.filter(
                competition=comp, kind=Competitor.KIND_AI, is_paused=False,
            ).count()

            slot_days, missing, late = [], [], []
            for day in _weekdays(max(first_day, comp.start_date), min(today, comp.end_date)):
                slot_at = datetime.combine(day, time(fc.hour, fc.minute), tzinfo=EASTERN)
                if slot_at > now_et:
                    continue
                start, end = _day_bounds(day)
                fired = list(
                    AgentWakeup.objects.filter(
                        reason=reason, competitor__competition=comp,
                        created_at__gte=start, created_at__lt=end,
                    ).values_list("created_at", flat=True)
                )
                slot_days.append(str(day))
                if not fired:
                    missing.append(str(day))
                    continue
                delay = int((_et(max(fired)) - slot_at).total_seconds() // 60)
                if delay >= BEAT_TICK_MINUTES:
                    late.append({"date": str(day), "delay_minutes": delay})

            label = f"{comp.name} {fc.hour:02d}:{fc.minute:02d} ET -- {fc.label}"
            self.stdout.write(f"  {label} ({expected_competitors} active AI competitor(s))")
            if unreachable:
                self.stdout.write(self.style.ERROR(
                    f"      MINUTE {fc.minute} IS NOT A MULTIPLE OF {BEAT_TICK_MINUTES} -- Beat ticks crontab(minute='*/5'), "
                    "so dispatch_forced_checkins' exact hour/minute match can NEVER fire this row."
                ))
            if missing:
                self.stdout.write(self.style.ERROR(
                    f"      DID NOT FIRE on {len(missing)}/{len(slot_days)} weekday(s): {', '.join(missing[:10])}"
                    + (f" (+{len(missing) - 10} more)" if len(missing) > 10 else "")
                ))
            if late:
                self.stdout.write(self.style.WARNING(f"      fired late on {len(late)} day(s): {late[:5]}"))
            if not unreachable and not missing:
                self.stdout.write(self.style.SUCCESS(f"      fired on all {len(slot_days)} weekday(s)"))

            findings.append({
                "competition": comp.name, "label": fc.label, "hour": fc.hour, "minute": fc.minute,
                "unreachable_by_beat_tick": unreachable, "inject_news": fc.inject_news,
                "news_slot": fc.news_slot, "active_ai_competitors": expected_competitors,
                "weekdays_expected": slot_days, "weekdays_missing": missing, "fired_late": late,
            })
        return findings

    # ------------------------------------------------------------------ 4

    def _section_daily_cap(self, competitors, first_day, today):
        """How much of LEAGUE_MAX_WAKEUPS_PER_DAY each competitor actually
        got to spend on its own scheduling. Grouped by ET calendar day and
        judged on the `own` count alone (wakeup_spacing.wakeups_today_
        count's own rule, the CURRENT/fixed behavior as of 2026-08-29) --
        forced/safety-net rows are still broken out per day for context,
        but no longer counted toward "capped", since they're documented as
        exempt and the counting logic now actually agrees. Before the fix,
        `total` (including exempt rows) was what schedule_wakeup compared
        against the cap, keyed by UTC calendar day -- keep that in mind if
        cross-referencing an incident that predates 2026-08-29 (e.g. the
        2026-08-25 17-wakeup safety-net loop this section was built to
        catch: `total` hit the cap that day, `own` did not)."""
        self.stdout.write(f"\n--- 4. DAILY CAP PRESSURE (cap = {LEAGUE_MAX_WAKEUPS_PER_DAY}/day, counted per ET calendar day, own rows only) ---")
        findings = []
        for competitor in competitors:
            per_day = defaultdict(lambda: {"forced": 0, "safety_net": 0, "own": 0})
            for created_at, reason in AgentWakeup.objects.filter(
                competitor=competitor,
                created_at__gte=datetime.combine(first_day, time(0, 0), tzinfo=EASTERN),
            ).values_list("created_at", "reason"):
                per_day[_et(created_at).date()][self._reason_kind(reason)] += 1

            capped_days, pressured = [], []
            for day, kinds in sorted(per_day.items()):
                total = sum(kinds.values())
                exempt = kinds["forced"] + kinds["safety_net"]
                row = {"et_date": str(day), "total": total, **kinds}
                if kinds["own"] >= LEAGUE_MAX_WAKEUPS_PER_DAY:
                    capped_days.append(row)
                elif kinds["own"] >= LEAGUE_MAX_WAKEUPS_PER_DAY - 2:
                    pressured.append(row)
                elif exempt >= 10:
                    # Not itself a cap violation under the fixed rule, but
                    # this many forced/safety-net rows in one day is its
                    # own anomaly worth a human look (a runaway safety-net
                    # loop, pre-fix, looked exactly like this).
                    pressured.append(row)

            findings.append({
                "competitor": competitor.name, "competitor_id": competitor.id,
                "capped_days": capped_days, "near_cap_or_anomalous_days": pressured,
                "days": [{"et_date": str(d), "total": sum(k.values()), **k} for d, k in sorted(per_day.items())],
            })

            if capped_days:
                self.stdout.write(self.style.ERROR(f"  {competitor.name} -- HIT THE CAP on its OWN scheduling on {len(capped_days)} day(s):"))
                for row in capped_days[:10]:
                    self.stdout.write(
                        f"      {row['et_date']}: own={row['own']} forced={row['forced']} safety_net={row['safety_net']}"
                    )
            elif pressured:
                self.stdout.write(self.style.WARNING(f"  {competitor.name} -- near cap or an anomalous exempt-row day on {len(pressured)} day(s):"))
                for row in pressured[:10]:
                    self.stdout.write(
                        f"      {row['et_date']}: own={row['own']} forced={row['forced']} safety_net={row['safety_net']}"
                    )
            else:
                self.stdout.write(self.style.SUCCESS(f"  {competitor.name} -- never near the cap"))
        return findings

    # ------------------------------------------------------------------ 5

    def _section_failures(self, window_start):
        """A FAILED turn whose error is TERMINAL (league.services.
        provider_errors.is_terminal) pauses the competitor and skips
        _ensure_future_wakeup_scheduled on purpose (as of 2026-08-29) --
        retrying the exact same call would just fail the same way. A
        non-terminal failure gets the usual safety-net retry. Measures
        what each failure actually cost in wall-clock darkness, and
        reads AgentWakeup.error directly (added 2026-08-29) instead of
        requiring a journalctl trip for the message."""
        self.stdout.write("\n--- 5. FAILED WAKE-UPS ---")
        failed = list(
            AgentWakeup.objects.filter(status=AgentWakeup.STATUS_FAILED, run_at__gte=window_start)
            .select_related("competitor").order_by("run_at")
        )
        if not failed:
            self.stdout.write(self.style.SUCCESS("  none"))
            return []

        self.stdout.write(self.style.ERROR(f"  {len(failed)} failed wake-up(s)"))
        findings = []
        for w in failed:
            after = w.completed_at or w.run_at
            next_turn = AgentTurn.objects.filter(
                competitor=w.competitor, created_at__gt=after,
            ).order_by("created_at").values_list("created_at", flat=True).first()
            gap = (next_turn - after) if next_turn else None
            paused_now = w.competitor.is_paused
            findings.append({
                "id": w.id, "competitor": w.competitor.name, "run_at_et": _fmt(w.run_at),
                "kind": self._reason_kind(w.reason),
                "error": w.error, "competitor_currently_paused": paused_now,
                "dark_minutes": int(gap.total_seconds() // 60) if gap else None,
                "reason": (w.reason or "")[:200],
            })
            gap_str = _fmt_td(gap) if gap else "STILL DARK (no turn since)"
            pause_flag = " [competitor is currently PAUSED]" if paused_now else ""
            self.stdout.write(
                f"      #{w.id} {w.competitor.name} {_fmt(w.run_at)}{pause_flag}: {w.error or '(no error recorded -- pre-2026-08-29 row, check server logs)'}"
            )
            self.stdout.write(f"          -> next turn after {gap_str}")
        return findings

    # ------------------------------------------------------------------ 6

    def _section_past_dated(self, window_start):
        """schedule_wakeup never validates run_at is in the future, so an
        agent that miscomputes a time gets an eta already in the past and
        fires instantly -- a burst, not a gap, but it burns the cap."""
        self.stdout.write("\n--- 6. PAST-DATED SCHEDULES (run_at was already in the past when created) ---")
        rows = [
            w for w in AgentWakeup.objects.filter(created_at__gte=window_start)
            .select_related("competitor").order_by("created_at")
            # Forced check-ins legitimately set run_at=now at creation, so
            # only a meaningful backdate counts here.
            if not (w.reason or "").startswith(FORCED_REASON_PREFIX) and w.run_at < w.created_at - timedelta(minutes=1)
        ]
        if not rows:
            self.stdout.write(self.style.SUCCESS("  none"))
            return []
        self.stdout.write(self.style.WARNING(f"  {len(rows)} wake-up(s) scheduled into the past -- these fired immediately"))
        for w in rows[:MAX_ROWS_PRINTED]:
            behind = _fmt_td(w.created_at - w.run_at)
            self.stdout.write(f"      #{w.id} {w.competitor.name}: asked for {_fmt(w.run_at)}, {behind} before it was created")
        return [
            {
                "id": w.id, "competitor": w.competitor.name, "run_at_et": _fmt(w.run_at),
                "created_at_et": _fmt(w.created_at),
                "behind_minutes": int((w.created_at - w.run_at).total_seconds() // 60),
                "reason": (w.reason or "")[:200],
            }
            for w in rows
        ]

    # ------------------------------------------------------------------

    @staticmethod
    def _reason_kind(reason):
        reason = reason or ""
        if reason.startswith(FORCED_REASON_PREFIX):
            return "forced"
        if reason.startswith(SAFETY_NET_REASON_PREFIX):
            return "safety_net"
        return "own"
