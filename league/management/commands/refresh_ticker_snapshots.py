"""One-off/ad-hoc TickerSnapshot backfill. Run this once right after
deploying the persisted-quote redesign (league/services/valuation.py's
module docstring) -- the table starts empty, and every open Position
would otherwise mark at avg_cost (scoreboard/return% flat at 0 for
everyone) until the next market-hours Beat tick
(league.tasks.refresh_open_position_quotes), which itself only runs
9:30am-4:00pm ET Mon-Fri -- deploying outside that window means a long
wait for real numbers without this. Also just generally useful any time
you want a refresh right now outside the normal Beat cadence: unlike that
task, this command has no market-hours gate.

Defaults to every open Position across every competition; --competition
<id> narrows to one (e.g. right after seed_league creates one and you
want real marks before the first wake-up)."""

from django.core.management.base import BaseCommand, CommandError

from league.models import Competition, Position
from league.services import valuation


class Command(BaseCommand):
    help = "Live-refetch and persist a TickerSnapshot for every open Position (or one competition's)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--competition", type=int, default=None,
            help="Limit to one Competition's positions (by id). Omit to refresh every open position.",
        )

    def handle(self, *args, **options):
        positions = Position.objects.all()
        if options["competition"] is not None:
            try:
                competition = Competition.objects.get(id=options["competition"])
            except Competition.DoesNotExist:
                raise CommandError(f"No competition with id {options['competition']}.")
            positions = positions.filter(competitor__competition=competition)

        count = positions.count()
        if count == 0:
            self.stdout.write("No open positions to refresh.")
            return

        self.stdout.write(f"Refreshing quotes for {count} open position(s)...")
        valuation.refresh_quotes_for_positions(positions)
        self.stdout.write(self.style.SUCCESS("Done."))
