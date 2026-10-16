"""SEC EDGAR fetchers: ticker resolution, filing history, XBRL concepts and
facts. Ported from scripts/explore_sec_api.py, now caching through
CompanySnapshot (Django ORM) instead of a standalone SQLite file."""

from datetime import timedelta

import requests
from django.utils import timezone

from companies.models import Company, CompanySnapshot, TickerDirectory

from . import http


def cik_padded(cik: int) -> str:
    return f"CIK{cik:010d}"


def resolve_company(ticker: str, max_age_days: float = 1.0) -> Company:
    """Ticker -> Company, creating it on first sight. Resolves via the
    cached SEC ticker directory, refetching the whole directory if it's
    stale (or the ticker's simply never been seen) rather than per-ticker --
    it's one file, not one call per lookup."""
    ticker = ticker.upper()
    company = Company.objects.filter(ticker=ticker).first()
    if company is not None:
        return company

    entry = TickerDirectory.objects.filter(ticker=ticker).first()
    stale = entry is None or (timezone.now() - entry.refreshed_at) > timedelta(days=max_age_days)
    if stale:
        _refresh_ticker_directory()
        entry = TickerDirectory.objects.filter(ticker=ticker).first()

    if entry is None:
        raise ValueError(f"Ticker {ticker!r} not found in SEC's company_tickers.json")

    company, _ = Company.objects.get_or_create(
        cik=entry.cik, defaults={"ticker": ticker, "name": entry.title}
    )
    return company


def _refresh_ticker_directory() -> None:
    resp = http.get("https://www.sec.gov/files/company_tickers.json", "ticker_directory", provider="sec")
    raw = resp.json()
    now = timezone.now()
    TickerDirectory.objects.bulk_create(
        [
            TickerDirectory(ticker=row["ticker"].upper(), cik=row["cik_str"], title=row["title"], refreshed_at=now)
            for row in raw.values()
        ],
        update_conflicts=True,
        update_fields=["cik", "title", "refreshed_at"],
        unique_fields=["ticker"],
    )


def _cached_or_none(company: Company, data_type: str, max_age_days: float):
    snapshot = CompanySnapshot.objects.filter(company=company, data_type=data_type).first()
    if snapshot is None or (timezone.now() - snapshot.fetched_at) > timedelta(days=max_age_days):
        return None
    return snapshot.raw_json


def _save(company: Company, data_type: str, raw_json) -> None:
    CompanySnapshot.objects.update_or_create(company=company, data_type=data_type, defaults={"raw_json": raw_json})


def get_submissions(company: Company, max_age_days: float = 1.0) -> dict:
    data_type = "submissions"
    cached = _cached_or_none(company, data_type, max_age_days)
    if cached is not None:
        http.log_cache_hit(data_type, ticker=company.ticker, company=company)
        return cached

    url = f"https://data.sec.gov/submissions/{cik_padded(company.cik)}.json"
    resp = http.get(url, data_type, provider="sec", ticker=company.ticker, company=company)
    data = resp.json()
    _save(company, data_type, data)
    return data


def get_concept(company: Company, tag: str, max_age_days: float = 1.0, taxonomy: str = "us-gaap") -> dict | None:
    """None if this filer simply doesn't report the tag (404) -- a real,
    expected outcome (e.g. multi-class share structures often have no plain
    CommonStockSharesOutstanding), not an error."""
    data_type = f"concept:{taxonomy}:{tag}"
    cached = _cached_or_none(company, data_type, max_age_days)
    if cached is not None:
        http.log_cache_hit(data_type, ticker=company.ticker, company=company, tag=tag)
        return cached

    url = f"https://data.sec.gov/api/xbrl/companyconcept/{cik_padded(company.cik)}/{taxonomy}/{tag}.json"
    try:
        resp = http.get(url, "concept", provider="sec", ticker=company.ticker, company=company, tag=tag)
    except requests.HTTPError as exc:
        if exc.response.status_code == 404:
            return None
        raise
    data = resp.json()
    _save(company, data_type, data)
    return data


def get_company_facts(company: Company, max_age_days: float = 1.0) -> dict:
    """Every XBRL tag this filer has ever reported, across all taxonomies --
    for discovering tag names, not part of the routine screen."""
    data_type = "facts"
    cached = _cached_or_none(company, data_type, max_age_days)
    if cached is not None:
        http.log_cache_hit(data_type, ticker=company.ticker, company=company)
        return cached

    url = f"https://data.sec.gov/api/xbrl/companyfacts/{cik_padded(company.cik)}.json"
    resp = http.get(url, data_type, provider="sec", ticker=company.ticker, company=company)
    data = resp.json()
    _save(company, data_type, data)
    return data


def list_filings(submissions: dict, forms: tuple[str, ...] = ("10-K", "10-Q", "20-F")) -> list[dict]:
    """Every filing of the given forms in EDGAR's "recent" submissions
    window (up to ~1,000 filings -- plenty for these young, small-cap
    filers; older filings live in a separate paginated `files` list this
    doesn't fetch), newest first, each with a direct link to the primary
    document on EDGAR -- what the frontend needs to let a user pull up the
    actual 10-K/10-Q instead of typing a citation by hand.

    `20-F` (a foreign private issuer's annual report, the equivalent of a
    10-K) is in the default on purpose -- a live spot-check across 10
    well-known tickers found only 3/10 actually file 10-K/10-Q at all;
    the rest are FPIs on 20-F/6-K. `6-K` (their interim-report form) is
    deliberately NOT included here: unlike 20-F, it's used for all kinds
    of ad hoc disclosures (press releases, dividend declarations) as well
    as actual interim financials, so including it by default would mix
    real filings a user wants to cite with routine noise -- pass
    forms=("6-K",) explicitly if that's specifically what's wanted."""
    recent = submissions["filings"]["recent"]
    # EDGAR's submissions JSON gives `cik` zero-padded ("0001819994"); its
    # own filing-index URLs want it unpadded. Padded still resolves (a 301
    # redirect to the unpadded form) but there's no reason to make the
    # frontend eat an extra hop, and it matches shares_float_fmp.source
    # elsewhere in the same report, which is already unpadded.
    cik = int(submissions["cik"])
    out = []
    for form, filing_date, report_date, accn, doc, desc in zip(
        recent["form"],
        recent["filingDate"],
        recent["reportDate"],
        recent["accessionNumber"],
        recent["primaryDocument"],
        recent["primaryDocDescription"],
    ):
        if form in forms:
            accn_nodash = accn.replace("-", "")
            out.append(
                {
                    "form": form,
                    "filing_date": filing_date,
                    "report_date": report_date,
                    "accession_number": accn,
                    "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn_nodash}/{doc}",
                    "description": desc,
                }
            )
    return out


def latest_filing(submissions: dict, forms: tuple[str, ...] = ("10-K", "10-Q", "20-F")) -> tuple[str, str] | None:
    """(filing_date, url) for the most recent filing of the given forms, or None."""
    filings = list_filings(submissions, forms)
    if not filings:
        return None
    return filings[0]["filing_date"], filings[0]["url"]
