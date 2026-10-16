from django.core.management.base import BaseCommand

from companies.services.vector_store import CLIP_COLLECTION, ensure_collection


class Command(BaseCommand):
    help = "Create the Weaviate collection for ResearchClip embeddings if it doesn't already exist. Idempotent -- safe to rerun."

    def handle(self, *args, **options):
        ensure_collection()
        self.stdout.write(self.style.SUCCESS(f"Weaviate collection {CLIP_COLLECTION!r} ready."))
