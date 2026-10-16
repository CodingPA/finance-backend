"""LLM fallback for mapping this app's fixed Mode 1 financial concepts
(companies/services/screening.py's MODE_1_CONCEPTS/REVENUE_TAGS/DEBT_TAGS)
onto whichever XBRL tags a filer actually uses, for filers that fixed
us-gaap tag list finds nothing at all for.

Why this exists: a live spot-check across 10 well-known tickers (AAPL,
MSFT, MELI, NU, NBIS, BABA, SHOP, TSM, UL, RIO) found several real,
large companies (NU, TSM, UL, RIO) report their financials EXCLUSIVELY
under a taxonomy other than us-gaap -- most commonly ifrs-full, with
genuinely different tag names for the same concepts (ifrs-full:Revenue
vs us-gaap:Revenues, ifrs-full:ProfitLoss vs us-gaap:NetIncomeLoss). No
fixed list can cover both without per-taxonomy branching maintained by
hand as new filers/taxonomies show up. companies/management/commands/
spike_xbrl_tag_mapping.py is the original exploratory spike this was
validated against -- kept in the repo as a standalone, re-runnable
sanity check independent of this production path.

Triggered from screening.py::run_screen ONLY when the primary us-gaap
revenue tag lookup finds nothing at all -- the ~30% "standard" case
(10-K/20-F, us-gaap) never reaches this, zero added LLM cost/latency
for the common path. Resolved once per company and cached via a
CompanySnapshot row (data_type=MAPPING_SNAPSHOT_TYPE, versioned in the
key so a future prompt/schema change invalidates old cached mappings by
bumping the suffix) for _MAPPING_CACHE_MAX_AGE_DAYS -- deliberately its
OWN, much longer TTL than get_company_facts' default 1-day cache: a
filer's XBRL tag vocabulary doesn't change year to year, so this is a
one-time cost per filer, not something worth re-paying on every screen
or even every "force refresh" (which is about current prices, not
rediscovering tag names). A concept that fails validation is cached as
absent from the mapping too -- otherwise every screen would re-pay the
LLM call just to fail the same way again.

Guardrails below all trace to real failures the spike surfaced, not
theoretical ones:
- Single best tag per concept, never a list -- a multi-tag answer left
  genuinely ambiguous whether the extras were fallback alternatives or
  components to sum (confirmed live: MELI's stock_based_comp mapped to
  4 different tags with no way to tell which is which).
- USD-only, for every concept except shares_outstanding (whose unit is
  "shares", not a currency at all) -- a mapped tag with no USD-
  denominated entries is treated as unmapped, not silently priced in
  whatever foreign currency it actually reports in (confirmed live:
  TSM's stock_based_comp only had TWD entries for the mapped tag).
- Recency, keyed off `filed` (when the entry hit EDGAR) rather than
  `fy` (fiscal-year label -- confirmed live that NBIS's own data has a
  `fy` that can be AHEAD of the actual period, e.g. fy=2025 on a
  2023-01-01-start period, so `fy` alone is not a trustworthy clock). A
  mapped tag whose most recent filing is older than _MAX_TAG_AGE_DAYS
  is treated as unmapped -- confirmed live: SHOP's shares_outstanding
  mapped to a real, plausible-sounding tag whose only ever value was
  filed back in 2015. A dead tag, not a wrong one.

Known limitation, confirmed live (not yet fixed): a filer whose PRIMARY
reporting currency isn't USD at all gets NOTHING from this fallback, not
a foreign-currency figure -- Unilever (UL) reports entirely in EUR (no
USD unit exists for any of its financial tags at all, not a RUB-vs-USD
choice the way NBIS was), so every currency concept fails the USD-only
guardrail above and the mapping resolves to {} (or close to it -- UL's
only survivor was shares_outstanding, a non-currency concept, and even
that came back empty downstream because UL's share count is tagged
exclusively via 6-K filings, never a 20-F with fp="FY", so annual_values()
correctly finds nothing to extract from it either). This is the
conservative, fail-safe outcome (no data beats wrong-currency data
silently labeled as dollars) rather than a bug -- fixing it for real
would need either live currency conversion or a schema change to carry
a per-concept currency label safely through build_report()'s cross-
metric math (ps_trailing divides USD market cap by whatever `revenue`
holds; that's only valid today because it's implicitly always USD)."""

from datetime import date, timedelta

from django.conf import settings
from django.utils import timezone
from openai import OpenAI
from pydantic import BaseModel

from companies.models import Company, CompanySnapshot

from . import sec_edgar

MAPPING_SNAPSHOT_TYPE = "ai_tag_mapping:v1"
_MAPPING_CACHE_MAX_AGE_DAYS = 365
# ~2 fiscal years -- long enough that a filer between annual filings
# isn't falsely flagged stale, short enough to catch a genuinely
# abandoned tag (see SHOP's 2015 case in the module docstring).
_MAX_TAG_AGE_DAYS = 730
_LUNA_MODEL_ID = "gpt-5.6-luna"

# Entity-metadata/disclosure-control namespaces, never where a financial
# concept like revenue would live -- confirmed live across the 10-ticker
# spot-check (dei, srt, ecd, ffd all showed up, none of them financial).
# us-gaap is excluded too: this module only ever runs when us-gaap
# already came back empty, so it's never itself a fallback candidate.
_NON_FINANCIAL_TAXONOMIES = {"dei", "srt", "ecd", "ffd", "us-gaap"}

# presentation.py's own field-name vocabulary (see its _FIELD_NAMES) --
# kept in sync with it deliberately, not re-derived, so a mapped concept
# name always lines up with the field build_report() actually looks for.
_FALLBACK_CONCEPTS = [
    "revenue", "gross_profit", "net_loss", "operating_cash_flow", "capex",
    "stock_based_comp", "cash", "debt_current", "debt_noncurrent", "shares_outstanding",
]
# Every concept above except shares_outstanding is a currency amount --
# shares_outstanding's unit is "shares", which never has a "USD" key to
# require in the first place.
_CURRENCY_CONCEPTS = set(_FALLBACK_CONCEPTS) - {"shares_outstanding"}

_PROMPT_TEMPLATE = """You are mapping standard financial statement concepts onto the exact XBRL \
tag names one specific company actually reports under the "{taxonomy}" taxonomy.

Below is the COMPLETE list of every tag this company has ever reported under that taxonomy. \
For each concept, return the SINGLE tag name from this exact list that best corresponds to it -- \
copy the name exactly as given, do not invent or guess a tag that isn't in the list, and pick only \
ONE tag per concept even if several seem related. Return null for a concept if nothing in the list \
is a genuine match -- do not force a weak match.

Concepts to map:
- revenue: top-line revenue/sales from ordinary operations.
- gross_profit: revenue minus cost of revenue/sales.
- net_loss: bottom-line profit or loss for the period (prefer the total attributable to the whole \
entity, not a sub-component like "attributable to noncontrolling interests" alone).
- operating_cash_flow: net cash provided by/used in operating activities.
- capex: cash paid to acquire property, plant, and equipment.
- stock_based_comp: non-cash share-based/stock-based compensation expense (the single aggregate \
figure, not a sub-component like just restricted stock or just options if a combined total also exists).
- cash: cash and cash equivalents (period-end balance, not a cash flow).
- debt_current: current/short-term portion of interest-bearing debt/borrowings (exclude trade \
payables, lease liabilities unless clearly debt-like borrowings).
- debt_noncurrent: noncurrent/long-term portion of interest-bearing debt/borrowings.
- shares_outstanding: total common/ordinary shares outstanding (balance-sheet count, not \
weighted-average-for-EPS).

Tag names available under "{taxonomy}" for this company:
{tag_list}
"""


class _ConceptMapping(BaseModel):
    revenue: str | None
    gross_profit: str | None
    net_loss: str | None
    operating_cash_flow: str | None
    capex: str | None
    stock_based_comp: str | None
    cash: str | None
    debt_current: str | None
    debt_noncurrent: str | None
    shares_outstanding: str | None


def _primary_fallback_taxonomy(facts_by_taxonomy: dict) -> str | None:
    """Whichever non-financial-metadata taxonomy has the most tags, or
    None if this filer has nothing but us-gaap/dei/etc. Picking "most
    tags" rather than a hardcoded taxonomy name (e.g. always "ifrs-full")
    is deliberate -- generalizes to a taxonomy this app hasn't seen yet
    without a code change, at the cost of no guarantee it's the RIGHT
    one for a filer using two non-us-gaap taxonomies at once (not
    observed live in the 10-ticker spot-check -- every non-us-gaap filer
    there had exactly one financial taxonomy)."""
    candidates = {
        taxonomy: len(tags) for taxonomy, tags in facts_by_taxonomy.items()
        if taxonomy not in _NON_FINANCIAL_TAXONOMIES
    }
    return max(candidates, key=candidates.get) if candidates else None


def _passes_guardrails(concept_data: dict, *, require_usd: bool) -> bool:
    """USD-only (for currency concepts) and recency, both against the
    UNFILTERED entry list (every form/fp this tag has ever been reported
    under) -- "is this tag still alive at all" is a different question
    from "does it have a usable annual FY figure," which is
    presentation.py's annual_values()' job, not this validation's."""
    units = concept_data.get("units") or {}
    if require_usd and "USD" not in units:
        return False
    unit_key = "USD" if "USD" in units else next(iter(units), None)
    if unit_key is None:
        return False
    entries = units[unit_key]
    filed_dates = [e["filed"] for e in entries if e.get("filed")]
    if not filed_dates:
        return False
    latest_filed = date.fromisoformat(max(filed_dates))
    return (date.today() - latest_filed).days <= _MAX_TAG_AGE_DAYS


def apply_fallback_mapping(company: Company, max_age_days: float = _MAPPING_CACHE_MAX_AGE_DAYS) -> None:
    """Resolves (with its own long-lived caching) which XBRL tag maps to
    each _FALLBACK_CONCEPTS entry for this company's primary non-us-gaap
    taxonomy, and fetches+caches each validated tag's concept data via
    the ordinary sec_edgar.get_concept (so presentation.py's existing
    concept() lookups find it with no changes needed there beyond
    knowing where to look). Writes a CompanySnapshot row regardless of
    outcome -- {"taxonomy": None, "mapping": {}} if this filer has no
    non-us-gaap financial taxonomy at all, so a second call within
    max_age_days is a cache hit either way, not a repeated no-op probe.

    No-ops (does NOT cache a negative result) if OPENAI_API_KEY or the
    gpt-5.6-luna AssistantModel row isn't configured -- that's a
    deployment misconfiguration to fix, not "this filer has no mapping,"
    and shouldn't get stuck cached as a permanent failure once fixed."""
    cached = CompanySnapshot.objects.filter(company=company, data_type=MAPPING_SNAPSHOT_TYPE).first()
    if cached and (timezone.now() - cached.fetched_at) <= timedelta(days=max_age_days):
        return

    facts_by_taxonomy = sec_edgar.get_company_facts(company).get("facts", {})
    taxonomy = _primary_fallback_taxonomy(facts_by_taxonomy)
    if taxonomy is None:
        CompanySnapshot.objects.update_or_create(
            company=company, data_type=MAPPING_SNAPSHOT_TYPE,
            defaults={"raw_json": {"taxonomy": None, "mapping": {}}},
        )
        return

    if not settings.OPENAI_API_KEY:
        return

    from assistant.models import AssistantModel
    try:
        model = AssistantModel.objects.get(model_id=_LUNA_MODEL_ID)
    except AssistantModel.DoesNotExist:
        return

    tag_names = sorted(facts_by_taxonomy[taxonomy].keys())
    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    prompt = _PROMPT_TEMPLATE.format(taxonomy=taxonomy, tag_list="\n".join(f"- {t}" for t in tag_names))
    response = client.responses.parse(model=model.model_id, input=[{"role": "user", "content": prompt}], text_format=_ConceptMapping)

    resolved: dict[str, str] = {}
    parsed = response.output_parsed
    if parsed is not None:
        for concept_name in _FALLBACK_CONCEPTS:
            tag = getattr(parsed, concept_name)
            if not tag or tag not in facts_by_taxonomy[taxonomy]:
                continue  # null answer, or a hallucinated tag not actually in the list given
            concept_data = sec_edgar.get_concept(company, tag, taxonomy=taxonomy)
            if concept_data is None:
                continue
            if not _passes_guardrails(concept_data, require_usd=concept_name in _CURRENCY_CONCEPTS):
                continue
            resolved[concept_name] = tag

    CompanySnapshot.objects.update_or_create(
        company=company, data_type=MAPPING_SNAPSHOT_TYPE,
        defaults={"raw_json": {"taxonomy": taxonomy, "mapping": resolved}},
    )
