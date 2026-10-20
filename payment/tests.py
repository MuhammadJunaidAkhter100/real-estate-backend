import json
from unittest.mock import patch, MagicMock
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from payment import plan_limits, services
from payment.exceptions import PlanLimitReached
from payment.models import ProcessedStripeEvent, UsageCounter
from users.models import Company, User


class PaymentPlansTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.company = Company.objects.create(name="Test Co", plan="free")
        self.admin = User.objects.create_user(
            email="admin@test.com",
            first_name="Admin",
            last_name="User",
            role=User.Role.COMPANY_ADMIN,
            company=self.company,
        )

    def test_get_plans_listing(self):
        self.client.force_authenticate(user=self.admin)
        url = reverse('admin-plans-list')
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        plan_slugs = [p['slug'] for p in response.data]
        self.assertIn('free', plan_slugs)
        self.assertIn('pro', plan_slugs)
        self.assertIn('plus', plan_slugs)
        self.assertIn('top_tier', plan_slugs)

    def test_get_plans_listing_is_public_without_auth(self):
        url = reverse('admin-plans-list')
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(isinstance(response.data, list))


class PlanLimitsServiceTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            name="Alpha Real Estate",
            plan=Company.Plan.FREE,
        )

    def test_roster_limit_enforcement(self):
        # Free allows 1 manager and 5 agents
        service = services.PlanLimitsService.for_company(self.company)
        self.assertEqual(service.limits()['agents'], 5)
        self.assertEqual(service.limits()['team_managers'], 1)

        # Create 5 agents
        for i in range(5):
            User.objects.create_user(
                email=f"agent{i}@alpha.com",
                password="password123",
                first_name=f"Agent{i}",
                last_name="Test",
                company=self.company,
                role=User.Role.AGENT,
                status=User.Status.ACTIVE,
            )

        # 6th agent should fail
        with self.assertRaises(PlanLimitReached):
            service.assert_can_add('agents')

    def test_credit_consumption(self):
        self.company.plan = Company.Plan.FREE
        self.company.save()

        # Chat turn costs 1 credit
        result = services.consume_credits(self.company.pk, 'chat_turn')
        self.assertTrue(result)
        current = services.current_count(self.company.pk, UsageCounter.Kind.AI_CREDITS)
        self.assertEqual(current, 1)

        # Refund chat turn
        services.refund_credits(self.company.pk, 'chat_turn')
        current = services.current_count(self.company.pk, UsageCounter.Kind.AI_CREDITS)
        self.assertEqual(current, 0)


class WebhookTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.company = Company.objects.create(
            name="Beta Realty",
            plan=Company.Plan.FREE,
            stripe_customer_id="cus_test_123",
        )

    @patch('payment.stripe_client.plan_and_interval_for_price', return_value=('pro', 'yearly'))
    def test_checkout_session_completed_activates_company(self, _mock_plan):
        from payment.webhooks import handle_checkout_session_completed

        session = {
            'customer': 'cus_test_123',
            'subscription': 'sub_test_abc',
            'client_reference_id': str(self.company.pk),
            'line_items': {'data': [{'price': {'id': 'price_pro_yearly'}}]},
        }
        handle_checkout_session_completed(session)

        self.company.refresh_from_db()
        self.assertEqual(self.company.status, Company.Status.ACTIVE)
        self.assertEqual(self.company.plan, 'pro')
        self.assertEqual(self.company.billing_interval, 'yearly')
        self.assertEqual(self.company.stripe_subscription_id, 'sub_test_abc')

    def test_checkout_session_completed_with_metadata(self):
        from payment.webhooks import handle_checkout_session_completed

        session = {
            'customer': 'cus_test_123',
            'subscription': 'sub_test_xyz',
            'client_reference_id': str(self.company.pk),
            'metadata': {'plan': 'plus', 'interval': 'yearly'},
        }
        handle_checkout_session_completed(session)

        self.company.refresh_from_db()
        self.assertEqual(self.company.status, Company.Status.ACTIVE)
        self.assertEqual(self.company.plan, 'plus')
        self.assertEqual(self.company.billing_interval, 'yearly')
        self.assertEqual(self.company.stripe_subscription_id, 'sub_test_xyz')

    @patch('payment.stripe_client._client')
    def test_create_checkout_session_dynamic_price_data(self, mock_client):
        from payment import stripe_client

        mock_instance = MagicMock()
        mock_instance.checkout.sessions.create.return_value = MagicMock(url='https://checkout.stripe.com/test', id='cs_123')
        mock_client.return_value = mock_instance

        session = stripe_client.create_checkout_session(
            customer_id='cus_123',
            plan='pro',
            interval='monthly',
            client_reference_id=1,
            success_url='https://example.com/success',
            cancel_url='https://example.com/cancel',
        )
        self.assertEqual(session.url, 'https://checkout.stripe.com/test')
        call_args = mock_instance.checkout.sessions.create.call_args[0][0]
        self.assertEqual(call_args['line_items'][0]['price_data']['unit_amount'], 4900)
        self.assertEqual(call_args['line_items'][0]['price_data']['recurring']['interval'], 'month')
        self.assertEqual(call_args['metadata']['plan'], 'pro')


class RegisterCompanyTests(TestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.client = APIClient()

    @patch('whatsapp.tasks.send_company_admin_welcome_task.delay')
    @patch('users.tasks.scrape_company_website_task.delay')
    def test_register_company_flow(self, mock_scrape, mock_whatsapp):
        from django.core import mail
        from django.test import override_settings

        with override_settings(ADMIN_NOTIFICATION_EMAILS=['superadmin@nexusproperty.io']):
            with self.captureOnCommitCallbacks(execute=True):
                payload = {
                    "company_name": "inaam.ai",
                    "countries": ["Algeria", "Andorra"],
                    "first_name": "inaam",
                    "last_name": "Ullah",
                    "email": "gonotab953@goldlicai.com",
                    "password": "inaam@123",
                    "plan": "basic",
                    "interval": "monthly",
                    "website": "https://legacyinvestmentconsultancy.com/",
                    "phone": "+923116585935",
                }
                response = self.client.post('/api/auth/register-company/', payload, format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertIn('access', response.data)
        self.assertIn('refresh', response.data)
        self.assertIn('user', response.data)

        # Check DB records
        company = Company.objects.get(name="inaam.ai")
        self.assertEqual(company.phone, "+923116585935")
        self.assertEqual(company.website, "https://legacyinvestmentconsultancy.com/")
        self.assertEqual(company.operating_countries, ["Algeria", "Andorra"])

        user = User.objects.get(email="gonotab953@goldlicai.com")
        self.assertEqual(user.role, User.Role.COMPANY_ADMIN)
        self.assertEqual(user.company, company)

        # Check website scraping queued
        mock_scrape.assert_called_once_with(company.pk, "https://legacyinvestmentconsultancy.com/")

        # Check WhatsApp welcome queued
        mock_whatsapp.assert_called_once_with(company.pk, user.pk, None)

        # Check emails sent:
        # 1. Super admin email (to superadmin@nexusproperty.io)
        # 2. Company admin welcome email (to gonotab953@goldlicai.com)
        admin_emails = [m for m in mail.outbox if 'superadmin@nexusproperty.io' in m.to]
        self.assertEqual(len(admin_emails), 1)
        admin_mail = admin_emails[0]
        self.assertIn("New company signed up", admin_mail.subject)
        self.assertIn("Company: inaam.ai", admin_mail.body)
        self.assertTrue(any(mime == 'text/html' for _, mime in admin_mail.alternatives))

        welcome_emails = [m for m in mail.outbox if 'gonotab953@goldlicai.com' in m.to]
        self.assertEqual(len(welcome_emails), 1)
        welcome_mail = welcome_emails[0]
        self.assertIn("Welcome to", welcome_mail.subject)
        self.assertIn("Your Workspace is Ready", welcome_mail.subject)
        self.assertTrue(any(mime == 'text/html' for _, mime in welcome_mail.alternatives))


class WebsiteScrapeWordExportTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    @patch('users.tasks.scrape_company_website_task.delay')
    @patch('chatbot.website_knowledge.crawl_website')
    @patch('chatbot.website_knowledge.normalize_website_url', return_value='https://example.com')
    def test_scrape_website_endpoint_queues_background_flow_and_creates_kb_document(
        self, mock_normalize, mock_crawl_website, mock_scrape
    ):
        from chatbot.models import KnowledgeBaseDocument
        from rest_framework_simplejwt.tokens import AccessToken

        company = Company.objects.create(name='Example Realty', plan='free')
        admin = User.objects.create_user(
            email='scrape-admin@example.com',
            first_name='Scrape',
            last_name='Admin',
            role=User.Role.COMPANY_ADMIN,
            company=company,
        )
        mock_scrape.return_value.id = 'scrape-task-id'
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(admin)}')

        response = self.client.post('/api/auth/scrape-website-docx/', {'website': 'https://example.com'}, format='json')

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        company.refresh_from_db()
        self.assertEqual(company.website, 'https://example.com')
        self.assertEqual(company.website_scrape_status, 'processing')
        document = KnowledgeBaseDocument.objects.get(
            user__company=company,
            source=KnowledgeBaseDocument.Source.WEBSITE,
        )
        self.assertEqual(document.status, KnowledgeBaseDocument.Status.PENDING)
        self.assertEqual(document.original_filename, 'Website Data.docx')
        self.assertEqual(document.file_type, 'docx')
        mock_scrape.assert_called_once_with(company.pk, 'https://example.com')
        mock_crawl_website.assert_not_called()
        self.assertEqual(response.data['document_id'], document.pk)
        self.assertEqual(response.data['task_id'], 'scrape-task-id')

    @patch('users.tasks.scrape_company_website_task.delay')
    def test_scrape_website_endpoint_requires_authentication(self, mock_scrape):
        company = Company.objects.create(name='Private Realty', plan='free')
        response = self.client.post('/api/auth/scrape-website-docx/', {'website': 'https://example.com'}, format='json')

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        company.refresh_from_db()
        self.assertEqual(company.website, '')
        mock_scrape.assert_not_called()

