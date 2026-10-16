"""Seed ResearchClips from a ticker's latest 10-K and 10-Q using the same
Docling + HybridChunker pipeline validated in spike_filing_chunks.py,
instead of hand-rolled text splitting -- so the search endpoints
(companies/views.py::ClipSearchView)
get tested against the actual chunker this app is committing to, not a
stand-in.

The pipeline itself (fetch, preprocess, parse, table-removal, chunk, embed,
save) lives in companies/services/filing_ingestion.py -- shared with the
assistant app's filing-ingestion tool, which does the same thing for one
specific filing at a time, gated on user confirmation rather than run in
bulk from the CLI.

Still not a schema/task/endpoint of its own -- these are ordinary
ResearchClips, no dedicated filing-chunk model."""

from django.core.management.base import BaseCommand, CommandError

from accounts.models import User
from companies.services import filing_ingestion, sec_edgar
from companies.models import TrackedCompany


class Command(BaseCommand):
    help = (
        "Seed ResearchClips from a ticker's latest 10-K and 10-Q via the real "
        "Docling + HybridChunker pipeline (table-removal included). See this "
        "file's module docstring."
    )

    FORMS = ("10-K", "10-Q")

    def add_arguments(self, parser):
        parser.add_argument("ticker")
        parser.add_argument(
            "--user", required=True,
            help="Email of the (already-existing) user to attach clips to.",
        )
        parser.add_argument(
            "--forms", nargs="+", default=list(self.FORMS),
            help="Which forms' latest filing to seed (default: 10-K 10-Q).",
        )
        parser.add_argument("--max-tokens", type=int, default=1000, help="HybridChunker's max tokens per chunk.")
        parser.add_argument(
            "--keep-tables", action="store_true",
            help="Skip table removal (matches spike_filing_chunks.py's flag) -- chunk tables in too, for comparison.",
        )
        parser.add_argument(
            "--force", action="store_true",
            help="Delete and re-seed if this filing (matched by accession number) was already seeded.",
        )

    def handle(self, *args, **options):
        ticker = options["ticker"].upper()

        try:
            user = User.objects.get(email=options["user"])
        except User.DoesNotExist:
            raise CommandError(f"No user with email {options['user']!r}.")

        try:
            company = sec_edgar.resolve_company(ticker)
        except ValueError as exc:
            raise CommandError(str(exc))

        tracked, created = TrackedCompany.objects.get_or_create(
            user=user, company=company, defaults={"origin": TrackedCompany.ORIGIN_USER}
        )
        if created:
            self.stdout.write(f"{user.email} wasn't tracking {ticker} -- tracking it now.")

        submissions = sec_edgar.get_submissions(company)

        for form in options["forms"]:
            filings = sec_edgar.list_filings(submissions, forms=(form,))
            if not filings:
                self.stdout.write(self.style.WARNING(f"No {form} filings found for {ticker} -- skipping."))
                continue
            self._seed_filing(tracked, ticker, filings[0], options)

    def _seed_filing(self, tracked, ticker, filing, options):
        accession = filing["accession_number"]
        if filing_ingestion.is_ingested(tracked, accession) and not options["force"]:
            self.stdout.write(
                f"{filing['form']} filed {filing['filing_date']} (accession {accession}) "
                "already seeded -- skipping. Use --force to re-seed."
            )
            return

        self.stdout.write(f"\n{filing['form']} filed {filing['filing_date']} (period {filing['report_date']})")
        self.stdout.write(f"  {filing['url']}")
        self.stdout.write("Fetching, preprocessing, parsing, and chunking (Docling)...")

        try:
            created_count = filing_ingestion.ingest_filing(
                tracked, filing,
                max_tokens=options["max_tokens"], keep_tables=options["keep_tables"], force=options["force"],
            )
        except ValueError as exc:
            raise CommandError(str(exc))

        self.stdout.write(self.style.SUCCESS(f"  Seeded {created_count} clips."))
