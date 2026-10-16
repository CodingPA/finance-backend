from django.conf import settings
from django.db import models


class Notification(models.Model):
    """A durable, ack'd notification. Unlike realtime.utils.send_to_user's
    fire-and-forget WebSocket push (which vanishes if the user isn't
    connected right now), this row persists until the client explicitly
    acknowledges it -- see notifications/services.py::notify() for the live
    push and unacknowledged_payloads() for what gets redelivered to a
    reconnecting client. acknowledged_at is a timestamp rather than a bool
    so the ack itself is retained for display/audit, matching
    PendingAction.resolved_at's precedent."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    notification_type = models.CharField(max_length=64)
    title = models.CharField(max_length=255)
    body = models.TextField(blank=True)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "acknowledged_at"])]

    def __str__(self):
        return f"{self.user_id}:{self.notification_type}:{self.pk}"
