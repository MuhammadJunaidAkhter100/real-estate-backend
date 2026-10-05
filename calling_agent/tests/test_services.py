from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import Mock, patch

import httpx
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from calling_agent.elevenlabs import AgentConfiguration, OutboundCallResult
from calling_agent.exceptions import (
    CallAuthorizationError,
    CallConflictError,
    CallingConfigurationError,
    ElevenLabsRequestError,
    ElevenLabsTransientError,
    InvalidPhoneNumberError,
    LeadNotCallableError,
)
from calling_agent.models import Call
from calling_agent.services import (
    CallTerminationService,
    ManualCallInitiationService,
    ScheduledCallService,
    TwilioCallHangupService,
)
from calling_agent.tests.factories import create_company, create_lead, create_user
from users.models import Lead, Task


class ManualCallInitiationServiceTests(TestCase):
    def setUp(self) -> None:
        self.company = create_company()
        self.user = create_user(company=self.company)
        self.lead = create_lead(user=self.user)
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
        self.resolver = Mock()
        self.resolver.resolve.return_value = self.configuration
        self.provider = Mock()
        self.provider.initiate_outbound_call.return_value = OutboundCallResult(
            conversation_id='conversation-1',
            provider_call_id='call-1',
            message='Started',
        )
        self.service = ManualCallInitiationService(
            configuration_resolver=self.resolver,
            provider_client=self.provider,
        )

    def test_initiates_call_with_tenant_and_provider_identity(self) -> None:
        call, created = self.service.initiate(
            user=self.user,
            lead_id=self.lead.pk,
            idempotency_key=uuid.uuid4(),
        )

        self.assertTrue(created)
        self.assertEqual(call.company, self.company)
        self.assertEqual(call.context_user, self.user)
        self.assertEqual(call.status, Call.Status.RINGING)
        self.assertEqual(call.phone_number, '+447911123456')
        self.assertEqual(call.outbound_number, '+442012345678')
        self.assertEqual(call.provider_conversation_id, 'conversation-1')
        self.assertEqual(call.provider_call_id, 'call-1')


    def test_returns_existing_call_for_same_idempotency_key(self) -> None:
        idempotency_key = uuid.uuid4()
        first_call, first_created = self.service.initiate(
            user=self.user,
            lead_id=self.lead.pk,
            idempotency_key=idempotency_key,
        )
        second_call, second_created = self.service.initiate(
            user=self.user,
            lead_id=self.lead.pk,
            idempotency_key=idempotency_key,
        )

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first_call.pk, second_call.pk)
        self.assertEqual(self.provider.initiate_outbound_call.call_count, 1)

    def test_rejects_lead_from_another_company(self) -> None:
        other_company = create_company('Other Company')
        other_user = create_user(
            company=other_company,
            email='other@example.com',
        )
        other_lead = create_lead(user=other_user)

        with self.assertRaises(CallAuthorizationError):
            self.service.initiate(
                user=self.user,
                lead_id=other_lead.pk,
                idempotency_key=uuid.uuid4(),
            )

    def test_rejects_invalid_phone_number_before_provider_call(self) -> None:
        self.lead.phone_no = 'not-a-phone'
        self.lead.save(update_fields=['phone_no'])

        with self.assertRaises(InvalidPhoneNumberError):
            self.service.initiate(
                user=self.user,
                lead_id=self.lead.pk,
                idempotency_key=uuid.uuid4(),
            )

        self.provider.initiate_outbound_call.assert_not_called()
        call = Call.objects.get()
        self.assertEqual(call.status, Call.Status.FAILED)
        self.assertEqual(call.failure_code, 'invalid_phone_number')
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, Lead.Status.WRONG_NUMBER)

    def test_rejects_do_not_contact_lead(self) -> None:
        self.lead.do_not_contact = True
        self.lead.save(update_fields=['do_not_contact'])

        with self.assertRaises(LeadNotCallableError):
            self.service.initiate(
                user=self.user,
                lead_id=self.lead.pk,
                idempotency_key=uuid.uuid4(),
            )

        self.provider.initiate_outbound_call.assert_not_called()
        self.assertFalse(Call.objects.exists())

    def test_rejects_non_callable_status(self) -> None:
        self.lead.status = 'converted_won'
        self.lead.save(update_fields=['status'])

        with self.assertRaises(LeadNotCallableError):
            self.service.initiate(
                user=self.user,
                lead_id=self.lead.pk,
                idempotency_key=uuid.uuid4(),
            )

        self.provider.initiate_outbound_call.assert_not_called()

    def test_allows_manual_initiate_when_unreachable(self) -> None:
        self.lead.status = Lead.Status.UNREACHABLE
        self.lead.save(update_fields=['status'])

        call, created = self.service.initiate(
            user=self.user,
            lead_id=self.lead.pk,
            idempotency_key=uuid.uuid4(),
        )

        self.assertTrue(created)
        self.assertEqual(call.trigger, Call.Trigger.MANUAL)
        self.provider.initiate_outbound_call.assert_called_once()

    def test_marks_transport_failure_as_initiation_unknown(self) -> None:
        self.provider.initiate_outbound_call.side_effect = ElevenLabsTransientError(
            'Unknown'
        )

        with self.assertRaises(ElevenLabsTransientError):
            self.service.initiate(
                user=self.user,
                lead_id=self.lead.pk,
                idempotency_key=uuid.uuid4(),
            )

        call = Call.objects.get()
        self.assertEqual(call.status, Call.Status.INITIATION_UNKNOWN)
        self.assertEqual(call.failure_code, 'provider_response_unknown')


class ScheduledCallServiceTests(TestCase):
    def setUp(self) -> None:
        self.now = timezone.now()
        self.company = create_company()
        self.user = create_user(company=self.company)
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
        self.resolver = Mock()
        self.resolver.resolve.return_value = self.configuration
        self.provider = Mock()
        self.provider.initiate_outbound_call.return_value = OutboundCallResult(
            conversation_id='conversation-1',
            provider_call_id='call-1',
            message='Started',
        )
        self.service = ScheduledCallService(
            configuration_resolver=self.resolver,
            provider_client=self.provider,
        )

    def test_claims_only_due_leads(self) -> None:
        due_lead = create_lead(
            user=self.user,
            scheduled_at=self.now - timedelta(minutes=1),
        )
        create_lead(
            user=self.user,
            name='Future Lead',
            scheduled_at=self.now + timedelta(minutes=5),
        )

        call_ids = self.service.claim_due_calls(now=self.now)

        call = Call.objects.get(pk=call_ids[0])
        self.assertEqual(len(call_ids), 1)
        self.assertEqual(call.lead, due_lead)
        self.assertEqual(call.status, Call.Status.CLAIMED)
        self.assertEqual(call.trigger, Call.Trigger.SCHEDULED)
        self.assertEqual(call.scheduled_for, due_lead.scheduled_at)

    def test_skips_do_not_contact_and_closed_leads(self) -> None:
        create_lead(
            user=self.user,
            name='DNC Lead',
            scheduled_at=self.now - timedelta(minutes=1),
        )
        dnc_lead = Lead.objects.get(name='DNC Lead')
        dnc_lead.do_not_contact = True
        dnc_lead.save(update_fields=['do_not_contact'])

        create_lead(
            user=self.user,
            name='Won Lead',
            scheduled_at=self.now - timedelta(minutes=1),
        )
        won_lead = Lead.objects.get(name='Won Lead')
        won_lead.status = 'converted_won'
        won_lead.save(update_fields=['status'])

        callable_lead = create_lead(
            user=self.user,
            name='Callable Lead',
            scheduled_at=self.now - timedelta(minutes=1),
        )

        call_ids = self.service.claim_due_calls(now=self.now)

        self.assertEqual(len(call_ids), 1)
        self.assertEqual(Call.objects.get(pk=call_ids[0]).lead_id, callable_lead.pk)

    def test_skips_unreachable_leads(self) -> None:
        lead = create_lead(
            user=self.user,
            scheduled_at=self.now - timedelta(minutes=1),
        )
        lead.status = Lead.Status.UNREACHABLE
        lead.save(update_fields=['status'])

        call_ids = self.service.claim_due_calls(now=self.now)

        self.assertEqual(call_ids, [])
        self.assertFalse(Call.objects.exists())

    def test_busy_scheduled_call_clears_schedule_and_is_not_reclaimed(self) -> None:
        from calling_agent.webhook_services import CallStatusTransitionService

        lead = create_lead(
            user=self.user,
            scheduled_at=self.now - timedelta(minutes=1),
        )
        call_id = self.service.claim_due_calls(now=self.now)[0]
        call = Call.objects.get(pk=call_id)
        CallStatusTransitionService().apply(
            call,
            Call.Status.BUSY,
            failure_detail='Busy',
        )
        lead.refresh_from_db()
        self.assertIsNone(lead.scheduled_at)
        self.assertEqual(
            self.service.claim_due_calls(now=self.now),
            [],
        )
        self.assertEqual(Call.objects.filter(lead=lead).count(), 1)

    def test_does_not_claim_due_callback_after_scheduled_busy(self) -> None:
        from calling_agent.webhook_services import CallStatusTransitionService

        lead = create_lead(
            user=self.user,
            scheduled_at=self.now - timedelta(minutes=2),
        )
        task = Task.objects.create(
            name='Callback',
            type=Task.Type.CALLBACK,
            scheduled_at=self.now - timedelta(minutes=1),
            due_date=(self.now - timedelta(minutes=1)).date(),
            status=Task.Status.PENDING,
            created_by=self.user,
            related_lead=lead,
        )
        call_id = self.service.claim_due_calls(now=self.now)[0]
        call = Call.objects.get(pk=call_id)
        self.assertEqual(Call.objects.filter(lead=lead).count(), 1)
        CallStatusTransitionService().apply(
            call,
            Call.Status.BUSY,
            failure_detail='Call declined',
        )
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.COMPLETED)
        self.assertEqual(self.service.claim_due_calls(now=self.now), [])

    def test_duplicate_claimers_create_one_call(self) -> None:
        create_lead(
            user=self.user,
            scheduled_at=self.now - timedelta(minutes=1),
        )

        first_claims = self.service.claim_due_calls(now=self.now)
        second_claims = ScheduledCallService(
            configuration_resolver=self.resolver,
            provider_client=self.provider,
        ).claim_due_calls(now=self.now)

        self.assertEqual(len(first_claims), 1)
        self.assertEqual(second_claims, [])
        self.assertEqual(Call.objects.count(), 1)

    def test_initiates_claimed_call_after_revalidation(self) -> None:
        create_lead(
            user=self.user,
            scheduled_at=self.now - timedelta(minutes=1),
        )
        call_id = self.service.claim_due_calls(now=self.now)[0]

        call, initiated = self.service.initiate_claimed_call(call_id)

        self.assertTrue(initiated)
        self.assertIsNotNone(call)
        self.assertEqual(call.status, Call.Status.RINGING)
        self.assertEqual(call.provider_conversation_id, 'conversation-1')
        self.provider.initiate_outbound_call.assert_called_once()

    def test_duplicate_initiation_task_does_not_redial(self) -> None:
        create_lead(
            user=self.user,
            scheduled_at=self.now - timedelta(minutes=1),
        )
        call_id = self.service.claim_due_calls(now=self.now)[0]

        first_call, first_initiated = self.service.initiate_claimed_call(
            call_id
        )
        second_call, second_initiated = self.service.initiate_claimed_call(
            call_id
        )

        self.assertTrue(first_initiated)
        self.assertFalse(second_initiated)
        self.assertEqual(first_call.pk, second_call.pk)
        self.provider.initiate_outbound_call.assert_called_once()

    def test_cancels_claim_when_lead_is_rescheduled(self) -> None:
        lead = create_lead(
            user=self.user,
            scheduled_at=self.now - timedelta(minutes=1),
        )
        call_id = self.service.claim_due_calls(now=self.now)[0]
        lead.scheduled_at = self.now + timedelta(hours=1)
        lead.save(update_fields=['scheduled_at'])

        call, initiated = self.service.initiate_claimed_call(call_id)

        self.assertFalse(initiated)
        self.assertEqual(call.status, Call.Status.CANCELLED)
        self.assertEqual(call.failure_code, 'schedule_changed')
        self.provider.initiate_outbound_call.assert_not_called()

    def test_cancels_claim_when_schedule_is_removed(self) -> None:
        lead = create_lead(
            user=self.user,
            scheduled_at=self.now - timedelta(minutes=1),
        )
        call_id = self.service.claim_due_calls(now=self.now)[0]
        lead.scheduled_at = None
        lead.save(update_fields=['scheduled_at'])

        call, initiated = self.service.initiate_claimed_call(call_id)

        self.assertFalse(initiated)
        self.assertEqual(call.status, Call.Status.CANCELLED)
        self.assertEqual(call.failure_code, 'schedule_cancelled')
        self.provider.initiate_outbound_call.assert_not_called()

    def test_invalid_phone_creates_terminal_audit_record(self) -> None:
        lead = create_lead(
            user=self.user,
            phone_number='invalid',
            scheduled_at=self.now - timedelta(minutes=1),
        )

        call_ids = self.service.claim_due_calls(now=self.now)

        call = Call.objects.get(lead=lead)
        self.assertEqual(call_ids, [])
        self.assertEqual(call.status, Call.Status.FAILED)
        self.assertEqual(call.failure_code, 'invalid_phone_number')
        self.provider.initiate_outbound_call.assert_not_called()

    def test_reclaims_expired_claim_for_redispatch(self) -> None:
        lead = create_lead(
            user=self.user,
            scheduled_at=self.now - timedelta(minutes=2),
        )
        call = Call.objects.create(
            company=self.company,
            lead=lead,
            context_user=self.user,
            trigger=Call.Trigger.SCHEDULED,
            status=Call.Status.CLAIMED,
            scheduled_for=lead.scheduled_at,
            claim_expires_at=self.now - timedelta(seconds=1),
        )

        call_ids = self.service.reclaim_expired_claims(now=self.now)

        call.refresh_from_db()
        self.assertEqual(call_ids, [call.pk])
        self.assertGreater(call.claim_expires_at, self.now)

    def test_stale_initiation_is_not_reclaimed_for_redial(self) -> None:
        lead = create_lead(
            user=self.user,
            scheduled_at=self.now - timedelta(minutes=2),
        )
        call = Call.objects.create(
            company=self.company,
            lead=lead,
            context_user=self.user,
            trigger=Call.Trigger.SCHEDULED,
            status=Call.Status.INITIATING,
            scheduled_for=lead.scheduled_at,
            claim_expires_at=self.now - timedelta(seconds=1),
        )

        unknown_ids = self.service.mark_stale_initiations_unknown(now=self.now)
        reclaimed_ids = self.service.reclaim_expired_claims(now=self.now)

        call.refresh_from_db()
        self.assertEqual(unknown_ids, [call.pk])
        self.assertEqual(reclaimed_ids, [])
        self.assertEqual(call.status, Call.Status.INITIATION_UNKNOWN)

    def test_claims_due_callback_tasks(self) -> None:
        lead = create_lead(user=self.user)
        due_task = Task.objects.create(
            name='Callback tomorrow',
            type=Task.Type.CALLBACK,
            scheduled_at=self.now - timedelta(minutes=1),
            due_date=(self.now - timedelta(minutes=1)).date(),
            status=Task.Status.PENDING,
            created_by=self.user,
            related_lead=lead,
        )
        Task.objects.create(
            name='Future callback',
            type=Task.Type.CALLBACK,
            scheduled_at=self.now + timedelta(hours=1),
            due_date=(self.now + timedelta(hours=1)).date(),
            status=Task.Status.PENDING,
            created_by=self.user,
            related_lead=lead,
        )
        Task.objects.create(
            name='Completed callback',
            type=Task.Type.CALLBACK,
            scheduled_at=self.now - timedelta(minutes=2),
            due_date=(self.now - timedelta(minutes=2)).date(),
            status=Task.Status.COMPLETED,
            created_by=self.user,
            related_lead=lead,
        )
        Task.objects.create(
            name='Normal task',
            scheduled_at=self.now - timedelta(minutes=1),
            due_date=(self.now - timedelta(minutes=1)).date(),
            status=Task.Status.PENDING,
            created_by=self.user,
            related_lead=lead,
        )

        call_ids = self.service.claim_due_calls(now=self.now)

        self.assertEqual(len(call_ids), 1)
        call = Call.objects.get(pk=call_ids[0])
        due_task.refresh_from_db()
        self.assertEqual(call.lead, lead)
        self.assertEqual(call.related_task, due_task)
        self.assertEqual(call.scheduled_for, due_task.scheduled_at)
        self.assertEqual(call.status, Call.Status.CLAIMED)
        self.assertEqual(due_task.status, Task.Status.IN_PROGRESS)

    def test_claims_lead_and_callback_task_schedules_together(self) -> None:
        due_lead = create_lead(
            user=self.user,
            name='Lead Schedule',
            scheduled_at=self.now - timedelta(minutes=2),
        )
        task_lead = create_lead(user=self.user, name='Task Schedule')
        task = Task.objects.create(
            name='Callback',
            type=Task.Type.CALLBACK,
            scheduled_at=self.now - timedelta(minutes=1),
            due_date=(self.now - timedelta(minutes=1)).date(),
            status=Task.Status.PENDING,
            created_by=self.user,
            related_lead=task_lead,
        )

        call_ids = self.service.claim_due_calls(now=self.now)

        self.assertEqual(len(call_ids), 2)
        lead_call = Call.objects.get(lead=due_lead)
        task_call = Call.objects.get(lead=task_lead)
        self.assertIsNone(lead_call.related_task)
        self.assertEqual(task_call.related_task, task)

    def test_skips_callback_task_without_related_lead(self) -> None:
        Task.objects.create(
            name='Orphan callback',
            type=Task.Type.CALLBACK,
            scheduled_at=self.now - timedelta(minutes=1),
            due_date=(self.now - timedelta(minutes=1)).date(),
            status=Task.Status.PENDING,
            created_by=self.user,
            related_lead=None,
        )

        call_ids = self.service.claim_due_calls(now=self.now)

        self.assertEqual(call_ids, [])
        self.assertEqual(Call.objects.count(), 0)

    def test_cancels_callback_claim_when_task_schedule_changes(self) -> None:
        lead = create_lead(user=self.user)
        task = Task.objects.create(
            name='Callback',
            type=Task.Type.CALLBACK,
            scheduled_at=self.now - timedelta(minutes=1),
            due_date=(self.now - timedelta(minutes=1)).date(),
            status=Task.Status.PENDING,
            created_by=self.user,
            related_lead=lead,
        )
        call_id = self.service.claim_due_calls(now=self.now)[0]
        task.scheduled_at = self.now + timedelta(hours=2)
        task.save(update_fields=['scheduled_at'])

        call, initiated = self.service.initiate_claimed_call(call_id)

        self.assertFalse(initiated)
        self.assertEqual(call.status, Call.Status.CANCELLED)
        self.assertEqual(call.failure_code, 'schedule_changed')
        self.provider.initiate_outbound_call.assert_not_called()

    def test_initiates_claimed_callback_task_call(self) -> None:
        lead = create_lead(user=self.user)
        Task.objects.create(
            name='Callback',
            type=Task.Type.CALLBACK,
            scheduled_at=self.now - timedelta(minutes=1),
            due_date=(self.now - timedelta(minutes=1)).date(),
            status=Task.Status.PENDING,
            created_by=self.user,
            related_lead=lead,
        )
        call_id = self.service.claim_due_calls(now=self.now)[0]

        call, initiated = self.service.initiate_claimed_call(call_id)

        self.assertTrue(initiated)
        self.assertIsNotNone(call)
        self.assertEqual(call.status, Call.Status.RINGING)
        self.provider.initiate_outbound_call.assert_called_once()


class TwilioCallHangupServiceTests(SimpleTestCase):
    def setUp(self) -> None:
        self.service = TwilioCallHangupService()

    @override_settings(TWILIO_ACCOUNT_SID='', TWILIO_AUTH_TOKEN='')
    def test_requires_twilio_credentials(self) -> None:
        with self.assertRaises(CallingConfigurationError):
            self.service.hangup('CA123')

    @override_settings(
        TWILIO_ACCOUNT_SID='AC123',
        TWILIO_AUTH_TOKEN='token-123',
        ELEVENLABS_REQUEST_TIMEOUT_SECONDS=10,
    )
    def test_hangs_up_twilio_call(self) -> None:
        twilio_response = Mock()
        twilio_response.status_code = 200
        with patch('httpx.post', return_value=twilio_response) as post_mock:
            self.service.hangup('CA123')

        post_mock.assert_called_once()
        self.assertIn('/Calls/CA123.json', post_mock.call_args.args[0])
        self.assertEqual(
            post_mock.call_args.kwargs['data'],
            {'Status': 'completed'},
        )
        self.assertEqual(
            post_mock.call_args.kwargs['auth'],
            ('AC123', 'token-123'),
        )

    @override_settings(
        TWILIO_ACCOUNT_SID='AC123',
        TWILIO_AUTH_TOKEN='token-123',
    )
    def test_treats_twilio_404_as_success(self) -> None:
        twilio_response = Mock()
        twilio_response.status_code = 404
        with patch('httpx.post', return_value=twilio_response):
            self.service.hangup('CA123')

    @override_settings(
        TWILIO_ACCOUNT_SID='AC123',
        TWILIO_AUTH_TOKEN='token-123',
    )
    def test_maps_client_error_to_request_error(self) -> None:
        twilio_response = Mock()
        twilio_response.status_code = 400
        with patch('httpx.post', return_value=twilio_response):
            with self.assertRaises(ElevenLabsRequestError):
                self.service.hangup('CA123')

    @override_settings(
        TWILIO_ACCOUNT_SID='AC123',
        TWILIO_AUTH_TOKEN='token-123',
    )
    def test_maps_transport_failure_to_transient_error(self) -> None:
        with patch('httpx.post', side_effect=httpx.ConnectError('down')):
            with self.assertRaises(ElevenLabsTransientError):
                self.service.hangup('CA123')


class CallTerminationServiceTests(TestCase):
    def setUp(self) -> None:
        self.company = create_company()
        self.user = create_user(company=self.company)
        self.lead = create_lead(user=self.user)
        self.hangup = Mock()
        self.service = CallTerminationService(hangup_service=self.hangup)

    def _create_ringing_call(self, **overrides) -> Call:
        values = {
            'company': self.company,
            'lead': self.lead,
            'context_user': self.user,
            'status': Call.Status.RINGING,
            'provider_conversation_id': 'conversation-1',
            'provider_call_id': 'call-1',
            'initiated_at': timezone.now() - timedelta(seconds=45),
        }
        values.update(overrides)
        return Call.objects.create(**values)

    def test_terminates_ringing_call(self) -> None:
        call = self._create_ringing_call()

        updated = self.service.terminate(
            user=self.user,
            public_id=call.public_id,
            reason='Operator hangup',
        )

        self.hangup.hangup.assert_called_once_with('call-1')
        self.assertEqual(updated.status, Call.Status.COMPLETED)
        self.assertEqual(updated.failure_code, 'user_terminated')
        self.assertEqual(updated.failure_detail, 'Operator hangup')
        self.assertIsNotNone(updated.ended_at)
        self.assertGreaterEqual(updated.duration_seconds, 45)
        self.assertLessEqual(updated.duration_seconds, 46)

    def test_rejects_terminal_call(self) -> None:
        call = self._create_ringing_call(status=Call.Status.COMPLETED)

        with self.assertRaises(CallConflictError):
            self.service.terminate(
                user=self.user,
                public_id=call.public_id,
            )

        self.hangup.hangup.assert_not_called()

    def test_rejects_call_without_twilio_sid(self) -> None:
        call = self._create_ringing_call(provider_call_id='')

        with self.assertRaises(CallConflictError):
            self.service.terminate(
                user=self.user,
                public_id=call.public_id,
            )

        self.hangup.hangup.assert_not_called()
        call.refresh_from_db()
        self.assertEqual(call.status, Call.Status.RINGING)

    def test_rejects_call_from_another_company(self) -> None:
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
            provider_call_id='call-other',
        )

        with self.assertRaises(CallAuthorizationError):
            self.service.terminate(
                user=self.user,
                public_id=call.public_id,
            )

        self.hangup.hangup.assert_not_called()

    def test_leaves_status_unchanged_when_provider_rejects(self) -> None:
        call = self._create_ringing_call()
        self.hangup.hangup.side_effect = ElevenLabsRequestError('Rejected')

        with self.assertRaises(ElevenLabsRequestError):
            self.service.terminate(
                user=self.user,
                public_id=call.public_id,
            )

        call.refresh_from_db()
        self.assertEqual(call.status, Call.Status.RINGING)
        self.assertEqual(call.failure_code, '')

    def test_leaves_status_unchanged_on_transport_failure(self) -> None:
        call = self._create_ringing_call()
        self.hangup.hangup.side_effect = ElevenLabsTransientError('Unknown')

        with self.assertRaises(ElevenLabsTransientError):
            self.service.terminate(
                user=self.user,
                public_id=call.public_id,
            )

        call.refresh_from_db()
        self.assertEqual(call.status, Call.Status.RINGING)
        self.assertEqual(call.failure_code, '')


class PostCallLeadStatusTransitionTests(TestCase):
    def setUp(self) -> None:
        from calling_agent.webhook_services import CallStatusTransitionService
        self.company = create_company()
        self.user = create_user(company=self.company)
        self.lead = create_lead(user=self.user)
        self.transition_service = CallStatusTransitionService()

    def test_completed_call_updates_lead_status_to_contacted(self) -> None:
        call = Call.objects.create(
            company=self.company,
            lead=self.lead,
            context_user=self.user,
            phone_number='+447911123456',
            status=Call.Status.IN_PROGRESS,
        )
        self.transition_service.apply(call, Call.Status.COMPLETED)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, Lead.Status.CONTACTED)

    def test_unanswered_call_updates_lead_status_to_no_answer(self) -> None:
        call = Call.objects.create(
            company=self.company,
            lead=self.lead,
            context_user=self.user,
            phone_number='+447911123456',
            status=Call.Status.RINGING,
        )
        self.transition_service.apply(call, Call.Status.NO_ANSWER, failure_detail='No answer')
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, Lead.Status.NO_ANSWER)

    def test_invalid_phone_number_call_failure_updates_lead_status_to_wrong_number(self) -> None:
        call = Call.objects.create(
            company=self.company,
            lead=self.lead,
            context_user=self.user,
            phone_number='123',
            status=Call.Status.INITIATING,
        )
        self.transition_service.apply(
            call,
            Call.Status.FAILED,
            failure_code='invalid_phone_number',
            failure_detail='The lead phone number is not valid.',
        )
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, Lead.Status.WRONG_NUMBER)

    def test_three_failed_call_attempts_update_lead_status_to_unreachable(self) -> None:
        for _ in range(2):
            Call.objects.create(
                company=self.company,
                lead=self.lead,
                context_user=self.user,
                phone_number='+447911123456',
                status=Call.Status.NO_ANSWER,
            )
        call = Call.objects.create(
            company=self.company,
            lead=self.lead,
            context_user=self.user,
            phone_number='+447911123456',
            status=Call.Status.RINGING,
        )
        self.transition_service.apply(call, Call.Status.NO_ANSWER, failure_detail='No answer')
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, Lead.Status.UNREACHABLE)

    def test_busy_call_updates_lead_status_to_no_answer(self) -> None:
        call = Call.objects.create(
            company=self.company,
            lead=self.lead,
            context_user=self.user,
            phone_number='+447911123456',
            status=Call.Status.RINGING,
        )
        self.transition_service.apply(call, Call.Status.BUSY, failure_detail='Busy')
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, Lead.Status.NO_ANSWER)

    def test_cancelled_call_updates_lead_status_to_no_answer(self) -> None:
        call = Call.objects.create(
            company=self.company,
            lead=self.lead,
            context_user=self.user,
            phone_number='+447911123456',
            status=Call.Status.RINGING,
        )
        self.transition_service.apply(
            call,
            Call.Status.CANCELLED,
            failure_detail='Call declined',
        )
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, Lead.Status.NO_ANSWER)

    def test_completed_call_does_not_clobber_qualification_status(self) -> None:
        self.lead.status = Lead.Status.INTERESTED
        self.lead.save(update_fields=['status', 'updated_at'])
        call = Call.objects.create(
            company=self.company,
            lead=self.lead,
            context_user=self.user,
            phone_number='+447911123456',
            status=Call.Status.IN_PROGRESS,
        )
        self.transition_service.apply(call, Call.Status.COMPLETED)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, Lead.Status.INTERESTED)


class LeadSaveHookTests(TestCase):
    def setUp(self) -> None:
        self.company = create_company()
        self.user = create_user(company=self.company)

    def test_save_does_not_force_new_when_unscheduled(self) -> None:
        lead = create_lead(user=self.user)
        lead.status = Lead.Status.INTERESTED
        lead.scheduled_at = None
        lead.save(update_fields=['status', 'scheduled_at', 'updated_at'])
        lead.refresh_from_db()
        self.assertEqual(lead.status, Lead.Status.INTERESTED)

    def test_scheduled_new_lead_becomes_call_pending(self) -> None:
        lead = create_lead(user=self.user, scheduled_at=timezone.now())
        lead.refresh_from_db()
        self.assertEqual(lead.status, Lead.Status.CALL_PENDING)

    def test_scheduled_at_does_not_revert_interested(self) -> None:
        lead = create_lead(user=self.user)
        lead.status = Lead.Status.INTERESTED
        lead.scheduled_at = timezone.now()
        lead.save(update_fields=['status', 'scheduled_at', 'updated_at'])
        lead.refresh_from_db()
        self.assertEqual(lead.status, Lead.Status.INTERESTED)


class PostCallStatusAliasTests(SimpleTestCase):
    def test_normalize_display_label_and_aliases(self) -> None:
        from calling_agent.webhook_services import normalize_post_call_lead_status

        status, dnc = normalize_post_call_lead_status('Highly Interested')
        self.assertEqual(status, Lead.Status.HIGHLY_INTERESTED)
        self.assertFalse(dnc)

        status, dnc = normalize_post_call_lead_status('qualified')
        self.assertEqual(status, Lead.Status.INTERESTED)
        self.assertFalse(dnc)

        status, dnc = normalize_post_call_lead_status('do_not_call')
        self.assertEqual(status, Lead.Status.NOT_INTERESTED)
        self.assertTrue(dnc)

        status, dnc = normalize_post_call_lead_status('wrong person')
        self.assertEqual(status, Lead.Status.WRONG_NUMBER)
        self.assertFalse(dnc)

        status, dnc = normalize_post_call_lead_status('unknown-disposition')
        self.assertIsNone(status)
        self.assertFalse(dnc)
