from rest_framework import permissions
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .models import Notification


def _row(n: Notification) -> dict:
    return {
        "id": n.id,
        "notification_type": n.notification_type,
        "title": n.title,
        "body": n.body,
        "payload": n.payload,
        "created_at": n.created_at,
        "acknowledged_at": n.acknowledged_at,
    }


class NotificationListView(APIView):
    """GET the requesting user's notifications, newest first, optionally
    filtered to unread with ?unread=1. Fills the gap PendingAction left (no
    list endpoint) -- a frontend can load notification history/unread
    state on page load independent of WebSocket connect timing."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        qs = Notification.objects.filter(user=request.user)
        if request.query_params.get("unread") == "1":
            qs = qs.filter(acknowledged_at__isnull=True)
        return Response([_row(n) for n in qs])


class NotificationAckView(APIView):
    """POST to mark one notification acknowledged. Scoped to request.user
    (404, not 403, if it belongs to someone else -- doesn't confirm
    existence to the wrong user). Idempotent: acking an already-acked
    notification 200s with the same body rather than erroring."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, notification_id):
        notification = services.acknowledge(request.user, notification_id)
        if notification is None:
            raise NotFound(f"No notification {notification_id}.")
        return Response(_row(notification))
