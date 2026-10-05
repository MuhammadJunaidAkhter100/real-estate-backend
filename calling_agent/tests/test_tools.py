from __future__ import annotations

import time
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from calling_agent.auth import CallContextTokenService
from calling_agent.models import Call, CallGeneratedAction
from calling_agent.tests.factories import (
    create_company,
    create_lead,
    create_project,
    create_unit,
    create_user,
)
from users.models import Task

TOOL_TEST_CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'calling-tool-tests-default',
    },
    'calling_agent_tools': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'calling-tool-tests-throttle',
    },
}


@override_settings(
    CACHES=TOOL_TEST_CACHES,
    ELEVENLABS_TOOL_AUTH_SECRET='test-tool-secret',
    ELEVENLABS_TOOL_CONTEXT_TTL_SECONDS=300,
    ELEVENLABS_TOOL_RATE='1000/min',
    ELEVENLABS_KNOWLEDGE_TOOL_RATE='1000/min',
    ELEVENLABS_KNOWLEDGE_TIMEOUT_SECONDS=1,
    ELEVENLABS_KNOWLEDGE_SCORE_THRESHOLD=0.15,
    ELEVENLABS_TRANSFER_ENABLED=False,
)
class ElevenLabsReadToolApiTests(APITestCase):
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
            status=Call.Status.RINGING,
            provider_conversation_id='conversation-tools-1',
        )
        self.context_token = CallContextTokenService.issue(self.call)
        self._set_credentials(self.context_token)

    def _set_credentials(self, context_token: str) -> None:
        self.client.credentials(
            HTTP_X_ELEVENLABS_TOOL_SECRET='test-tool-secret',
            HTTP_X_ELEVENLABS_CALL_CONTEXT=context_token,
        )

    def test_requires_static_tool_secret(self) -> None:
        self.client.credentials(
            HTTP_X_ELEVENLABS_CALL_CONTEXT=self.context_token,
        )

        response = self.client.post(
            reverse('calling-tool-lead-context'),
            {},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertFalse(response.data['success'])
        self.assertEqual(
            response.data['error']['code'],
            'authentication_failed',
        )

    def test_rejects_tampered_context_token(self) -> None:
        self._set_credentials(f'{self.context_token}tampered')

        response = self.client.post(
            reverse('calling-tool-lead-context'),
            {},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @override_settings(ELEVENLABS_TOOL_CONTEXT_TTL_SECONDS=-1)
    def test_rejects_expired_context_token(self) -> None:
        response = self.client.post(
            reverse('calling-tool-lead-context'),
            {},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_rejects_context_for_inactive_call(self) -> None:
        self.call.status = Call.Status.COMPLETED
        self.call.save(update_fields=['status'])

        response = self.client.post(
            reverse('calling-tool-lead-context'),
            {},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_lead_context_returns_only_call_scoped_allowlisted_fields(
        self,
    ) -> None:
        response = self.client.post(
            reverse('calling-tool-lead-context'),
            {},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        lead_data = response.data['data']['lead']
        self.assertEqual(lead_data['name'], self.lead.name)
        self.assertNotIn('email', lead_data)
        self.assertNotIn('phone_no', lead_data)
        self.assertEqual(response.data['data']['assigned_projects'], [])
        self.assertEqual(response.data['data']['assigned_projects_count'], 0)

    def test_lead_context_includes_assigned_projects(self) -> None:
        visible = create_project(user=self.user, title='Assigned Visible')
        visible.associated_country = 'UK'
        visible.location = 'Preston'
        visible.save()
        hidden = create_project(user=self.user, title='Assigned Hidden')
        hidden.visible_to_companies.add(self.company)
        draft = create_project(
            user=self.user,
            title='Assigned Draft',
            project_status='draft',
        )
        unassigned = create_project(user=self.user, title='Not Assigned')
        self.lead.projects.add(visible, hidden, draft)
        create_unit(project=unassigned)

        response = self.client.post(
            reverse('calling-tool-lead-context'),
            {},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        projects = response.data['data']['assigned_projects']
        self.assertEqual(response.data['data']['assigned_projects_count'], 1)
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]['id'], visible.pk)
        self.assertEqual(projects[0]['title'], 'Assigned Visible')
        self.assertEqual(projects[0]['location'], 'Preston')
        self.assertEqual(projects[0]['country'], 'UK')
        self.assertNotIn('description', projects[0])
        self.assertNotIn('email', response.data['data']['lead'])

    def test_tool_serializers_reject_caller_supplied_context_ids(self) -> None:
        response = self.client.post(
            reverse('calling-tool-lead-context'),
            {'lead_id': self.lead.pk},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('lead_id', response.data['error']['fields'])

    def test_project_context_excludes_company_hidden_projects(self) -> None:
        visible_project = create_project(
            user=self.user,
            title='Visible Project',
        )
        hidden_project = create_project(
            user=self.user,
            title='Hidden Project',
        )
        hidden_project.visible_to_companies.add(self.company)
        self.lead.projects.add(visible_project, hidden_project)

        response = self.client.post(
            reverse('calling-tool-project-context'),
            {},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        project_titles = {
            project['title']
            for project in response.data['data']['projects']
        }
        self.assertEqual(project_titles, {'Visible Project'})

    def test_unit_search_is_restricted_to_linked_projects(self) -> None:
        linked_project = create_project(
            user=self.user,
            title='Linked Project',
        )
        unlinked_project = create_project(
            user=self.user,
            title='Unlinked Project',
        )
        self.lead.projects.add(linked_project)
        linked_unit = create_unit(project=linked_project)
        unlinked_unit = create_unit(
            project=unlinked_project,
            label='PRIVATE-1',
            price=Decimal('100000.00'),
        )

        response = self.client.post(
            reverse('calling-tool-unit-search'),
            {
                'status': 'available',
                'max_budget': '250000.00',
                'limit': 5,
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        unit_ids = {
            unit['id']
            for unit in response.data['data']['units']
        }
        self.assertEqual(unit_ids, {linked_unit.pk})

        # Explicit eligible project_id may discover outside linked set.
        discovered_response = self.client.post(
            reverse('calling-tool-unit-search'),
            {'project_id': unlinked_project.pk},
            format='json',
        )
        self.assertEqual(discovered_response.status_code, status.HTTP_200_OK)
        discovered_ids = {
            unit['id']
            for unit in discovered_response.data['data']['units']
        }
        self.assertEqual(discovered_ids, {unlinked_unit.pk})

        draft_project = create_project(
            user=self.user,
            title='Draft Project',
            project_status='draft',
        )
        create_unit(project=draft_project, label='DRAFT-1')
        draft_response = self.client.post(
            reverse('calling-tool-unit-search'),
            {'project_id': draft_project.pk},
            format='json',
        )
        self.assertEqual(draft_response.data['data']['units'], [])

        hidden_project = create_project(
            user=self.user,
            title='Hidden For Unit Search',
        )
        create_unit(project=hidden_project, label='HIDDEN-1')
        hidden_project.visible_to_companies.add(self.company)
        hidden_response = self.client.post(
            reverse('calling-tool-unit-search'),
            {'project_id': hidden_project.pk},
            format='json',
        )
        self.assertEqual(hidden_response.data['data']['units'], [])

    def test_unit_search_matches_legacy_bedroom_category_labels(self) -> None:
        project = create_project(
            user=self.user,
            title='London House',
        )
        self.lead.projects.add(project)
        canonical = create_unit(project=project, label='LH-1')
        underscore = create_unit(project=project, label='LH-2')
        hyphen = create_unit(project=project, label='LH-3')
        one_bed = create_unit(project=project, label='LH-4')
        project.units.filter(label='LH-1').update(category='2 Bed')
        project.units.filter(label='LH-2').update(category='2_bed')
        project.units.filter(label='LH-3').update(category='2-Bed')
        project.units.filter(label='LH-4').update(category='1_bed')

        response = self.client.post(
            reverse('calling-tool-unit-search'),
            {
                'project_id': project.pk,
                'category': '2 Bed',
                'status': 'available',
                'limit': 5,
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        unit_ids = {
            unit['id']
            for unit in response.data['data']['units']
        }
        self.assertEqual(unit_ids, {canonical.pk, underscore.pk, hyphen.pk})
        self.assertNotIn(one_bed.pk, unit_ids)

    def test_project_search_rejects_unknown_and_context_fields(self) -> None:
        response = self.client.post(
            reverse('calling-tool-project-search'),
            {
                'lead_id': self.lead.pk,
                'company_id': self.company.pk,
                'location': 'Preston',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        fields = response.data['error']['fields']
        self.assertIn('lead_id', fields)
        self.assertIn('company_id', fields)

    def test_project_search_filters_and_excludes_ineligible(self) -> None:
        linked_project = create_project(
            user=self.user,
            title='Linked Preston Flats',
        )
        linked_project.associated_country = 'UK'
        linked_project.location = 'Preston'
        linked_project.property_category = 'Apartment'
        linked_project.starting_price = Decimal('220000.00')
        linked_project.save()
        create_unit(
            project=linked_project,
            label='L-1',
            price=Decimal('220000.00'),
        )
        create_unit(
            project=linked_project,
            label='L-2',
            price=Decimal('230000.00'),
        )
        linked_project.units.filter(label='L-2').update(category='2 Bed')
        linked_project.recalculate_unit_counts(save=True)

        discoverable = create_project(
            user=self.user,
            title='Fountain Court',
        )
        discoverable.associated_country = 'UK'
        discoverable.location = 'Preston'
        discoverable.property_category = 'Apartment'
        discoverable.starting_price = Decimal('210000.00')
        discoverable.save()
        create_unit(
            project=discoverable,
            label='F-1',
            price=Decimal('210000.00'),
        )
        discoverable.units.update(category='2 Bed')
        discoverable.recalculate_unit_counts(save=True)

        expensive = create_project(
            user=self.user,
            title='Luxury Tower',
        )
        expensive.associated_country = 'UK'
        expensive.location = 'London'
        expensive.property_category = 'Apartment'
        expensive.starting_price = Decimal('500000.00')
        expensive.save()
        create_unit(
            project=expensive,
            label='X-1',
            price=Decimal('500000.00'),
        )

        dubai = create_project(
            user=self.user,
            title='Marina Point',
        )
        dubai.associated_country = 'UAE'
        dubai.location = 'Dubai'
        dubai.property_category = 'Apartment'
        dubai.starting_price = Decimal('200000.00')
        dubai.save()
        create_unit(project=dubai, label='D-1', price=Decimal('200000.00'))

        draft = create_project(
            user=self.user,
            title='Draft Preston',
            project_status='draft',
        )
        draft.associated_country = 'UK'
        draft.location = 'Preston'
        draft.save()
        create_unit(project=draft, label='DR-1')

        hidden = create_project(
            user=self.user,
            title='Hidden Preston',
        )
        hidden.associated_country = 'UK'
        hidden.location = 'Preston'
        hidden.starting_price = Decimal('180000.00')
        hidden.save()
        create_unit(project=hidden, label='H-1', price=Decimal('180000.00'))
        hidden.visible_to_companies.add(self.company)

        no_units = create_project(
            user=self.user,
            title='Empty Preston',
        )
        no_units.associated_country = 'UK'
        no_units.location = 'Preston'
        no_units.starting_price = Decimal('150000.00')
        no_units.save()

        self.lead.projects.add(linked_project)

        response = self.client.post(
            reverse('calling-tool-project-search'),
            {
                'country': 'UK',
                'location': 'Preston',
                'property_category': 'Apartment',
                'bedrooms': 2,
                'max_budget': '250000.00',
                'limit': 5,
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['success'])
        projects = response.data['data']['projects']
        titles = {project['title'] for project in projects}
        self.assertIn('Fountain Court', titles)
        self.assertIn('Linked Preston Flats', titles)
        self.assertNotIn('Luxury Tower', titles)
        self.assertNotIn('Marina Point', titles)
        self.assertNotIn('Draft Preston', titles)
        self.assertNotIn('Hidden Preston', titles)
        self.assertNotIn('Empty Preston', titles)
        for project in projects:
            self.assertIsInstance(project['id'], int)
            self.assertEqual(project['country'], 'UK')
            self.assertNotIn('description', project)

    def test_project_search_name_budget_limit_and_empty(self) -> None:
        exact = create_project(user=self.user, title='London House')
        exact.associated_country = 'UK'
        exact.location = 'London'
        exact.starting_price = Decimal('225000.00')
        exact.save()
        create_unit(project=exact, label='LH-1', price=Decimal('225000.00'))

        similar = create_project(user=self.user, title='London House Annex')
        similar.associated_country = 'UK'
        similar.location = 'London'
        similar.starting_price = Decimal('200000.00')
        similar.save()
        create_unit(project=similar, label='LHA-1', price=Decimal('200000.00'))

        response = self.client.post(
            reverse('calling-tool-project-search'),
            {'project_name': 'London House', 'limit': 5},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        titles = [p['title'] for p in response.data['data']['projects']]
        self.assertEqual(titles[0], 'London House')
        self.assertIn('London House Annex', titles)

        budget_response = self.client.post(
            reverse('calling-tool-project-search'),
            {
                'min_budget': '210000.00',
                'max_budget': '230000.00',
            },
            format='json',
        )
        self.assertEqual(budget_response.status_code, status.HTTP_200_OK)
        budget_titles = {
            p['title'] for p in budget_response.data['data']['projects']
        }
        self.assertEqual(budget_titles, {'London House'})

        invalid_range = self.client.post(
            reverse('calling-tool-project-search'),
            {
                'min_budget': '300000.00',
                'max_budget': '200000.00',
            },
            format='json',
        )
        self.assertEqual(invalid_range.status_code, status.HTTP_400_BAD_REQUEST)

        invalid_limit = self.client.post(
            reverse('calling-tool-project-search'),
            {'limit': 11},
            format='json',
        )
        self.assertEqual(invalid_limit.status_code, status.HTTP_400_BAD_REQUEST)

        empty = self.client.post(
            reverse('calling-tool-project-search'),
            {'location': 'Nowhereville'},
            format='json',
        )
        self.assertEqual(empty.status_code, status.HTTP_200_OK)
        self.assertTrue(empty.data['success'])
        self.assertEqual(empty.data['data']['projects'], [])
        self.assertEqual(empty.data['data']['count'], 0)

        # Deterministic ordering by starting_price when no special ranks.
        order_response = self.client.post(
            reverse('calling-tool-project-search'),
            {'country': 'UK', 'limit': 10},
            format='json',
        )
        prices = [
            Decimal(p['starting_price'])
            for p in order_response.data['data']['projects']
        ]
        self.assertEqual(prices, sorted(prices))

    def test_project_search_discovers_unlinked_then_unit_search(self) -> None:
        linked = create_project(user=self.user, title='Already Linked')
        linked.associated_country = 'UK'
        linked.location = 'Manchester'
        linked.save()
        create_unit(project=linked, label='AL-1')
        self.lead.projects.add(linked)

        discovered = create_project(user=self.user, title='Fountain Court')
        discovered.associated_country = 'UK'
        discovered.location = 'Preston'
        discovered.starting_price = Decimal('215000.00')
        discovered.save()
        discovered_unit = create_unit(
            project=discovered,
            label='FC-1',
            price=Decimal('215000.00'),
        )

        search_response = self.client.post(
            reverse('calling-tool-project-search'),
            {'location': 'Preston', 'max_budget': '250000.00'},
            format='json',
        )
        self.assertEqual(search_response.status_code, status.HTTP_200_OK)
        projects = search_response.data['data']['projects']
        self.assertEqual(len(projects), 1)
        project_id = projects[0]['id']
        self.assertEqual(project_id, discovered.pk)
        self.assertIsInstance(project_id, int)

        unit_response = self.client.post(
            reverse('calling-tool-unit-search'),
            {'project_id': project_id, 'status': 'available'},
            format='json',
        )
        self.assertEqual(unit_response.status_code, status.HTTP_200_OK)
        unit_ids = {u['id'] for u in unit_response.data['data']['units']}
        self.assertEqual(unit_ids, {discovered_unit.pk})

    @patch('calling_agent.tool_services.PineconeService')
    def test_knowledge_search_always_uses_restrictive_filters(
        self,
        pinecone_class: Mock,
    ) -> None:
        project = create_project(user=self.user)
        self.lead.projects.add(project)
        pinecone = pinecone_class.return_value
        pinecone.search.side_effect = [
            [
                {
                    'text': 'Specialist document context.',
                    'score': 0.9,
                    'metadata': {'original_filename': 'private.pdf'},
                }
            ],
            [
                {
                    'text': 'Authorized project context.',
                    'score': 0.8,
                    'metadata': {'project_title': project.title},
                }
            ],
        ]

        response = self.client.post(
            reverse('calling-tool-knowledge-search'),
            {'query': 'What information is available?', 'top_k': 3},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['data']['answerable'])
        filters = [
            call.kwargs['metadata_filter']
            for call in pinecone.search.call_args_list
        ]
        self.assertIn({'user_id': self.user.pk}, filters)
        self.assertIn(
            {
                'source': {'$eq': 'project_document'},
                'project_id': {'$in': [project.pk]},
            },
            filters,
        )
        self.assertNotIn(None, filters)

    @patch('calling_agent.tool_services.PineconeService')
    def test_knowledge_search_scopes_to_requested_project_id(
        self,
        pinecone_class: Mock,
    ) -> None:
        linked = create_project(user=self.user, title='Linked')
        discovered = create_project(user=self.user, title='Discovered')
        self.lead.projects.add(linked)
        pinecone = pinecone_class.return_value
        pinecone.search.return_value = [
            {
                'text': 'Discovered project payment plan.',
                'score': 0.9,
                'metadata': {'project_title': discovered.title},
            }
        ]

        response = self.client.post(
            reverse('calling-tool-knowledge-search'),
            {
                'query': 'What is the payment plan?',
                'project_id': discovered.pk,
                'top_k': 3,
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['data']['answerable'])
        self.assertEqual(pinecone.search.call_count, 1)
        self.assertEqual(
            pinecone.search.call_args.kwargs['metadata_filter'],
            {
                'source': {'$eq': 'project_document'},
                'project_id': {'$in': [discovered.pk]},
            },
        )
        self.assertNotIn(
            'user_id',
            pinecone.search.call_args.kwargs['metadata_filter'],
        )

    @patch('calling_agent.tool_services.PineconeService')
    def test_knowledge_search_rejects_ineligible_project_id(
        self,
        pinecone_class: Mock,
    ) -> None:
        draft = create_project(
            user=self.user,
            title='Draft Project',
            project_status='draft',
        )
        pinecone = pinecone_class.return_value

        response = self.client.post(
            reverse('calling-tool-knowledge-search'),
            {
                'query': 'What is the payment plan?',
                'project_id': draft.pk,
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data['data']['answerable'])
        self.assertEqual(response.data['data']['results'], [])
        pinecone.search.assert_not_called()

    @patch('calling_agent.tool_services.PineconeService')
    @override_settings(ELEVENLABS_KNOWLEDGE_TIMEOUT_SECONDS=0.01)
    def test_knowledge_timeout_returns_empty_result(
        self,
        pinecone_class: Mock,
    ) -> None:
        def slow_search(*args: object, **kwargs: object) -> list[dict]:
            time.sleep(0.05)
            return []

        pinecone_class.return_value.search.side_effect = slow_search

        response = self.client.post(
            reverse('calling-tool-knowledge-search'),
            {'query': 'A valid knowledge question'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data['data']['answerable'])
        self.assertEqual(response.data['data']['results'], [])
        self.assertEqual(response.data['data']['count'], 0)

    @override_settings(ELEVENLABS_TOOL_RATE='1/min')
    def test_tool_rate_limit_is_call_scoped(self) -> None:
        url = reverse('calling-tool-lead-context')

        first_response = self.client.post(url, {}, format='json')
        second_response = self.client.post(url, {}, format='json')

        self.assertEqual(first_response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            second_response.status_code,
            status.HTTP_429_TOO_MANY_REQUESTS,
        )
        self.assertEqual(
            second_response.data['error']['code'],
            'rate_limited',
        )

    def test_create_task_is_idempotent_and_call_scoped(self) -> None:
        scheduled_at = timezone.now() + timedelta(hours=2)
        payload = {
            'title': 'Send the payment plan',
            'priority': 'high',
            'scheduled_at': scheduled_at.isoformat(),
            'reason': 'The lead requested it during the call.',
        }
        url = reverse('calling-tool-create-task')

        first_response = self.client.post(url, payload, format='json')
        second_response = self.client.post(url, payload, format='json')

        self.assertEqual(first_response.status_code, status.HTTP_200_OK)
        self.assertTrue(first_response.data['data']['created'])
        self.assertFalse(second_response.data['data']['created'])
        self.assertEqual(Task.objects.count(), 1)
        task = Task.objects.get()
        self.assertEqual(task.created_by, self.user)
        self.assertEqual(task.related_lead, self.lead)
        self.assertEqual(task.related_call, self.call)
        self.assertEqual(task.type, Task.Type.CALLBACK)
        self.assertIsNotNone(task.scheduled_at)
        self.assertEqual(task.due_date, timezone.localtime(task.scheduled_at).date())
        self.assertEqual(
            first_response.data['data']['task']['type'],
            Task.Type.CALLBACK,
        )
        self.assertEqual(CallGeneratedAction.objects.count(), 1)

    def test_create_task_defaults_type_to_callback_when_missing(self) -> None:
        response = self.client.post(
            reverse('calling-tool-create-task'),
            {
                'title': 'Schedule callback',
                'scheduled_at': (
                    timezone.now() + timedelta(hours=2)
                ).isoformat(),
                'reason': 'Lead asked to be called back.',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Task.objects.get().type, Task.Type.CALLBACK)
        self.assertEqual(
            response.data['data']['task']['type'],
            Task.Type.CALLBACK,
        )

    def test_create_task_allows_missing_scheduled_at(self) -> None:
        response = self.client.post(
            reverse('calling-tool-create-task'),
            {
                'title': 'Send brochure',
                'reason': 'The lead requested a brochure.',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        task = Task.objects.get()
        self.assertIsNone(task.scheduled_at)
        self.assertEqual(task.type, Task.Type.CALLBACK)
        self.assertIsNone(response.data['data']['task']['scheduled_at'])

    def test_create_task_accepts_explicit_type(self) -> None:
        response = self.client.post(
            reverse('calling-tool-create-task'),
            {
                'title': 'Schedule callback',
                'type': Task.Type.CALLBACK,
                'scheduled_at': (
                    timezone.now() + timedelta(hours=2)
                ).isoformat(),
                'reason': 'Lead asked to be called back.',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Task.objects.get().type, Task.Type.CALLBACK)

    def test_create_task_coerces_follow_up_type_to_callback(self) -> None:
        response = self.client.post(
            reverse('calling-tool-create-task'),
            {
                'title': 'London House: Confirm Payment Plan, Viewing, Send Details',
                'type': 'follow_up',
                'reason': (
                    'Lead requested a Property Specialist to confirm '
                    'payment plan and viewing, and to send details to WhatsApp.'
                ),
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Task.objects.get().type, Task.Type.CALLBACK)
        self.assertEqual(
            response.data['data']['task']['type'],
            Task.Type.CALLBACK,
        )

    def test_create_task_coerces_viewing_and_whatsapp_aliases_to_callback(
        self,
    ) -> None:
        for alias in (
            'follow-up',
            'followup',
            'viewing',
            'site_visit',
            'site-visit',
            'whatsapp',
            'whatsapp_follow_up',
        ):
            with self.subTest(alias=alias):
                Task.objects.all().delete()
                CallGeneratedAction.objects.all().delete()
                response = self.client.post(
                    reverse('calling-tool-create-task'),
                    {
                        'title': f'Follow up via {alias}',
                        'type': alias,
                        'reason': 'Lead requested a specialist next step.',
                    },
                    format='json',
                )
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(Task.objects.get().type, Task.Type.CALLBACK)

    def test_create_task_rejects_unknown_type(self) -> None:
        response = self.client.post(
            reverse('calling-tool-create-task'),
            {
                'title': 'Schedule callback',
                'type': 'not_a_task_type',
                'reason': 'Lead asked to be called back.',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['error']['code'], 'invalid_request')
        self.assertIn('type', response.data['error']['fields'])
        self.assertEqual(Task.objects.count(), 0)

    def test_create_task_defaults_null_type_to_callback(self) -> None:
        response = self.client.post(
            reverse('calling-tool-create-task'),
            {
                'title': 'Schedule callback',
                'type': None,
                'scheduled_at': (
                    timezone.now() + timedelta(hours=2)
                ).isoformat(),
                'reason': 'Lead asked to be called back.',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Task.objects.get().type, Task.Type.CALLBACK)

    def test_create_task_rejects_past_scheduled_at(self) -> None:
        response = self.client.post(
            reverse('calling-tool-create-task'),
            {
                'title': 'Call back yesterday',
                'scheduled_at': (
                    timezone.now() - timedelta(hours=1)
                ).isoformat(),
                'reason': 'Invalid past schedule.',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('scheduled_at', response.data['error']['fields'])
        self.assertEqual(Task.objects.count(), 0)

    def test_create_task_uses_assigned_specialist_in_same_company(self) -> None:
        specialist = create_user(
            company=self.company,
            email='specialist@example.com',
        )
        self.lead.assigned_to = specialist
        self.lead.save(update_fields=['assigned_to'])

        response = self.client.post(
            reverse('calling-tool-create-task'),
            {
                'title': 'Arrange a viewing',
                'scheduled_at': (
                    timezone.now() + timedelta(hours=3)
                ).isoformat(),
                'reason': 'The lead requested a viewing.',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Task.objects.get().created_by, specialist)

    def test_create_task_rejects_cross_company_assignee_as_owner(self) -> None:
        other_company = create_company('Other Company')
        other_user = create_user(
            company=other_company,
            email='outside@example.com',
        )
        self.lead.assigned_to = other_user
        self.lead.save(update_fields=['assigned_to'])

        response = self.client.post(
            reverse('calling-tool-create-task'),
            {
                'title': 'Send requested details',
                'scheduled_at': (
                    timezone.now() + timedelta(hours=3)
                ).isoformat(),
                'reason': 'The lead requested additional details.',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Task.objects.get().created_by, self.user)

    def test_different_create_task_payloads_create_separate_actions(
        self,
    ) -> None:
        url = reverse('calling-tool-create-task')
        first_payload = {
            'title': 'Send brochure',
            'scheduled_at': (
                timezone.now() + timedelta(hours=2)
            ).isoformat(),
            'reason': 'The lead requested a brochure.',
        }
        second_payload = {
            'title': 'Arrange a viewing',
            'scheduled_at': (
                timezone.now() + timedelta(hours=4)
            ).isoformat(),
            'reason': 'The lead requested a brochure.',
        }

        first_response = self.client.post(
            url,
            first_payload,
            format='json',
        )
        second_response = self.client.post(
            url,
            second_payload,
            format='json',
        )

        self.assertEqual(first_response.status_code, status.HTTP_200_OK)
        self.assertEqual(second_response.status_code, status.HTTP_200_OK)
        self.assertTrue(first_response.data['data']['created'])
        self.assertTrue(second_response.data['data']['created'])
        self.assertEqual(Task.objects.count(), 2)
        self.assertEqual(CallGeneratedAction.objects.count(), 2)

    def test_create_task_rejects_client_supplied_idempotency_key(self) -> None:
        response = self.client.post(
            reverse('calling-tool-create-task'),
            {
                'idempotency_key': str(uuid.uuid4()),
                'title': 'Send brochure',
                'scheduled_at': (
                    timezone.now() + timedelta(hours=2)
                ).isoformat(),
                'reason': 'The lead requested a brochure.',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('idempotency_key', response.data['error']['fields'])

    @patch('calling_agent.tool_services.PineconeService')
    def test_knowledge_search_payment_plan_drops_floor_plan_only(
        self,
        pinecone_class: Mock,
    ) -> None:
        project = create_project(user=self.user)
        self.lead.projects.add(project)
        pinecone_class.return_value.search.return_value = [
            {
                'text': 'Studio 45 sqm living room dimensions.',
                'score': 0.95,
                'metadata': {
                    'original_filename': 'London House floor plans.pdf',
                },
            }
        ]

        response = self.client.post(
            reverse('calling-tool-knowledge-search'),
            {
                'query': 'What is the payment plan for London House?',
                'top_k': 3,
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data['data']['answerable'])
        self.assertEqual(response.data['data']['results'], [])

    @patch('calling_agent.tool_services.PineconeService')
    def test_knowledge_search_payment_plan_keeps_payment_chunk(
        self,
        pinecone_class: Mock,
    ) -> None:
        project = create_project(user=self.user)
        self.lead.projects.add(project)
        pinecone_class.return_value.search.return_value = [
            {
                'text': 'Studio 45 sqm living room dimensions.',
                'score': 0.95,
                'metadata': {
                    'original_filename': 'London House floor plans.pdf',
                },
            },
            {
                'text': '20 percent deposit then 80 percent on handover.',
                'score': 0.7,
                'metadata': {
                    'original_filename': 'London House payment plan.pdf',
                },
            },
        ]

        response = self.client.post(
            reverse('calling-tool-knowledge-search'),
            {
                'query': 'What is the payment plan for London House?',
                'top_k': 3,
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['data']['answerable'])
        sources = [row['source'] for row in response.data['data']['results']]
        self.assertEqual(sources, ['London House payment plan.pdf'])

    @patch('calling_agent.tool_services.PineconeService')
    def test_knowledge_search_completion_drops_floor_plan_only(
        self,
        pinecone_class: Mock,
    ) -> None:
        project = create_project(user=self.user)
        self.lead.projects.add(project)
        pinecone_class.return_value.search.return_value = [
            {
                'text': 'Unit mix and floor plates.',
                'score': 0.92,
                'metadata': {
                    'original_filename': 'floorplan.pdf',
                },
            }
        ]

        response = self.client.post(
            reverse('calling-tool-knowledge-search'),
            {
                'query': 'So when can I view the property?',
                'top_k': 3,
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data['data']['answerable'])
        self.assertEqual(response.data['data']['results'], [])

    @patch('calling_agent.tool_services.PineconeService')
    def test_knowledge_search_completion_keeps_handover_chunk(
        self,
        pinecone_class: Mock,
    ) -> None:
        project = create_project(user=self.user)
        self.lead.projects.add(project)
        pinecone_class.return_value.search.return_value = [
            {
                'text': 'Unit mix and floor plates.',
                'score': 0.92,
                'metadata': {
                    'original_filename': 'floorplan.pdf',
                },
            },
            {
                'text': 'Estimated completion Q4 2027 with handover in 2028.',
                'score': 0.6,
                'metadata': {
                    'original_filename': 'London House brochure.pdf',
                },
            },
        ]

        response = self.client.post(
            reverse('calling-tool-knowledge-search'),
            {
                'query': 'So when can I view the property?',
                'top_k': 3,
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['data']['answerable'])
        sources = [row['source'] for row in response.data['data']['results']]
        self.assertEqual(sources, ['London House brochure.pdf'])

    def test_update_lead_is_allowlisted_and_idempotent(self) -> None:
        payload = {
            'status': 'interested',
            'desired_location': 'Manchester',
            'estimated_budget': '350000.00',
            'reason': 'The lead confirmed the requirements.',
        }
        url = reverse('calling-tool-update-lead')

        first_response = self.client.post(url, payload, format='json')
        second_response = self.client.post(url, payload, format='json')

        self.lead.refresh_from_db()
        self.assertEqual(first_response.status_code, status.HTTP_200_OK)
        self.assertTrue(first_response.data['data']['updated'])
        self.assertFalse(second_response.data['data']['updated'])
        self.assertEqual(self.lead.status, 'interested')
        self.assertEqual(self.lead.desired_location, 'Manchester')
        self.assertEqual(
            self.lead.estimated_budget,
            Decimal('350000.00'),
        )
        self.assertEqual(
            CallGeneratedAction.objects.filter(
                action_type=CallGeneratedAction.ActionType.UPDATE_LEAD
            ).count(),
            1,
        )

    def test_update_lead_accepts_hot_status_alias(self) -> None:
        response = self.client.post(
            reverse('calling-tool-update-lead'),
            {
                'status': 'hot',
                'reason': 'Lead asked about units, payment plan, and a viewing.',
            },
            format='json',
        )

        self.lead.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.lead.status, 'highly_interested')

    def test_update_lead_rejects_unknown_status(self) -> None:
        response = self.client.post(
            reverse('calling-tool-update-lead'),
            {
                'status': 'not_a_real_status',
                'reason': 'Invalid model-supplied status.',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['error']['code'], 'invalid_request')
        self.assertIn('status', response.data['error']['fields'])
        self.lead.refresh_from_db()
        self.assertNotEqual(self.lead.status, 'not_a_real_status')

    def test_update_lead_rejects_sensitive_or_scheduling_fields(self) -> None:
        response = self.client.post(
            reverse('calling-tool-update-lead'),
            {
                'phone_no': '+441234567890',
                'scheduled_at': '2026-08-05T10:00:00Z',
                'reason': 'Unsafe model-supplied changes.',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('phone_no', response.data['error']['fields'])
        self.assertIn('scheduled_at', response.data['error']['fields'])

    def test_update_lead_rejects_backward_status_transition(self) -> None:
        self.lead.status = 'converted_won'
        self.lead.save(update_fields=['status'])

        response = self.client.post(
            reverse('calling-tool-update-lead'),
            {
                'status': 'contacted',
                'reason': 'An invalid reopen of a closed lead.',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(
            response.data['error']['code'],
            'action_rejected',
        )
        self.assertFalse(CallGeneratedAction.objects.exists())

    def test_disabled_transfer_creates_one_fallback_task(self) -> None:
        payload = {
            'reason': 'The lead requested a specialist.',
        }
        url = reverse('calling-tool-resolve-transfer')

        first_response = self.client.post(url, payload, format='json')
        second_response = self.client.post(url, payload, format='json')

        self.assertEqual(first_response.status_code, status.HTTP_200_OK)
        resolution = first_response.data['data']
        self.assertFalse(resolution['available'])
        self.assertIsNone(resolution['destination'])
        self.assertTrue(resolution['fallback_created'])
        self.assertFalse(second_response.data['data']['fallback_created'])
        self.assertEqual(Task.objects.count(), 1)
        self.assertEqual(
            CallGeneratedAction.objects.get().action_type,
            CallGeneratedAction.ActionType.TRANSFER_FALLBACK,
        )

    def test_lead_context_includes_stage_and_do_not_contact(self) -> None:
        self.lead.status = 'highly_interested'
        self.lead.do_not_contact = False
        self.lead.save(update_fields=['status', 'do_not_contact'])

        response = self.client.post(
            reverse('calling-tool-lead-context'),
            {},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        lead_data = response.data['data']['lead']
        self.assertEqual(lead_data['status'], 'highly_interested')
        self.assertEqual(lead_data['stage'], 'qualification')
        self.assertFalse(lead_data['do_not_contact'])

    def test_update_lead_sets_not_interested_and_do_not_contact(self) -> None:
        response = self.client.post(
            reverse('calling-tool-update-lead'),
            {
                'status': 'not_interested',
                'do_not_contact': True,
                'reason': 'Lead asked not to be contacted again.',
            },
            format='json',
        )

        self.lead.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.lead.status, 'not_interested')
        self.assertTrue(self.lead.do_not_contact)

    def test_update_lead_rejects_clearing_do_not_contact(self) -> None:
        response = self.client.post(
            reverse('calling-tool-update-lead'),
            {
                'do_not_contact': False,
                'reason': 'Attempt to clear DNC.',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_update_lead_rejects_changes_when_already_dnc(self) -> None:
        self.lead.do_not_contact = True
        self.lead.save(update_fields=['do_not_contact'])

        response = self.client.post(
            reverse('calling-tool-update-lead'),
            {
                'status': 'interested',
                'reason': 'Should not update a DNC lead.',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, 'new')

    def test_update_lead_assigns_eligible_project(self) -> None:
        project = create_project(user=self.user, title='Assignable')
        create_unit(project=project)

        response = self.client.post(
            reverse('calling-tool-update-lead'),
            {
                'assigned_project_id': project.pk,
                'reason': 'Recommending a better project for the new budget.',
            },
            format='json',
        )

        self.lead.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn(
            'assigned_project_id',
            response.data['data']['changed_fields'],
        )
        self.assertEqual(self.lead.project_id, project.pk)
        self.assertTrue(self.lead.projects.filter(pk=project.pk).exists())

    def test_update_lead_rejects_ineligible_assigned_project(self) -> None:
        draft = create_project(
            user=self.user,
            title='Draft Only',
            project_status='draft',
        )
        create_unit(project=draft)

        response = self.client.post(
            reverse('calling-tool-update-lead'),
            {
                'assigned_project_id': draft.pk,
                'reason': 'Try to assign a draft project.',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.lead.refresh_from_db()
        self.assertIsNone(self.lead.project_id)

    def test_unit_search_includes_discount_and_yield_fields(self) -> None:
        project = create_project(user=self.user, title='Yield Project')
        self.lead.projects.add(project)
        create_unit(
            project=project,
            label='Y-1',
            price=Decimal('250000.00'),
            discounted_price=Decimal('230000.00'),
            est_yield_gross=Decimal('5.25'),
        )

        response = self.client.post(
            reverse('calling-tool-unit-search'),
            {'project_id': project.pk},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        unit = response.data['data']['units'][0]
        self.assertEqual(Decimal(unit['price']), Decimal('230000.00'))
        self.assertEqual(Decimal(unit['list_price']), Decimal('250000.00'))
        self.assertEqual(Decimal(unit['discounted_price']), Decimal('230000.00'))
        self.assertTrue(unit['has_discount'])
        self.assertEqual(Decimal(unit['est_yield_gross']), Decimal('5.25'))
        self.assertEqual(unit['yield_basis'], 'estimated')

    def test_project_search_includes_estimated_yield_basis(self) -> None:
        project = create_project(user=self.user, title='Search Yield')
        create_unit(project=project)

        response = self.client.post(
            reverse('calling-tool-project-search'),
            {'project_name': 'Search Yield'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        found = response.data['data']['projects'][0]
        self.assertEqual(found['yield_percentage'], '6.50')
        self.assertEqual(found['yield_basis'], 'estimated')
