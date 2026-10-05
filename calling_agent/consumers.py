from __future__ import annotations

import logging
from typing import cast
from uuid import UUID

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from calling_agent.live_transcript import (
    LIVE_CALL_STATUSES,
    TERMINAL_CALL_STATUSES,
    call_transcript_group_name,
    ensure_live_transcript_monitor,
    live_transcript_enabled,
)
from calling_agent.models import Call
from calling_agent.services import calls_visible_to_user

logger = logging.getLogger(__name__)


class CallTranscriptConsumer(AsyncJsonWebsocketConsumer):
    """Server-push live transcript events for a single Call."""

    async def connect(self):
        user = self.scope.get('user')
        if user is None or not getattr(user, 'is_authenticated', False):
            await self.close(code=4401)
            return

        public_id = self.scope.get('url_route', {}).get('kwargs', {}).get(
            'public_id'
        )
        call = await self._get_visible_call(user, public_id)
        if call is None:
            await self.close(code=4403)
            return

        self.call_public_id = str(call.public_id)
        self.call_pk = call.pk
        self.group_name = call_transcript_group_name(self.call_public_id)
        self._ended = call.status in TERMINAL_CALL_STATUSES
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        await self.send_json({
            'type': 'connection.ack',
            'call_id': self.call_public_id,
        })
        status_value = 'ended' if self._ended else 'processing'
        await self.send_json({
            'type': 'transcript.status',
            'payload': {
                'call_id': self.call_public_id,
                'status': status_value,
            },
        })
        if self._ended:
            await self.send_json({
                'type': 'call.ended',
                'payload': {
                    'call_id': self.call_public_id,
                    'status': call.status,
                },
            })
            return
        if (
            live_transcript_enabled()
            and call.status in LIVE_CALL_STATUSES
        ):
            try:
                await ensure_live_transcript_monitor(call.pk)
            except Exception:
                logger.exception(
                    'Failed to start live transcript monitor public_id=%s',
                    self.call_public_id,
                )

    async def disconnect(self, code):
        group_name = getattr(self, 'group_name', None)
        if group_name:
            await self.channel_layer.group_discard(group_name, self.channel_name)

    async def receive_json(self, content, **kwargs):
        return

    async def transcript_turn(self, event):
        if getattr(self, '_ended', False):
            return
        await self.send_json({
            'type': 'transcript.turn',
            'payload': event.get('payload', {}),
        })

    async def transcript_status(self, event):
        await self.send_json({
            'type': 'transcript.status',
            'payload': event.get('payload', {}),
        })

    async def call_ended(self, event):
        self._ended = True
        await self.send_json({
            'type': 'call.ended',
            'payload': event.get('payload', {}),
        })

    @database_sync_to_async
    def _get_visible_call(self, user, public_id) -> Call | None:
        try:
            parsed = UUID(str(public_id))
        except (TypeError, ValueError):
            return None
        return cast(
            Call | None,
            calls_visible_to_user(user).filter(public_id=parsed).first(),
        )
