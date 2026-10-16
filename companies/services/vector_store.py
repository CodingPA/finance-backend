"""Weaviate wiring for semantic search over ResearchClip text. Infra only
for now -- nothing queries this yet; the actual search endpoint is parked
as a later decision (scope: per-ticker vs. global). This is
just the ingestion side: embed a clip on creation, remove it on deletion.

CLIP_COLLECTION is namespaced by WEAVIATE_ENVIRONMENT because this Weaviate
server is shared across other apps, and this app doesn't have a prod
instance yet -- the collection name carries both namespaces itself rather
than relying on a separate Weaviate database/tenant per app+environment."""

import uuid
from urllib.parse import urlparse

import weaviate
from django.conf import settings
from weaviate.auth import AuthApiKey
from weaviate.classes.config import Configure, DataType, Property
from weaviate.classes.query import Filter, MetadataQuery

CLIP_COLLECTION = f"FinanceApp_{settings.WEAVIATE_ENVIRONMENT}_ResearchClip"

# Deterministic namespace for deriving a Weaviate object id from a Django
# ResearchClip id, so there's nothing extra to store on the Django side to
# find/delete the matching Weaviate object later -- always re-derivable
# from the clip's own primary key.
_ID_NAMESPACE = uuid.UUID("6f1b1a1e-6e5a-4c3b-9f0a-8f5b1a2c3d4e")


def clip_object_id(clip_id: int) -> uuid.UUID:
    return uuid.uuid5(_ID_NAMESPACE, f"clip:{clip_id}")


def _connect() -> weaviate.WeaviateClient:
    parsed = urlparse(settings.WEAVIATE_URL)
    secure = parsed.scheme == "https"
    return weaviate.connect_to_custom(
        http_host=parsed.hostname,
        http_port=parsed.port or (443 if secure else 80),
        http_secure=secure,
        grpc_host=settings.WEAVIATE_GRPC_HOST,
        grpc_port=443,
        grpc_secure=True,
        auth_credentials=AuthApiKey(settings.WEAVIATE_API_KEY) if settings.WEAVIATE_API_KEY else None,
    )


def ensure_collection() -> None:
    """Idempotent -- creates CLIP_COLLECTION if it doesn't exist yet.
    Called from the init_weaviate management command, not at request time."""
    client = _connect()
    try:
        if not client.collections.exists(CLIP_COLLECTION):
            client.collections.create(
                CLIP_COLLECTION,
                vector_config=Configure.Vectors.self_provided(),
                properties=[
                    Property(name="clip_id", data_type=DataType.INT),
                    Property(name="user_id", data_type=DataType.TEXT),
                    Property(name="ticker", data_type=DataType.TEXT),
                    Property(name="category", data_type=DataType.TEXT),
                    Property(name="source_type", data_type=DataType.TEXT),
                    Property(name="label", data_type=DataType.TEXT),
                    Property(name="created_at", data_type=DataType.DATE),
                ],
            )
    finally:
        client.close()


def upsert_clip(clip, vector: list[float]) -> None:
    client = _connect()
    try:
        collection = client.collections.get(CLIP_COLLECTION)
        collection.data.insert(
            uuid=clip_object_id(clip.id),
            vector=vector,
            properties={
                "clip_id": clip.id,
                "user_id": str(clip.tracked_company.user_id),
                "ticker": clip.tracked_company.company.ticker,
                "category": clip.category,
                "source_type": clip.source_type,
                "label": clip.label,
                "created_at": clip.created_at,
            },
        )
    finally:
        client.close()


def delete_clip(clip_id: int) -> None:
    client = _connect()
    try:
        collection = client.collections.get(CLIP_COLLECTION)
        collection.data.delete_by_id(clip_object_id(clip_id))
    finally:
        client.close()


def search_clips(vector: list[float], user_id: str, ticker: str | None = None, limit: int = 10) -> list[dict]:
    """Nearest-neighbor search, always scoped to user_id -- clips are
    per-user data, and this is the only thing standing between one user's
    search and another user's pasted material. ticker further narrows to
    one company; omitted, this is the global cross-portfolio search.
    Returns Weaviate's own properties only (no clip text -- callers join
    back to ResearchClip in Postgres for that), ordered nearest-first."""
    client = _connect()
    try:
        collection = client.collections.get(CLIP_COLLECTION)
        filters = Filter.by_property("user_id").equal(user_id)
        if ticker:
            filters = filters & Filter.by_property("ticker").equal(ticker)
        response = collection.query.near_vector(
            near_vector=vector,
            limit=limit,
            filters=filters,
            return_metadata=MetadataQuery(distance=True),
        )
        return [
            {
                "clip_id": obj.properties["clip_id"],
                "distance": obj.metadata.distance,
            }
            for obj in response.objects
        ]
    finally:
        client.close()
