"""Cancels already-scheduled AgentWakeups that violate the minimum
3-minute spacing league.tools.schedule_wakeup now enforces going forward
(league.services.wakeup_spacing) -- for wakeups created before that check
existed, e.g. the Sol 5.6 Gambling Daytrader triple-booking at 1:20pm ET
found in George's Paper Trading League on 2026-08-13.

Walks each AI competitor's future STATUS_SCHEDULED wakeups in run_at
order, keeping the earliest of any cluster within 3 minutes of each other
and cancelling the rest, then separately cancelling anything within 3
minutes of one of that competitor's own competition's ForcedCheckin floor
slots (league.models) regardless of what else survived. "Cancel" sets
status=STATUS_CANCELLED rather than
deleting the row -- there's no way to pull an already-dispatched
apply_async(eta=...) task back out of the Celery broker, so
run_agent_wakeup checks this status itself and no-ops if it fires anyway
(see that task's docstring)."""

from django.core.management.base import BaseCommand
from django.utils import timezone

from league.models import AgentWakeup, Competitor
from league.services.wakeup_spacing import EASTERN, MIN_WAKEUP_SPACING, nearest_forced_checkin_gap


class Command(BaseCommand):
    help = (
        "Cancel scheduled AgentWakeups within 3 minutes of another scheduled wake-up "
        "or a forced daily check-in (8:00am/9:35am/3:15pm ET)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true", help="Report conflicts without cancelling anything.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        now = timezone.now()
        cancelled = 0

        for competitor in Competitor.objects.filter(kind=Competitor.KIND_AI):
            wakeups = list(
                AgentWakeup.objects.filter(
                    competitor=competitor, status=AgentWakeup.STATUS_SCHEDULED, run_at__gt=now,
                ).order_by("run_at")
            )
            kept_run_at = None
            for wakeup in wakeups:
                forced_gap = nearest_forced_checkin_gap(competitor, wakeup.run_at)
                too_close_to_floor = forced_gap is not None and forced_gap <= MIN_WAKEUP_SPACING
                too_close_to_kept = kept_run_at is not None and (wakeup.run_at - kept_run_at) <= MIN_WAKEUP_SPACING

                if too_close_to_floor or too_close_to_kept:
                    reason = "a forced daily check-in" if too_close_to_floor else "another scheduled wake-up"
                    cancelled += 1
                    self.stdout.write(
                        f"{'[dry-run] would cancel' if dry_run else 'Cancelling'} "
                        f"{competitor.name} wake-up at {wakeup.run_at.astimezone(EASTERN):%Y-%m-%d %I:%M%p ET} "
                        f"-- within 3 minutes of {reason}."
                    )
                    if not dry_run:
                        wakeup.status = AgentWakeup.STATUS_CANCELLED
                        wakeup.completed_at = now
                        wakeup.save(update_fields=["status", "completed_at"])
                else:
                    kept_run_at = wakeup.run_at

        verb = "Would cancel" if dry_run else "Cancelled"
        self.stdout.write(self.style.SUCCESS(f"{verb} {cancelled} wake-up(s)."))
