"""Extracts stock ticker mentions from a batch of Reddit posts via a
cheap OpenAI model (gpt-5.6-luna, assistant.migrations.0012_seed_luna_model)
with structured outputs -- cashtag/keyword regex matching alone can't tell
"SLS the stock" from "SLS the NASA rocket" from context (confirmed live),
and misses tickers mentioned
without a $ prefix. One call for the WHOLE batch of posts, not one per
post -- scanning a subreddit means 25+ posts, and a call-per-post would be
both slow and needlessly expensive for what's fundamentally one extraction
task. Same graceful-without-a-key convention as companies/services/
embeddings.py. Looked up here by exact model_id, not through an
is_active-filtered query (assistant.migrations.0013_unhide_luna_model
flipped it back to is_active=True once assistant/tasks.py grew a real
OpenAI call loop -- selectable for chat/league now, but this module's own
use was never gated on that flag either way)."""

import logging
import re

from django.conf import settings
from openai import OpenAI
from pydantic import BaseModel

logger = logging.getLogger(__name__)

_LUNA_MODEL_ID = "gpt-5.6-luna"
# Per-post text cap keeps the batched prompt bounded regardless of how
# long a WSB DD post's selftext runs (some are thousands of words) --
# title + this many chars of selftext is plenty of context to tell which
# ticker(s) a post is actually about.
_MAX_CHARS_PER_POST = 500

_EXTRACTION_PROMPT = (
    "Extract every stock ticker symbol these Reddit posts are actually "
    "discussing as a company/fund/security -- not words that happen to "
    "match a ticker but mean something else in context (e.g. NASA's SLS "
    "rocket program is not the ticker SLS; the Mercedes SLS AMG is not "
    "either). Return an empty list if none of these posts are actually "
    "about a stock.\n\n"
)


class _TickerExtraction(BaseModel):
    tickers: list[str]


def extract_tickers(posts: list[dict]) -> list[str] | None:
    """Returns the deduplicated, uppercased list of stock ticker symbols
    genuinely being discussed across `posts` (each a dict shaped like
    companies.services.steady.get_reddit_search/get_reddit_posts's
    return) -- contextual, not a cashtag/keyword scan, so "NASA is
    rolling out the SLS moon rocket" correctly yields no ticker while
    "$SLS mooning" does. Returns None if OPENAI_API_KEY isn't configured
    or the gpt-5.6-luna AssistantModel row is missing (an admin deleted
    it) -- callers should treat both the same as "extraction
    unavailable," not raise. Returns an empty list (not None) if no
    tickers are found -- that's a real "nothing here," not an
    unconfigured/failure state."""
    if not settings.OPENAI_API_KEY:
        return None
    if not posts:
        return []

    # Deferred import: keeps this module importable (e.g. for tests that
    # only exercise the "not configured" path) without needing the
    # assistant app's model registry loaded, and avoids a module-level
    # cross-app import for what's a single lookup.
    from assistant.models import AssistantModel
    try:
        model = AssistantModel.objects.get(model_id=_LUNA_MODEL_ID)
    except AssistantModel.DoesNotExist:
        logger.error("extract_tickers: no AssistantModel row for %r -- run migrations?", _LUNA_MODEL_ID)
        return None

    lines = []
    for post in posts:
        title = post.get("title") or ""
        selftext = (post.get("selftext") or "")[:_MAX_CHARS_PER_POST]
        lines.append(f"- {title}\n{selftext}".strip())
    batch_text = "\n\n".join(lines)

    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    response = client.responses.parse(
        model=model.model_id,
        input=[{"role": "user", "content": _EXTRACTION_PROMPT + batch_text}],
        text_format=_TickerExtraction,
    )

    usage = response.usage
    if usage is not None:
        cost = model.cost_for(usage.input_tokens, usage.output_tokens)
        logger.info(
            "extract_tickers: %d posts -> %d input/%d output tokens, $%s",
            len(posts), usage.input_tokens, usage.output_tokens, cost,
        )

    parsed = response.output_parsed
    tickers = {t.strip().upper() for t in (parsed.tickers if parsed else []) if t.strip()}
    return sorted(tickers)


def attribute_mentions(posts: list[dict], tickers: list[str]) -> dict[str, list[dict]]:
    """Pure-Python, no LLM call: for each ticker in `tickers` (already
    validated by extract_tickers -- the hard "is this really the stock"
    judgment was already made once, at the batch level), find which
    posts actually mention it and return {ticker: [matching posts]}
    (tickers with zero matching posts are omitted). Matches a $CASHTAG
    (case-insensitive -- people type $sls and $SLS interchangeably) or a
    bare, case-SENSITIVE, word-boundary occurrence of the ticker itself
    -- deliberately case-sensitive for the bare form, since "SLS" the
    stock vs. a stray lowercase "sls" substring is exactly the kind of
    noise a case-insensitive bare match would let back in, undoing the
    whole point of running extract_tickers first. Safe as a regex pass
    specifically BECAUSE `tickers` is a small, pre-validated set, not a
    blind scan across every possible ticker (the original SLS-vs-NASA-
    rocket problem this whole module exists to avoid)."""
    if not tickers:
        return {}

    patterns = {
        ticker: (
            re.compile(rf"\${re.escape(ticker)}\b", re.IGNORECASE),
            re.compile(rf"\b{re.escape(ticker)}\b"),
        )
        for ticker in tickers
    }

    mentions: dict[str, list[dict]] = {ticker: [] for ticker in tickers}
    for post in posts:
        text = f"{post.get('title') or ''} {post.get('selftext') or ''}"
        for ticker, (cashtag_re, bare_re) in patterns.items():
            if cashtag_re.search(text) or bare_re.search(text):
                mentions[ticker].append(post)
    return {ticker: matched for ticker, matched in mentions.items() if matched}
