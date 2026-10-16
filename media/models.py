from django.conf import settings
from django.db import models


class MediaFile(models.Model):
    """A user-uploaded mp3/mp4, stored in S3 (see STORAGES in settings.py --
    `file` is a plain FileField, the storage backend handles S3 vs local
    transparently). Not tied to a Conversation -- like ResearchClip, this
    is a standalone per-user asset searchable across every conversation,
    not scoped to whichever chat it happened to be mentioned in.

    Processing (audio extraction, diarized transcription, chunking into
    MediaSegment) starts automatically once uploaded -- unlike the
    assistant's propose_filing_ingestion tool, there's no separate
    confirmation step here, because uploading the file *is* the user's
    explicit authorization to process it (nobody else decided to do this
    on the user's behalf)."""

    MEDIA_TYPE_AUDIO = "audio"
    MEDIA_TYPE_VIDEO = "video"
    MEDIA_TYPE_CHOICES = [
        (MEDIA_TYPE_AUDIO, "Audio"),
        (MEDIA_TYPE_VIDEO, "Video"),
    ]

    STATUS_UPLOADED = "uploaded"
    STATUS_PROCESSING = "processing"
    STATUS_READY = "ready"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_UPLOADED, "Uploaded"),
        (STATUS_PROCESSING, "Processing"),
        (STATUS_READY, "Ready"),
        (STATUS_FAILED, "Failed"),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="media_files")
    company = models.ForeignKey(
        "companies.Company", on_delete=models.SET_NULL, null=True, blank=True, related_name="media_files"
    )
    file = models.FileField(upload_to="media_uploads/%Y/%m/")
    original_filename = models.CharField(max_length=255)
    media_type = models.CharField(max_length=16, choices=MEDIA_TYPE_CHOICES)
    duration_seconds = models.FloatField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_UPLOADED)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.original_filename} ({self.status})"


class MediaSegment(models.Model):
    """One embeddable, speaker-attributed chunk of a MediaFile's
    transcript -- consecutive same-speaker utterances merged up to a
    ~1000-token budget (same convention as the Docling filing chunker),
    never mixing speakers within one chunk since that would lose the
    "who said this" attribution that's the whole point of diarizing in
    the first place. Embedded and upserted into the MediaSegment Weaviate
    collection (media/services/vector_store.py) the same way ResearchClips
    are, just a separate collection since the properties differ
    (speaker/timestamps vs. category/source_type)."""

    media_file = models.ForeignKey(MediaFile, on_delete=models.CASCADE, related_name="segments")
    speaker = models.CharField(max_length=32)
    start_seconds = models.FloatField()
    end_seconds = models.FloatField()
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["start_seconds"]

    def __str__(self):
        return f"{self.media_file_id}:{self.speaker}@{self.start_seconds:.0f}s"
