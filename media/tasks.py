import logging
import os
import tempfile

from celery import shared_task
from django.conf import settings

from companies.services import embeddings
from realtime.utils import send_to_user

from .models import MediaFile, MediaSegment
from .services import ffmpeg, pipeline, vector_store

logger = logging.getLogger(__name__)

# What the user sees when processing fails. The real exception (ffmpeg
# stderr with server paths, provider API errors) goes to the worker log
# instead of the API response.
PROCESSING_FAILED_MESSAGE = "Processing failed -- check the Celery worker log for details."


@shared_task
def process_media_file(media_file_id):
    """Runs automatically once a file is uploaded -- no confirmation step
    (unlike the assistant's propose_filing_ingestion tool) since uploading
    the file *is* the user's explicit authorization to process it; nobody
    else decided this on their behalf mid-conversation.

    Downloads the file locally regardless of storage backend (S3 in prod,
    local disk in dev) since ffmpeg needs a real filesystem path -- Django's
    File API abstracts the read, so this works the same either way."""
    media_file = MediaFile.objects.select_related("user", "company").get(id=media_file_id)
    media_file.status = MediaFile.STATUS_PROCESSING
    media_file.save(update_fields=["status"])
    send_to_user(media_file.user_id, "media.progress", media_file_id=media_file.id, stage="downloading")

    try:
        with tempfile.TemporaryDirectory(prefix="media_upload_") as tmp_dir:
            ext = os.path.splitext(media_file.original_filename)[1] or ".bin"
            source_path = os.path.join(tmp_dir, f"source{ext}")
            with media_file.file.open("rb") as src, open(source_path, "wb") as dst:
                for chunk in src.chunks():
                    dst.write(chunk)

            if media_file.media_type == MediaFile.MEDIA_TYPE_VIDEO:
                send_to_user(media_file.user_id, "media.progress", media_file_id=media_file.id, stage="extracting_audio")
                audio_path = os.path.join(tmp_dir, "audio.mp3")
                ffmpeg.extract_audio(source_path, audio_path)
            else:
                audio_path = source_path

            media_file.duration_seconds = ffmpeg.get_duration(audio_path)
            media_file.save(update_fields=["duration_seconds"])

            send_to_user(media_file.user_id, "media.progress", media_file_id=media_file.id, stage="transcribing")
            merged_segments = pipeline.transcribe(audio_path)

        grouped = pipeline.group_into_segments(merged_segments)

        created = []
        for group in grouped:
            segment = MediaSegment.objects.create(
                media_file=media_file,
                speaker=group.speaker,
                start_seconds=group.start,
                end_seconds=group.end,
                text=group.text,
            )
            vector = embeddings.embed_text(segment.text)
            if vector is not None:
                vector_store.upsert_segment(segment, vector)
            created.append(segment)

    except Exception:
        logger.exception("process_media_file: failed for MediaFile %s", media_file.id)
        media_file.status = MediaFile.STATUS_FAILED
        media_file.error_message = PROCESSING_FAILED_MESSAGE
        media_file.save(update_fields=["status", "error_message"])
        send_to_user(
            media_file.user_id, "media.failed", media_file_id=media_file.id, message=PROCESSING_FAILED_MESSAGE
        )
        return

    media_file.status = MediaFile.STATUS_READY
    media_file.save(update_fields=["status"])
    send_to_user(
        media_file.user_id,
        "media.complete",
        media_file_id=media_file.id,
        segment_count=len(created),
        duration_seconds=media_file.duration_seconds,
    )


@shared_task
def delete_media_file(media_file_id, segment_ids):
    """Cleanup after a DELETE request -- removes the Weaviate objects. The
    Postgres MediaFile/MediaSegment rows and the S3 file are already gone
    by the time this runs (the view deletes those synchronously); this
    just handles the one thing worth not blocking the HTTP response on."""
    vector_store.delete_media_file(media_file_id, segment_ids)
