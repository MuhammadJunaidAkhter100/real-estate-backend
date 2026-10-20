from unittest.mock import patch
from django.test import TestCase, override_settings
from django.core import mail
from rest_framework.test import APIClient
from rest_framework import status

from users.models import SupportTicket
from users.tasks import send_support_ticket_emails


@override_settings(
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    DEFAULT_FROM_EMAIL='Nexusproperty <hello@nexusproperty.io>',
    ADMIN_NOTIFICATION_EMAILS=['admin@example.com'],
    EMAIL_BRAND_NAME='Nexusproperty',
)
class SupportContactApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.payload = {
            "name": "inaam Ullah",
            "email": "customer@example.com",
            "subject": "Billing issue",
            "message": "Hello, I have a question regarding my subscription invoice.",
        }

    def test_post_support_enquiry_success(self):
        response = self.client.post('/api/users/support/', self.payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data.get('success'))
        self.assertIn('ticket_id', response.data)

        # Check ticket saved in DB
        ticket = SupportTicket.objects.get(id=response.data['ticket_id'])
        self.assertEqual(ticket.name, "inaam Ullah")
        self.assertEqual(ticket.email, "customer@example.com")
        self.assertEqual(ticket.subject, "Billing issue")
        self.assertEqual(ticket.message, "Hello, I have a question regarding my subscription invoice.")
        self.assertEqual(ticket.status, SupportTicket.Status.OPEN)

    def test_root_support_url_route(self):
        response = self.client.post('/api/support/', self.payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_validation_errors(self):
        # Missing email
        bad_payload = dict(self.payload)
        del bad_payload['email']
        res = self.client.post('/api/users/support/', bad_payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('email', res.data)

        # Invalid email format
        bad_payload = dict(self.payload, email="not-an-email")
        res = self.client.post('/api/users/support/', bad_payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('email', res.data)

        # Empty message
        bad_payload = dict(self.payload, message="   ")
        res = self.client.post('/api/users/support/', bad_payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('message', res.data)

    def test_send_support_ticket_emails_task(self):
        # Clear outbox
        mail.outbox = []

        send_support_ticket_emails(
            name="inaam Ullah",
            user_email="customer@example.com",
            subject="Technical inquiry",
            message="Need help setting up my domain integration.",
            ticket_id=42,
        )

        # Should send 2 emails: 1 to admin (itsinaam89@gmail.com), 1 to customer
        self.assertEqual(len(mail.outbox), 2)

        admin_email = mail.outbox[0]
        user_email = mail.outbox[1]

        # Verify admin email
        self.assertIn("itsinaam89@gmail.com", admin_email.to)
        self.assertIn("Technical inquiry", admin_email.subject)
        self.assertIn("inaam Ullah", admin_email.body)
        self.assertIn("Need help setting up my domain integration.", admin_email.body)

        # Verify user confirmation email
        self.assertEqual(user_email.to, ["customer@example.com"])
        self.assertIn("Technical inquiry", user_email.subject)
        self.assertIn("inaam Ullah", user_email.body)
        self.assertIn("received your inquiry", user_email.body)
