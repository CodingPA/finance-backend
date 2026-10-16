"""FMP fetchers: price/market cap and the SEC-derived shares-float staleness
tripwire. Ported from scripts/explore_sec_api.py."""

import re
from datetime import date, timedelta

import requests
from django.conf import settings
from django.utils import timezone

from companies.models import Company, CompanySnapshot

from . import http


def _cached_or_none(company: Company, data_type: str, max_age_days: float):
    snapshot = CompanySnapshot.objects.filter(company=company, data_type=data_type).first()
    if snapshot is None or (timezone.now() - snapshot.fetched_at) > timedelta(days=max_age_days):
        return None
    return snapshot.raw_json


def _save(company: Company, data_type: str, raw_json) -> None:
    CompanySnapshot.objects.update_or_create(company=company, data_type=data_type, defaults={"raw_json": raw_json})


def _fmp_get(company: Company, endpoint: str, data_type: str, max_age_days: float):
    """Returns (payload, from_cache). payload is FMP's raw shape when
    freshly fetched, or our own normalized shape when served from cache --
    callers key off from_cache, no field-name guessing needed."""
    cached = _cached_or_none(company, data_type, max_age_days)
    if cached is not None:
        http.log_cache_hit(endpoint, provider="fmp", ticker=company.ticker, company=company)
        return cached, True

    if not settings.FMP_API_KEY:
        return None, False

    url = f"https://financialmodelingprep.com/stable/{endpoint}?symbol={company.ticker}&apikey={settings.FMP_API_KEY}"
    resp = http.get(url, endpoint, provider="fmp", headers={"User-Agent": http.user_agent()}, ticker=company.ticker, company=company)
    payload = resp.json()
    if not payload:
        return None, False
    return (payload[0] if isinstance(payload, list) else payload), False


def get_profile(company: Company, max_age_days: float = 1.0) -> dict | None:
    """Price + market cap + sector/industry classification. Not a filed
    fact -- a live-ish snapshot as of whenever this was called, not a
    dated end-of-day close. sector/industry are FMP's own free-text
    classification strings (e.g. "Technology" / "Consumer Electronics"),
    passed through verbatim -- no controlled vocabulary/GICS mapping on
    this side, so don't assume two companies with a related business are
    tagged identically. A snapshot cached before this field existed
    returns sector/industry as None until its next real fetch (max_age_days
    later) -- same as any other field added to this dict over time."""
    data_type = "profile:fmp"
    raw, from_cache = _fmp_get(company, "profile", data_type, max_age_days)
    if raw is None:
        return None
    if from_cache:
        return raw
    result = {
        "price": raw.get("price"),
        "market_cap": raw.get("marketCap"),
        "sector": raw.get("sector"),
        "industry": raw.get("industry"),
        "queried_at": timezone.now().isoformat(),
        "source": "fmp:/stable/profile",
    }
    _save(company, data_type, result)
    return result


def get_shares_float(company: Company, max_age_days: float = 1.0) -> dict | None:
    """Shares outstanding from FMP. This is SEC-derived (FMP's own 'source'
    field cites the filing it came from), not an independent vendor
    estimate -- its value is as a staleness tripwire: if it cites a newer
    filing than our own latest EDGAR pull, that's the signal to re-poll.

    Same 402-tolerant convention as get_price_history: if FMP's plan
    doesn't include this endpoint, treated as "no data" not an error, and
    the empty result is cached too, so a ticker outside the plan's
    coverage doesn't cost a fresh 402 on every call."""
    data_type = "shares_float:fmp"
    try:
        raw, from_cache = _fmp_get(company, "shares-float", data_type, max_age_days)
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 402:
            _save(company, data_type, {})
            return None
        raise
    if raw is None:
        return None
    if from_cache:
        return raw
    result = {
        "shares_outstanding": raw.get("outstandingShares"),
        "fmp_date": raw.get("date"),
        "source": raw.get("source"),
    }
    _save(company, data_type, result)
    return result


def get_price_history(company: Company, max_age_days: float = 1.0) -> list[dict]:
    """Full daily EOD price series since listing, from FMP's own historical
    endpoint -- not something this app accumulates itself day by day.
    Cached the same way as every other fetch here: one row per company,
    overwritten on refetch, so repeat chart loads on the same day cost zero
    extra FMP calls (matters at a free-tier rate limit).

    FMP's free/starter plans gate this specific endpoint to a handful of
    demo symbols (AAPL works; confirmed live) -- everything else, including
    every ticker this app actually tracks, gets a 402. Treated the same as
    "no data" rather than an error, and the empty result is cached too, so
    a chart that keeps asking for an unavailable symbol doesn't cost a
    fresh 402 on every load."""
    data_type = "price_history:fmp"
    cached = _cached_or_none(company, data_type, max_age_days)
    if cached is not None:
        http.log_cache_hit(data_type, provider="fmp", ticker=company.ticker, company=company)
        return cached

    if not settings.FMP_API_KEY:
        return []

    url = f"https://financialmodelingprep.com/stable/historical-price-eod/full?symbol={company.ticker}&apikey={settings.FMP_API_KEY}"
    try:
        resp = http.get(url, data_type, provider="fmp", headers={"User-Agent": http.user_agent()}, ticker=company.ticker, company=company)
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 402:
            _save(company, data_type, [])
            return []
        raise
    payload = resp.json()
    result = [
        {
            "date": row["date"],
            "open": row.get("open"),
            "high": row.get("high"),
            "low": row.get("low"),
            "close": row.get("close"),
            "volume": row.get("volume"),
        }
        for row in payload
    ]
    _save(company, data_type, result)
    return result


def get_earnings_calendar(company: Company, max_age_days: float = 1.0) -> list[dict]:
    """Past and upcoming reported/estimated earnings dates for one company,
    from FMP's /stable/earnings endpoint -- companies/services/technicals.py's
    source for "days to next earnings," independent of which options-
    pricing provider (Steady/Tradier) is configured, since neither
    reliably carries this for every deployment (Steady only exposes it as
    a per-option-contract field, Tradier not at all -- see technicals.py's
    module docstring).

    Same 402-tolerant convention as get_price_history: if FMP's plan
    doesn't include this endpoint, treated as "no data" not an error, and
    the empty result is cached too, so a ticker outside the plan's
    coverage doesn't cost a fresh 402 on every call."""
    data_type = "earnings_calendar:fmp"
    cached = _cached_or_none(company, data_type, max_age_days)
    if cached is not None:
        http.log_cache_hit(data_type, provider="fmp", ticker=company.ticker, company=company)
        return cached

    if not settings.FMP_API_KEY:
        return []

    url = f"https://financialmodelingprep.com/stable/earnings?symbol={company.ticker}&apikey={settings.FMP_API_KEY}"
    try:
        resp = http.get(url, data_type, provider="fmp", headers={"User-Agent": http.user_agent()}, ticker=company.ticker, company=company)
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 402:
            _save(company, data_type, [])
            return []
        raise
    payload = resp.json()
    result = [
        {"date": row.get("date"), "eps_estimated": row.get("epsEstimated"), "eps_actual": row.get("epsActual")}
        for row in payload
        if row.get("date")
    ]
    _save(company, data_type, result)
    return result


def get_next_earnings_date(company: Company, max_age_days: float = 1.0) -> str | None:
    """Nearest earnings date today or later from get_earnings_calendar, or
    None if there isn't one -- FMP's plan doesn't cover this endpoint,
    FMP_API_KEY isn't set, or the company genuinely has nothing scheduled
    yet."""
    calendar = get_earnings_calendar(company, max_age_days=max_age_days)
    today = date.today().isoformat()
    upcoming = sorted(row["date"] for row in calendar if row.get("date") and row["date"] >= today)
    return upcoming[0] if upcoming else None


def accession_from_url(url: str) -> str | None:
    """Pull the 18-digit accession number out of an EDGAR Archives URL --
    the reliable way to check whether two links point at the same filing,
    since CIK zero-padding (1819994 vs 0001819994) differs across sources."""
    match = re.search(r"/(\d{18})/", url)
    return match.group(1) if match else None


def check_sanity(price: float | None, shares_outstanding: float | None, market_cap: float | None) -> float | None:
    """Recompute price * shares_outstanding and compare to FMP's own
    marketCap. A pass only proves FMP is internally self-consistent -- it
    does NOT verify shares_outstanding against EDGAR (observed on RDW: FMP
    cited the exact filing our EDGAR pull had, yet disagreed with EDGAR's
    own share count by ~24%, while this check still passed at 0% drift)."""
    if not price or not shares_outstanding or not market_cap:
        return None
    implied = price * shares_outstanding
    return abs(implied - market_cap) / market_cap
