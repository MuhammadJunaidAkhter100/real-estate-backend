from __future__ import annotations

import tempfile
from unittest.mock import MagicMock, patch
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from chatbot.models import PublicKnowledgeBaseDocument
from chatbot.pinecone_service import PineconeService
from chatbot.public_chat import get_public_knowledge_context
from chatbot.public_prompt import get_public_system_prompt
from chatbot.tasks import extract_public_knowledge_base_document
from users.models import Company, User

MEDIA_ROOT = tempfile.mkdtemp(prefix='public-kb-tests-')


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class PublicKnowledgeBaseTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.company = Company.objects.create(name='Test Agency')
        self.admin = User.objects.create_superuser(
            email='admin@nexusproperty.io',
            first_name='Super',
            last_name='Admin',
            password='secretpassword123',
            role='superadmin',
            company=self.company,
        )
        self.regular_user = User.objects.create_user(
            email='agent@nexusproperty.io',
            first_name='Regular',
            last_name='Agent',
            password='secretpassword123',
            role='agent',
            company=self.company,
        )

    def test_non_admin_cannot_access_public_kb(self):
        self.client.force_authenticate(user=self.regular_user)
        resp = self.client.get('/api/chatbot/admin/public-kb/')
        self.assertEqual(resp.status_code, 403)

        sample_file = SimpleUploadedFile("faq.txt", b"Public FAQs content", content_type="text/plain")
        resp = self.client.post('/api/chatbot/admin/public-kb/', {'files': [sample_file]}, format='multipart')
        self.assertEqual(resp.status_code, 403)

    @patch('chatbot.tasks.extract_public_knowledge_base_document.delay')
    def test_admin_can_upload_and_list_public_documents(self, mock_delay):
        mock_delay.return_value = MagicMock(id='task-test-123')
        self.client.force_authenticate(user=self.admin)

        sample_file = SimpleUploadedFile("public_brochure.txt", b"Nexusproperty GCC features and facts", content_type="text/plain")
        resp = self.client.post('/api/chatbot/admin/public-kb/', {'files': [sample_file]}, format='multipart')

        self.assertEqual(resp.status_code, 202)
        data = resp.json()
        self.assertEqual(len(data['documents']), 1)
        doc_id = data['documents'][0]['id']
        self.assertEqual(data['documents'][0]['original_filename'], 'public_brochure.txt')
        self.assertEqual(data['documents'][0]['task_id'], 'task-test-123')

        # Verify in DB
        doc = PublicKnowledgeBaseDocument.objects.get(pk=doc_id)
        self.assertEqual(doc.uploaded_by, self.admin)
        self.assertEqual(doc.status, PublicKnowledgeBaseDocument.Status.PENDING)

        # List documents
        list_resp = self.client.get('/api/chatbot/admin/public-kb/')
        self.assertEqual(list_resp.status_code, 200)
        docs = list_resp.json()
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]['id'], doc_id)

        # Status check
        status_resp = self.client.get(f'/api/chatbot/admin/public-kb/{doc_id}/status/')
        self.assertEqual(status_resp.status_code, 200)
        self.assertEqual(status_resp.json()['id'], doc_id)

    @patch('chatbot.pinecone_service.PineconeService.delete_public_document')
    def test_admin_can_delete_public_document(self, mock_delete_vec):
        self.client.force_authenticate(user=self.admin)
        doc = PublicKnowledgeBaseDocument.objects.create(
            uploaded_by=self.admin,
            original_filename="old_terms.txt",
            file_type="txt",
            status=PublicKnowledgeBaseDocument.Status.COMPLETED,
            chunk_count=3,
        )

        resp = self.client.delete(f'/api/chatbot/admin/public-kb/{doc.pk}/')
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(PublicKnowledgeBaseDocument.objects.filter(pk=doc.pk).exists())
        mock_delete_vec.assert_called_once_with(doc.pk, chunk_count=3)

    @patch('chatbot.pinecone_service.PineconeService.upsert_public_document')
    def test_celery_task_extracts_and_upserts_public_doc(self, mock_upsert):
        mock_upsert.return_value = 5

        test_file = SimpleUploadedFile("pricing_guide.txt", b"All about pricing and plans", content_type="text/plain")
        doc = PublicKnowledgeBaseDocument.objects.create(
            uploaded_by=self.admin,
            file=test_file,
            original_filename="pricing_guide.txt",
        )

        result = extract_public_knowledge_base_document(doc.pk)
        self.assertTrue(result['success'])
        self.assertEqual(result['chunks_stored'], 5)

        doc.refresh_from_db()
        self.assertEqual(doc.status, PublicKnowledgeBaseDocument.Status.COMPLETED)
        self.assertEqual(doc.chunk_count, 5)
        self.assertEqual(doc.file_type, 'txt')
        mock_upsert.assert_called_once()

    @patch('chatbot.pinecone_service.PineconeService.search_public')
    def test_public_chat_knowledge_context_and_prompt(self, mock_search):
        mock_search.return_value = [
            {"text": "Nexusproperty Enterprise plan includes bespoke integrations.", "score": 0.88},
            {"text": "We offer 24/7 phone support for Enterprise.", "score": 0.75},
        ]

        context = get_public_knowledge_context("enterprise plan")
        self.assertIn("Enterprise plan includes bespoke integrations", context)
        self.assertIn("24/7 phone support", context)

        prompt = get_public_system_prompt(context)
        self.assertIn("Public Knowledge Base Documents", prompt)
        self.assertIn("Enterprise plan includes bespoke integrations", prompt)
        self.assertNotIn("Website facts", prompt)
