import logging

from channels.generic.websocket import AsyncJsonWebsocketConsumer

logger = logging.getLogger(__name__)


class WhatsAppConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        user = self.scope.get("user")
        if user is None or not getattr(user, "is_authenticated", False):
            await self.close(code=4401)
            return

        self.group_name = f"whatsapp_{user.pk}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        await self.send_json({"type": "connection.ack", "user_id": user.pk})

    async def disconnect(self, code):
        group_name = getattr(self, "group_name", None)
        if group_name:
            await self.channel_layer.group_discard(group_name, self.channel_name)

    async def receive_json(self, content, **kwargs):
        return

    async def whatsapp_message(self, event):
        await self.send_json({
            "type": "whatsapp.message",
            "payload": event.get("payload", {}),
        })

    async def whatsapp_message_ack(self, event):
        await self.send_json({
            "type": "whatsapp.message_ack",
            "payload": event.get("payload", {}),
        })

    async def whatsapp_message_reaction(self, event):
        await self.send_json({
            "type": "whatsapp.message_reaction",
            "payload": event.get("payload", {}),
        })

