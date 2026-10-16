"""MediaWiki API client for the wiki-linking feature. Two distinct fetch
modes, kept in separate functions so a caller can never accidentally pull a
full article when it only meant to do a live search:

- search_pages(): opensearch-style live search, INTRO-ONLY extracts
  (exintro=1) via a single generator=search API call -- verified live that
  generator=search + prop=extracts|pageprops + exintro=1 + explaintext=1
  returns per-result "extract" (intro text) and "index" (rank) fields in
  ONE request, no follow-up call per result needed.
- fetch_full_page(): one page's FULL extract (no exintro), used only by the
  linking/ingestion path, never by live search. Resolves redirects
  (redirects=1) and returns the canonical title actually resolved to, plus
  disambiguation status via pageprops.

Every call goes through companies.services.http.get() for rate limiting
(PROVIDER_MIN_INTERVAL["wikipedia"]) and audit logging (ApiCallLog), same
as the SEC/FMP clients."""

import hashlib
from urllib.parse import parse_qs, unquote, urlencode, urlparse

from django.conf import settings

from . import http

API_URL = "https://en.wikipedia.org/w/api.php"


def _headers() -> dict:
    """Always built explicitly and passed as headers= to http.get() --
    http.get()'s default (`headers or {"User-Agent": user_agent()}`) calls
    companies.services.http.user_agent(), which RAISES if SEC_CONTACT_EMAIL
    isn't set. Wikipedia's API etiquette wants a descriptive User-Agent but
    doesn't hard-require one the way SEC does, so this builds its own
    fallback rather than touching http.user_agent() (which must keep
    hard-failing for SEC calls)."""
    contact = settings.SEC_CONTACT_EMAIL or "no-contact-configured"
    return {"User-Agent": f"the-lab/0.1 (wikipedia research feature; {contact})"}


def _build_url(params: dict) -> str:
    return f"{API_URL}?{urlencode(params)}"


def search_pages(query: str, limit: int = 5) -> list[dict]:
    """Live search -- NOT persisted, no DB write. Returns up to `limit`
    results ranked by relevance, each {"title", "pageid", "extract"} where
    extract is intro-only plain text. Distinct from fetch_full_page(): this
    never pulls a full article body."""
    params = {
        "action": "query", "format": "json",
        "generator": "search", "gsrsearch": query, "gsrlimit": limit,
        "prop": "extracts|pageprops", "exintro": 1, "explaintext": 1, "redirects": 1,
    }
    resp = http.get(_build_url(params), "wiki_search", provider="wikipedia", headers=_headers(), tag=query[:128])
    pages = resp.json().get("query", {}).get("pages", {})
    results = sorted(pages.values(), key=lambda p: p.get("index", 999))
    return [
        {"title": p["title"], "pageid": p["pageid"], "extract": p.get("extract", "")}
        for p in results
        if "missing" not in p
    ]


def fetch_full_page(title: str) -> dict:
    """Full plaintext extract for one page, resolving redirects. Returns
    {"canonical_title", "text", "is_disambiguation"}. Raises ValueError if
    the page doesn't exist -- callers turn this into a ToolError/400 as
    appropriate."""
    params = {
        "action": "query", "format": "json",
        "titles": title, "prop": "extracts|pageprops",
        "explaintext": 1, "redirects": 1,
    }
    resp = http.get(_build_url(params), "wiki_full_extract", provider="wikipedia", headers=_headers(), tag=title[:128])
    data = resp.json()["query"]
    pages = data.get("pages", {})
    page = next(iter(pages.values()), None)
    if page is None or "missing" in page:
        raise ValueError(f'No Wikipedia page found for "{title}".')

    redirects = data.get("redirects", [])
    canonical_title = redirects[0]["to"] if redirects else page["title"]
    return {
        "canonical_title": canonical_title,
        "text": page.get("extract", ""),
        "is_disambiguation": "disambiguation" in page.get("pageprops", {}),
    }


def resolve_title_input(raw: str) -> str:
    """Accepts either a bare page title ("Rocket Lab") or a full Wikipedia
    URL (https://en.wikipedia.org/wiki/Rocket_Lab, optionally with a
    #section anchor, or the /w/index.php?title=... form) and returns a
    plain title ready for fetch_full_page(). URLs are the primary input
    from the frontend -- a user pastes a link, not a title, and the exact
    edge cases (%-decoding, underscores-to-spaces, section anchors) belong
    in one place, not duplicated client-side. Only en.wikipedia.org is
    supported; other-language Wikipedia URLs raise ValueError rather than
    silently fetching the wrong article under an English title lookup."""
    raw = raw.strip()
    if not raw.lower().startswith(("http://", "https://")):
        return raw

    parsed = urlparse(raw)
    host = parsed.netloc.lower()
    if host not in ("en.wikipedia.org", "en.m.wikipedia.org"):
        raise ValueError(
            f'Only English Wikipedia links are supported (got "{host}") -- '
            "paste an en.wikipedia.org URL, or just the page title."
        )

    if parsed.path.startswith("/wiki/"):
        title = parsed.path[len("/wiki/"):]
    else:
        title = parse_qs(parsed.query).get("title", [""])[0]

    title = unquote(title).replace("_", " ").strip()
    if not title:
        raise ValueError(f'Could not find a page title in "{raw}".')
    return title


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
