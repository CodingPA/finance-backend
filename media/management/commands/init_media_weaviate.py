from django.core.management.base import BaseCommand

from media.services.vector_store import MEDIA_SEGMENT_COLLECTION, ensure_collection


class Command(BaseCommand):
    help = "Create the Weaviate collection for MediaSegment embeddings if it doesn't already exist. Idempotent -- safe to rerun."

    def handle(self, *args, **options):
        ensure_collection()
        self.stdout.write(self.style.SUCCESS(f"Weaviate collection {MEDIA_SEGMENT_COLLECTION!r} ready."))
