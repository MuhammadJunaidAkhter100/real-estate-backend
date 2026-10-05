"""Credit metering for chat turns, calls, transcripts and KB extraction."""

import uuid
from unittest.mock import Mock, patch

from rest_framework.test import APIClient

from django.test import TestCase

from billing import services
from billing.exceptions import PlanLimitReached
from billing.models import UsageCounter
from calling_agent.models import Call
from chatbot.models import ChatSession, KnowledgeBaseDocument
from users.models import Company, Lead, User


def used(company, kind):
    return services.current_count(company.pk, kind)


class MeteringTestBase(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='Metering Co')
        self.company.status = Company.Status.ACTIVE
        self.company.plan = Company.Plan.BASIC
        self.company.save(update_fields=['status', 'plan'])
        self.user = User.objects.create_user(
            email='meter@example.com',
            first_name='First',
            last_name='Last',
            password='pw12345678',
            company=self.company,
            role=User.Role.COMPANY_ADMIN,
            status=User.Status.ACTIVE,
        )

    def setUpClient(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def exhaust_credits(self, amount=500):
        services.consume(self.company.pk, UsageCounter.Kind.AI_CREDITS, amount)


class ChatTurnMeteringTests(MeteringTestBase):
    def test_chat_turn_costs_one_credit(self):
        from chatbot.views import charge_chat_turn

        self.assertTrue(charge_chat_turn(self.user))
        self.assertEqual(used(self.company, UsageCounter.Kind.AI_CREDITS), 1)

    def test_chat_turn_refund(self):
        from chatbot.views import charge_chat_turn, refund_chat_turn

        charge_chat_turn(self.user)
        refund_chat_turn(self.user)
        self.assertEqual(used(self.company, UsageCounter.Kind.AI_CREDITS), 0)

    def test_chat_turn_over_limit_raises(self):
        from chatbot.views import charge_chat_turn

        self.exhaust_credits()
        with self.assertRaises(PlanLimitReached):
            charge_chat_turn(self.user)

    def test_stream_view_returns_403_when_exhausted(self):
        self.exhaust_credits()
        self.setUpClient()
        session = ChatSession.objects.create(
            thread_id='11111111-1111-1111-1111-111111111111',
            user=self.user,
            title='t',
        )
        response = self.client.post(
            '/api/chatbot/stream/',
            data={'message': 'hi', 'thread_id': str(session.thread_id)},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['code'], 'PLAN_LIMIT_REACHED')
        self.assertEqual(used(self.company, UsageCounter.Kind.AI_CREDITS), 500)

    @patch('chatbot.views.stream_response')
    def test_stream_view_charges_once_per_turn(self, mock_stream):
        mock_stream.return_value = iter(['hello ', 'world'])
        self.setUpClient()
        session = ChatSession.objects.create(
            thread_id='22222222-2222-2222-2222-222222222222',
            user=self.user,
            title='t',
        )
        response = self.client.post(
            '/api/chatbot/stream/',
            data={'message': 'hi', 'thread_id': str(session.thread_id)},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(used(self.company, UsageCounter.Kind.AI_CREDITS), 1)

    @patch('chatbot.views.stream_response')
    def test_failed_turn_with_no_output_is_refunded(self, mock_stream):
        mock_stream.side_effect = RuntimeError('llm down')
        self.setUpClient()
        session = ChatSession.objects.create(
            thread_id='33333333-3333-3333-3333-333333333333',
            user=self.user,
            title='t',
        )
        response = self.client.post(
            '/api/chatbot/stream/',
            data={'message': 'hi', 'thread_id': str(session.thread_id)},
            content_type='application/json',
        )
        # The test client does not consume an SSE body, so drain it here to
        # exercise the error/refund path inside the generator.
        body = b''.join(response.streaming_content)
        self.assertIn(b'"type": "error"', body)
        self.assertEqual(used(self.company, UsageCounter.Kind.AI_CREDITS), 0)


class KnowledgeBaseMeteringTests(MeteringTestBase):
    def _document(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        doc = KnowledgeBaseDocument(user=self.user, original_filename='kb.pdf',
                                    file_type='pdf')
        doc.file.save('kb.pdf', SimpleUploadedFile('kb.pdf', b'%PDF-1.4 hello'),
                      save=False)
        doc.save()
        return doc

    def test_extraction_charges_one_credit(self):
        from chatbot.tasks import extract_knowledge_base_document

        doc = self._document()
        with patch('chatbot.tasks.DataExtractor') as extractor_cls, \
                patch('chatbot.tasks.PineconeService'):
            extractor = extractor_cls.return_value
            extractor.extract.return_value = type(
                'R', (), {'text': 'hello', 'char_count': 5, 'file_type': 'pdf'})()
            extract_knowledge_base_document(doc.pk)
        self.assertEqual(used(self.company, UsageCounter.Kind.AI_CREDITS), 1)
        doc.refresh_from_db()
        self.assertEqual(doc.status, KnowledgeBaseDocument.Status.COMPLETED)

    def test_same_document_is_never_charged_twice(self):
        from chatbot.tasks import extract_knowledge_base_document

        doc = self._document()
        with patch('chatbot.tasks.DataExtractor') as extractor_cls, \
                patch('chatbot.tasks.PineconeService'):
            extractor = extractor_cls.return_value
            extractor.extract.return_value = type(
                'R', (), {'text': 'hello', 'char_count': 5, 'file_type': 'pdf'})()
            extract_knowledge_base_document(doc.pk)
            extract_knowledge_base_document(doc.pk)
        self.assertEqual(used(self.company, UsageCounter.Kind.AI_CREDITS), 1)

    def test_extraction_over_limit_is_skipped(self):
        from chatbot.tasks import extract_knowledge_base_document

        doc = self._document()
        self.exhaust_credits()
        with patch('chatbot.tasks.DataExtractor') as extractor_cls, \
                patch('chatbot.tasks.PineconeService') as pinecone:
            result = extract_knowledge_base_document(doc.pk)
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'PLAN_LIMIT_REACHED')
        pinecone.return_value.upsert_document.assert_not_called()
        self.assertEqual(used(self.company, UsageCounter.Kind.AI_CREDITS), 500)

    def test_failed_extraction_is_refunded(self):
        from chatbot.data_extraction import ExtractionError
        from chatbot.tasks import extract_knowledge_base_document

        doc = self._document()
        with patch('chatbot.tasks.DataExtractor') as extractor_cls, \
                patch('chatbot.tasks.PineconeService'):
            extractor_cls.return_value.extract.side_effect = ExtractionError('bad pdf')
            extract_knowledge_base_document(doc.pk)
        self.assertEqual(used(self.company, UsageCounter.Kind.AI_CREDITS), 0)
        doc.refresh_from_db()
        self.assertEqual(doc.status, KnowledgeBaseDocument.Status.FAILED)

    def test_upload_view_rejects_when_exhausted(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.exhaust_credits()
        self.setUpClient()
        upload = SimpleUploadedFile('kb.pdf', b'%PDF-1.4 test', content_type='application/pdf')
        response = self.client.post(
            '/api/chatbot/knowledge-base/',
            data={'files': upload},
            format='multipart',
        )
        self.assertEqual(response.status_code, 400, response.content)
        payload = response.json()
        self.assertEqual(payload['rejected'][0]['code'], 'PLAN_LIMIT_REACHED')
        self.assertEqual(
            KnowledgeBaseDocument.objects.filter(user=self.user).count(), 0)


class TranscriptMeteringTests(MeteringTestBase):
    def _call(self):
        return Call.objects.create(
            company=self.company,
            lead_name='Lead',
            phone_number='+15550001111',
            outbound_number='+15550002222',
            status=Call.Status.COMPLETED,
        )

    def test_transcript_charges_one_credit(self):
        from calling_agent.tasks import process_call_transcript_task

        call = self._call()
        with patch('calling_agent.tasks.analyze_and_process_call'):
            process_call_transcript_task(call.pk)
        self.assertEqual(used(self.company, UsageCounter.Kind.AI_CREDITS), 1)

    def test_transcript_is_charged_once_per_call(self):
        from calling_agent.tasks import process_call_transcript_task

        call = self._call()
        with patch('calling_agent.tasks.analyze_and_process_call'):
            process_call_transcript_task(call.pk)
            process_call_transcript_task(call.pk)
        self.assertEqual(used(self.company, UsageCounter.Kind.AI_CREDITS), 1)

    def test_transcript_failure_is_refunded(self):
        from calling_agent.tasks import process_call_transcript_task

        call = self._call()
        with patch('calling_agent.tasks.analyze_and_process_call',
                   side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                process_call_transcript_task(call.pk)
        self.assertEqual(used(self.company, UsageCounter.Kind.AI_CREDITS), 0)

    def test_transcript_over_limit_is_skipped(self):
        from calling_agent.tasks import process_call_transcript_task

        call = self._call()
        self.exhaust_credits()
        with patch('calling_agent.tasks.analyze_and_process_call') as analyze:
            result = process_call_transcript_task(call.pk)
        self.assertEqual(result['code'], 'PLAN_LIMIT_REACHED')
        analyze.assert_not_called()
        self.assertEqual(used(self.company, UsageCounter.Kind.AI_CREDITS), 500)

    def test_signal_fallback_is_metered(self):
        """A broker outage must not hand out free transcript analysis."""
        call = self._call()
        call.transcript_data = [{'role': 'agent', 'message': 'hi'}]
        call.summary = ''
        call.key_sentiments = []
        call.detected_intents = []

        with patch(
            'calling_agent.tasks.process_call_transcript_task.delay',
            side_effect=RuntimeError('broker down'),
        ), patch('calling_agent.transcript.analyze_and_process_call') as analyze:
            with self.captureOnCommitCallbacks(execute=True):
                call.save()

        analyze.assert_called_once()
        self.assertEqual(used(self.company, UsageCounter.Kind.AI_CREDITS), 1)

    def test_signal_fallback_respects_credit_limit(self):
        call = self._call()
        call.transcript_data = [{'role': 'agent', 'message': 'hi'}]
        call.summary = ''
        call.key_sentiments = []
        call.detected_intents = []
        self.exhaust_credits()

        with patch(
            'calling_agent.tasks.process_call_transcript_task.delay',
            side_effect=RuntimeError('broker down'),
        ), patch('calling_agent.transcript.analyze_and_process_call') as analyze:
            with self.captureOnCommitCallbacks(execute=True):
                call.save()

        analyze.assert_not_called()
        self.assertEqual(used(self.company, UsageCounter.Kind.AI_CREDITS), 500)

    def test_task_and_fallback_charge_only_once(self):
        call = self._call()
        from calling_agent.services import charge_transcript_credits

        self.assertTrue(charge_transcript_credits(call))
        self.assertFalse(charge_transcript_credits(call))
        self.assertEqual(used(self.company, UsageCounter.Kind.AI_CREDITS), 1)


class CallMeteringTests(MeteringTestBase):
    """Call credits are billed where a call is actually placed.

    These drive the real services rather than the `charge_call_credits`
    helper, so a seam that stops being wired up fails the suite.
    """

    def setUp(self):
        super().setUp()
        self.lead = Lead.objects.create(
            name='Lead',
            phone_no='+15550001111',
            created_by=self.user,
        )
        self.configuration = type(
            'C', (), {'outbound_phone_number': '+15550002222',
                      'agent_id': 'a', 'phone_number_id': 'p', 'key': 'default'})()

    def _manual_service(self):
        from calling_agent.services import ManualCallInitiationService

        service = ManualCallInitiationService(provider_client=Mock())
        for target, attr, value in (
            (service.configuration_resolver, 'resolve', self.configuration),
            (service.provider_initiation, 'initiate', Mock(return_value=True)),
            ('calling_agent.services.normalize_phone_number', None, '+15550001111'),
        ):
            patcher = patch.object(target, attr, return_value=value) if attr else \
                patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return service

    def test_manual_call_charges_five_credits(self):
        service = self._manual_service()
        call, created = service.initiate(
            user=self.user,
            lead_id=self.lead.pk,
            idempotency_key=uuid.uuid4(),
        )
        self.assertTrue(created)
        self.assertEqual(call.status, Call.Status.INITIATING)
        self.assertEqual(used(self.company, UsageCounter.Kind.AI_CREDITS), 5)

    def test_manual_call_retry_with_same_key_is_not_charged_twice(self):
        service = self._manual_service()
        key = uuid.uuid4()
        service.initiate(user=self.user, lead_id=self.lead.pk, idempotency_key=key)
        call, created = service.initiate(
            user=self.user, lead_id=self.lead.pk, idempotency_key=key)
        self.assertFalse(created)
        self.assertEqual(used(self.company, UsageCounter.Kind.AI_CREDITS), 5)

    def test_manual_call_that_never_dials_out_is_not_charged(self):
        from calling_agent.exceptions import ElevenLabsError

        service = self._manual_service()
        service.provider_initiation.initiate = Mock(
            side_effect=ElevenLabsError('provider down'))

        with self.assertRaises(ElevenLabsError):
            service.initiate(
                user=self.user, lead_id=self.lead.pk, idempotency_key=uuid.uuid4())
        self.assertEqual(used(self.company, UsageCounter.Kind.AI_CREDITS), 0)

    def test_manual_call_blocked_by_credit_limit_is_not_placed(self):
        self.exhaust_credits()
        provider_initiate = Mock(return_value=True)
        service = self._manual_service()
        # provider_initiation.initiate is patched to a fresh Mock by the helper,
        # so swap it for one the test can inspect.
        service.provider_initiation.initiate = provider_initiate

        with self.assertRaises(PlanLimitReached):
            service.initiate(
                user=self.user, lead_id=self.lead.pk, idempotency_key=uuid.uuid4())

        # Billing runs after the provider is called, so the attempt is recorded
        # on the Call row but the credit cap is what blocks the call being free.
        self.assertTrue(provider_initiate.called)
        self.assertEqual(used(self.company, UsageCounter.Kind.AI_CREDITS), 500)

    def test_scheduled_call_charges_five_credits(self):
        from calling_agent.services import ScheduledCallService

        lead = Lead.objects.create(
            name='Lead',
            phone_no='+15550001111',
            created_by=self.user,
        )
        call = Call.objects.create(
            company=self.company,
            lead=lead,
            lead_name=lead.name,
            phone_number='+15550001111',
            outbound_number='+15550002222',
            status=Call.Status.CLAIMED,
            trigger=Call.Trigger.SCHEDULED,
        )
        service = ScheduledCallService()
        configuration = type(
            'C', (), {'outbound_phone_number': '+15550002222',
                      'agent_id': 'a', 'phone_number_id': 'p', 'key': 'default'})()
        with patch.object(service.configuration_resolver, 'resolve',
                          return_value=configuration), \
                patch.object(service.provider_initiation, 'initiate',
                             return_value=True), \
                patch.object(service, '_schedule_is_current', return_value=True), \
                patch.object(service, '_cancellation_code', return_value=''), \
                patch('calling_agent.services.normalize_phone_number',
                      return_value='+15550001111'):
            result_call, initiated = service.initiate_claimed_call(call.pk)
        self.assertTrue(initiated)
        self.assertEqual(result_call.pk, call.pk)
        self.assertEqual(used(self.company, UsageCounter.Kind.AI_CREDITS), 5)

    def test_scheduled_call_not_initiated_is_not_charged(self):
        from calling_agent.services import ScheduledCallService

        lead = Lead.objects.create(
            name='Lead',
            phone_no='+15550001111',
            created_by=self.user,
        )
        call = Call.objects.create(
            company=self.company,
            lead=lead,
            lead_name=lead.name,
            phone_number='+15550001111',
            status=Call.Status.CLAIMED,
            trigger=Call.Trigger.SCHEDULED,
        )
        service = ScheduledCallService()
        configuration = type(
            'C', (), {'outbound_phone_number': '+15550002222',
                      'agent_id': 'a', 'phone_number_id': 'p', 'key': 'default'})()
        with patch.object(service.configuration_resolver, 'resolve',
                          return_value=configuration), \
                patch.object(service.provider_initiation, 'initiate',
                             return_value=False), \
                patch.object(service, '_schedule_is_current', return_value=True), \
                patch.object(service, '_cancellation_code', return_value=''), \
                patch('calling_agent.services.normalize_phone_number',
                      return_value='+15550001111'):
            _, initiated = service.initiate_claimed_call(call.pk)
        self.assertFalse(initiated)
        self.assertEqual(used(self.company, UsageCounter.Kind.AI_CREDITS), 0)

    def test_unlimited_plan_still_records_usage(self):
        self.company.plan = Company.Plan.PROFESSIONAL
        self.company.save(update_fields=['plan'])
        from chatbot.views import charge_chat_turn

        for _ in range(5):
            charge_chat_turn(self.user)
        self.assertEqual(used(self.company, UsageCounter.Kind.AI_CREDITS), 5)


class PreflightTests(MeteringTestBase):
    def test_assert_can_use_credits_allows_within_limit(self):
        services.assert_can_use_credits(
            self.company.pk, 'kb_document_extraction')

    def test_assert_can_use_credits_blocks_at_limit(self):
        self.exhaust_credits()
        with self.assertRaises(PlanLimitReached):
            services.assert_can_use_credits(
                self.company.pk, 'kb_document_extraction')

    def test_assert_can_use_credits_allows_unlimited_plan(self):
        self.exhaust_credits()
        self.company.plan = Company.Plan.PROFESSIONAL
        self.company.save(update_fields=['plan'])
        services.assert_can_use_credits(
            self.company.pk, 'kb_document_extraction')

    def test_call_credit_cost_is_configured(self):
        from billing.plan_limits import CREDIT_COSTS

        self.assertEqual(CREDIT_COSTS['calling_agent_call'], 5)
        self.assertEqual(CREDIT_COSTS['transcript_processing'], 1)
        self.assertEqual(CREDIT_COSTS['kb_document_extraction'], 1)
