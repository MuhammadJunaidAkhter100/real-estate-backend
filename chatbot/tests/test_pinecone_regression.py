from __future__ import annotations

import json
from unittest.mock import Mock, patch

from django.test import TestCase

from chatbot.tools import search_knowledge_base
from users.models import User


class ChatbotPineconeRegressionTests(TestCase):
    @patch('chatbot.pinecone_service.PineconeService')
    def test_existing_chatbot_search_invocation_is_unchanged(
        self,
        pinecone_class: Mock,
    ) -> None:
        # The superadmin searches every document, so no metadata filter.
        superadmin = User.objects.create_superuser(
            email='root@techfy.test', first_name='Root', last_name='User', password='password123')
        pinecone_class.return_value.search.return_value = [
            {
                'text': 'Existing chatbot passage.',
                'score': 0.9,
                'metadata': {'original_filename': 'chatbot.pdf'},
            }
        ]

        result = json.loads(
            search_knowledge_base.func(
                'What does the document say?',
                config={'configurable': {'user_id': superadmin.pk}},
            )
        )

        pinecone_class.return_value.search.assert_called_once_with(
            'What does the document say?',
            top_k=5,
            metadata_filter=None,
        )
        self.assertTrue(result['answerable'])
        self.assertEqual(result['results'][0]['source'], 'chatbot.pdf')
