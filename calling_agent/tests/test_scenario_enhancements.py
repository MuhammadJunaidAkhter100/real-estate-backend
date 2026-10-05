from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from calling_agent.auth import CallContextTokenService
from calling_agent.models import Call
from calling_agent.tests.factories import (
    create_company,
    create_lead,
    create_project,
    create_unit,
    create_user,
)
from calling_agent.tests.test_tools import TOOL_TEST_CACHES
from calling_agent.webhook_services import PostCallAnalysisService
from new_proposal.models import GeneratedProposal
from projects.models import Promotion
from users.models import Task
from users.tasks import check_and_update_task_expirations


@override_settings(
    CACHES=TOOL_TEST_CACHES,
    ELEVENLABS_TOOL_AUTH_SECRET='test-tool-secret',
    ELEVENLABS_TOOL_CONTEXT_TTL_SECONDS=300,
    ELEVENLABS_TRANSFER_ENABLED=False,
    CALLING_AGENT_PROMOTION_NEAR_EXPIRY_DAYS=7,
)
class ScenarioEnhancementToolTests(APITestCase):
    def setUp(self) -> None:
        self.company = create_company()
        self.user = create_user(company=self.company)
        self.lead = create_lead(user=self.user)
        self.lead.estimated_budget = Decimal('300000.00')
        self.lead.save(update_fields=['estimated_budget'])
        self.call = Call.objects.create(
            company=self.company,
            lead=self.lead,
            context_user=self.user,
            lead_name=self.lead.name,
            phone_number=self.lead.phone_no,
            status=Call.Status.RINGING,
            provider_conversation_id='conversation-scenario-1',
        )
        self.context_token = CallContextTokenService.issue(self.call)
        self.client.credentials(
            HTTP_X_ELEVENLABS_TOOL_SECRET='test-tool-secret',
            HTTP_X_ELEVENLABS_CALL_CONTEXT=self.context_token,
        )

    def test_lead_context_budget_fit_and_proposal(self) -> None:
        project = create_project(user=self.user, title='Budget Project')
        create_unit(project=project, price=Decimal('250000.00'))
        self.lead.project = project
        self.lead.save(update_fields=['project'])
        unit = create_unit(project=project, label='B-202', price=Decimal('260000.00'))
        GeneratedProposal.objects.create(
            project=project,
            lead=self.lead,
            unit=unit,
            status=GeneratedProposal.Status.COMPLETED,
            hosted_url='https://example.com/proposal.pdf',
            ai_facts={'location_label': 'Downtown'},
        )

        response = self.client.post(
            reverse('calling-tool-lead-context'),
            {},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data['data']
        self.assertEqual(data['lead']['phone_number'], self.lead.phone_no)
        self.assertEqual(data['assigned_projects'][0]['budget_fit'], 'within')
        self.assertIsNotNone(
            data['assigned_projects'][0]['lowest_available_unit_price']
        )
        self.assertEqual(data['latest_proposal']['project_title'], 'Budget Project')

    def test_project_context_exposes_active_promotion_not_expired(self) -> None:
        project = create_project(user=self.user, title='Promo Project')
        self.lead.projects.add(project)
        now = timezone.now()
        Promotion.objects.create(
            project=project,
            title='Live Offer',
            discount=12,
            start_date=now - timedelta(days=1),
            end_date=now + timedelta(days=10),
            status=Promotion.Status.ACTIVE,
            created_by=self.user,
        )
        Promotion.objects.create(
            project=project,
            title='Expired Offer',
            discount=20,
            start_date=now - timedelta(days=40),
            end_date=now - timedelta(days=5),
            status=Promotion.Status.EXPIRED,
            created_by=self.user,
        )

        response = self.client.post(
            reverse('calling-tool-project-context'),
            {},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        promotion = response.data['data']['projects'][0]['promotion']
        self.assertEqual(promotion['status'], 'active')
        self.assertEqual(promotion['title'], 'Live Offer')

    def test_unit_search_includes_pricing_and_promotion(self) -> None:
        project = create_project(user=self.user, title='Unit Promo Project')
        self.lead.projects.add(project)
        create_unit(
            project=project,
            label='U-1',
            price=Decimal('300000.00'),
            discounted_price=Decimal('280000.00'),
        )

        response = self.client.post(
            reverse('calling-tool-unit-search'),
            {'project_id': project.pk},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        unit = response.data['data']['units'][0]
        self.assertTrue(unit['pricing']['has_discount'])
        self.assertEqual(unit['promotion']['status'], 'none')

    def test_create_open_ended_task_survives_expiry_cron(self) -> None:
        response = self.client.post(
            reverse('calling-tool-create-task'),
            {
                'title': 'Notify when discount available for Promo Project',
                'open_ended': True,
                'reason': 'Lead asked to be notified when a discount is available.',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        task = Task.objects.get()
        self.assertTrue(task.open_ended)
        self.assertIsNone(task.due_date)
        self.assertIsNone(task.scheduled_at)

        result = check_and_update_task_expirations()
        self.assertEqual(result['expired_count'], 0)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.PENDING)

    def test_transfer_fallback_task_has_no_due_date(self) -> None:
        response = self.client.post(
            reverse('calling-tool-resolve-transfer'),
            {'reason': 'Lead requested a human agent.'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        task = Task.objects.get()
        self.assertIsNone(task.due_date)

    def test_demo_budget_increase_reassigns_project(self) -> None:
        cheap = create_project(user=self.user, title='Starter Homes')
        premium = create_project(user=self.user, title='Premium Residences')
        premium.starting_price = Decimal('500000.00')
        premium.save(update_fields=['starting_price'])
        create_unit(project=premium, price=Decimal('500000.00'))
        self.lead.project = cheap
        self.lead.save(update_fields=['project'])

        update_response = self.client.post(
            reverse('calling-tool-update-lead'),
            {
                'estimated_budget': '550000.00',
                'assigned_project_id': premium.pk,
                'reason': 'Lead increased budget and wants a better project.',
            },
            format='json',
        )

        self.assertEqual(update_response.status_code, status.HTTP_200_OK)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.project_id, premium.pk)

    def test_demo_dnc_update(self) -> None:
        response = self.client.post(
            reverse('calling-tool-update-lead'),
            {
                'status': 'not_interested',
                'do_not_contact': True,
                'reason': 'Lead asked to be removed from future calls.',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.lead.refresh_from_db()
        self.assertTrue(self.lead.do_not_contact)
        self.assertEqual(self.lead.status, 'not_interested')

    def test_post_call_skips_tasks_when_live_task_exists(self) -> None:
        self.client.post(
            reverse('calling-tool-create-task'),
            {
                'title': 'Notify when discount available',
                'open_ended': True,
                'reason': 'Lead requested discount alert.',
            },
            format='json',
        )
        self.call.status = Call.Status.COMPLETED
        self.call.save(update_fields=['status'])

        created = PostCallAnalysisService().materialize_tasks(
            self.call,
            [
                {
                    'name': 'Duplicate discount alert',
                    'description': 'Should not be created',
                    'priority': 'high',
                    'due_days': 1,
                }
            ],
        )

        self.assertEqual(created, 0)
        self.assertEqual(Task.objects.count(), 1)


class PostCallInsightsTests(APITestCase):
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
            status=Call.Status.COMPLETED,
        )

    def test_persist_analysis_stores_call_insights(self) -> None:
        service = PostCallAnalysisService()
        service.persist_analysis(
            self.call,
            {
                'summary': 'Lead discussed pricing.',
                'key_sentiments': ['Price sensitive'],
                'detected_intents': ['Needs follow-up'],
                'objections': ['Too expensive'],
                'preferences': ['2-bedroom'],
                'requirements_changed': True,
            },
        )

        self.call.refresh_from_db()
        self.assertEqual(self.call.call_insights['objections'], ['Too expensive'])
        self.assertEqual(self.call.call_insights['preferences'], ['2-bedroom'])
        self.assertTrue(self.call.call_insights['requirements_changed'])
