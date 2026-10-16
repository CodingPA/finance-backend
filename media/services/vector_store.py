"""Weaviate wiring for semantic search over MediaSegment transcript text.
Parallel structure to companies/services/vector_store.py (same Weaviate
server, same self_provided()-vector / deterministic-uuid conventions) but
a separate collection -- MediaSegment's properties (speaker, timestamps)
don't fit ResearchClip's schema, and the two are different apps."""

import uuid
from urllib.parse import urlparse

import weaviate
from django.conf import settings
from weaviate.auth import AuthApiKey
from weaviate.classes.config import Configure, DataType, Property
from weaviate.classes.query import Filter, MetadataQuery

MEDIA_SEGMENT_COLLECTION = f"FinanceApp_{settings.WEAVIATE_ENVIRONMENT}_MediaSegment"

# Deterministic namespace for deriving a Weaviate object id from a Django
# MediaSegment id -- same technique as ResearchClip's, a different UUID so
# the two id spaces never collide.
_ID_NAMESPACE = uuid.UUID("9c2b6a4e-1f3d-4e7a-8b5c-2d6e9f1a3b7c")


def segment_object_id(segment_id: int) -> uuid.UUID:
    return uuid.uuid5(_ID_NAMESPACE, f"media_segment:{segment_id}")


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
    """Idempotent -- creates MEDIA_SEGMENT_COLLECTION if it doesn't exist
    yet. Called from the init_media_weaviate management command, not at
    request time."""
    client = _connect()
    try:
        if not client.collections.exists(MEDIA_SEGMENT_COLLECTION):
            client.collections.create(
                MEDIA_SEGMENT_COLLECTION,
                vector_config=Configure.Vectors.self_provided(),
                properties=[
                    Property(name="segment_id", data_type=DataType.INT),
                    Property(name="user_id", data_type=DataType.TEXT),
                    Property(name="ticker", data_type=DataType.TEXT),
                    Property(name="speaker", data_type=DataType.TEXT),
                    Property(name="start_seconds", data_type=DataType.NUMBER),
                    Property(name="end_seconds", data_type=DataType.NUMBER),
                    Property(name="created_at", data_type=DataType.DATE),
                ],
            )
    finally:
        client.close()


def upsert_segment(segment, vector: list[float]) -> None:
    client = _connect()
    try:
        collection = client.collections.get(MEDIA_SEGMENT_COLLECTION)
        collection.data.insert(
            uuid=segment_object_id(segment.id),
            vector=vector,
            properties={
                "segment_id": segment.id,
                "user_id": str(segment.media_file.user_id),
                "ticker": segment.media_file.company.ticker if segment.media_file.company_id else "",
                "speaker": segment.speaker,
                "start_seconds": segment.start_seconds,
                "end_seconds": segment.end_seconds,
                "created_at": segment.created_at,
            },
        )
    finally:
        client.close()


def delete_segment(segment_id: int) -> None:
    client = _connect()
    try:
        collection = client.collections.get(MEDIA_SEGMENT_COLLECTION)
        collection.data.delete_by_id(segment_object_id(segment_id))
    finally:
        client.close()


def delete_media_file(media_file_id: int, segment_ids: list[int]) -> None:
    """Batch-delete every segment belonging to one MediaFile (used when the
    file itself is deleted) -- one connection instead of one per segment."""
    client = _connect()
    try:
        collection = client.collections.get(MEDIA_SEGMENT_COLLECTION)
        for segment_id in segment_ids:
            collection.data.delete_by_id(segment_object_id(segment_id))
    finally:
        client.close()


def search_segments(vector: list[float], user_id: str, ticker: str | None = None, limit: int = 10) -> list[dict]:
    """Nearest-neighbor search, always scoped to user_id -- media transcripts
    are per-user data, same privacy boundary as ResearchClip search. ticker
    further narrows to one company's media."""
    client = _connect()
    try:
        collection = client.collections.get(MEDIA_SEGMENT_COLLECTION)
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
                "segment_id": obj.properties["segment_id"],
                "distance": obj.metadata.distance,
            }
            for obj in response.objects
        ]
    finally:
        client.close()
