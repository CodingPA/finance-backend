"""Mode 1 (Screen), per docs/overview.md's field table -- the exact set:
revenue, gross profit, net loss, OCF, capex, SBC, cash, total debt, shares
(cover + balance), price + market cap, the FMP shares tracker, and the
tripwire. Deliberately excludes segment revenue, backlog, and P/S forward --
overview.md removed those from the screen on purpose ("they don't exist
uniformly... can't be screened, only researched").

run_screen() only fetches and caches (via CompanySnapshot, through
sec_edgar.py / fmp.py). It does not compute or store derived values (margin
%, FCF, growth %, P/S, runway) -- those are trivially recomputable from the
cached raw snapshots, so they're computed at read time by the API layer
(Phase 3), not duplicated and risking going stale here.
"""

from dataclasses import dataclass, field

from companies.models import Company

from . import fmp, sec_edgar, xbrl_tag_mapping

FMP_SANITY_TOLERANCE = 0.01

# (taxonomy, tag) -- the plain, single-tag Mode 1 fields. Revenue and debt
# are handled separately below (fallback tags / summed tags respectively).
MODE_1_CONCEPTS = [
    ("us-gaap", "GrossProfit"),
    ("us-gaap", "NetIncomeLoss"),
    ("us-gaap", "NetCashProvidedByUsedInOperatingActivities"),
    ("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment"),
    ("us-gaap", "ShareBasedCompensation"),
    ("us-gaap", "CashAndCashEquivalentsAtCarryingValue"),
]

# Revenue tag varies by filer and by era (pre/post ASC 606) -- try in order,
# keep the first that's actually reported.
REVENUE_TAGS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
]

# Total debt = current + noncurrent, per overview.md ("EDGAR debt tags,
# current + noncurrent summed").
DEBT_TAGS = ["LongTermDebtCurrent", "LongTermDebtNoncurrent"]


@dataclass
class ScreenResult:
    company: Company
    fetched_data_types: list[str] = field(default_factory=list)
    missing_tags: list[str] = field(default_factory=list)
    tripwire_drift: float | None = None
    tripwire_triggered: bool = False
    filing_stale_vs_fmp: bool = False
    latest_filing: tuple[str, str] | None = None


def run_screen(ticker: str, max_age_days: float = 1.0) -> ScreenResult:
    company = sec_edgar.resolve_company(ticker, max_age_days)
    result = ScreenResult(company=company)

    submissions = sec_edgar.get_submissions(company, max_age_days)
    result.latest_filing = sec_edgar.latest_filing(submissions)
    result.fetched_data_types.append("submissions")

    # Shares: two authoritative EDGAR sources, kept separate on purpose --
    # cover-page vs balance-sheet dates legitimately differ (dilution
    # between the two dates is itself a signal).
    _fetch_concept(company, "dei", "EntityCommonStockSharesOutstanding", max_age_days, result)
    _fetch_concept(company, "us-gaap", "CommonStockSharesOutstanding", max_age_days, result)

    # Revenue: first reported tag wins.
    revenue_found = False
    for tag in REVENUE_TAGS:
        data = sec_edgar.get_concept(company, tag, max_age_days)
        if data is not None:
            result.fetched_data_types.append(f"concept:us-gaap:{tag}")
            revenue_found = True
            break
    else:
        result.missing_tags.extend(f"us-gaap:{t}" for t in REVENUE_TAGS)

    # A us-gaap filer virtually always has SOME revenue tag -- if neither
    # of REVENUE_TAGS resolved at all, this filer almost certainly has no
    # us-gaap financial data to speak of (confirmed live: NU/TSM/UL/RIO,
    # all ifrs-full exclusively), not just an unusual revenue tag name.
    # xbrl_tag_mapping caches aggressively (own ~1yr TTL), so this is a
    # one-time LLM cost per such filer, not a per-screen one.
    if not revenue_found:
        xbrl_tag_mapping.apply_fallback_mapping(company)
        result.fetched_data_types.append("ai_tag_mapping:v1")

    for taxonomy, tag in MODE_1_CONCEPTS:
        _fetch_concept(company, taxonomy, tag, max_age_days, result)

    for tag in DEBT_TAGS:
        _fetch_concept(company, "us-gaap", tag, max_age_days, result)

    # Price + FMP shares tracker, and the tripwire.
    profile = fmp.get_profile(company, max_age_days)
    shares_float = fmp.get_shares_float(company, max_age_days)
    if profile:
        result.fetched_data_types.append("profile:fmp")
    if shares_float:
        result.fetched_data_types.append("shares_float:fmp")

    if profile and shares_float:
        drift = fmp.check_sanity(profile["price"], shares_float["shares_outstanding"], profile["market_cap"])
        result.tripwire_drift = drift
        result.tripwire_triggered = drift is not None and drift > FMP_SANITY_TOLERANCE

        if result.latest_filing:
            our_accn = fmp.accession_from_url(result.latest_filing[1])
            fmp_accn = fmp.accession_from_url(shares_float.get("source") or "")
            result.filing_stale_vs_fmp = bool(our_accn and fmp_accn and our_accn != fmp_accn)

    return result


def _fetch_concept(company: Company, taxonomy: str, tag: str, max_age_days: float, result: ScreenResult) -> None:
    data = sec_edgar.get_concept(company, tag, max_age_days, taxonomy=taxonomy)
    if data is None:
        result.missing_tags.append(f"{taxonomy}:{tag}")
    else:
        result.fetched_data_types.append(f"concept:{taxonomy}:{tag}")
