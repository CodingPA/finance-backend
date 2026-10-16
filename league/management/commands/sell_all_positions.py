"""Close every open Position for a competitor, an entire competition, or
the whole app, by submitting real closing trades through
execution.submit_trade() -- never deletes/edits Position rows directly,
so cash_balance and the Trade audit trail stay exactly as consistent as
any other path through this app (a "sold" position is a real filled Trade
you can see in the history, not a row that silently vanished). Built for
"I'm just testing, not actually running the competition, flatten
everything" -- see also Competitor.is_paused for stopping an AI's own
wake-up loop (that's the cost center; this command doesn't touch it, and
a paused competitor's positions just sit there until you run this or
unpause and let the agent close them itself).

Short option positions (the leg of a covered call/cash-covered put) are
closed BEFORE the long stock/other positions they might be covering --
matches execution.py's own _covered_call_available check, which rejects
selling shares still backing an open covered call -- so this sorts
is_short=True first rather than processing in whatever order the
queryset happens to return.

An option Position whose expiration has already passed can't go through
submit_trade at all -- it refuses every trade on an expired contract,
opening or closing (league.services.execution's own module docstring).
Those go through execution.settle_expired_position instead, the same
settlement league.tasks.settle_expired_options runs nightly, fetched
here on demand rather than waiting for that batch -- this command exists
for "flatten everything now," and an expired covered call otherwise
leaves its covering shares permanently stuck too (selling them is
rejected as stranding the call's collateral, and the call itself can't
be closed manually once expired)."""

from django.core.management.base import BaseCommand, CommandError

from league.models import Competition, Competitor, Position, Trade
from league.services import market_hours
from league.services.execution import closing_price_for_settlement, settle_expired_position, submit_trade

DEFAULT_THESIS = "Closed via sell_all_positions management command."


class Command(BaseCommand):
    help = "Close every open position for one competitor, one competition, or everything, via real closing trades."

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--competitor", type=int, help="Close every open position for one Competitor (by id).")
        group.add_argument(
            "--competition", type=int, help="Close every open position for every Competitor in one Competition (by id).",
        )
        group.add_argument("--all", action="store_true", help="Close every open position, across every competition.")
        parser.add_argument("--thesis", type=str, default=DEFAULT_THESIS, help="Thesis text recorded on each closing Trade.")
        parser.add_argument("--dry-run", action="store_true", help="Report what would be closed without submitting any trades.")

    def handle(self, *args, **options):
        thesis = options["thesis"]
        dry_run = options["dry_run"]

        positions = Position.objects.select_related("competitor")
        if options["competitor"] is not None:
            try:
                competitor = Competitor.objects.get(id=options["competitor"])
            except Competitor.DoesNotExist:
                raise CommandError(f"No competitor with id {options['competitor']}.")
            positions = positions.filter(competitor=competitor)
        elif options["competition"] is not None:
            try:
                competition = Competition.objects.get(id=options["competition"])
            except Competition.DoesNotExist:
                raise CommandError(f"No competition with id {options['competition']}.")
            positions = positions.filter(competitor__competition=competition)
        # else --all: no filter, every open Position everywhere.

        # Short legs before the long positions they might cover -- see
        # module docstring. Stable sort: within each bucket, order doesn't
        # matter (a cash-covered put has no `covers` FK to anything, and
        # multiple short calls on the same stock can close in any order
        # relative to each other).
        positions = sorted(positions, key=lambda p: not p.is_short)

        if not positions:
            self.stdout.write("No open positions to close.")
            return

        closed = failed = 0
        for position in positions:
            label = self._label(position)
            is_expired_option = (
                position.instrument_type != Position.INSTRUMENT_STOCK
                and market_hours.option_expired(position.expiration)
            )

            if dry_run:
                if is_expired_option:
                    self.stdout.write(f"[dry-run] would settle (expired) {label}")
                else:
                    side = Trade.SIDE_BUY_TO_CLOSE if position.is_short else Trade.SIDE_SELL
                    self.stdout.write(f"[dry-run] would {side} {label}")
                continue

            if is_expired_option:
                closing_price = closing_price_for_settlement(position.ticker)
                if closing_price is None:
                    failed += 1
                    self.stderr.write(self.style.WARNING(
                        f"Failed to settle {label}: no live or closing quote available for {position.ticker}.",
                    ))
                    continue
                trades = settle_expired_position(position, closing_price)
                closed += 1
                self.stdout.write(f"Settled {label} @ {closing_price} closing price ({len(trades)} trade(s))")
                continue

            side = Trade.SIDE_BUY_TO_CLOSE if position.is_short else Trade.SIDE_SELL
            trade = submit_trade(
                position.competitor, position.ticker, side, position.instrument_type, position.quantity,
                thesis, strike=position.strike, expiration=position.expiration,
                # This command means "flatten everything right now" -- an
                # operator override, not a competitor's own order, so it must
                # never queue even if this competition restricts after-hours
                # trading (see submit_trade's own docstring on force_immediate).
                force_immediate=True,
            )
            if trade.status == Trade.STATUS_FILLED:
                closed += 1
                self.stdout.write(f"Closed {label} @ {trade.fill_price}")
            else:
                failed += 1
                self.stderr.write(self.style.WARNING(f"Failed to close {label}: {trade.rejection_reason}"))

        if dry_run:
            self.stdout.write(self.style.SUCCESS(f"Would close {len(positions)} position(s)."))
        else:
            verb = self.style.SUCCESS if failed == 0 else self.style.WARNING
            self.stdout.write(verb(f"Closed {closed} position(s), {failed} failed."))

    @staticmethod
    def _label(position: Position) -> str:
        base = f"{position.competitor.name}: {position.quantity} {position.ticker}"
        if position.instrument_type == Position.INSTRUMENT_STOCK:
            return base
        opt = "C" if position.instrument_type == Position.INSTRUMENT_CALL else "P"
        return f"{base} {position.strike}{opt} exp {position.expiration}"
