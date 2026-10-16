"""Vertical spike for filing ingestion: fetch one real filing, parse it
with Docling, chunk it with HybridChunker, embed the chunks, run one
in-memory similarity query.

Deliberately writes nothing to Postgres or Weaviate -- no schema exists yet
for filing chunks (they aren't ResearchClips: no category, no user, a
different shape entirely). The point is to look at real chunk output and
real retrieval quality by hand before committing to that schema. Once
that's decided, this becomes a real Celery task + model, not a command."""

import io

import numpy as np
import tiktoken
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from docling.chunking import HybridChunker
from docling.document_converter import DocumentConverter
from docling_core.transforms.chunker.tokenizer.openai import OpenAITokenizer
from docling_core.types.io import DocumentStream
from openai import OpenAI

from companies.models import Company
from companies.services import http, sec_edgar
from companies.services.edgar_html import preprocess_edgar_html


def _embed_batch(texts: list[str], encoding) -> list[list[float]]:
    """OpenAI's embeddings endpoint caps a single request at 300k input
    tokens total (separate from the 8191/input limit) -- a big filing's
    full chunk set can exceed that on its own (RDW's 10-K: 449 chunks,
    ~394k tokens), so this batches by running token count. Also capped at
    2048 inputs per request -- a filing full of small chunks can hit that
    ceiling without coming close to the token cap, so both limits gate
    each batch independently."""
    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    vectors = []
    batch, batch_tokens = [], 0
    for text in texts:
        tokens = len(encoding.encode(text))
        if batch and (batch_tokens + tokens > 250_000 or len(batch) >= 2048):
            response = client.embeddings.create(model=settings.OPENAI_EMBEDDING_MODEL, input=batch)
            vectors.extend(d.embedding for d in sorted(response.data, key=lambda d: d.index))
            batch, batch_tokens = [], 0
        batch.append(text)
        batch_tokens += tokens
    if batch:
        response = client.embeddings.create(model=settings.OPENAI_EMBEDDING_MODEL, input=batch)
        vectors.extend(d.embedding for d in sorted(response.data, key=lambda d: d.index))
    return vectors


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


class Command(BaseCommand):
    help = (
        "Spike: parse + chunk + embed one real filing (Docling + HybridChunker), "
        "then run one in-memory similarity query to eyeball chunk/retrieval "
        "quality. Writes nothing to the database or Weaviate."
    )

    DEFAULT_QUERIES = [
        "risks from launch vehicle failures and anomalies",
        "revenue backlog and book-to-bill",
        "customer concentration and dependence on a small number of customers",
    ]

    def add_arguments(self, parser):
        parser.add_argument("ticker", nargs="?", default="RKLB")
        parser.add_argument("--form", default="10-K")
        parser.add_argument(
            "--query", action="append", dest="queries",
            help="Repeatable. Defaults to the three re-test checklist queries if omitted.",
        )
        parser.add_argument("--max-tokens", type=int, default=1000)
        parser.add_argument("--top", type=int, default=5)
        parser.add_argument(
            "--keep-tables", action="store_true",
            help=(
                "Skip the table-removal step and chunk the document as Docling produced it, "
                "tables included. Default is to "
                "remove TableItems before chunking, since they serialize into per-cell "
                "'Label, N = Value' noise that outranks real prose in retrieval and a 27% "
                "mixed-chunk rate across 5 filings ruled out filtering after the fact."
            ),
        )

    def handle(self, *args, **options):
        if not settings.OPENAI_API_KEY:
            raise CommandError("OPENAI_API_KEY is not set -- needed to embed chunks for the query step.")

        ticker = options["ticker"].upper()
        company = Company.objects.filter(ticker=ticker).first()
        if not company:
            raise CommandError(f"{ticker} isn't in the database yet -- run `manage.py screen {ticker}` first.")

        submissions = sec_edgar.get_submissions(company)
        filings = sec_edgar.list_filings(submissions, forms=(options["form"],))
        if not filings:
            raise CommandError(f"No {options['form']} filings found for {ticker}.")
        filing = filings[0]
        self.stdout.write(f"{ticker}: {filing['form']} filed {filing['filing_date']}\n  {filing['url']}\n")

        self.stdout.write("Fetching filing document...")
        resp = http.get(filing["url"], "filing_document_spike", provider="sec", ticker=ticker, company=company)
        filename = filing["url"].rsplit("/", 1)[-1]

        self.stdout.write("Preprocessing EDGAR HTML (unwrap iXBRL, promote Item headings)...")
        cleaned_html = preprocess_edgar_html(resp.text)

        self.stdout.write("Parsing with Docling...")
        stream = DocumentStream(name=filename, stream=io.BytesIO(cleaned_html.encode("utf-8")))
        result = DocumentConverter().convert(source=stream)
        doc = result.document

        if not options["keep_tables"]:
            n_tables = len(doc.tables)
            doc.delete_items(node_items=list(doc.tables))
            self.stdout.write(f"Removed {n_tables} TableItems before chunking (--keep-tables to skip this).")

        self.stdout.write("Chunking with HybridChunker...")
        encoding = tiktoken.get_encoding("cl100k_base")
        tokenizer = OpenAITokenizer(tokenizer=encoding, max_tokens=options["max_tokens"])
        chunker = HybridChunker(tokenizer=tokenizer, merge_peers=True)

        chunks = []
        for i, chunk in enumerate(chunker.chunk(doc)):
            text = chunker.contextualize(chunk)
            doc_items = chunk.meta.doc_items or []
            chunks.append({
                "index": i,
                "text": text,
                "token_count": len(encoding.encode(text)),
                "element_type": doc_items[0].label if doc_items else None,
                "heading_path": list(chunk.meta.headings or []),
            })

        if not chunks:
            raise CommandError("Docling produced zero chunks -- parsing likely failed silently. Inspect `doc` by hand.")

        token_counts = [c["token_count"] for c in chunks]
        self.stdout.write(self.style.SUCCESS(
            f"\n{len(chunks)} chunks. tokens: min={min(token_counts)} "
            f"max={max(token_counts)} avg={sum(token_counts) / len(token_counts):.0f}\n"
        ))

        heading_count = sum(1 for c in chunks if c["heading_path"])
        self.stdout.write(f"Chunks with a non-empty heading_path: {heading_count}/{len(chunks)}")

        self.stdout.write("\n--- first 5 chunks ---")
        for c in chunks[:5]:
            heading = " > ".join(c["heading_path"]) or "(no heading)"
            self.stdout.write(f"\n[{c['index']}] {c['element_type']} | {c['token_count']} tok | {heading}")
            self.stdout.write(f"  {c['text'][:200]!r}")

        self.stdout.write("\nEmbedding all chunks (batched)...")
        vectors = _embed_batch([c["text"] for c in chunks], encoding)
        for c, v in zip(chunks, vectors):
            c["vector"] = np.array(v)
        self.stdout.write(f"Embedded {len(vectors)} chunks, {len(vectors[0])} dims each.")

        queries = options["queries"] or self.DEFAULT_QUERIES
        for query in queries:
            self.stdout.write(f"\n\n=== Query: {query!r} ===")
            query_vector = np.array(_embed_batch([query], encoding)[0])

            ranked = sorted(chunks, key=lambda c: _cosine(query_vector, c["vector"]), reverse=True)
            for c in ranked[: options["top"]]:
                score = _cosine(query_vector, c["vector"])
                heading = " > ".join(c["heading_path"]) or "(no heading)"
                self.stdout.write(f"\n[{c['index']}] score={score:.4f} | {heading}")
                self.stdout.write(f"  {c['text'][:300]!r}")
