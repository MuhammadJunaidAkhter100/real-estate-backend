from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from calling_agent.live_transcript import (
    LiveTranscriptSession,
    build_monitor_ws_url,
    call_transcript_group_name,
    relative_call_time_secs,
)
from calling_agent.models import Call, CallTranscriptTurn
from calling_agent.tests.factories import create_company, create_lead, create_user
from calling_agent.webhook_services import CallStatusTransitionService
from users.models import User

CHANNEL_LAYERS_MEMORY = {
    'default': {
        'BACKEND': 'channels.layers.InMemoryChannelLayer',
    },
}


def _access_token(user: User) -> str:
    return str(RefreshToken.for_user(user).access_token)


def _ws_path(public_id, token: str | None = None) -> str:
    path = f'/ws/calling-agent/calls/{public_id}/transcript/'
    if token:
        return f'{path}?token={token}'
    return path


async def _safe_disconnect(communicator: WebsocketCommunicator) -> None:
    try:
        await communicator.disconnect()
    except (asyncio.CancelledError, Exception):
        return


async def _drain_handshake(communicator: WebsocketCommunicator) -> list[dict]:
    messages = []
    ack = await communicator.receive_json_from(timeout=2)
    messages.append(ack)
    status_message = await communicator.receive_json_from(timeout=2)
    messages.append(status_message)
    return messages


@override_settings(CHANNEL_LAYERS=CHANNEL_LAYERS_MEMORY)
class LiveTranscriptMapperTests(TestCase):
    def setUp(self) -> None:
        self.session = LiveTranscriptSession()
        self.call_id = str(uuid.uuid4())

    def _apply(self, event: dict) -> dict | None:
        turn = self.session.apply_event(
            event,
            call_public_id=self.call_id,
            time_in_call_secs=1.5,
        )
        return turn.to_payload() if turn is not None else None

    def test_user_and_agent_final_turns_are_separate(self) -> None:
        user_turn = self._apply({
            'type': 'user_transcript',
            'user_transcription_event': {
                'user_transcript': 'Hello there',
                'event_id': 10,
            },
        })
        agent_turn = self._apply({
            'type': 'agent_response',
            'agent_response_event': {
                'agent_response': 'Hi, how can I help?',
                'event_id': 11,
            },
        })

        self.assertIsNotNone(user_turn)
        self.assertIsNotNone(agent_turn)
        assert user_turn is not None
        assert agent_turn is not None
        self.assertEqual(user_turn['speaker'], 'customer')
        self.assertEqual(user_turn['text'], 'Hello there')
        self.assertTrue(user_turn['is_final'])
        self.assertEqual(user_turn['event_id'], 10)
        self.assertEqual(agent_turn['speaker'], 'agent')
        self.assertEqual(agent_turn['text'], 'Hi, how can I help?')
        self.assertTrue(agent_turn['is_final'])
        self.assertEqual(agent_turn['event_id'], 11)
        self.assertEqual(CallTranscriptTurn.objects.count(), 0)

    def test_partial_then_final_agent_response_does_not_duplicate(self) -> None:
        start = self._apply({
            'type': 'agent_chat_response_part',
            'text_response_part': {
                'type': 'start',
                'text': '',
                'event_id': 20,
            },
        })
        first_delta = self._apply({
            'type': 'agent_chat_response_part',
            'text_response_part': {
                'type': 'delta',
                'text': 'Hello, how can I',
                'event_id': 20,
            },
        })
        second_delta = self._apply({
            'type': 'agent_chat_response_part',
            'text_response_part': {
                'type': 'delta',
                'text': ' assist you?',
                'event_id': 20,
            },
        })
        final = self._apply({
            'type': 'agent_response',
            'agent_response_event': {
                'agent_response': 'Hello, how can I assist you?',
                'event_id': 99,
            },
        })

        self.assertIsNone(start)
        assert first_delta is not None
        assert second_delta is not None
        assert final is not None
        self.assertEqual(first_delta['event_id'], 20)
        self.assertFalse(first_delta['is_final'])
        self.assertEqual(first_delta['text'], 'Hello, how can I')
        self.assertEqual(second_delta['event_id'], 20)
        self.assertEqual(second_delta['text'], 'Hello, how can I assist you?')
        self.assertFalse(second_delta['is_final'])
        self.assertEqual(final['event_id'], 20)
        self.assertTrue(final['is_final'])
        self.assertEqual(final['text'], 'Hello, how can I assist you?')
        self.assertEqual(CallTranscriptTurn.objects.count(), 0)

    def test_agent_response_correction_replaces_last_agent_turn(self) -> None:
        self._apply({
            'type': 'agent_response',
            'agent_response_event': {
                'agent_response': 'Let me tell you about the complete history...',
                'event_id': 30,
            },
        })
        corrected = self._apply({
            'type': 'agent_response_correction',
            'agent_response_correction_event': {
                'original_agent_response': (
                    'Let me tell you about the complete history...'
                ),
                'corrected_agent_response': 'Let me tell you about...',
                'event_id': 31,
            },
        })

        assert corrected is not None
        self.assertEqual(corrected['event_id'], 30)
        self.assertEqual(corrected['text'], 'Let me tell you about...')
        self.assertTrue(corrected['is_final'])

    def test_unknown_event_types_are_ignored(self) -> None:
        turn = self._apply({
            'type': 'audio',
            'audio_event': {'audio_base_64': 'aaaa', 'event_id': 1},
        })
        self.assertIsNone(turn)

    def test_monitor_url_uses_wss_and_conversation_id(self) -> None:
        url = build_monitor_ws_url(
            'conv_123',
            'https://api.elevenlabs.io',
        )
        self.assertEqual(
            url,
            'wss://api.elevenlabs.io/v1/convai/conversations/conv_123/monitor',
        )

    def test_relative_call_time_uses_answered_at(self) -> None:
        company = create_company()
        user = create_user(company=company)
        lead = create_lead(user=user)
        now = timezone.now()
        call = Call.objects.create(
            company=company,
            lead=lead,
            context_user=user,
            answered_at=now - timedelta(seconds=12),
        )
        elapsed = relative_call_time_secs(call, now=now)
        self.assertIsNotNone(elapsed)
        assert elapsed is not None
        self.assertAlmostEqual(elapsed, 12.0, places=1)


@override_settings(
    CHANNEL_LAYERS=CHANNEL_LAYERS_MEMORY,
    ELEVENLABS_LIVE_TRANSCRIPT_ENABLED=True,
    ELEVENLABS_API_KEY='test-key',
    ELEVENLABS_AGENT_ID='agent-1',
    ELEVENLABS_AGENT_PHONE_NUMBER_ID='phone-1',
    ELEVENLABS_OUTBOUND_PHONE_NUMBER='+442012345678',
)
class LiveTranscriptConsumerTests(TransactionTestCase):
    def setUp(self) -> None:
        from channels.layers import channel_layers

        channel_layers.backends.clear()
        self.company = create_company()
        self.user = create_user(company=self.company)
        self.lead = create_lead(user=self.user)
        self.call = Call.objects.create(
            company=self.company,
            lead=self.lead,
            context_user=self.user,
            lead_name=self.lead.name,
            phone_number=self.lead.phone_no,
            status=Call.Status.RINGING,
            provider_conversation_id='conversation-live-1',
        )

    def _application(self):
        from api.asgi import application

        return application

    async def _connect(self, *, user=None, call=None, token: str | None = ''):
        target_user = user or self.user
        target_call = call or self.call
        if token == '':
            token = _access_token(target_user)
        communicator = WebsocketCommunicator(
            self._application(),
            _ws_path(target_call.public_id, token),
        )
        connected, close_code = await communicator.connect()
        return communicator, connected, close_code

    @patch(
        'calling_agent.consumers.ensure_live_transcript_monitor',
        new_callable=AsyncMock,
    )
    def test_unauthenticated_connect_is_rejected(self, _monitor) -> None:
        async def body() -> None:
            communicator, connected, close_code = await self._connect(token=None)
            self.assertFalse(connected)
            self.assertEqual(close_code, 4401)
            await _safe_disconnect(communicator)

        async_to_sync(body)()

    @patch(
        'calling_agent.consumers.ensure_live_transcript_monitor',
        new_callable=AsyncMock,
    )
    def test_other_company_user_is_rejected(self, _monitor) -> None:
        other_company = create_company('Other Company')
        other_user = create_user(
            company=other_company,
            email='other@example.com',
        )

        async def body() -> None:
            communicator, connected, close_code = await self._connect(
                user=other_user,
            )
            self.assertFalse(connected)
            self.assertEqual(close_code, 4403)
            await _safe_disconnect(communicator)

        async_to_sync(body)()

    @patch(
        'calling_agent.consumers.ensure_live_transcript_monitor',
        new_callable=AsyncMock,
    )
    def test_non_visible_same_company_agent_is_rejected(self, _monitor) -> None:
        other_user = create_user(
            company=self.company,
            email='other-agent@example.com',
        )

        async def body() -> None:
            communicator, connected, close_code = await self._connect(
                user=other_user,
            )
            self.assertFalse(connected)
            self.assertEqual(close_code, 4403)
            await _safe_disconnect(communicator)

        async_to_sync(body)()

    @patch(
        'calling_agent.consumers.ensure_live_transcript_monitor',
        new_callable=AsyncMock,
    )
    def test_authorized_user_receives_ack(self, monitor_mock) -> None:
        async def body() -> None:
            communicator, connected, _close_code = await self._connect()
            self.assertTrue(connected)
            messages = await _drain_handshake(communicator)
            self.assertEqual(messages[0]['type'], 'connection.ack')
            self.assertEqual(messages[0]['call_id'], str(self.call.public_id))
            self.assertEqual(messages[1]['type'], 'transcript.status')
            monitor_mock.assert_awaited()
            await _safe_disconnect(communicator)

        async_to_sync(body)()

    @patch(
        'calling_agent.consumers.ensure_live_transcript_monitor',
        new_callable=AsyncMock,
    )
    def test_events_are_scoped_to_the_call(self, _monitor) -> None:
        other_call = Call.objects.create(
            company=self.company,
            lead=self.lead,
            context_user=self.user,
            status=Call.Status.RINGING,
            provider_conversation_id='conversation-live-2',
        )

        async def body() -> None:
            first, first_ok, _first_code = await self._connect(call=self.call)
            second, second_ok, _second_code = await self._connect(call=other_call)
            self.assertTrue(first_ok)
            self.assertTrue(second_ok)
            await _drain_handshake(first)
            await _drain_handshake(second)

            from channels.layers import get_channel_layer

            layer = get_channel_layer()
            payload = {
                'call_id': str(self.call.public_id),
                'event_id': 1,
                'speaker': 'customer',
                'text': 'Only for call A',
                'is_final': True,
                'sequence': 1,
                'received_at': timezone.now().isoformat(),
                'time_in_call_secs': 1.0,
            }
            await layer.group_send(
                call_transcript_group_name(self.call.public_id),
                {'type': 'transcript.turn', 'payload': payload},
            )
            message = await first.receive_json_from(timeout=2)
            self.assertEqual(message['type'], 'transcript.turn')
            self.assertEqual(message['payload']['text'], 'Only for call A')
            with self.assertRaises(asyncio.TimeoutError):
                await second.receive_json_from(timeout=0.3)
            await _safe_disconnect(first)
            await _safe_disconnect(second)

        async_to_sync(body)()

    @patch(
        'calling_agent.consumers.ensure_live_transcript_monitor',
        new_callable=AsyncMock,
    )
    def test_disconnect_then_reconnect_receives_later_events(
        self,
        _monitor,
    ) -> None:
        async def body() -> None:
            first, connected, _close_code = await self._connect()
            self.assertTrue(connected)
            await _drain_handshake(first)
            await _safe_disconnect(first)

            second, reconnected, _reconnect_code = await self._connect()
            self.assertTrue(reconnected)
            await _drain_handshake(second)

            from channels.layers import get_channel_layer

            layer = get_channel_layer()
            payload = {
                'call_id': str(self.call.public_id),
                'event_id': 7,
                'speaker': 'agent',
                'text': 'After reconnect',
                'is_final': True,
                'sequence': 7,
                'received_at': timezone.now().isoformat(),
                'time_in_call_secs': 2.0,
            }
            await layer.group_send(
                call_transcript_group_name(self.call.public_id),
                {'type': 'transcript.turn', 'payload': payload},
            )
            message = await second.receive_json_from(timeout=2)
            self.assertEqual(message['type'], 'transcript.turn')
            self.assertEqual(message['payload']['text'], 'After reconnect')
            await _safe_disconnect(second)

        async_to_sync(body)()

    @patch(
        'calling_agent.consumers.ensure_live_transcript_monitor',
        new_callable=AsyncMock,
    )
    def test_call_ended_stops_turn_forwarding(self, _monitor) -> None:
        async def body() -> None:
            communicator, connected, _close_code = await self._connect()
            self.assertTrue(connected)
            await _drain_handshake(communicator)

            from channels.layers import get_channel_layer

            layer = get_channel_layer()
            await layer.group_send(
                call_transcript_group_name(self.call.public_id),
                {
                    'type': 'call.ended',
                    'payload': {
                        'call_id': str(self.call.public_id),
                        'status': Call.Status.COMPLETED,
                    },
                },
            )
            ended = await communicator.receive_json_from(timeout=2)
            self.assertEqual(ended['type'], 'call.ended')

            await layer.group_send(
                call_transcript_group_name(self.call.public_id),
                {
                    'type': 'transcript.turn',
                    'payload': {
                        'call_id': str(self.call.public_id),
                        'event_id': 8,
                        'speaker': 'agent',
                        'text': 'Should be ignored',
                        'is_final': True,
                        'sequence': 8,
                        'received_at': timezone.now().isoformat(),
                        'time_in_call_secs': 3.0,
                    },
                },
            )
            with self.assertRaises(asyncio.TimeoutError):
                await communicator.receive_json_from(timeout=0.3)
            await _safe_disconnect(communicator)

        async_to_sync(body)()

    @patch(
        'calling_agent.consumers.ensure_live_transcript_monitor',
        new_callable=AsyncMock,
    )
    def test_terminal_status_broadcasts_call_ended(self, _monitor) -> None:
        with patch(
            'calling_agent.live_transcript.broadcast_call_ended',
        ) as broadcast_mock:
            changed = CallStatusTransitionService().apply(
                self.call,
                Call.Status.COMPLETED,
            )
        self.assertTrue(changed)
        broadcast_mock.assert_called_once()
        self.assertEqual(
            CallTranscriptTurn.objects.filter(call=self.call).count(),
            0,
        )
