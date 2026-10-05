"""
WebSocket consumer for realtime notifications.

Path: ws/notifications/
Auth: JWT (see notifications.middleware.JWTAuthMiddleware).
On connect, the authenticated user is added to group `notifications_<user_id>`.
Server pushes events of shape: {"type": "notification.new", "payload": {...}}
"""
import json
import logging

from channels.generic.websocket import AsyncJsonWebsocketConsumer

from .services import user_group_name

logger = logging.getLogger(__name__)


class NotificationConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        user = self.scope.get('user')
        if user is None or not getattr(user, 'is_authenticated', False):
            await self.close(code=4401)
            return

        self.group_name = user_group_name(user.pk)
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        await self.send_json({"type": "connection.ack", "user_id": user.pk})

    async def disconnect(self, code):
        group_name = getattr(self, 'group_name', None)
        if group_name:
            await self.channel_layer.group_discard(group_name, self.channel_name)

    async def receive_json(self, content, **kwargs):
        # This socket is server-push only; ignore client messages.
        return

    async def notification_new(self, event):
        """Handler for `notification.new` group events."""
        await self.send_json({
            "type": "notification.new",
            "payload": event.get("payload", {}),
        })
