"""Wikipedia ingestion: fetch a linked page's full text, detect changes by
whole-article hash comparison, snapshot+prune history, and keep
ResearchClip (the SEARCHABLE set) in sync with only the CURRENT content --
old clips for a changed page are deleted (Postgres row + Weaviate object,
per-clip, matching filing_ingestion's force=True pattern) and replaced with
freshly chunked+embedded clips from the new text. Also provides the
paragraph-level diff helper for the "what changed" assistant tool.

Text is plain MediaWiki extract text with literal "== Section ==" /
"=== Subsection ===" markers, NOT HTML -- this does NOT go through Docling
(that's for the HTML filings path in filing_ingestion.py). Chunking here is
a simple section-aware paragraph splitter capped by token count."""

import re

import tiktoken
from django.utils import timezone

from notifications.services import notify

from ..models import ResearchClip, WikiSnapshot, WikiSource
from . import embeddings, vector_store, wikipedia_client

ENCODING = tiktoken.get_encoding("cl100k_base")
MAX_CHUNK_TOKENS = 1000
HEADING_RE = re.compile(r"^(={2,})\s*(.+?)\s*\1\s*$", re.MULTILINE)

DIFF_CHAR_CAP = 4000


def sync_source(source: WikiSource, force: bool = False) -> dict:
    """Fetch the current article text, compare against the latest
    snapshot's hash, and if changed (or force=True): snapshot it, prune to
    the 3 most recent, replace all of this source's ResearchClips, and
    notify() the user -- UNLESS this is the source's first-ever sync (no
    prior snapshot) or the page turned out unchanged, in either of which
    case notify() is skipped ("10 pages linked" shouldn't mean "10
    notifications", and a no-op force-refresh isn't news). Always updates
    last_checked_at regardless of outcome. Returns
    {"changed": bool, "clip_count": int}."""
    page = wikipedia_client.fetch_full_page(source.page_title)
    # Canonical title can drift (a page gets renamed/merged upstream after
    # linking) -- keep the stored title current so future polls use the
    # right value. This is bookkeeping, not "content changed".
    if page["canonical_title"] != source.page_title:
        source.page_title = page["canonical_title"]

    new_hash = wikipedia_client.content_hash(page["text"])
    latest_snapshot = source.snapshots.first()  # Meta.ordering handles "most recent"
    is_first_sync = latest_snapshot is None
    changed = is_first_sync or latest_snapshot.content_hash != new_hash

    source.last_checked_at = timezone.now()
    if changed:
        source.last_changed_at = timezone.now()
    source.save(update_fields=["page_title", "last_checked_at", "last_changed_at"])

    if not changed and not force:
        return {"changed": False, "clip_count": _source_clip_count(source)}

    WikiSnapshot.objects.create(source=source, content_hash=new_hash, text=page["text"])
    prune_snapshots(source)

    clip_count = _resync_clips(source, page["text"])

    if changed and not is_first_sync:
        notify(
            source.tracked_company.user, "wiki.page_changed",
            title=f'{source.tracked_company.company.ticker}: "{source.page_title}" changed on Wikipedia',
            body="Content changed -- use get_wiki_page_changes to see what.",
            payload={
                "ticker": source.tracked_company.company.ticker,
                "wiki_source_id": source.id,
                "page_title": source.page_title,
            },
        )
    return {"changed": changed, "clip_count": clip_count}


def prune_snapshots(source: WikiSource, keep: int = 3) -> None:
    stale_ids = list(source.snapshots.values_list("id", flat=True)[keep:])
    if stale_ids:
        WikiSnapshot.objects.filter(id__in=stale_ids).delete()


def clip_source_key(source: WikiSource) -> str:
    """The EXACT ResearchClip.source value shared by every chunk of this
    page -- deliberately exact-match, not filing_ingestion's
    source__contains pattern: with two linked pages whose titles are
    substrings of each other (e.g. "Rocket Lab" and "Rocket Lab Neutron"),
    `contains` would match both, and resyncing one would delete the
    other's clips too. Section heading goes in `label`, not `source`, so
    every chunk from the same page shares one exact source key."""
    return f"wikipedia:{source.page_title}"[:500]


def _source_clip_count(source: WikiSource) -> int:
    return source.tracked_company.clips.filter(source=clip_source_key(source)).count()


def _resync_clips(source: WikiSource, text: str) -> int:
    tracked = source.tracked_company
    source_key = clip_source_key(source)

    existing = tracked.clips.filter(source=source_key)
    for clip in existing:
        vector_store.delete_clip(clip.id)  # per-clip, BEFORE the Postgres delete -- bulk .delete() would orphan Weaviate objects
    existing.delete()

    created = 0
    for heading, chunk_text in _chunk_sections(text):
        label = f"{tracked.company.ticker} Wikipedia: {source.page_title}" + (f" -- {heading}" if heading else "")
        clip = ResearchClip.objects.create(
            tracked_company=tracked,
            category=ResearchClip.CATEGORY_CONTEXT,
            source_type=ResearchClip.SOURCE_TYPE_WIKI,
            label=label[:255],
            source=source_key,
            text=chunk_text,
        )
        vector = embeddings.embed_text(clip.text)
        if vector is not None:
            vector_store.upsert_clip(clip, vector)
        created += 1
    return created


def _chunk_sections(text: str) -> list[tuple[str, str]]:
    """Split on literal "== Heading ==" markers, then group each section's
    paragraphs into MAX_CHUNK_TOKENS-capped chunks (tiktoken cl100k_base,
    same encoding filing_ingestion already uses). Returns
    [(heading_or_empty, chunk_text), ...]."""
    matches = list(HEADING_RE.finditer(text))
    sections: list[tuple[str, str]] = []
    if not matches:
        sections.append(("", text))
    else:
        if matches[0].start() > 0:
            sections.append(("", text[: matches[0].start()]))
        for i, match in enumerate(matches):
            heading = match.group(2).strip()
            start = match.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            sections.append((heading, text[start:end]))

    chunks: list[tuple[str, str]] = []
    for heading, body in sections:
        paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
        if not paragraphs:
            continue
        current: list[str] = []
        current_tokens = 0
        for paragraph in paragraphs:
            paragraph_tokens = len(ENCODING.encode(paragraph))
            if current and current_tokens + paragraph_tokens > MAX_CHUNK_TOKENS:
                chunks.append((heading, "\n\n".join(current)))
                current, current_tokens = [], 0
            current.append(paragraph)
            current_tokens += paragraph_tokens
        if current:
            chunks.append((heading, "\n\n".join(current)))
    return chunks


def diff_latest(source: WikiSource) -> dict:
    """Paragraph-level diff between the two most recent snapshots, for the
    "what changed" tool. Returns {"available", "diff", "truncated",
    "from_fetched_at", "to_fetched_at"}. available=False if fewer than 2
    snapshots exist yet."""
    snapshots = list(source.snapshots.all()[:2])  # ordering: -fetched_at, -id => [newest, previous]
    if len(snapshots) < 2:
        return {"available": False, "diff": "", "truncated": False}

    newer, older = snapshots
    old_paragraphs = {p.strip() for p in older.text.split("\n\n") if p.strip()}
    new_paragraphs = [p.strip() for p in newer.text.split("\n\n") if p.strip()]

    added = [p for p in new_paragraphs if p not in old_paragraphs]
    new_set = set(new_paragraphs)
    removed = [p for p in old_paragraphs if p not in new_set]

    parts = [f"+ {p}" for p in added] + [f"- {p}" for p in removed]
    text = "\n\n".join(parts)
    truncated = len(text) > DIFF_CHAR_CAP
    if truncated:
        text = text[:DIFF_CHAR_CAP] + "\n\n(truncated)"
    return {
        "available": True, "diff": text, "truncated": truncated,
        "from_fetched_at": older.fetched_at.isoformat(), "to_fetched_at": newer.fetched_at.isoformat(),
    }
