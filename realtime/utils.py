from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from .consumers import user_group_name


def send_to_user(user_id, message_type: str, **payload) -> None:
    """Push a JSON message to every open WebSocket connection for a user
    (all their open tabs). Safe to call from sync code -- Celery tasks,
    Django views -- via async_to_sync; the consumer's async side handles
    actually writing to the socket.

    Example, from a Celery task:
        send_to_user(user.id, "screen.progress", ticker="RKLB", pct=50)
    Frontend receives: {"type": "screen.progress", "ticker": "RKLB", "pct": 50}
    """
    broadcast_to_group(user_group_name(user_id), message_type, **payload)


def broadcast_to_group(group_name: str, message_type: str, **payload) -> None:
    """Push a JSON message to every open WebSocket connection joined to an
    arbitrary Channels group (not just a per-user one) -- e.g. every
    browser watching a league's live trade feed
    (realtime.consumers.league_group_name), regardless of which user owns
    which competitor. Safe to call from sync code, same as send_to_user.
    """
    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        group_name,
        {"type": "task.message", "payload": {"type": message_type, **payload}},
    )
