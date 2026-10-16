"""The only module that touches the Notification model directly, so the
wire payload shape sent by notify()'s live push and by the reconnect-time
backlog flush (realtime/consumers.py) are guaranteed identical -- a
frontend never has to special-case which path delivered a given
notification."""

from django.utils import timezone

from realtime.utils import send_to_user

from .models import Notification


def _wire_payload(notification: Notification) -> dict:
    """Never include a "type" key here -- send_to_user() already injects
    "type": message_type at the top level; a nested "type" inside this
    dict would collide with it."""
    return {
        "id": notification.id,
        "notification_type": notification.notification_type,
        "title": notification.title,
        "body": notification.body,
        "payload": notification.payload,
        "created_at": notification.created_at.isoformat(),
    }


def notify(user, notification_type: str, title: str, body: str = "", payload: dict | None = None) -> Notification:
    """Create a Notification row and push it live over the user's
    WebSocket group. The push is best-effort -- if they're offline right
    now it just vanishes, same as every other send_to_user call -- but the
    row persisting is what lets unacknowledged_payloads() redeliver it the
    next time they connect. notification_type is dot-namespaced, matching
    the existing screen.*/assistant.*/media.* convention (e.g.
    "wiki.page_changed")."""
    notification = Notification.objects.create(
        user=user, notification_type=notification_type, title=title, body=body, payload=payload or {},
    )
    send_to_user(user.id, "notification.new", **_wire_payload(notification))
    return notification


def unacknowledged_payloads(user_id) -> list[dict]:
    """Every still-unacknowledged notification for a user, oldest first, as
    plain wire-shaped dicts -- already list()-realized, since the only
    caller today (realtime/consumers.py, across an async boundary) can't
    evaluate a lazy queryset on its own thread."""
    qs = Notification.objects.filter(user_id=user_id, acknowledged_at__isnull=True).order_by("created_at")
    return [_wire_payload(n) for n in qs]


def acknowledge(user, notification_id: int) -> Notification | None:
    """Idempotent ack: sets acknowledged_at if unset, no-ops (returns the
    row unchanged) if already acknowledged -- a double-ack from a flaky
    frontend is not an error. Returns None if the notification doesn't
    exist or isn't this user's, so the caller can turn that into a 404
    without confirming to the wrong user that some other id exists."""
    notification = Notification.objects.filter(id=notification_id, user=user).first()
    if notification is None:
        return None
    if notification.acknowledged_at is None:
        notification.acknowledged_at = timezone.now()
        notification.save(update_fields=["acknowledged_at"])
    return notification
