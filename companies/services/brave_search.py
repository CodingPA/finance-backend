"""Brave Search API client -- the only GENERIC, open-web search this app
has. Everything else in companies/services (sec_edgar, fmp, wikipedia_client,
steady's Reddit endpoints) is a narrow, purpose-specific data provider; this
is the one that answers an arbitrary query against the open web, same role
Anthropic/OpenAI/Gemini's own server-side web_search tools play for
league/tools.py's build_league_tools -- see search_web there and in
assistant/tools.py, the two callers. Both share this one implementation
rather than each hitting Brave directly."""

from urllib.parse import quote

from django.conf import settings

from . import http

BASE_URL = "https://api.search.brave.com/res/v1/web/search"
MAX_RESULTS = 20  # Brave's own per-request cap.


def search_web(query: str, count: int = 5) -> list[dict] | None:
    """Live web search via Brave's Search API. Returns a list of
    {title, url, description} dicts -- Brave's own field names
    (web.results[].{title,url,description}), passed through unchanged
    rather than reshaped, since there's no other provider's shape to
    normalize against here. None if BRAVE_API_KEY isn't configured, same
    "unconfigured" convention as steady.py/tradier.py/fmp.py -- callers
    treat this as "search unavailable," not a crash. Empty list (not
    None) for a genuine zero-result query.

    `count` is clamped to Brave's own max of 20 per request -- there's no
    pagination support here (Brave's `offset` param), same "first page
    only" convention as steady.py's Reddit search/posts."""
    if not settings.BRAVE_API_KEY:
        return None

    count = max(1, min(count, MAX_RESULTS))
    resp = http.get(
        f"{BASE_URL}?q={quote(query)}&count={count}",
        "web_search",
        provider="brave",
        headers={"Accept": "application/json", "X-Subscription-Token": settings.BRAVE_API_KEY},
        tag=query[:100],
    )
    payload = resp.json()
    results = payload.get("web", {}).get("results", [])
    return [
        {"title": r.get("title"), "url": r.get("url"), "description": r.get("description")}
        for r in results
    ]
