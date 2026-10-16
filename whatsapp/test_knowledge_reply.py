from types import SimpleNamespace
from unittest.mock import ANY, patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from users.models import Company
from whatsapp.models import WhatsAppAccount, WhatsAppChat
from whatsapp.services import WhatsAppService


class WhatsAppKnowledgeReplyTests(TestCase):
    def setUp(self):
        User = get_user_model()
        company = Company.objects.create(
            name='Test Company',
            operating_countries=['US'],
        )
        self.user = User.objects.create_user(
            email='wa-user@example.com',
            password='secret123',
            first_name='Wa',
            last_name='User',
            company=company,
        )
        self.account = WhatsAppAccount.objects.create(
            user=self.user,
            session_name='test-session',
            name='Test Account',
        )
        self.chat = WhatsAppChat.objects.create(
            account=self.account,
            chat_id='1234567890',
            name='Customer',
        )

    @patch('chatbot.pinecone_service.PineconeService')
    @patch('langchain_openai.ChatOpenAI')
    @patch.object(WhatsAppService, 'send_text_message')
    def test_auto_reply_uses_llm_to_format_kb_results(self, send_text_message, llm_class, pinecone_class):
        pinecone_class.return_value.search.return_value = [
            {'text': 'Dubai villas are a popular property option with strong rental demand.', 'score': 0.82},
            {'text': 'Villas are available in several Dubai communities.', 'score': 0.61},
        ]
        llm_class.return_value.invoke.side_effect = [
            SimpleNamespace(content='YES'),
            SimpleNamespace(content='We have villas in several Dubai communities. What area do you prefer?'),
        ]

        service = WhatsAppService()
        result = service._maybe_auto_reply_from_kb(
            self.account,
            self.chat,
            'Can you tell me what options are available?',
        )

        self.assertEqual(result, send_text_message.return_value)
        pinecone_class.return_value.search.assert_called_once_with(
            'Can you tell me what options are available?',
            top_k=5,
        )
        send_text_message.assert_called_once_with(
            user=self.user,
            payload={
                'chat_id': self.chat.chat_id,
                'text': 'We have villas in several Dubai communities. What area do you prefer?',
            },
        )

    @patch('chatbot.pinecone_service.PineconeService')
    @patch('langchain_openai.ChatOpenAI')
    @patch.object(WhatsAppService, 'send_text_message')
    def test_auto_reply_skips_irrelevant_query(self, send_text_message, llm_class, pinecone_class):
        llm_class.return_value.invoke.return_value = SimpleNamespace(content='NO')

        service = WhatsAppService()
        result = service._maybe_auto_reply_from_kb(
            self.account,
            self.chat,
            'Hello, how are you?',
        )

        self.assertIsNone(result)
        pinecone_class.return_value.search.assert_not_called()
        send_text_message.assert_not_called()

    @patch('whatsapp.tasks.auto_reply_from_kb_task.delay')
    @patch.object(WhatsAppService, '_broadcast_message')
    def test_incoming_text_queues_one_background_reply(self, broadcast_message, auto_reply_task):
        service = WhatsAppService()
        webhook_payload = {
            'event': 'message',
            'session': self.account.session_name,
            'payload': {
                'id': 'incoming-message-1',
                'fromMe': False,
                'from': '15551234567@c.us',
                'body': 'Can you tell me what options are available?',
            },
        }

        service.persist_incoming_webhook(webhook_payload)
        service.persist_incoming_webhook(webhook_payload)

        auto_reply_task.assert_called_once_with(
            self.account.pk,
            ANY,
            'Can you tell me what options are available?',
        )
