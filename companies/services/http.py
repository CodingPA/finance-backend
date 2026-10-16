"""Shared HTTP transport for the companies app: per-provider rate limiting
and audit logging (ApiCallLog), ported from scripts/explore_sec_api.py's
_get() but writing through the Django ORM instead of a standalone SQLite
cache."""

import time

import requests
from django.conf import settings

from companies.models import ApiCallLog, Company

APP_VERSION = "0.1"

# SEC's hard limit is 10 req/sec; FMP's free tier is a daily quota rather
# than per-second, so this is just politeness. Wikimedia has no hard
# per-second cap for this volume either -- 0.4s is just politeness too.
# SteadyAPI's hard limit is 15 req/sec (1/15 =~ 0.067s); 0.07 keeps a hair
# of margin. Tradier's limit is per-minute, not per-second (120/min prod,
# 60/min sandbox -- https://docs.tradier.com/docs/rate-limiting): 0.5s
# covers the stricter sandbox case too, and nothing here calls it in a
# tight loop. Brave's paid "Search" plan documents 50 req/sec; the free
# tier's own per-second cap isn't published anywhere Brave's docs surface
# (only a $5/mo free credit, ~1000 requests at $5/1000) -- 1.0s is a
# deliberately conservative placeholder pending an actual 429 to tune
# against, not a confirmed number the way the others above are.
PROVIDER_MIN_INTERVAL = {
    "sec": 0.15,
    "fmp": 0.5,
    "wikipedia": 0.4,
    "steady": 0.07,
    "tradier": 0.5,
    "brave": 1.0,
    # Polygon's free "Stocks Basic" tier is 5 req/min (confirmed live,
    # 2026-09-20) -- 12.5s clears that with margin. Actual call volume
    # never gets near this limit: one grouped-daily call per session, and
    # the tickers-reference pagination (companies/services/polygon.py)
    # runs far less than daily.
    "polygon": 12.5,
}

_last_request_time: dict[str, float] = {}


def user_agent() -> str:
    if not settings.SEC_CONTACT_EMAIL:
        raise RuntimeError(
            "SEC_CONTACT_EMAIL is not set. SEC requires a contact email in the "
            "User-Agent on every request and 403s without one -- set it before "
            "screening anything."
        )
    return f"the-lab/{APP_VERSION} ({settings.SEC_CONTACT_EMAIL})"


def get(
    url: str,
    endpoint_type: str,
    *,
    provider: str = "sec",
    headers: dict | None = None,
    ticker: str | None = None,
    company: Company | None = None,
    tag: str | None = None,
) -> requests.Response:
    """GET with per-provider rate limiting and a logged call. Raises for
    non-2xx after logging, so a failed call still shows up in the audit
    trail with its real status code."""
    min_interval = PROVIDER_MIN_INTERVAL[provider]
    elapsed = time.monotonic() - _last_request_time.get(provider, 0.0)
    if elapsed < min_interval:
        time.sleep(min_interval - elapsed)

    resp = requests.get(url, headers=headers or {"User-Agent": user_agent()}, timeout=30)
    _last_request_time[provider] = time.monotonic()

    ApiCallLog.objects.create(
        url=url,
        endpoint_type=endpoint_type,
        provider=provider,
        ticker=ticker,
        company=company,
        tag=tag,
        status_code=resp.status_code,
        from_cache=False,
    )
    resp.raise_for_status()
    return resp


def log_cache_hit(endpoint_type: str, *, provider: str = "sec", ticker=None, company=None, tag=None) -> None:
    ApiCallLog.objects.create(
        url=f"cache:{endpoint_type}:{tag or ''}",
        endpoint_type=endpoint_type,
        provider=provider,
        ticker=ticker,
        company=company,
        tag=tag,
        from_cache=True,
    )
