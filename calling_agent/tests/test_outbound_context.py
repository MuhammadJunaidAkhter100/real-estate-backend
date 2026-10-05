from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock

from django.test import TestCase
from django.utils import timezone

from calling_agent.elevenlabs import AgentConfiguration, OutboundCallResult
from calling_agent.models import Call
from calling_agent.outbound_context import (
    PRIOR_CALL_SUMMARY_MAX_LENGTH,
    OutboundDynamicVariablesBuilder,
)
from calling_agent.services import CallProviderInitiationService
from calling_agent.tests.factories import (
    create_company,
    create_lead,
    create_project,
    create_user,
)


class OutboundDynamicVariablesBuilderTests(TestCase):
    def setUp(self) -> None:
        self.company = create_company()
        self.user = create_user(company=self.company)
        self.specialist = create_user(
            company=self.company,
            email='specialist@example.com',
        )
        self.specialist.first_name = 'Ada'
        self.specialist.last_name = 'Agent'
        self.specialist.save(update_fields=['first_name', 'last_name'])
        self.lead = create_lead(user=self.user)
        self.lead.status = 'interested'
        self.lead.desired_country = 'UK'
        self.lead.desired_location = 'Manchester'
        self.lead.estimated_budget = Decimal('350000.00')
        self.lead.category = 'Apartment'
        self.lead.type = '2 Bed'
        self.lead.assigned_to = self.specialist
        self.lead.save()
        self.call = Call.objects.create(
            company=self.company,
            lead=self.lead,
            context_user=self.user,
            lead_name=self.lead.name,
            phone_number=self.lead.phone_no,
            status=Call.Status.INITIATING,
            trigger=Call.Trigger.MANUAL,
        )

    def test_builds_full_briefing_payload(self) -> None:
        project_a = create_project(user=self.user, title='Alpha Residences')
        project_b = create_project(user=self.user, title='Beta Heights')
        self.lead.projects.add(project_a, project_b)

        payload = OutboundDynamicVariablesBuilder.build(self.call)

        self.assertEqual(payload['today_date'], timezone.localdate().isoformat())
        self.assertEqual(payload['lead_status'], 'interested')
        self.assertEqual(payload['desired_country'], 'UK')
        self.assertEqual(payload['desired_location'], 'Manchester')
        self.assertEqual(payload['estimated_budget'], '350000.00')
        self.assertEqual(payload['category'], 'Apartment')
        self.assertEqual(payload['property_type'], '2 Bed')
        self.assertEqual(payload['assigned_specialist_name'], 'Ada Agent')
        self.assertEqual(
            payload['linked_project_titles'],
            'Alpha Residences, Beta Heights',
        )
        self.assertEqual(payload['prior_call_summary'], '')
        self.assertEqual(payload['call_objective'], 'Manual outbound call')
        self.assertEqual(payload['has_sent_proposal'], 'false')
        self.assertEqual(payload['proposal_project_title'], '')
        self.assertIn('Budget fit', payload['budget_fit_summary'])
        self.assertTrue(
            all(isinstance(value, str) for value in payload.values())
        )
        self.assertNotIn('phone_no', payload)
        self.assertNotIn('email', payload)

    def test_uses_empty_strings_when_optional_data_missing(self) -> None:
        sparse_lead = create_lead(
            user=self.user,
            name='Sparse Lead',
            phone_number='+447911999888',
        )
        sparse_lead.estimated_budget = None
        sparse_lead.desired_country = ''
        sparse_lead.desired_location = ''
        sparse_lead.category = ''
        sparse_lead.type = ''
        sparse_lead.assigned_to = None
        sparse_lead.save()
        call = Call.objects.create(
            company=self.company,
            lead=sparse_lead,
            context_user=None,
            lead_name=sparse_lead.name,
            phone_number=sparse_lead.phone_no,
            status=Call.Status.INITIATING,
            trigger=Call.Trigger.SCHEDULED,
        )

        payload = OutboundDynamicVariablesBuilder.build(call)

        self.assertEqual(payload['desired_country'], '')
        self.assertEqual(payload['desired_location'], '')
        self.assertEqual(payload['estimated_budget'], '')
        self.assertEqual(payload['category'], '')
        self.assertEqual(payload['property_type'], '')
        self.assertEqual(payload['assigned_specialist_name'], '')
        self.assertEqual(payload['linked_project_titles'], '')
        self.assertEqual(payload['prior_call_summary'], '')
        self.assertEqual(payload['call_objective'], 'Scheduled outbound call')

    def test_falls_back_to_context_user_for_specialist_name(self) -> None:
        self.lead.assigned_to = None
        self.lead.save(update_fields=['assigned_to'])
        self.user.first_name = 'Casey'
        self.user.last_name = 'Context'
        self.user.save(update_fields=['first_name', 'last_name'])
        self.call.context_user = self.user
        self.call.save(update_fields=['context_user'])

        payload = OutboundDynamicVariablesBuilder.build(self.call)

        self.assertEqual(payload['assigned_specialist_name'], 'Casey Context')

    def test_prior_call_summary_uses_latest_completed_and_truncates(
        self,
    ) -> None:
        older = Call.objects.create(
            company=self.company,
            lead=self.lead,
            context_user=self.user,
            lead_name=self.lead.name,
            phone_number=self.lead.phone_no,
            status=Call.Status.COMPLETED,
            summary='Older summary should not win.',
            ended_at=timezone.now() - timedelta(days=2),
        )
        latest_summary = ('Latest completed summary. ' * 40).strip()
        Call.objects.create(
            company=self.company,
            lead=self.lead,
            context_user=self.user,
            lead_name=self.lead.name,
            phone_number=self.lead.phone_no,
            status=Call.Status.COMPLETED,
            summary=latest_summary,
            ended_at=timezone.now() - timedelta(days=1),
        )
        Call.objects.create(
            company=self.company,
            lead=self.lead,
            context_user=self.user,
            lead_name=self.lead.name,
            phone_number=self.lead.phone_no,
            status=Call.Status.FAILED,
            summary='Failed call summary must be ignored.',
            ended_at=timezone.now(),
        )
        # Current call must never be selected as prior context.
        self.call.summary = 'Current call summary must be ignored.'
        self.call.status = Call.Status.INITIATING
        self.call.save(update_fields=['summary', 'status'])

        payload = OutboundDynamicVariablesBuilder.build(self.call)

        expected = ' '.join(latest_summary.split())[
            :PRIOR_CALL_SUMMARY_MAX_LENGTH
        ]
        self.assertEqual(payload['prior_call_summary'], expected)
        self.assertLessEqual(
            len(payload['prior_call_summary']),
            PRIOR_CALL_SUMMARY_MAX_LENGTH,
        )
        self.assertNotIn(older.summary, payload['prior_call_summary'])
        self.assertNotEqual(
            payload['prior_call_summary'],
            self.call.summary,
        )

    def test_excludes_company_hidden_projects_from_titles(self) -> None:
        visible = create_project(user=self.user, title='Visible Project')
        hidden = create_project(user=self.user, title='Hidden Project')
        hidden.visible_to_companies.add(self.company)
        self.lead.projects.add(visible, hidden)

        payload = OutboundDynamicVariablesBuilder.build(self.call)

        self.assertEqual(payload['linked_project_titles'], 'Visible Project')


class CallProviderInitiationDynamicVariablesTests(TestCase):
    def setUp(self) -> None:
        self.company = create_company()
        self.user = create_user(company=self.company)
        self.lead = create_lead(user=self.user)
        self.lead.status = 'contacted'
        self.lead.desired_country = 'UAE'
        self.lead.save(update_fields=['status', 'desired_country'])
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
        self.provider = Mock()
        self.provider.initiate_outbound_call.return_value = OutboundCallResult(
            conversation_id='conversation-dyn-1',
            provider_call_id='call-dyn-1',
            message='Started',
        )
        self.service = CallProviderInitiationService(
            provider_client=self.provider,
        )

    def test_passes_builder_extras_into_provider_call(self) -> None:
        call = Call.objects.create(
            company=self.company,
            lead=self.lead,
            context_user=self.user,
            lead_name=self.lead.name,
            phone_number=self.lead.phone_no,
            status=Call.Status.INITIATING,
            trigger=Call.Trigger.SCHEDULED,
        )

        initiated = self.service.initiate(call, self.configuration)

        self.assertTrue(initiated)
        kwargs = self.provider.initiate_outbound_call.call_args.kwargs
        extras = kwargs['extra_dynamic_variables']
        self.assertEqual(extras['lead_status'], 'contacted')
        self.assertEqual(extras['desired_country'], 'UAE')
        self.assertEqual(extras['call_objective'], 'Scheduled outbound call')
        self.assertEqual(extras['today_date'], timezone.localdate().isoformat())
        self.assertEqual(kwargs['lead_id'], self.lead.pk)
        self.assertEqual(kwargs['company_id'], self.company.pk)
        self.assertEqual(kwargs['lead_name'], self.lead.name)
        self.assertTrue(kwargs['call_context_token'])
        self.assertEqual(
            kwargs['call_public_id'],
            str(call.public_id),
        )
