"""Spike: can an LLM map this app's Mode 1 financial concepts (revenue,
gross profit, net income, operating cash flow, capex, stock-based comp,
cash, debt, shares outstanding) onto whichever XBRL tags a SPECIFIC filer
actually uses, instead of the fixed us-gaap-only tag list companies/
services/screening.py hardcodes today?

Why this exists: a live spot-check across 10 well-known tickers (AAPL,
MSFT, MELI, NU, NBIS, BABA, SHOP, TSM, UL, RIO) found only 3/10 actually
fit "files a 10-K, reports under us-gaap" -- the rest are foreign private
issuers (20-F/6-K) that either report under us-gaap anyway (NBIS, BABA,
SHOP -- a real fixable gap, screening.py's hardcoded form="10-K" check in
presentation.py::annual_values just throws their data away) or under
ifrs-full exclusively (NU, TSM, UL, RIO -- genuinely different tag names
for the same concepts, e.g. ifrs-full:Revenue vs us-gaap:Revenues,
ifrs-full:ProfitLoss vs us-gaap:NetIncomeLoss, no fixed list covers both
without per-taxonomy branching maintained by hand).

This spike measures whether an LLM given just the list of tag NAMES a
filer actually reports (sec_edgar.get_company_facts, already implemented,
never used in the real screening flow) can pick the right ones, and what
that costs per company. Writes nothing to the database -- prints the
mapping, a sanity-check of the mapped tags' actual latest values, and
token/cost usage. The point is to look at real output/cost before
deciding whether this becomes a real cached-per-company step in
screening.py, not to ship anything from this file directly."""

import json
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from openai import OpenAI
from pydantic import BaseModel

from companies.services import sec_edgar

_MODE_1_CONCEPTS = [
    "revenue", "gross_profit", "net_income", "operating_cash_flow",
    "capex", "stock_based_comp", "cash", "debt_current", "debt_noncurrent",
    "shares_outstanding",
]

# Taxonomies to actually consider mapping -- dei/srt/ecd/ffd (seen live
# across the 10-ticker spot-check) are entity-metadata/disclosure-control
# namespaces, never where a financial concept like revenue would live.
_FINANCIAL_TAXONOMY_PRIORITY = ["us-gaap", "ifrs-full"]

_PROMPT_TEMPLATE = """You are mapping standard financial statement concepts onto the exact XBRL \
tag names one specific company actually reports under the "{taxonomy}" taxonomy.

Below is the COMPLETE list of every tag this company has ever reported under that taxonomy. \
For each concept, return the tag name(s) from this exact list that correspond to it -- \
copy the name exactly as given, do not invent or guess a tag that isn't in the list. \
An empty list is correct if nothing in the list matches that concept at all -- do not force a weak match.

Concepts to map:
- revenue: top-line revenue/sales from ordinary operations.
- gross_profit: revenue minus cost of revenue/sales.
- net_income: bottom-line profit or loss for the period (prefer the total attributable to \
the whole entity, not a sub-component like "attributable to noncontrolling interests" alone).
- operating_cash_flow: net cash provided by/used in operating activities.
- capex: cash paid to acquire property, plant, and equipment.
- stock_based_comp: non-cash share-based/stock-based compensation expense.
- cash: cash and cash equivalents (period-end balance, not a cash flow).
- debt_current: current/short-term portion of interest-bearing debt (exclude trade payables, \
lease liabilities unless clearly debt-like borrowings).
- debt_noncurrent: noncurrent/long-term portion of interest-bearing debt.
- shares_outstanding: total common/ordinary shares outstanding (balance-sheet or cover-page \
count, not weighted-average-for-EPS).

Tag names available under "{taxonomy}" for this company:
{tag_list}
"""


class _ConceptMapping(BaseModel):
    revenue: list[str]
    gross_profit: list[str]
    net_income: list[str]
    operating_cash_flow: list[str]
    capex: list[str]
    stock_based_comp: list[str]
    cash: list[str]
    debt_current: list[str]
    debt_noncurrent: list[str]
    shares_outstanding: list[str]


class Command(BaseCommand):
    help = (
        "Spike: LLM-mapped XBRL tag discovery per filer, as a fallback for filers whose "
        "financials this app's fixed us-gaap tag list can't find. Prints the mapping, a "
        "sanity check against real reported values, and token/cost usage. Writes nothing."
    )

    def add_arguments(self, parser):
        parser.add_argument("tickers", nargs="+", help="One or more tickers, e.g. NU NBIS TSM UL RIO.")
        parser.add_argument(
            "--model", default="gpt-5.6-luna",
            help="AssistantModel model_id to use for the mapping call. Default gpt-5.6-luna (cheapest tier).",
        )

    def handle(self, *args, **options):
        if not settings.OPENAI_API_KEY:
            raise CommandError("OPENAI_API_KEY is not set.")

        from assistant.models import AssistantModel
        try:
            model = AssistantModel.objects.get(model_id=options["model"])
        except AssistantModel.DoesNotExist:
            raise CommandError(f"No AssistantModel row for {options['model']!r}.")

        client = OpenAI(api_key=settings.OPENAI_API_KEY)
        total_cost = Decimal("0")

        for ticker in options["tickers"]:
            ticker = ticker.upper()
            self.stdout.write(self.style.SUCCESS(f"\n=== {ticker} ==="))
            try:
                company = sec_edgar.resolve_company(ticker)
            except ValueError as exc:
                self.stdout.write(self.style.ERROR(f"  {exc}"))
                continue

            # get_company_facts returns the raw SEC payload verbatim -- financial
            # taxonomies live under the top-level "facts" key.
            facts_by_taxonomy = sec_edgar.get_company_facts(company).get("facts", {})
            taxonomy = next((t for t in _FINANCIAL_TAXONOMY_PRIORITY if t in facts_by_taxonomy), None)
            if taxonomy is None:
                self.stdout.write(self.style.WARNING(
                    f"  No us-gaap or ifrs-full facts at all -- taxonomies present: {list(facts_by_taxonomy.keys())}"
                ))
                continue

            tag_names = sorted(facts_by_taxonomy[taxonomy].keys())
            self.stdout.write(f"  Taxonomy: {taxonomy} ({len(tag_names)} tags)")

            prompt = _PROMPT_TEMPLATE.format(taxonomy=taxonomy, tag_list="\n".join(f"- {t}" for t in tag_names))
            response = client.responses.parse(model=model.model_id, input=[{"role": "user", "content": prompt}], text_format=_ConceptMapping)
            usage = response.usage
            cost = model.cost_for(usage.input_tokens, usage.output_tokens) if usage else Decimal("0")
            total_cost += cost
            self.stdout.write(
                f"  {usage.input_tokens} input / {usage.output_tokens} output tokens -- ${cost:.6f}"
                if usage else "  (no usage reported)"
            )

            mapping = response.output_parsed
            if mapping is None:
                self.stdout.write(self.style.ERROR("  Model returned no parsed output."))
                continue

            for concept in _MODE_1_CONCEPTS:
                tags = getattr(mapping, concept)
                if not tags:
                    self.stdout.write(f"  {concept:20s} -> (none)")
                    continue
                # Flag a hallucinated tag immediately -- the prompt says "copy exactly,
                # don't invent," but nothing stops the model from ignoring that.
                bad = [t for t in tags if t not in facts_by_taxonomy[taxonomy]]
                if bad:
                    self.stdout.write(self.style.ERROR(f"  {concept:20s} -> {tags}  ** NOT IN TAG LIST: {bad} **"))
                    continue
                # Sanity check: pull the latest reported value for the first mapped tag
                # so a wrong-but-plausible-sounding tag name is visible by eye, not just trusted.
                concept_data = sec_edgar.get_concept(company, tags[0], taxonomy=taxonomy)
                latest = None
                if concept_data and concept_data.get("units"):
                    unit_key = "USD" if "USD" in concept_data["units"] else next(iter(concept_data["units"]))
                    values = concept_data["units"][unit_key]
                    if values:
                        latest = values[-1]
                latest_str = (
                    f"latest: {latest['val']:,} ({unit_key}, FY{latest.get('fy')}, form={latest.get('form')})"
                    if latest else "latest: no data returned"
                )
                self.stdout.write(f"  {concept:20s} -> {tags}  [{latest_str}]")

        self.stdout.write(self.style.SUCCESS(f"\nTotal cost across {len(options['tickers'])} tickers: ${total_cost:.6f}"))
