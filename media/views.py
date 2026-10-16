import os

from rest_framework import permissions, status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from companies.services.sec_edgar import resolve_company

from .models import MediaFile
from .tasks import delete_media_file, process_media_file

EXTENSION_MEDIA_TYPES = {
    ".mp3": MediaFile.MEDIA_TYPE_AUDIO,
    ".mp4": MediaFile.MEDIA_TYPE_VIDEO,
}


def _media_file_row(media_file: MediaFile) -> dict:
    return {
        "id": media_file.id,
        "original_filename": media_file.original_filename,
        "media_type": media_file.media_type,
        "ticker": media_file.company.ticker if media_file.company_id else None,
        "duration_seconds": media_file.duration_seconds,
        "status": media_file.status,
        "error_message": media_file.error_message,
        "created_at": media_file.created_at,
        "updated_at": media_file.updated_at,
    }


def _get_media_file_or_404(user, media_file_id) -> MediaFile:
    try:
        return MediaFile.objects.get(id=media_file_id, user=user)
    except MediaFile.DoesNotExist:
        raise NotFound(f"No media file {media_file_id}.")


class MediaFileListView(APIView):
    """GET every media file the user has uploaded. POST uploads a new
    mp3/mp4 -- processing (audio extraction, diarized transcription,
    embedding into semantic search) starts automatically, no confirmation
    step: uploading the file is itself the user's authorization to
    process it. Result streams over the user's WebSocket group as
    media.progress / media.complete / media.failed (docs/websockets.md)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        media_files = MediaFile.objects.filter(user=request.user)
        return Response([_media_file_row(m) for m in media_files])

    def post(self, request):
        uploaded = request.FILES.get("file")
        if not uploaded:
            raise ValidationError({"file": ["This field is required."]})

        ext = os.path.splitext(uploaded.name)[1].lower()
        media_type = EXTENSION_MEDIA_TYPES.get(ext)
        if media_type is None:
            raise ValidationError({"file": [f"Unsupported file type {ext!r} -- only .mp3 and .mp4 are supported."]})

        company = None
        ticker = request.data.get("ticker")
        if ticker:
            try:
                company = resolve_company(ticker)
            except ValueError as exc:
                raise ValidationError({"ticker": [str(exc)]})

        media_file = MediaFile.objects.create(
            user=request.user,
            company=company,
            file=uploaded,
            original_filename=uploaded.name,
            media_type=media_type,
        )
        process_media_file.delay(media_file.id)
        return Response(_media_file_row(media_file), status=status.HTTP_202_ACCEPTED)


class MediaFileDetailView(APIView):
    """GET a single media file's status/detail. DELETE removes it --
    Postgres rows and the S3 file synchronously (so a follow-up GET/list
    reflects it immediately), the Weaviate segments via a Celery task
    (matches ResearchClip's DELETE precedent for one cheap-ish call not
    worth blocking the response on... except this can be several calls if
    there were many segments, hence the task instead of doing it inline)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, media_file_id):
        media_file = _get_media_file_or_404(request.user, media_file_id)
        return Response(_media_file_row(media_file))

    def delete(self, request, media_file_id):
        media_file = _get_media_file_or_404(request.user, media_file_id)
        segment_ids = list(media_file.segments.values_list("id", flat=True))
        media_file.file.delete(save=False)
        media_file.delete()
        delete_media_file.delay(media_file_id, segment_ids)
        return Response(status=status.HTTP_204_NO_CONTENT)
