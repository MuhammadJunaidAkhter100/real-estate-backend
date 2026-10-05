from __future__ import annotations

import tempfile
import uuid
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from calling_agent.elevenlabs import OutboundCallResult
from calling_agent.models import Call
from calling_agent.storage import get_call_recording_storage
from calling_agent.tests.factories import create_company, create_lead, create_user
from users.models import Lead


@override_settings(
    ELEVENLABS_API_KEY='test-key',
    ELEVENLABS_AGENT_ID='agent-1',
    ELEVENLABS_AGENT_PHONE_NUMBER_ID='phone-1',
    ELEVENLABS_OUTBOUND_PHONE_NUMBER='+442012345678',
    ELEVENLABS_API_BASE_URL='https://api.elevenlabs.io',
    ELEVENLABS_REQUEST_TIMEOUT_SECONDS=10,
    ELEVENLABS_CALL_RECORDING_ENABLED=True,
    DEFAULT_PHONE_REGION='',
)
class CallingAgentApiTests(APITestCase):
    def setUp(self) -> None:
        self.company = create_company()
        self.user = create_user(company=self.company)
        self.lead = create_lead(user=self.user)

    def test_call_list_requires_authentication(self) -> None:
        response = self.client.get(reverse('call-list'))

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_call_list_is_company_scoped(self) -> None:
        Call.objects.create(
            company=self.company,
            lead=self.lead,
            context_user=self.user,
        )
        other_company = create_company('Other Company')
        other_user = create_user(
            company=other_company,
            email='other@example.com',
        )
        Call.objects.create(
            company=other_company,
            lead=create_lead(user=other_user),
            context_user=other_user,
        )
        self.user.role = self.user.Role.COMPANY_ADMIN
        self.user.save(update_fields=['role'])
        self.client.force_authenticate(self.user)

        response = self.client.get(reverse('call-list'))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(len(response.data['results']), 1)

    def test_call_list_includes_lead_status(self) -> None:
        self.lead.status = self.lead.Status.INTERESTED
        self.lead.save(update_fields=['status', 'updated_at'])
        Call.objects.create(
            company=self.company,
            lead=self.lead,
            context_user=self.user,
            status=Call.Status.COMPLETED,
        )
        self.user.role = self.user.Role.COMPANY_ADMIN
        self.user.save(update_fields=['role'])
        self.client.force_authenticate(self.user)

        response = self.client.get(reverse('call-list'))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['results'][0]['lead_status'], 'interested')

    @patch('calling_agent.elevenlabs.ElevenLabsClient.initiate_outbound_call')
    def test_manual_initiation_is_idempotent(self, initiate_mock) -> None:
        initiate_mock.return_value = OutboundCallResult(
            conversation_id='conversation-1',
            provider_call_id='call-1',
            message='Started',
        )
        self.client.force_authenticate(self.user)
        idempotency_key = str(uuid.uuid4())
        payload = {
            'lead_id': self.lead.pk,
            'idempotency_key': idempotency_key,
        }

        first_response = self.client.post(
            reverse('call-initiate'),
            payload,
            format='json',
        )
        second_response = self.client.post(
            reverse('call-initiate'),
            payload,
            format='json',
        )

        self.assertEqual(first_response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second_response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            first_response.data['public_id'],
            second_response.data['public_id'],
        )
        self.assertEqual(initiate_mock.call_count, 1)

    def test_initiate_rejected_closed_lead_returns_lead_not_callable(self) -> None:
        self.lead.status = Lead.Status.CONVERTED_WON
        self.lead.save(update_fields=['status'])
        self.client.force_authenticate(self.user)

        response = self.client.post(
            reverse('call-initiate'),
            {
                'lead_id': self.lead.pk,
                'idempotency_key': str(uuid.uuid4()),
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data['error'], 'lead_not_callable')

    def test_call_detail_hides_other_company_call(self) -> None:
        other_company = create_company('Other Company')
        other_user = create_user(
            company=other_company,
            email='other@example.com',
        )
        call = Call.objects.create(
            company=other_company,
            context_user=other_user,
        )
        self.client.force_authenticate(self.user)

        response = self.client.get(
            reverse('call-detail', kwargs={'public_id': call.public_id})
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @override_settings(
        USE_S3=False,
        STORAGES={
            'default': {
                'BACKEND': 'django.core.files.storage.FileSystemStorage',
            },
        },
    )
    def test_recording_stream_requires_visible_call(self) -> None:
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                storage = get_call_recording_storage()
                storage_key = storage.save(
                    'call-recordings/test.mp3',
                    ContentFile(b'test-audio'),
                )
                call = Call.objects.create(
                    company=self.company,
                    lead=self.lead,
                    context_user=self.user,
                    recording_available=True,
                    recording_storage_key=storage_key,
                    recording_content_type='audio/mpeg',
                )
                self.client.force_authenticate(self.user)

                response = self.client.get(
                    reverse(
                        'call-recording',
                        kwargs={'public_id': call.public_id},
                    )
                )

                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(response.get('Accept-Ranges'), 'bytes')
                response.close()

    @override_settings(
        STORAGES={
            'default': {
                'BACKEND': 'django.core.files.storage.FileSystemStorage',
            },
        },
    )
    def test_recording_stream_accepts_access_token_query(self) -> None:
        from rest_framework_simplejwt.tokens import RefreshToken

        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                storage = get_call_recording_storage()
                storage_key = storage.save(
                    'call-recordings/test-token.mp3',
                    ContentFile(b'test-audio-token'),
                )
                call = Call.objects.create(
                    company=self.company,
                    lead=self.lead,
                    context_user=self.user,
                    recording_available=True,
                    recording_storage_key=storage_key,
                    recording_content_type='audio/mpeg',
                )
                access = str(RefreshToken.for_user(self.user).access_token)

                response = self.client.get(
                    reverse(
                        'call-recording',
                        kwargs={'public_id': call.public_id},
                    ),
                    {'access_token': access},
                )

                self.assertEqual(response.status_code, status.HTTP_200_OK)
                response.close()

    def test_end_call_requires_authentication(self) -> None:
        call = Call.objects.create(
            company=self.company,
            lead=self.lead,
            context_user=self.user,
            status=Call.Status.RINGING,
            provider_conversation_id='conversation-1',
        )

        response = self.client.post(
            reverse('call-end', kwargs={'public_id': call.public_id}),
            {},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_end_call_hides_other_company_call(self) -> None:
        other_company = create_company('Other Company')
        other_user = create_user(
            company=other_company,
            email='other@example.com',
        )
        call = Call.objects.create(
            company=other_company,
            context_user=other_user,
            status=Call.Status.RINGING,
            provider_conversation_id='conversation-other',
        )
        self.client.force_authenticate(self.user)

        response = self.client.post(
            reverse('call-end', kwargs={'public_id': call.public_id}),
            {},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data['error'], 'call_not_found')

    def test_end_call_rejects_terminal_call(self) -> None:
        call = Call.objects.create(
            company=self.company,
            lead=self.lead,
            context_user=self.user,
            status=Call.Status.COMPLETED,
            provider_conversation_id='conversation-1',
        )
        self.client.force_authenticate(self.user)

        response = self.client.post(
            reverse('call-end', kwargs={'public_id': call.public_id}),
            {},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data['error'], 'call_not_active')

    @patch('calling_agent.services.TwilioCallHangupService.hangup')
    def test_end_call_completes_ringing_call(self, hangup_mock) -> None:
        call = Call.objects.create(
            company=self.company,
            lead=self.lead,
            context_user=self.user,
            status=Call.Status.RINGING,
            provider_conversation_id='conversation-1',
            provider_call_id='call-1',
        )
        self.client.force_authenticate(self.user)

        response = self.client.post(
            reverse('call-end', kwargs={'public_id': call.public_id}),
            {'reason': 'Manual hangup'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['status'], Call.Status.COMPLETED)
        hangup_mock.assert_called_once_with('call-1')
        call.refresh_from_db()
        self.assertEqual(call.status, Call.Status.COMPLETED)
        self.assertEqual(call.failure_code, 'user_terminated')
        self.assertEqual(call.failure_detail, 'Manual hangup')
