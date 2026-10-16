"""Filing ingestion: fetch one EDGAR filing, parse+chunk it with Docling
(tables are stripped before chunking -- they serialize into per-cell
"Label, N = Value" noise that outranks real prose in retrieval), and
save each chunk as a ResearchClip. Extracted from
management/commands/seed_filing_clips.py so the pipeline itself lives in
one place -- that command (bulk, CLI) and the assistant's filing-ingestion
tool (targeted, one filing, gated on user confirmation) both call this
rather than each carrying their own copy of the Docling wiring."""

import io

import tiktoken

from ..models import ResearchClip, TrackedCompany
from . import embeddings, http, vector_store

ENCODING = tiktoken.get_encoding("cl100k_base")


def is_ingested(tracked_company: TrackedCompany, accession_number: str) -> bool:
    """Whether any ResearchClip already exists for this filing (matched by
    accession number embedded in `source`) -- "available but not ingested"
    in the dashboard's sense."""
    return tracked_company.clips.filter(source__contains=accession_number).exists()


def ingest_filing(
    tracked_company: TrackedCompany, filing: dict, max_tokens: int = 1000, keep_tables: bool = False, force: bool = False
) -> int:
    """Fetch, parse, chunk, and save one filing as ResearchClips for
    tracked_company. Returns the number of clips created (0 if this filing
    was already ingested and force=False -- a no-op, not an error).
    Raises ValueError if Docling produces zero chunks (parsing likely
    failed silently)."""
    # Imported here, not at module level: docling pulls in the full torch/
    # OCR/PDF stack (~527MB, docling-slim[standard]'s own install comment
    # in requirements.txt) with no partial-install path, and
    # preprocess_edgar_html (below) pulls in beautifulsoup4/lxml --
    # requirements-filings.txt's other two packages. This module gets
    # imported unconditionally at Django startup -- core/__init__.py ->
    # Celery's app.autodiscover_tasks() -> assistant/tasks.py -> here --
    # so a module-level import would load all of that into every uvicorn
    # worker, Celery worker, and manage.py invocation whether or not
    # anyone ever ingests a filing. Confirmed live: an earlier version of
    # this fix moved only the docling imports and missed
    # preprocess_edgar_html, which on its own was enough to crash every
    # process's startup with ModuleNotFoundError: No module named 'bs4'
    # the moment requirements-filings.txt wasn't installed (the correct,
    # intended default) -- caught by an actual clean-environment run
    # (Docker, no filings extras installed), not by this project's own
    # dev venv, which already had bs4 installed from before the
    # requirements split and so never exercised its absence.
    from docling.chunking import HybridChunker
    from docling.document_converter import DocumentConverter
    from docling_core.transforms.chunker.tokenizer.openai import OpenAITokenizer
    from docling_core.types.io import DocumentStream

    from .edgar_html import preprocess_edgar_html

    ticker = tracked_company.company.ticker
    accession = filing["accession_number"]

    existing = tracked_company.clips.filter(source__contains=accession)
    if existing.exists():
        if not force:
            return 0
        for clip in existing:
            vector_store.delete_clip(clip.id)
        existing.delete()

    resp = http.get(
        filing["url"], "filing_document_ingest", provider="sec", ticker=ticker, company=tracked_company.company
    )
    filename = filing["url"].rsplit("/", 1)[-1]
    cleaned_html = preprocess_edgar_html(resp.text)

    stream = DocumentStream(name=filename, stream=io.BytesIO(cleaned_html.encode("utf-8")))
    doc = DocumentConverter().convert(source=stream).document

    if not keep_tables:
        doc.delete_items(node_items=list(doc.tables))

    tokenizer = OpenAITokenizer(tokenizer=ENCODING, max_tokens=max_tokens)
    chunker = HybridChunker(tokenizer=tokenizer, merge_peers=True)
    chunks = list(chunker.chunk(doc))
    if not chunks:
        raise ValueError("Docling produced zero chunks -- parsing likely failed silently.")

    source = f"{filing['form']} filed {filing['filing_date']}, accession {accession}: {filing['url']}"[:500]
    created_count = 0
    for chunk in chunks:
        text = chunker.contextualize(chunk)
        heading = " > ".join(chunk.meta.headings or []) or "(no heading)"
        label = f"{ticker} {filing['form']} {filing['filing_date']} -- {heading}"[:255]
        clip = ResearchClip.objects.create(
            tracked_company=tracked_company,
            category=ResearchClip.CATEGORY_CONTEXT,
            source_type=ResearchClip.SOURCE_TYPE_FILING,
            label=label,
            source=source,
            text=text,
        )
        vector = embeddings.embed_text(clip.text)
        if vector is not None:
            vector_store.upsert_clip(clip, vector)
        created_count += 1

    return created_count
