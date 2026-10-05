from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import httpx
from django.test import SimpleTestCase

from calling_agent.elevenlabs import AgentConfiguration, ElevenLabsClient
from calling_agent.exceptions import (
    ElevenLabsRequestError,
    ElevenLabsResponseError,
    ElevenLabsTransientError,
)


class ElevenLabsClientTests(SimpleTestCase):
    def setUp(self) -> None:
        self.configuration = AgentConfiguration(
            key='default',
            api_key='test-key',
            agent_id='agent-1',
            phone_number_id='phone-1',
            outbound_phone_number='+442012345678',
            api_base_url='https://api.elevenlabs.io',
            timeout_seconds=10,
            recording_enabled=True,
        )
        self.outbound_call = Mock()
        sdk_client = SimpleNamespace(
            conversational_ai=SimpleNamespace(
                twilio=SimpleNamespace(outbound_call=self.outbound_call)
            )
        )
        self.client = ElevenLabsClient(self.configuration, client=sdk_client)

    def call_provider(self, **overrides):
        kwargs = {
            'to_number': '+447911123456',
            'call_public_id': '74c92418-18e7-4f04-9a0f-9d5099d356b4',
            'lead_id': 10,
            'lead_name': 'Test Lead',
            'company_id': 20,
            'call_context_token': 'signed-context-token',
        }
        kwargs.update(overrides)
        return self.client.initiate_outbound_call(**kwargs)

    def test_maps_outbound_call_and_disables_sdk_retries(self) -> None:
        self.outbound_call.return_value = SimpleNamespace(
            success=True,
            message='Call started',
            conversation_id='conversation-1',
            call_sid='call-1',
        )

        result = self.call_provider()

        self.assertEqual(result.conversation_id, 'conversation-1')
        self.assertEqual(result.provider_call_id, 'call-1')
        kwargs = self.outbound_call.call_args.kwargs
        self.assertEqual(kwargs['agent_id'], 'agent-1')
        self.assertEqual(kwargs['agent_phone_number_id'], 'phone-1')
        self.assertEqual(kwargs['to_number'], '+447911123456')
        self.assertTrue(kwargs['call_recording_enabled'])
        self.assertEqual(kwargs['request_options']['max_retries'], 0)
        dynamic_variables = kwargs[
            'conversation_initiation_client_data'
        ].dynamic_variables
        self.assertEqual(
            dynamic_variables['secret__call_context_token'],
            'signed-context-token',
        )
        self.assertEqual(
            dynamic_variables['call_id'],
            '74c92418-18e7-4f04-9a0f-9d5099d356b4',
        )
        self.assertEqual(dynamic_variables['lead_id'], 10)
        self.assertEqual(dynamic_variables['lead_name'], 'Test Lead')
        self.assertEqual(dynamic_variables['company_id'], 20)

    def test_merges_extra_dynamic_variables_without_overwriting_core_keys(
        self,
    ) -> None:
        self.outbound_call.return_value = SimpleNamespace(
            success=True,
            message='Call started',
            conversation_id='conversation-2',
            call_sid='call-2',
        )

        self.call_provider(
            extra_dynamic_variables={
                'today_date': '2026-08-06',
                'lead_status': 'interested',
                'desired_country': 'UK',
                'desired_location': 'Manchester',
                'estimated_budget': '350000.00',
                'category': 'Apartment',
                'property_type': '2 Bed',
                'assigned_specialist_name': 'Ada Agent',
                'linked_project_titles': 'Alpha Residences',
                'prior_call_summary': 'Discussed budget.',
                'call_objective': 'Manual outbound call',
                'lead_name': 'ShouldNotOverwrite',
                'call_id': 'ShouldNotOverwrite',
            }
        )

        dynamic_variables = self.outbound_call.call_args.kwargs[
            'conversation_initiation_client_data'
        ].dynamic_variables
        self.assertEqual(dynamic_variables['today_date'], '2026-08-06')
        self.assertEqual(dynamic_variables['lead_status'], 'interested')
        self.assertEqual(dynamic_variables['desired_country'], 'UK')
        self.assertEqual(dynamic_variables['desired_location'], 'Manchester')
        self.assertEqual(dynamic_variables['estimated_budget'], '350000.00')
        self.assertEqual(dynamic_variables['category'], 'Apartment')
        self.assertEqual(dynamic_variables['property_type'], '2 Bed')
        self.assertEqual(
            dynamic_variables['assigned_specialist_name'],
            'Ada Agent',
        )
        self.assertEqual(
            dynamic_variables['linked_project_titles'],
            'Alpha Residences',
        )
        self.assertEqual(
            dynamic_variables['prior_call_summary'],
            'Discussed budget.',
        )
        self.assertEqual(
            dynamic_variables['call_objective'],
            'Manual outbound call',
        )
        self.assertEqual(dynamic_variables['lead_name'], 'Test Lead')
        self.assertEqual(
            dynamic_variables['call_id'],
            '74c92418-18e7-4f04-9a0f-9d5099d356b4',
        )
        self.assertEqual(
            dynamic_variables['secret__call_context_token'],
            'signed-context-token',
        )
        self.assertEqual(dynamic_variables['lead_id'], 10)
        self.assertEqual(dynamic_variables['company_id'], 20)


    def test_rejects_unsuccessful_provider_response(self) -> None:
        self.outbound_call.return_value = SimpleNamespace(
            success=False,
            message='Rejected',
            conversation_id=None,
            call_sid=None,
        )

        with self.assertRaises(ElevenLabsRequestError):
            self.call_provider()

    def test_rejects_success_without_conversation_id(self) -> None:
        self.outbound_call.return_value = SimpleNamespace(
            success=True,
            message='Accepted',
            conversation_id=None,
            call_sid=None,
        )

        with self.assertRaises(ElevenLabsResponseError):
            self.call_provider()

    def test_marks_transport_timeout_as_unknown(self) -> None:
        request = httpx.Request('POST', 'https://api.elevenlabs.io')
        self.outbound_call.side_effect = httpx.ReadTimeout(
            'timeout',
            request=request,
        )

        with self.assertRaises(ElevenLabsTransientError):
            self.call_provider()

    def test_fetch_conversation_audio_joins_stream(self) -> None:
        audio_get = Mock(return_value=[b'part-1', b'part-2'])
        sdk_client = SimpleNamespace(
            conversational_ai=SimpleNamespace(
                twilio=SimpleNamespace(outbound_call=self.outbound_call),
                conversations=SimpleNamespace(
                    audio=SimpleNamespace(get=audio_get)
                ),
            )
        )
        client = ElevenLabsClient(self.configuration, client=sdk_client)

        audio_bytes = client.fetch_conversation_audio('conversation-1')

        self.assertEqual(audio_bytes, b'part-1part-2')
        audio_get.assert_called_once()
