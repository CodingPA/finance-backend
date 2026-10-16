"""Turns cached CompanySnapshot rows into the report the API/frontend
actually wants: per-fiscal-year values plus the derived metrics (margins,
FCF, growth, P/S, runway). Computed at read time from raw snapshots rather
than stored, so there's nothing to keep in sync -- re-derive, don't cache
the derivation."""

from django.db.models import Max

from companies.models import Company, CompanySnapshot

from . import fmp, sec_edgar
from .screening import MODE_1_CONCEPTS, REVENUE_TAGS
from .xbrl_tag_mapping import MAPPING_SNAPSHOT_TYPE

# 1:1 with screening.py's DEBT_TAGS (["LongTermDebtCurrent",
# "LongTermDebtNoncurrent"]) -- named explicitly here (not re-derived
# from that list positionally) so each side can also be looked up in
# xbrl_tag_mapping's per-concept fallback mapping by name.
_DEBT_TAGS_BY_CONCEPT = {"debt_current": "LongTermDebtCurrent", "debt_noncurrent": "LongTermDebtNoncurrent"}

FMP_SANITY_TOLERANCE = 0.01

# Annual-report forms whose FY entries annual_values() trusts -- 20-F is a
# foreign private issuer's 10-K equivalent. A live spot-check across 10
# well-known tickers found only 3/10 actually file 10-K at all; several of
# the rest (NBIS, BABA, SHOP) report under us-gaap despite filing 20-F, so
# their real, correctly-tagged annual figures were being silently thrown
# away by a 10-K-only check. Amendments included too -- confirmed live
# that RIO's debt_current concept has its most recent entry filed under
# 20-F/A, not a plain 20-F; excluding amendments would resolve a tag via
# xbrl_tag_mapping's own (unfiltered-by-form) validation and then have it
# come back empty here anyway, a confusing half-failure.
_ANNUAL_FORMS = ("10-K", "10-K/A", "20-F", "20-F/A")


def _preferred_unit_key(units: dict) -> str | None:
    """Which key in a concept's `units` dict to read -- "USD" if this
    concept reports it at all, else whatever single other unit is present
    (e.g. a pure share count, whose unit is "shares", not a currency at
    all -- unaffected by this preference since there's no "USD" key to
    compete with it there). Never silently picks a NON-USD entry when a
    USD one exists just because it happened to come first in the dict --
    confirmed live: NBIS's Revenues concept has units {"RUB": [...],
    "USD": [...]} in that exact key order (legacy Yandex-era RUB
    reporting predates its USD-only present-day figures), so the old
    next(iter(...)) picked RUB. None if `units` is empty."""
    if not units:
        return None
    return "USD" if "USD" in units else next(iter(units))


def annual_values(concept_json: dict | None) -> dict[int, float]:
    """{fiscal_year: value}, from _ANNUAL_FORMS/FY entries only, USD (or
    the concept's only unit, e.g. shares) preferred over another currency
    that happens to share the tag. Later (more recently filed) entries
    win on duplicate years -- reflects restatements."""
    if concept_json is None:
        return {}
    unit_key = _preferred_unit_key(concept_json["units"])
    if unit_key is None:
        return {}
    out: dict[int, float] = {}
    for entry in concept_json["units"][unit_key]:
        if entry.get("form") in _ANNUAL_FORMS and entry.get("fp") == "FY" and entry.get("fy") is not None:
            out[entry["fy"]] = entry["val"]
    return out


def latest_entry(concept_json: dict | None) -> dict | None:
    """Most recent raw entry, unfiltered by form/fp -- e.g. cover-page
    shares, where the most current 10-Q figure is more relevant than the
    latest 10-K-only one. Same USD-preference reasoning as annual_values."""
    if concept_json is None:
        return None
    unit_key = _preferred_unit_key(concept_json["units"])
    if unit_key is None:
        return None
    values = concept_json["units"][unit_key]
    return values[-1] if values else None


def _snapshot_map(company: Company) -> dict[str, dict]:
    return {s.data_type: s.raw_json for s in CompanySnapshot.objects.filter(company=company)}


def build_report(company: Company) -> dict:
    snapshots = _snapshot_map(company)

    def concept(taxonomy: str, tag: str) -> dict | None:
        return snapshots.get(f"concept:{taxonomy}:{tag}")

    # Set only for a filer xbrl_tag_mapping.apply_fallback_mapping actually
    # ran for (screening.py::run_screen triggers it when the plain us-gaap
    # revenue lookup below finds nothing) -- {"taxonomy": None, "mapping":
    # {}} for one that ran but found no non-us-gaap taxonomy to map at
    # all, missing entirely for a filer that never needed this (the ~30%
    # "standard" case), or one that's simply never been screened yet.
    ai_mapping = snapshots.get(MAPPING_SNAPSHOT_TYPE)
    ai_mapped_concepts: list[str] = []

    def annual_values_with_fallback(key: str, primary: dict | None) -> dict[int, float]:
        """annual_values(primary), falling back to xbrl_tag_mapping's
        resolved tag for `key` ONLY when the primary lookup came back
        genuinely empty -- a us-gaap filer with real (if partial) data
        never gets overridden by a fallback tag it was never even
        computed for. Records `key` in ai_mapped_concepts when the
        fallback is what actually produced the data, so callers of
        build_report can tell an AI-resolved figure from a directly
        tagged one instead of the two being silently indistinguishable."""
        values = annual_values(primary)
        if values or not ai_mapping or not ai_mapping.get("mapping", {}).get(key):
            return values
        fallback_values = annual_values(concept(ai_mapping["taxonomy"], ai_mapping["mapping"][key]))
        if fallback_values:
            ai_mapped_concepts.append(key)
        return fallback_values

    revenue_json = next((concept("us-gaap", t) for t in REVENUE_TAGS if concept("us-gaap", t)), None)
    revenue_by_fy = annual_values_with_fallback("revenue", revenue_json)

    financials_by_field = {"revenue": revenue_by_fy}
    for taxonomy, tag in MODE_1_CONCEPTS:
        key = _FIELD_NAMES.get(tag, tag)
        financials_by_field[key] = annual_values_with_fallback(key, concept(taxonomy, tag))

    debt_by_fy: dict[int, float] = {}
    for key, tag in _DEBT_TAGS_BY_CONCEPT.items():
        for fy, val in annual_values_with_fallback(key, concept("us-gaap", tag)).items():
            debt_by_fy[fy] = debt_by_fy.get(fy, 0) + val
    financials_by_field["total_debt"] = debt_by_fy

    fiscal_years = sorted({fy for series in financials_by_field.values() for fy in series})

    derived = _derive(financials_by_field, fiscal_years)

    cover_entry = latest_entry(concept("dei", "EntityCommonStockSharesOutstanding"))
    balance_by_fy = annual_values_with_fallback(
        "shares_outstanding", concept("us-gaap", "CommonStockSharesOutstanding")
    )

    profile = snapshots.get("profile:fmp")
    shares_float = snapshots.get("shares_float:fmp")
    tripwire_drift = None
    if profile and shares_float:
        tripwire_drift = fmp.check_sanity(
            profile.get("price"), shares_float.get("shares_outstanding"), profile.get("market_cap")
        )

    market_cap = profile.get("market_cap") if profile else None
    latest_fy = max(fiscal_years) if fiscal_years else None
    latest_revenue = revenue_by_fy.get(latest_fy) if latest_fy else None
    derived["ps_trailing"] = market_cap / latest_revenue if market_cap and latest_revenue else None

    submissions = snapshots.get("submissions")
    filings = sec_edgar.list_filings(submissions) if submissions else []

    return {
        "ticker": company.ticker,
        "cik": company.cik,
        "name": company.name,
        # False for a company that's been added/resolved (so the Company row
        # exists) but never actually screened -- every other field below is
        # then an empty shell (no fiscal_years, every financial field {},
        # price/market_cap null), not an error. Check this before rendering,
        # not the emptiness of individual fields.
        "screened": bool(snapshots),
        "fiscal_years": fiscal_years,
        "financials_by_fy": {field: series for field, series in financials_by_field.items()},
        # Which financials_by_fy/shares.balance_sheet_by_fy fields (if any)
        # came from xbrl_tag_mapping's AI-resolved tag rather than this
        # app's own fixed us-gaap list -- surfaced rather than left
        # silently indistinguishable, since these numbers were never
        # vetted by a human the way the fixed list's tag names were.
        "ai_mapped_concepts": ai_mapped_concepts,
        "derived": derived,
        "shares": {
            "cover_page": {"value": cover_entry["val"], "as_of": cover_entry["end"], "form": cover_entry["form"]}
            if cover_entry else None,
            "balance_sheet_by_fy": balance_by_fy,
        },
        "price": profile.get("price") if profile else None,
        "market_cap": market_cap,
        "sector": profile.get("sector") if profile else None,
        "industry": profile.get("industry") if profile else None,
        "price_queried_at": profile.get("queried_at") if profile else None,
        "shares_float_fmp": shares_float,
        "filings": filings,
        "tripwire": {
            "drift": tripwire_drift,
            "triggered": tripwire_drift is not None and tripwire_drift > FMP_SANITY_TOLERANCE,
        },
    }


def build_summary(company: Company) -> dict:
    """The compact per-row shape for the dashboard table: latest-year
    revenue growth/FCF, runway, price/market cap, tripwire, and when this
    company's data was last refreshed. Built off build_report() rather than
    duplicating its logic -- the list view just doesn't need the full
    multi-year breakdown."""
    report = build_report(company)
    fiscal_years = report["fiscal_years"]
    latest_fy = max(fiscal_years) if fiscal_years else None

    updated_at = CompanySnapshot.objects.filter(company=company).aggregate(Max("fetched_at"))["fetched_at__max"]

    return {
        "ticker": report["ticker"],
        "name": report["name"],
        "screened": report["screened"],
        "price": report["price"],
        "market_cap": report["market_cap"],
        "revenue_growth_pct": report["derived"]["revenue_growth_pct_by_fy"].get(latest_fy) if latest_fy else None,
        "fcf": report["derived"]["fcf_by_fy"].get(latest_fy) if latest_fy else None,
        "runway_years": report["derived"]["runway_years"],
        "runway_status": report["derived"]["runway_status"],
        "fcf_approximated": report["derived"]["fcf_approximated"],
        "tripwire_triggered": report["tripwire"]["triggered"],
        "updated_at": updated_at,
    }


# EDGAR tag -> the field name we report it under.
_FIELD_NAMES = {
    "GrossProfit": "gross_profit",
    "NetIncomeLoss": "net_loss",
    "NetCashProvidedByUsedInOperatingActivities": "operating_cash_flow",
    "PaymentsToAcquirePropertyPlantAndEquipment": "capex",
    "ShareBasedCompensation": "stock_based_comp",
    "CashAndCashEquivalentsAtCarryingValue": "cash",
}


def _derive(financials_by_field: dict[str, dict[int, float]], fiscal_years: list[int]) -> dict:
    revenue = financials_by_field["revenue"]
    gross_profit = financials_by_field["gross_profit"]
    ocf = financials_by_field["operating_cash_flow"]
    capex = financials_by_field["capex"]
    cash = financials_by_field["cash"]

    gross_margin_pct = {
        fy: gross_profit[fy] / revenue[fy] for fy in fiscal_years if fy in gross_profit and revenue.get(fy)
    }
    fcf = {fy: ocf[fy] - capex[fy] for fy in fiscal_years if fy in ocf and fy in capex}

    revenue_growth_pct = {}
    for fy in fiscal_years:
        if fy in revenue and (fy - 1) in revenue and revenue[fy - 1]:
            revenue_growth_pct[fy] = (revenue[fy] / revenue[fy - 1]) - 1

    latest_fy = max(fiscal_years) if fiscal_years else None
    latest_fcf = fcf.get(latest_fy) if latest_fy else None

    # Runway-only fallback: some filers (pre-revenue biotechs especially)
    # stop tagging capex once it's immaterial, which starves fcf_by_fy for
    # the latest year even though OCF is right there. Approximate with OCF
    # alone for the survival calc specifically -- don't touch fcf_by_fy
    # itself, which stays capex-complete-years-only everywhere else it's
    # shown, since "unknown" is truer than "assume capex is zero."
    fcf_approximated = False
    if latest_fy and latest_fcf is None and latest_fy in ocf and latest_fy not in capex:
        latest_fcf = ocf[latest_fy]
        fcf_approximated = True

    runway_years = None
    if latest_fy and latest_fcf is not None and latest_fcf < 0 and cash.get(latest_fy):
        runway_years = cash[latest_fy] / abs(latest_fcf)
    else:
        fcf_approximated = False

    # Collapses the three reasons runway_years can be null into one field,
    # so the frontend doesn't have to re-derive "is this profitable or just
    # missing data" from fcf's sign itself -- "profitable" (FCF >= 0, runway
    # isn't a meaningful question) and "unknown" (burning cash but missing
    # FCF or cash data) render very differently and shouldn't both be a bare
    # null/dash.
    if latest_fcf is None:
        runway_status = "unknown"
    elif latest_fcf >= 0:
        runway_status = "profitable"
    elif runway_years is not None:
        runway_status = "computed"
    else:
        runway_status = "unknown"

    return {
        "gross_margin_pct_by_fy": gross_margin_pct,
        "fcf_by_fy": fcf,
        "revenue_growth_pct_by_fy": revenue_growth_pct,
        "runway_years": runway_years,
        "fcf_approximated": fcf_approximated,
        "runway_status": runway_status,
    }
