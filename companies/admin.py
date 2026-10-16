from django.contrib import admin

from .models import SubredditMention, TrackedSubreddit


@admin.register(TrackedSubreddit)
class TrackedSubredditAdmin(admin.ModelAdmin):
    # last_scanned_at second, right after the name -- the "is the scan job
    # actually reaching this row" check (see that field's own docstring:
    # set at the START of every attempt, success or failure, so a stuck/
    # never-advancing value here is the direct signal something's wrong,
    # independent of whether any mentions were ever found).
    list_display = ["name", "last_scanned_at", "user", "added_at"]
    list_filter = ["user"]
    search_fields = ["name", "user__email"]


@admin.register(SubredditMention)
class SubredditMentionAdmin(admin.ModelAdmin):
    # scanned_at first and list_filter'd -- the two things worth checking
    # when the scan job's health is in question: "did anything land
    # recently" (sort/filter by scanned_at) and "for whom/where" (user,
    # subreddit). Everything here is written by companies.tasks.
    # scan_tracked_subreddits only, never edited by hand -- readonly_fields
    # keeps this a read-only diagnostic view rather than inviting someone
    # to hand-edit a row that a future scan's get_or_create would just
    # leave alone anyway (it only ever sets defaults on first creation).
    list_display = ["scanned_at", "subreddit", "ticker", "title", "user", "score", "num_comments"]
    list_filter = ["subreddit", "ticker", "user"]
    search_fields = ["ticker", "subreddit", "title"]
    readonly_fields = [
        "user", "subreddit", "ticker", "post_id", "title", "permalink",
        "excerpt", "score", "num_comments", "scanned_at",
    ]
