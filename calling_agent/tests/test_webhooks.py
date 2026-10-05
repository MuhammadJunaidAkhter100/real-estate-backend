from __future__ import annotations

import base64
import hashlib
import hmac
import json
import tempfile
import time
import uuid
from unittest.mock import patch

from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from calling_agent.elevenlabs import OutboundCallResult
from calling_agent.models import (
    Call,
    CallGeneratedAction,
    CallTranscriptTurn,
    CallWebhookEvent,
)
from calling_agent.serializers import CallTranscriptTurnSerializer
from calling_agent.storage import get_call_recording_storage
from calling_agent.tests.factories import create_company, create_lead, create_user
from calling_agent.webhook_services import (
    CallStatusTransitionService,
    CallTranscriptNormalizationService,
    CallWebhookProcessingService,
    _filter_webhook_payload,
    build_event_key,
)
from notifications.models import Notification
from users.models import Task


def _signed_webhook_body(
    *,
    secret: str,
    event: dict,
) -> tuple[str, str]:
    raw_body = json.dumps(event, separators=(',', ':'))
    timestamp = str(int(time.time()))
    signature = hmac.new(
        secret.encode('utf-8'),
        f'{timestamp}.{raw_body}'.encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()
    return raw_body, f't={timestamp},v0={signature}'


@override_settings(
    ELEVENLABS_API_KEY='test-key',
    ELEVENLABS_AGENT_ID='agent-1',
    ELEVENLABS_AGENT_PHONE_NUMBER_ID='phone-1',
    ELEVENLABS_OUTBOUND_PHONE_NUMBER='+442012345678',
    ELEVENLABS_WEBHOOK_SECRET='webhook-secret',
    ELEVENLABS_CALL_RECORDING_ENABLED=True,
    USE_S3=False,
    STORAGES={
        'default': {
            'BACKEND': 'django.core.files.storage.FileSystemStorage',
        },
    },
)
class WebhookApiTests(APITestCase):
    def setUp(self) -> None:
        self.company = create_company()
        self.user = create_user(company=self.company)
        self.lead = create_lead(user=self.user)
        self.call = Call.objects.create(
            company=self.company,
            lead=self.lead,
            context_user=self.user,
            lead_name=self.lead.name,
            phone_number=self.lead.phone_no,
            status=Call.Status.IN_PROGRESS,
            provider_conversation_id='conversation-1',
        )

    def test_webhook_rejects_invalid_signature(self) -> None:
        response = self.client.post(
            reverse('calling-webhook-elevenlabs'),
            data='{"type":"post_call_transcription"}',
            content_type='application/json',
            HTTP_ELEVENLABS_SIGNATURE='t=1,v0=bad',
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(CallWebhookEvent.objects.count(), 0)

    @patch('calling_agent.views.process_webhook_event_task.apply_async')
    def test_webhook_accepts_valid_signature_and_dedupes(
        self,
        process_mock,
    ) -> None:
        event = {
            'type': 'post_call_transcription',
            'event_timestamp': int(time.time()),
            'data': {
                'conversation_id': 'conversation-1',
                'transcript': [
                    {'role': 'agent', 'message': 'Hello'},
                    {'role': 'user', 'message': 'Hi there'},
                ],
                'analysis': {
                    'summary': 'Customer showed interest.',
                    'key_sentiments': ['High Interest'],
                    'detected_intents': ['Schedule viewing'],
                    'tasks': [
                        {
                            'name': 'Send brochure',
                            'priority': 'high',
                            'due_days': 1,
                        }
                    ],
                },
                'metadata': {
                    'call_duration_secs': 120,
                },
            },
        }
        raw_body, signature = _signed_webhook_body(
            secret='webhook-secret',
            event=event,
        )

        with self.captureOnCommitCallbacks(execute=True):
            first = self.client.post(
                reverse('calling-webhook-elevenlabs'),
                data=raw_body,
                content_type='application/json',
                HTTP_ELEVENLABS_SIGNATURE=signature,
            )
            second = self.client.post(
                reverse('calling-webhook-elevenlabs'),
                data=raw_body,
                content_type='application/json',
                HTTP_ELEVENLABS_SIGNATURE=signature,
            )

        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertEqual(CallWebhookEvent.objects.count(), 1)
        self.assertEqual(process_mock.call_count, 1)

    def test_post_call_transcription_processing(self) -> None:
        event = CallWebhookEvent.objects.create(
            event_key='post_call_transcription:conversation-1:1',
            event_type=CallWebhookEvent.EventType.POST_CALL_TRANSCRIPTION,
            conversation_id='conversation-1',
            call=self.call,
            body_hash='abc',
            payload={
                'type': 'post_call_transcription',
                'event_timestamp': int(time.time()),
                'data': {
                    'conversation_id': 'conversation-1',
                    'transcript': [
                        {'role': 'agent', 'message': 'Hello'},
                        {'role': 'user', 'message': 'Interested in a viewing'},
                    ],
                    'analysis': {
                        'summary': 'Lead requested a viewing.',
                        'key_sentiments': ['High Interest'],
                        'detected_intents': ['Schedule viewing'],
                        'tasks': [
                            {
                                'name': 'Book viewing',
                                'description': 'Schedule site visit with client for 2-bed unit.',
                                'priority': 'high',
                                'due_days': 2,
                            }
                        ],
                    },
                    'metadata': {'call_duration_secs': 95},
                },
            },
        )

        CallWebhookProcessingService().process(event.pk)

        self.call.refresh_from_db()
        self.assertEqual(self.call.status, Call.Status.COMPLETED)
        self.assertEqual(self.call.duration_seconds, 95)
        self.assertEqual(self.call.summary, 'Lead requested a viewing.')
        self.assertEqual(
            CallTranscriptTurn.objects.filter(call=self.call).count(),
            2,
        )
        task = Task.objects.get(related_call=self.call)
        self.assertEqual(task.name, 'Book viewing')
        self.assertEqual(task.description, 'Schedule site visit with client for 2-bed unit.')
        self.assertEqual(
            CallGeneratedAction.objects.filter(call=self.call).count(),
            1,
        )
        self.assertEqual(
            Notification.objects.filter(recipient=self.user).count(),
            1,
        )

    def test_post_call_transcription_maps_full_elevenlabs_shape(self) -> None:
        self.call.provider_agent_id = ''
        self.call.provider_call_id = ''
        self.call.provider_phone_number_id = ''
        self.call.save(
            update_fields=[
                'provider_agent_id',
                'provider_call_id',
                'provider_phone_number_id',
                'updated_at',
            ]
        )
        event = CallWebhookEvent.objects.create(
            event_key='post_call_transcription:conversation-1:full-shape',
            event_type=CallWebhookEvent.EventType.POST_CALL_TRANSCRIPTION,
            conversation_id='conversation-1',
            call=self.call,
            body_hash='full-shape',
            payload={
                'type': 'post_call_transcription',
                'event_timestamp': 1786563349,
                'data': {
                    'agent_id': 'agent_0601kxedybj0ecasd89rcjn8kpc4',
                    'conversation_id': 'conversation-1',
                    'status': 'done',
                    'metadata': {
                        'start_time_unix_secs': 1786563327,
                        'accepted_time_unix_secs': 1786563327,
                        'call_duration_secs': 16,
                        'termination_reason': 'Call ended by remote party',
                        'phone_call': {
                            'direction': 'outbound',
                            'phone_number_id': 'phnum_0601kxga5rbxe62vfg7zbqnhdn88',
                            'agent_number': '+16893885980',
                            'external_number': '+923140309296',
                            'type': 'twilio',
                            'call_sid': 'CA90350154822cadba0077e81442b65643',
                        },
                    },
                    'analysis': {
                        'call_successful': 'success',
                        'transcript_summary': (
                            'The user initiated the conversation with "No." '
                            'followed by two "Hello." messages.'
                        ),
                        'call_summary_title': 'Hello',
                        'sentiment_analysis': None,
                    },
                    'transcript': [
                        {
                            'role': 'user',
                            'message': 'No.',
                            'time_in_call_secs': 8,
                            'tool_calls': [],
                            'tool_results': [],
                        },
                        {
                            'role': 'user',
                            'message': 'Hello.',
                            'time_in_call_secs': 9,
                            'tool_calls': [],
                            'tool_results': [],
                        },
                        {
                            'role': 'user',
                            'message': 'Hello.',
                            'time_in_call_secs': 12,
                            'tool_calls': [],
                            'tool_results': [],
                        },
                    ],
                },
            },
        )

        CallWebhookProcessingService().process(event.pk)

        self.call.refresh_from_db()
        self.assertEqual(self.call.status, Call.Status.COMPLETED)
        self.assertEqual(self.call.duration_seconds, 16)
        self.assertEqual(self.call.provider_agent_id, 'agent_0601kxedybj0ecasd89rcjn8kpc4')
        self.assertEqual(
            self.call.provider_call_id,
            'CA90350154822cadba0077e81442b65643',
        )
        self.assertEqual(
            self.call.provider_phone_number_id,
            'phnum_0601kxga5rbxe62vfg7zbqnhdn88',
        )
        self.assertIsNotNone(self.call.answered_at)
        self.assertEqual(
            int(self.call.answered_at.timestamp()),
            1786563327,
        )
        self.assertEqual(int(self.call.ended_at.timestamp()), 1786563349)
        self.assertIn('No.', self.call.summary)
        self.assertEqual(self.call.provider_analysis.get('call_successful'), 'success')
        self.assertEqual(self.call.provider_analysis.get('call_summary_title'), 'Hello')
        turns = list(
            CallTranscriptTurn.objects.filter(call=self.call).order_by('turn_index')
        )
        self.assertEqual(len(turns), 3)
        self.assertEqual(turns[0].speaker, CallTranscriptTurn.Speaker.CUSTOMER)
        self.assertEqual(turns[0].message, 'No.')
        self.assertIsNone(turns[0].started_at)

    def test_call_initiation_failure_marks_call_failed(self) -> None:
        event = CallWebhookEvent.objects.create(
            event_key='call_initiation_failure:conversation-1:1',
            event_type=CallWebhookEvent.EventType.CALL_INITIATION_FAILURE,
            conversation_id='conversation-1',
            call=self.call,
            body_hash='abc',
            payload={
                'type': 'call_initiation_failure',
                'data': {
                    'conversation_id': 'conversation-1',
                    'failure_reason': 'unknown',
                    'metadata': {
                        'type': 'twilio',
                        'body': {'CallStatus': 'failed', 'ErrorCode': '30001'},
                    },
                },
            },
        )

        CallWebhookProcessingService().process(event.pk)

        self.call.refresh_from_db()
        self.assertEqual(self.call.status, Call.Status.FAILED)
        self.assertEqual(self.call.failure_code, 'initiation_failure')
        self.assertIn('unknown', self.call.failure_detail.lower())
        self.assertIn('twilio', self.call.failure_detail.lower())

    def test_call_initiation_failure_busy_and_no_answer(self) -> None:
        busy_event = CallWebhookEvent.objects.create(
            event_key='call_initiation_failure:conversation-1:busy',
            event_type=CallWebhookEvent.EventType.CALL_INITIATION_FAILURE,
            conversation_id='conversation-1',
            call=self.call,
            body_hash='busy',
            payload={
                'type': 'call_initiation_failure',
                'data': {
                    'conversation_id': 'conversation-1',
                    'failure_reason': 'busy',
                    'metadata': {
                        'type': 'sip',
                        'body': {'sip_status_code': 486, 'error_reason': 'Busy'},
                    },
                },
            },
        )
        CallWebhookProcessingService().process(busy_event.pk)
        self.call.refresh_from_db()
        self.assertEqual(self.call.status, Call.Status.BUSY)

        self.call.status = Call.Status.RINGING
        self.call.failure_code = ''
        self.call.failure_detail = ''
        self.call.save(update_fields=['status', 'failure_code', 'failure_detail', 'updated_at'])

        no_answer_event = CallWebhookEvent.objects.create(
            event_key='call_initiation_failure:conversation-1:no-answer',
            event_type=CallWebhookEvent.EventType.CALL_INITIATION_FAILURE,
            conversation_id='conversation-1',
            call=self.call,
            body_hash='no-answer',
            payload={
                'type': 'call_initiation_failure',
                'data': {
                    'conversation_id': 'conversation-1',
                    'failure_reason': 'no-answer',
                },
            },
        )
        CallWebhookProcessingService().process(no_answer_event.pk)
        self.call.refresh_from_db()
        self.assertEqual(self.call.status, Call.Status.NO_ANSWER)

    def test_status_transitions_are_monotonic(self) -> None:
        self.call.status = Call.Status.COMPLETED
        self.call.save(update_fields=['status', 'updated_at'])
        service = CallStatusTransitionService()

        changed = service.apply(self.call, Call.Status.IN_PROGRESS)

        self.assertFalse(changed)
        self.call.refresh_from_db()
        self.assertEqual(self.call.status, Call.Status.COMPLETED)

    def test_build_event_key_is_stable_for_same_payload(self) -> None:
        event = {
            'type': 'post_call_transcription',
            'event_timestamp': 123,
            'data': {'conversation_id': 'conv-1'},
        }
        self.assertEqual(build_event_key(event), build_event_key(event))

    def test_build_event_key_uses_compact_fingerprint(self) -> None:
        small = {
            'type': 'post_call_transcription',
            'event_timestamp': 1700000000,
            'data': {'conversation_id': 'conv-1'},
        }
        bulky = {
            **small,
            'data': {
                'conversation_id': 'conv-1',
                'transcript': [
                    {
                        'role': 'agent',
                        'message': 'x' * 5000,
                        'tool_details': {'body': 'y' * 10000},
                    }
                ],
            },
        }
        self.assertEqual(build_event_key(small), build_event_key(bulky))
        self.assertIn('post_call_transcription:conv-1:1700000000:', build_event_key(small))

    @patch('calling_agent.webhook_services.analyze_transcript_with_openai')
    def test_post_call_uses_transcript_summary_without_openai(
        self,
        openai_mock,
    ) -> None:
        summary = 'Caller confirmed interest in Dubai marina units.'
        event = CallWebhookEvent.objects.create(
            event_key='post_call_transcription:conversation-1:transcript-summary',
            event_type=CallWebhookEvent.EventType.POST_CALL_TRANSCRIPTION,
            conversation_id='conversation-1',
            call=self.call,
            body_hash='abc',
            payload={
                'type': 'post_call_transcription',
                'event_timestamp': int(time.time()),
                'data': {
                    'conversation_id': 'conversation-1',
                    'transcript': [
                        {'role': 'agent', 'message': 'Hello'},
                        {'role': 'user', 'message': 'Looking in Dubai'},
                    ],
                    'analysis': {
                        'transcript_summary': summary,
                        'key_sentiments': ['interested'],
                        'detected_intents': ['inquiry'],
                        'tasks': [{'name': 'Follow up'}],
                        'sentiment_analysis': None,
                        'call_successful': True,
                    },
                    'metadata': {'call_duration_secs': 80},
                },
            },
        )

        CallWebhookProcessingService().process(event.pk)

        self.call.refresh_from_db()
        self.assertEqual(self.call.summary, summary)
        openai_mock.assert_not_called()

    def test_transcript_time_in_call_secs_does_not_set_epoch_started_at(
        self,
    ) -> None:
        turns = CallTranscriptNormalizationService().upsert_turns(
            self.call,
            [
                {
                    'role': 'agent',
                    'message': 'Hello',
                    'time_in_call_secs': 0,
                },
                {
                    'role': 'user',
                    'message': 'Hi',
                    'time_in_call_secs': 15,
                },
                {
                    'role': 'agent',
                    'message': 'Thanks',
                    'time_in_call_secs': 159,
                },
            ],
        )

        self.assertEqual(len(turns), 3)
        for turn in turns:
            self.assertIsNone(turn.started_at)

        self.call.refresh_from_db()
        self.assertEqual(
            [item.get('time_in_call_secs') for item in self.call.transcript_data],
            [0, 15, 159],
        )

        serialized = CallTranscriptTurnSerializer(turns, many=True).data
        self.assertEqual(
            [item.get('time_in_call_secs') for item in serialized],
            [0, 15, 159],
        )

    def test_filter_webhook_payload_redacts_secrets_and_tool_details(
        self,
    ) -> None:
        event = {
            'type': 'post_call_transcription',
            'event_timestamp': 1700000000,
            'data': {
                'conversation_id': 'conversation-1',
                'conversation_initiation_client_data': {
                    'dynamic_variables': {
                        'lead_name': 'Alex',
                        'system__conversation_history': 'huge history ' * 200,
                        'secret__call_context_token': 'secret-token-value',
                    }
                },
                'transcript': [
                    {
                        'role': 'agent',
                        'message': 'Calling create_task',
                        'tool_calls': [
                            {
                                'tool_name': 'create_task',
                                'is_error': False,
                                'type': 'webhook',
                                'tool_details': {
                                    'body': '{"idempotency_key":"123"}',
                                    'headers': {'Authorization': 'Bearer x'},
                                },
                                'params_as_json': '{"name":"Follow up"}',
                            }
                        ],
                        'tool_results': [
                            {
                                'tool_name': 'create_task',
                                'is_error': True,
                                'result_value': 'conflict',
                                'tool_details': {
                                    'response_body': 'x' * 2000,
                                },
                            }
                        ],
                    }
                ],
                'analysis': {
                    'transcript_summary': 'Short summary',
                },
                'metadata': {'call_duration_secs': 42},
            },
        }

        filtered = _filter_webhook_payload(event)
        variables = filtered['data']['conversation_initiation_client_data'][
            'dynamic_variables'
        ]
        self.assertEqual(variables['system__conversation_history'], '[redacted]')
        self.assertEqual(variables['secret__call_context_token'], '[redacted]')
        self.assertEqual(variables['lead_name'], 'Alex')

        tool_call = filtered['data']['transcript'][0]['tool_calls'][0]
        self.assertEqual(tool_call['tool_name'], 'create_task')
        self.assertNotIn('tool_details', tool_call)
        self.assertNotIn('params_as_json', tool_call)

        tool_result = filtered['data']['transcript'][0]['tool_results'][0]
        self.assertEqual(tool_result['tool_name'], 'create_task')
        self.assertNotIn('tool_details', tool_result)

    @patch('calling_agent.views.process_webhook_event_task.apply_async')
    def test_ingest_large_elevenlabs_shaped_payload_returns_200(
        self,
        process_mock,
    ) -> None:
        history_blob = 'prior turn ' * 5000
        tool_body = json.dumps({'payload': 'z' * 8000})
        event = {
            'type': 'post_call_transcription',
            'event_timestamp': int(time.time()),
            'data': {
                'conversation_id': 'conversation-1',
                'conversation_initiation_client_data': {
                    'dynamic_variables': {
                        'system__conversation_history': history_blob,
                        'secret__call_context_token': 'tok-abc',
                        'lead_name': 'Sam',
                    }
                },
                'transcript': [
                    {
                        'role': 'agent',
                        'message': 'Hello',
                        'time_in_call_secs': 0,
                    },
                    {
                        'role': 'user',
                        'message': 'Hi',
                        'time_in_call_secs': 12,
                    },
                    {
                        'role': 'agent',
                        'message': None,
                        'time_in_call_secs': 20,
                        'tool_calls': [
                            {
                                'tool_name': 'create_task',
                                'is_error': False,
                                'tool_details': {
                                    'body': tool_body,
                                    'headers': {'X-Secret': 'abc'},
                                },
                            }
                        ],
                        'tool_results': [
                            {
                                'tool_name': 'create_task',
                                'is_error': True,
                                'result_value': 'idempotency_conflict',
                                'tool_details': {'response_body': 'y' * 4000},
                            }
                        ],
                    },
                ],
                'analysis': {
                    'transcript_summary': 'Lead asked for a brochure.',
                    'sentiment_analysis': None,
                },
                'metadata': {
                    'call_duration_secs': 55,
                    'phone_call': {'call_id': 'phone-call-1'},
                },
            },
        }
        raw_body, signature = _signed_webhook_body(
            secret='webhook-secret',
            event=event,
        )

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse('calling-webhook-elevenlabs'),
                data=raw_body,
                content_type='application/json',
                HTTP_ELEVENLABS_SIGNATURE=signature,
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(CallWebhookEvent.objects.count(), 1)
        stored = CallWebhookEvent.objects.get()
        variables = stored.payload['data']['conversation_initiation_client_data'][
            'dynamic_variables'
        ]
        self.assertEqual(variables['system__conversation_history'], '[redacted]')
        self.assertEqual(variables['secret__call_context_token'], '[redacted]')
        self.assertNotIn(
            'tool_details',
            stored.payload['data']['transcript'][2]['tool_calls'][0],
        )
        process_mock.assert_called_once()

    @patch('calling_agent.views.process_webhook_event_task.apply_async')
    def test_ingest_post_call_audio_stores_base64_and_redacts_payload(
        self,
        process_mock,
    ) -> None:
        audio_bytes = b'ID3fake-mp3-bytes'
        event = {
            'type': 'post_call_audio',
            'event_timestamp': int(time.time()),
            'data': {
                'agent_id': 'agent-1',
                'conversation_id': 'conversation-1',
                'full_audio': base64.b64encode(audio_bytes).decode('ascii'),
            },
        }
        raw_body, signature = _signed_webhook_body(
            secret='webhook-secret',
            event=event,
        )

        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                with self.captureOnCommitCallbacks(execute=True):
                    response = self.client.post(
                        reverse('calling-webhook-elevenlabs'),
                        data=raw_body,
                        content_type='application/json',
                        HTTP_ELEVENLABS_SIGNATURE=signature,
                    )

                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(CallWebhookEvent.objects.count(), 1)
                stored = CallWebhookEvent.objects.get()
                self.assertEqual(
                    stored.event_type,
                    CallWebhookEvent.EventType.POST_CALL_AUDIO,
                )
                self.assertEqual(stored.payload['data']['full_audio'], '[redacted]')
                self.assertTrue(stored.payload['data']['recording_storage_key'])
                self.call.refresh_from_db()
                self.assertTrue(self.call.recording_available)
                self.assertEqual(self.call.recording_size_bytes, len(audio_bytes))
                storage = get_call_recording_storage()
                self.assertTrue(storage.exists(self.call.recording_storage_key))
                process_mock.assert_called_once()

                CallWebhookProcessingService().process(stored.pk)
                stored.refresh_from_db()
                self.assertEqual(
                    stored.process_status,
                    CallWebhookEvent.ProcessStatus.COMPLETED,
                )

    @patch('calling_agent.views.process_webhook_event_task.apply_async')
    def test_unknown_event_type_returns_200_and_is_ignored(
        self,
        process_mock,
    ) -> None:
        event = {
            'type': 'some_future_event',
            'event_timestamp': int(time.time()),
            'data': {'conversation_id': 'conversation-1'},
        }
        raw_body, signature = _signed_webhook_body(
            secret='webhook-secret',
            event=event,
        )

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse('calling-webhook-elevenlabs'),
                data=raw_body,
                content_type='application/json',
                HTTP_ELEVENLABS_SIGNATURE=signature,
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        stored = CallWebhookEvent.objects.get()
        self.assertEqual(stored.event_type, 'some_future_event')
        process_mock.assert_called_once()
        CallWebhookProcessingService().process(stored.pk)
        stored.refresh_from_db()
        self.assertEqual(
            stored.process_status,
            CallWebhookEvent.ProcessStatus.IGNORED,
        )

    @patch(
        'calling_agent.webhook_services.ElevenLabsClient.fetch_conversation_audio'
    )
    def test_recording_fetch_stores_private_audio(
        self,
        fetch_mock,
    ) -> None:
        fetch_mock.return_value = b'fake-audio-bytes'
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                from calling_agent.webhook_services import CallRecordingFetchService

                service = CallRecordingFetchService()
                stored = service.fetch_and_store(self.call)

                self.assertTrue(stored)
                self.call.refresh_from_db()
                self.assertTrue(self.call.recording_available)
                storage = get_call_recording_storage()
                self.assertTrue(storage.exists(self.call.recording_storage_key))


@override_settings(
    ELEVENLABS_API_KEY='test-key',
    ELEVENLABS_AGENT_ID='agent-1',
    ELEVENLABS_AGENT_PHONE_NUMBER_ID='phone-1',
    ELEVENLABS_OUTBOUND_PHONE_NUMBER='+442012345678',
)
class CallAnalyticsApiTests(APITestCase):
    def setUp(self) -> None:
        self.company = create_company()
        self.user = create_user(company=self.company)
        self.lead = create_lead(user=self.user)
        self.user.role = self.user.Role.COMPANY_ADMIN
        self.user.save(update_fields=['role'])
        Call.objects.create(
            company=self.company,
            lead=self.lead,
            context_user=self.user,
            status=Call.Status.COMPLETED,
            duration_seconds=120,
            key_sentiments=['High Interest'],
        )
        Call.objects.create(
            company=self.company,
            lead=self.lead,
            context_user=self.user,
            status=Call.Status.FAILED,
        )
        self.client.force_authenticate(self.user)

    def _post_call(self) -> Call:
        self.call = Call.objects.create(
            company=self.company,
            lead=self.lead,
            context_user=self.user,
            status=Call.Status.COMPLETED,
        )
        return self.call

    def test_analytics_returns_aggregate_metrics(self) -> None:
        response = self.client.get(reverse('call-analytics'))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['total_calls'], 2)
        self.assertEqual(response.data['completed_calls'], 1)
        self.assertEqual(response.data['failed_calls'], 1)
        self.assertEqual(response.data['average_duration_seconds'], 120.0)
        self.assertEqual(response.data['sentiment_breakdown']['High Interest'], 1)

    @patch('calling_agent.webhook_services.analyze_transcript_with_openai')
    def test_post_call_transcript_updates_lead_status(self, mock_openai) -> None:
        from users.models import Lead
        from calling_agent.webhook_services import PostCallAnalysisService

        self._post_call()
        self.call.transcript_data = [
            {'role': 'agent', 'message': 'Hi, is now a good time?'},
            {'role': 'user', 'message': 'Please call me back tomorrow at 3 PM.'},
        ]
        self.call.save()

        mock_openai.return_value = {
            'summary': 'Customer asked for callback tomorrow.',
            'key_sentiments': ['Wants callback'],
            'detected_intents': ['Call Back Requested'],
            'tasks': [],
            'lead_status': 'call_back_requested',
        }

        service = PostCallAnalysisService()
        analysis = service.analyze_call(self.call)
        service.persist_analysis(self.call, analysis)

        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, Lead.Status.CALL_BACK_REQUESTED)
        self.assertEqual(self.lead.stage, Lead.Stage.CONTACT_ATTEMPT)

    @patch('calling_agent.webhook_services.analyze_transcript_with_openai')
    def test_real_world_scenario_whatsapp_nurturing(self, mock_openai) -> None:
        from users.models import Lead
        from calling_agent.webhook_services import PostCallAnalysisService

        self._post_call()
        self.call.transcript_data = [
            {'role': 'agent', 'message': 'Would you like project details?'},
            {'role': 'user', 'message': 'Yes, please send them to me on WhatsApp.'},
        ]
        self.call.save()

        mock_openai.return_value = {
            'summary': 'Customer requested brochure on WhatsApp.',
            'key_sentiments': ['Prefers WhatsApp'],
            'detected_intents': ['WhatsApp Follow-up'],
            'tasks': [],
            'lead_status': 'whatsapp_follow_up',
        }

        service = PostCallAnalysisService()
        analysis = service.analyze_call(self.call)
        service.persist_analysis(self.call, analysis)

        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, Lead.Status.WHATSAPP_FOLLOW_UP)
        self.assertEqual(self.lead.stage, Lead.Stage.NURTURING)

    @patch('calling_agent.webhook_services.analyze_transcript_with_openai')
    def test_real_world_scenario_site_visit_scheduled(self, mock_openai) -> None:
        from users.models import Lead
        from calling_agent.webhook_services import PostCallAnalysisService

        self._post_call()
        self.call.transcript_data = [
            {'role': 'agent', 'message': 'Can you visit London House this Saturday?'},
            {'role': 'user', 'message': 'Yes, I will be there on Saturday at 2 PM.'},
        ]
        self.call.save()

        mock_openai.return_value = {
            'summary': 'Customer confirmed site viewing for Saturday 2 PM.',
            'key_sentiments': ['Eager for site visit'],
            'detected_intents': ['Site Visit Confirmed'],
            'tasks': [{'name': 'Host site viewing on Saturday 2 PM', 'priority': 'high', 'status': 'pending', 'due_days': 2}],
            'lead_status': 'site_visit_scheduled',
        }

        service = PostCallAnalysisService()
        analysis = service.analyze_call(self.call)
        service.persist_analysis(self.call, analysis)

        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, Lead.Status.SITE_VISIT_SCHEDULED)
        self.assertEqual(self.lead.stage, Lead.Stage.SALES_PROCESS)

    @patch('calling_agent.webhook_services.analyze_transcript_with_openai')
    def test_real_world_scenario_negotiation_ongoing(self, mock_openai) -> None:
        from users.models import Lead
        from calling_agent.webhook_services import PostCallAnalysisService

        self._post_call()
        self.call.transcript_data = [
            {'role': 'agent', 'message': 'The unit price is £169,750.'},
            {'role': 'user', 'message': 'If you can offer a 5% discount on the deposit, I will sign.'},
        ]
        self.call.save()

        mock_openai.return_value = {
            'summary': 'Customer negotiating deposit discount.',
            'key_sentiments': ['Price Sensitive'],
            'detected_intents': ['Negotiation Ongoing'],
            'tasks': [],
            'lead_status': 'negotiation_ongoing',
        }

        service = PostCallAnalysisService()
        analysis = service.analyze_call(self.call)
        service.persist_analysis(self.call, analysis)

        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, Lead.Status.NEGOTIATION_ONGOING)
        self.assertEqual(self.lead.stage, Lead.Stage.SALES_PROCESS)

    @patch('calling_agent.webhook_services.analyze_transcript_with_openai')
    def test_real_world_scenario_lost_to_competitor(self, mock_openai) -> None:
        from users.models import Lead
        from calling_agent.webhook_services import PostCallAnalysisService

        self._post_call()
        self.call.transcript_data = [
            {'role': 'agent', 'message': 'Hi, following up on your UK property enquiry.'},
            {'role': 'user', 'message': 'I already bought an apartment in Manchester last week from another agency.'},
        ]
        self.call.save()

        mock_openai.return_value = {
            'summary': 'Customer already purchased property through another agency.',
            'key_sentiments': ['Bought Elsewhere'],
            'detected_intents': ['Lost to Competitor'],
            'tasks': [],
            'lead_status': 'lost_to_competitor',
        }

        service = PostCallAnalysisService()
        analysis = service.analyze_call(self.call)
        service.persist_analysis(self.call, analysis)

        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, Lead.Status.LOST_TO_COMPETITOR)
        self.assertEqual(self.lead.stage, Lead.Stage.CLOSED)

    @patch('calling_agent.webhook_services.analyze_transcript_with_openai')
    def test_real_world_scenario_future_prospect(self, mock_openai) -> None:
        from users.models import Lead
        from calling_agent.webhook_services import PostCallAnalysisService

        self._post_call()
        self.call.transcript_data = [
            {'role': 'agent', 'message': 'Are you looking to invest in Q3 2026?'},
            {'role': 'user', 'message': 'I am interested, but I am currently bound by fixed deposits until next year. Call me next year.'},
        ]
        self.call.save()

        mock_openai.return_value = {
            'summary': 'Customer interested but funds locked until next year.',
            'key_sentiments': ['Long Term Prospect'],
            'detected_intents': ['Future Prospect'],
            'tasks': [],
            'lead_status': 'future_prospect',
        }

        service = PostCallAnalysisService()
        analysis = service.analyze_call(self.call)
        service.persist_analysis(self.call, analysis)

        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, Lead.Status.FUTURE_PROSPECT)
        self.assertEqual(self.lead.stage, Lead.Stage.CLOSED)

    def test_post_call_alias_qualified_maps_to_interested(self) -> None:
        from users.models import Lead
        from calling_agent.webhook_services import PostCallAnalysisService

        self._post_call()
        self.lead.status = Lead.Status.CONTACTED
        self.lead.save(update_fields=['status', 'updated_at'])
        service = PostCallAnalysisService()
        service.persist_analysis(self.call, {
            'summary': 'Customer is interested.',
            'key_sentiments': [],
            'detected_intents': [],
            'lead_status': 'qualified',
        })
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, Lead.Status.INTERESTED)

    def test_post_call_dnc_alias_sets_flag(self) -> None:
        from users.models import Lead
        from calling_agent.webhook_services import PostCallAnalysisService

        self._post_call()
        service = PostCallAnalysisService()
        service.persist_analysis(self.call, {
            'summary': 'Customer asked not to be called.',
            'key_sentiments': [],
            'detected_intents': [],
            'lead_status': 'dnc',
        })
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, Lead.Status.NOT_INTERESTED)
        self.assertTrue(self.lead.do_not_contact)

    def test_post_call_contacted_does_not_clobber_live_update_lead(self) -> None:
        from users.models import Lead
        from calling_agent.webhook_services import PostCallAnalysisService

        self._post_call()
        self.lead.status = Lead.Status.HIGHLY_INTERESTED
        self.lead.save(update_fields=['status', 'updated_at'])
        service = PostCallAnalysisService()
        service.persist_analysis(self.call, {
            'summary': 'Generic wrap-up.',
            'key_sentiments': [],
            'detected_intents': [],
            'lead_status': 'contacted',
        })
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, Lead.Status.HIGHLY_INTERESTED)

    def test_post_call_does_not_override_update_lead_status(self) -> None:
        from users.models import Lead
        from calling_agent.webhook_services import PostCallAnalysisService

        self._post_call()
        self.lead.status = Lead.Status.INTERESTED
        self.lead.save(update_fields=['status', 'updated_at'])
        CallGeneratedAction.objects.create(
            call=self.call,
            action_type=CallGeneratedAction.ActionType.UPDATE_LEAD,
            title='Lead updated during call',
            payload={
                'changes': {'status': 'interested'},
                'reason': 'Caller is interested and requested a proposal.',
            },
            idempotency_key=uuid.uuid4(),
            status=CallGeneratedAction.Status.COMPLETED,
        )
        service = PostCallAnalysisService()
        service.persist_analysis(self.call, {
            'summary': 'Customer requested a proposal.',
            'key_sentiments': [],
            'detected_intents': [],
            'lead_status': 'need_more_information',
        })
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, Lead.Status.INTERESTED)



@override_settings(
    ELEVENLABS_API_KEY='test-key',
    ELEVENLABS_AGENT_ID='agent-1',
    ELEVENLABS_AGENT_PHONE_NUMBER_ID='phone-1',
    ELEVENLABS_OUTBOUND_PHONE_NUMBER='+442012345678',
)
class CallLifecycleTests(APITestCase):
    @patch('calling_agent.elevenlabs.ElevenLabsClient.initiate_outbound_call')
    @patch('calling_agent.webhook_services.CallRecordingFetchService.fetch_and_store')
    def test_mocked_call_lifecycle(
        self,
        fetch_mock,
        initiate_mock,
    ) -> None:
        fetch_mock.return_value = False
        initiate_mock.return_value = OutboundCallResult(
            conversation_id='conversation-lifecycle',
            provider_call_id='provider-call-1',
            message='Started',
        )
        company = create_company()
        user = create_user(company=company)
        lead = create_lead(user=user)
        client = self.client
        client.force_authenticate(user)
        idempotency_key = str(uuid.uuid4())

        initiate_response = client.post(
            reverse('call-initiate'),
            {
                'lead_id': lead.pk,
                'idempotency_key': idempotency_key,
            },
            format='json',
        )
        self.assertEqual(initiate_response.status_code, status.HTTP_201_CREATED)
        public_id = initiate_response.data['public_id']

        call = Call.objects.get(public_id=public_id)
        self.assertEqual(call.provider_conversation_id, 'conversation-lifecycle')

        event = CallWebhookEvent.objects.create(
            event_key='lifecycle:conversation-lifecycle:1',
            event_type=CallWebhookEvent.EventType.POST_CALL_TRANSCRIPTION,
            conversation_id='conversation-lifecycle',
            call=call,
            body_hash='abc',
            payload={
                'type': 'post_call_transcription',
                'data': {
                    'conversation_id': 'conversation-lifecycle',
                    'transcript': [
                        {'role': 'agent', 'message': 'Good afternoon'},
                        {'role': 'user', 'message': 'Tell me more'},
                    ],
                    'analysis': {
                        'summary': 'Lead asked for more details.',
                        'tasks': [],
                    },
                    'metadata': {'call_duration_secs': 45},
                },
            },
        )
        CallWebhookProcessingService().process(event.pk)

        detail_response = client.get(
            reverse('call-detail', kwargs={'public_id': public_id})
        )
        self.assertEqual(detail_response.status_code, status.HTTP_200_OK)
        self.assertEqual(detail_response.data['status'], Call.Status.COMPLETED)
        self.assertEqual(len(detail_response.data['transcript_turns']), 2)
        self.assertEqual(
            detail_response.data['summary'],
            'Lead asked for more details.',
        )


