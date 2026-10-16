"""OpenAI embeddings for ResearchClip text, feeding vector_store.py. Same
graceful-without-a-key convention as fmp.py's FMP_API_KEY handling: nothing
crashes if OPENAI_API_KEY isn't set, indexing just doesn't happen yet."""

from django.conf import settings
from openai import OpenAI


def embed_text(text: str) -> list[float] | None:
    if not settings.OPENAI_API_KEY:
        return None
    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    response = client.embeddings.create(model=settings.OPENAI_EMBEDDING_MODEL, input=text)
    return response.data[0].embedding
