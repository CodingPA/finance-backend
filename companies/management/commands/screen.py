from django.core.management.base import BaseCommand, CommandError

from companies.services.screening import run_screen


class Command(BaseCommand):
    help = "Run Mode 1 (Screen) for a ticker: fetch + cache EDGAR/FMP data, report the tripwire."

    def add_arguments(self, parser):
        parser.add_argument("ticker")
        parser.add_argument(
            "--max-age-days",
            type=float,
            default=1.0,
            help="Reuse cached data younger than this many days (default 1). Use 0 to force refresh.",
        )

    def handle(self, *args, **options):
        ticker = options["ticker"].upper()
        try:
            result = run_screen(ticker, max_age_days=options["max_age_days"])
        except ValueError as exc:
            raise CommandError(str(exc))

        self.stdout.write(f"{ticker} -> {result.company.name} (CIK {result.company.cik})")
        if result.latest_filing:
            date, url = result.latest_filing
            self.stdout.write(f"Latest filing: {date}  {url}")

        self.stdout.write(f"\nFetched ({len(result.fetched_data_types)}):")
        for dt in result.fetched_data_types:
            self.stdout.write(f"  {dt}")

        if result.missing_tags:
            self.stdout.write(f"\nNot reported by this filer ({len(result.missing_tags)}):")
            for tag in result.missing_tags:
                self.stdout.write(f"  {tag}")

        if result.tripwire_drift is not None:
            self.stdout.write(f"\nTripwire drift: {result.tripwire_drift:.3%}")
            if result.tripwire_triggered:
                self.stderr.write(self.style.ERROR(
                    "WARNING: price * shares_outstanding differs from FMP's marketCap "
                    "beyond tolerance -- check for a units change or schema break."
                ))
        else:
            self.stdout.write("\nTripwire: skipped (missing price/shares/marketCap, likely no FMP_API_KEY)")

        if result.filing_stale_vs_fmp:
            self.stderr.write(self.style.WARNING(
                "NOTE: FMP's shares-float cites a different filing than our latest EDGAR pull -- "
                "our EDGAR data may be stale. Re-run with --max-age-days 0."
            ))
