from __future__ import annotations

import shutil
import tempfile
from datetime import timedelta
from unittest.mock import patch

from celery.exceptions import SoftTimeLimitExceeded
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from chatbot.models import KnowledgeBaseDocument
from chatbot.pinecone_service import OPENAI_TIMEOUT_SECONDS, PineconeService
from chatbot.tasks import (
    KB_PROCESSING_STALLED_AFTER,
    KB_TASK_SOFT_TIME_LIMIT,
    KB_TASK_TIME_LIMIT,
    STALLED_ERROR,
    TIMED_OUT_ERROR,
    extract_knowledge_base_document,
    import_company_website_knowledge,
)
from chatbot.website_knowledge import WEBSITE_DOCUMENT_NAME
from users.models import Company, User

MEDIA_ROOT = tempfile.mkdtemp(prefix='kb-stalled-tests-')
LIST_URL = '/api/chatbot/knowledge-base/'
WEBSITE_URL = '/api/chatbot/knowledge-base/website/'

Status = KnowledgeBaseDocument.Status


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class StalledDocumentTestCase(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        self.company = Company.objects.create(name='Acme Realty', website='https://acme.test')
        self.admin = User.objects.create_user(
            email='admin@acme.test',
            first_name='Test',
            last_name='User',
            password='password123',
            company=self.company,
            role=User.Role.COMPANY_ADMIN,
            status=User.Status.ACTIVE,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def make_doc(self, status, age, **fields):
        doc = KnowledgeBaseDocument.objects.create(
            user=self.admin,
            file=SimpleUploadedFile('notes.txt', b'Acme Realty sells villas in Dubai.'),
            original_filename=fields.pop('original_filename', 'notes.txt'),
            file_type='txt',
            status=status,
            **fields,
        )
        KnowledgeBaseDocument.objects.filter(pk=doc.pk).update(updated_at=timezone.now() - age)
        return doc


class FailStalledDocumentsTests(StalledDocumentTestCase):
    def test_the_list_marks_documents_whose_worker_is_gone_as_failed(self):
        dead = self.make_doc(Status.PROCESSING, timedelta(minutes=20))
        lost = self.make_doc(Status.PENDING, timedelta(hours=2))

        response = self.client.get(LIST_URL)

        self.assertEqual(response.status_code, 200)
        rows = {row['id']: row for row in response.data}
        for doc in (dead, lost):
            self.assertEqual(rows[doc.pk]['status'], 'failed')
            self.assertEqual(rows[doc.pk]['error'], STALLED_ERROR)
            doc.refresh_from_db()
            self.assertEqual(doc.status, Status.FAILED)

    def test_the_list_leaves_running_and_finished_documents_alone(self):
        running = self.make_doc(Status.PROCESSING, timedelta(minutes=5))
        queued = self.make_doc(Status.PENDING, timedelta(minutes=30))
        done = self.make_doc(Status.COMPLETED, timedelta(days=3))

        response = self.client.get(LIST_URL)

        rows = {row['id']: row['status'] for row in response.data}
        self.assertEqual(rows, {running.pk: 'processing', queued.pk: 'pending', done.pk: 'completed'})

    def test_another_users_documents_are_not_touched(self):
        other = User.objects.create_user(
            email='agent@acme.test',
            first_name='Test',
            last_name='User',
            password='password123',
            company=self.company,
            role=User.Role.AGENT,
            status=User.Status.ACTIVE,
        )
        doc = self.make_doc(Status.PROCESSING, timedelta(minutes=20))
        KnowledgeBaseDocument.objects.filter(pk=doc.pk).update(user=other)

        self.client.get(LIST_URL)

        doc.refresh_from_db()
        self.assertEqual(doc.status, Status.PROCESSING)

    def test_a_stalled_website_import_shows_as_failed_and_can_be_run_again(self):
        self.make_doc(
            Status.PROCESSING,
            timedelta(minutes=20),
            original_filename=WEBSITE_DOCUMENT_NAME,
            source=KnowledgeBaseDocument.Source.WEBSITE,
            source_url='https://acme.test',
        )

        response = self.client.get(WEBSITE_URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'failed')
        self.assertEqual(response.data['error'], STALLED_ERROR)
        self.assertEqual(response.data['document']['status'], 'failed')

        with patch('chatbot.views.import_company_website_knowledge.delay') as queue_import:
            queue_import.return_value.id = 'task-2'
            response = self.client.post(WEBSITE_URL, {}, format='json')
        self.assertEqual(response.status_code, 202, response.data)
        queue_import.assert_called_once()

    def test_a_running_website_import_is_left_alone(self):
        self.make_doc(
            Status.PROCESSING,
            timedelta(minutes=2),
            original_filename=WEBSITE_DOCUMENT_NAME,
            source=KnowledgeBaseDocument.Source.WEBSITE,
        )

        response = self.client.get(WEBSITE_URL)

        self.assertEqual(response.data['status'], 'processing')

    def test_a_document_is_only_called_stalled_after_the_task_could_have_finished(self):
        self.assertGreater(KB_PROCESSING_STALLED_AFTER.total_seconds(), KB_TASK_TIME_LIMIT)


class TaskTimeLimitTests(StalledDocumentTestCase):
    def test_both_tasks_have_time_limits(self):
        for task in (extract_knowledge_base_document, import_company_website_knowledge):
            self.assertEqual(task.soft_time_limit, KB_TASK_SOFT_TIME_LIMIT, task.name)
            self.assertEqual(task.time_limit, KB_TASK_TIME_LIMIT, task.name)

    @patch('payment.services.consume_credits', return_value=True)
    def test_an_upload_that_runs_too_long_is_marked_failed(self, _consume):
        doc = self.make_doc(Status.PENDING, timedelta(0))

        with patch('chatbot.tasks.PineconeService') as pinecone:
            pinecone.return_value.upsert_document.side_effect = SoftTimeLimitExceeded()
            result = extract_knowledge_base_document.run(doc.pk)

        self.assertEqual(result, {'success': False, 'error': TIMED_OUT_ERROR})
        doc.refresh_from_db()
        self.assertEqual(doc.status, Status.FAILED)
        self.assertEqual(doc.error, TIMED_OUT_ERROR)

    def test_a_website_import_that_runs_too_long_is_marked_failed(self):
        with patch('chatbot.tasks.crawl_website', side_effect=SoftTimeLimitExceeded()):
            result = import_company_website_knowledge.run(self.company.pk)

        self.assertFalse(result['success'])
        self.assertEqual(result['error'], TIMED_OUT_ERROR)
        doc = KnowledgeBaseDocument.objects.get(source=KnowledgeBaseDocument.Source.WEBSITE)
        self.assertEqual(doc.status, Status.FAILED)
        self.assertEqual(doc.error, TIMED_OUT_ERROR)


@override_settings(PINECONE_API_KEY='test-key', PINECONE_INDEX_NAME='test-index')
class EmbeddingTimeoutTests(TestCase):
    def test_openai_calls_give_up_instead_of_waiting_ten_minutes(self):
        service = PineconeService()
        service._dimension = 3

        with patch('openai.OpenAI') as openai_client:
            openai_client.return_value.embeddings.create.return_value.data = []
            service._embed(['hello'])

        self.assertEqual(openai_client.call_args.kwargs['timeout'], OPENAI_TIMEOUT_SECONDS)
        self.assertEqual(openai_client.call_args.kwargs['max_retries'], 2)
