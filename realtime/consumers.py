from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.contrib.auth import get_user_model
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import AccessToken

User = get_user_model()


def user_group_name(user_id) -> str:
    return f"user_{user_id}"


def league_group_name(competition_id) -> str:
    return f"league_{competition_id}"


class _TokenAuthGroupConsumer(AsyncJsonWebsocketConsumer):
    """Shared shape for a group-subscription WebSocket consumer: query-param
    JWT auth (handshakes carry no Authorization header, so the access token
    travels as ?token=<access_token>), join self.group_name on connect,
    leave it on disconnect, relay group_send(..., {"type": "task.message"})
    events straight to the socket. Subclasses set self.group_name inside an
    overridden connect() before calling super()."""

    async def _authenticate(self):
        query_string = self.scope.get("query_string", b"").decode()
        raw_token = parse_qs(query_string).get("token", [None])[0]
        if not raw_token:
            return None
        return await self._get_user(raw_token)

    @database_sync_to_async
    def _get_user(self, raw_token):
        try:
            validated = AccessToken(raw_token)
            return User.objects.get(id=validated["user_id"], is_active=True)
        except (TokenError, User.DoesNotExist, KeyError, ValueError):
            return None

    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def task_message(self, event):
        """Handler for group_send(..., {"type": "task.message", "payload": {...}}).
        Channels maps the "." in the type string to "_" for the method name."""
        await self.send_json(event["payload"])


class UserConsumer(_TokenAuthGroupConsumer):
    """One connection per browser tab; joins a group keyed on the user's
    (alphanumeric, UUID) id. Anything -- a Celery task, a view -- can push a
    message to every open connection for that user via
    realtime.utils.send_to_user(), without needing to know which server
    process or worker is holding the socket open."""

    async def connect(self):
        user = await self._authenticate()
        if user is None:
            await self.close(code=4001)
            return
        self.user = user
        self.group_name = user_group_name(user.id)
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        await self._flush_notification_backlog()

    async def _flush_notification_backlog(self):
        """Redeliver anything the user missed while disconnected --
        notifications.services.notify()'s live push vanishes if nobody's
        connected, so this is what makes delivery durable: every unacked
        notification gets resent, in order, the moment they reconnect.
        Imported locally (not at module level) so realtime stays
        domain-agnostic/transport-only at import time -- it's the one
        function-local exception to that."""
        for payload in await self._unacknowledged_payloads():
            await self.send_json({"type": "notification.new", **payload})

    @database_sync_to_async
    def _unacknowledged_payloads(self):
        from notifications.services import unacknowledged_payloads
        return unacknowledged_payloads(self.user.id)


class LeagueFeedConsumer(_TokenAuthGroupConsumer):
    """One connection per browser tab watching a competition's live trade
    feed -- joins group league_{competition_id}, broadcast into by
    league.services.execution.submit_trade() every time a Trade row is
    created (filled or rejected), for every competitor in that
    competition, human or AI. Unlike UserConsumer's per-owner group, this
    is competition-wide: anyone authenticated can watch every competitor's
    activity, not just their own.

    No backlog replay on reconnect (unlike UserConsumer's notification
    backlog) -- this is a live monitor, not a durable inbox; GET
    /api/league/competitions/<id>/trades/ is the source of truth for
    anything missed while disconnected."""

    async def connect(self):
        competition_id = self.scope["url_route"]["kwargs"]["competition_id"]
        user = await self._authenticate()
        if user is None:
            await self.close(code=4001)
            return
        self.group_name = league_group_name(competition_id)
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
