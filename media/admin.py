from django.contrib import admin

from .models import MediaFile, MediaSegment


class MediaSegmentInline(admin.TabularInline):
    model = MediaSegment
    extra = 0
    fields = ["speaker", "start_seconds", "end_seconds", "text", "created_at"]
    readonly_fields = fields
    can_delete = False


@admin.register(MediaFile)
class MediaFileAdmin(admin.ModelAdmin):
    list_display = [
        "id", "original_filename", "user", "company", "media_type",
        "duration_seconds", "status", "created_at",
    ]
    list_filter = ["media_type", "status", "company"]
    inlines = [MediaSegmentInline]


@admin.register(MediaSegment)
class MediaSegmentAdmin(admin.ModelAdmin):
    list_display = ["id", "media_file", "speaker", "start_seconds", "end_seconds", "created_at"]
    list_filter = ["speaker"]
