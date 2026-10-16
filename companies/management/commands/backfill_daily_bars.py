"""One-off backfill for DailyBar (companies/models.py): walks backward from
the most recent completed session, pulling one Polygon grouped-daily call
per trading day (companies/services/polygon.py) until `--days` distinct
sessions have been collected. Needed because companies.tasks.ingest_daily_bars
only ever appends ONE day going forward -- a fresh DailyBar table has no
way to reach 200 days of history without this.

--days defaults to 220 rather than exactly 200: a 20-session buffer against
holidays this command has to walk past anyway (see the loop below -- a
weekday with no session, e.g. Thanksgiving, still costs a wasted API call
and doesn't count toward the target), and against any single day Polygon
itself returns empty for a transient reason. Same "weekday-only, no
exchange holiday calendar" gap as league.services.market_hours and
companies.tasks.ingest_daily_bars -- a holiday just costs one extra call
and is skipped, not an error.

Runtime: one call per trading day at the free tier's rate-limited 12.5s
(companies/services/http.py's PROVIDER_MIN_INTERVAL["polygon"]) -- 220
days is ~46 minutes, plus a handful of extra ~12.5s calls for any holidays
encountered along the way. Safe to re-run: bulk_create's update_conflicts
upserts on (ticker, date), so re-running over an already-backfilled range
just re-writes the same rows rather than duplicating them."""

from datetime import timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from companies.models import DailyBar
from companies.services import polygon

EASTERN = ZoneInfo("America/New_York")
# Calendar-day walk-back cap so a persistently empty provider (bad key,
# outage) fails loudly in minutes instead of looping for hours -- generous
# enough to clear a target's worth of weekends plus a stretch of holidays.
MAX_CALENDAR_DAYS = 400


class Command(BaseCommand):
    help = "Backfill DailyBar with the last N trading days of full-market OHLCV from Polygon's grouped-daily endpoint."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days", type=int, default=220,
            help="Number of distinct trading sessions to backfill (default: 220 -- 200 for a full SMA, plus a holiday buffer).",
        )

    def handle(self, *args, **options):
        if not settings.POLYGON_API_KEY:
            raise CommandError("POLYGON_API_KEY is not set -- nothing to backfill from.")

        target_days = options["days"]
        if target_days < 1:
            raise CommandError("--days must be at least 1.")

        # Start from yesterday, not today -- if this runs mid-session, today's
        # bar isn't closed yet and Polygon won't have a final print for it;
        # companies.tasks.ingest_daily_bars owns today's bar once it closes.
        cursor = timezone.localdate(timezone=EASTERN) - timedelta(days=1)
        calendar_days_walked = 0
        collected = 0
        total_rows = 0

        while collected < target_days:
            if calendar_days_walked > MAX_CALENDAR_DAYS:
                raise CommandError(
                    f"Walked back {calendar_days_walked} calendar days and only found {collected}/{target_days} "
                    "sessions with data -- check POLYGON_API_KEY and network access."
                )

            if cursor.weekday() < 5:
                day = cursor.isoformat()
                bars = polygon.get_grouped_daily(day)
                if bars:
                    objs = [
                        DailyBar(
                            ticker=bar["T"], date=cursor, open=bar["o"], high=bar["h"],
                            low=bar["l"], close=bar["c"], volume=bar["v"],
                        )
                        for bar in bars if "T" in bar
                    ]
                    DailyBar.objects.bulk_create(
                        objs,
                        update_conflicts=True,
                        unique_fields=["ticker", "date"],
                        update_fields=["open", "high", "low", "close", "volume"],
                    )
                    collected += 1
                    total_rows += len(objs)
                    self.stdout.write(f"{day}: {len(objs)} bars  ({collected}/{target_days})")
                else:
                    self.stdout.write(f"{day}: no data (holiday), skipping")

            cursor -= timedelta(days=1)
            calendar_days_walked += 1

        self.stdout.write(self.style.SUCCESS(
            f"Done. {collected} sessions, {total_rows} total bar rows upserted."
        ))
