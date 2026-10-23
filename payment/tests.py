import json
from unittest.mock import patch, MagicMock
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from payment import plan_limits, services
from payment.exceptions import PlanLimitReached
from payment.models import CreditCost, ProcessedStripeEvent, ReferralInvitation, UsageCounter
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

    def test_billing_usage_returns_frontend_ready_remaining_balances(self):
        from payment.plan_limits import current_period_start

        UsageCounter.objects.create(
            company=self.company,
            kind=UsageCounter.Kind.AI_CREDITS,
            period_start=current_period_start(),
            used=25,
        )
        UsageCounter.objects.create(
            company=self.company,
            kind=UsageCounter.Kind.AI_PROPOSALS,
            period_start=current_period_start(),
            used=2,
        )
        self.client.force_authenticate(user=self.admin)

        response = self.client.get(reverse('billing-usage'))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['plan']['slug'], 'free')
        self.assertEqual(response.data['usage']['aiCredits']['used'], 25)
        self.assertEqual(response.data['usage']['aiCredits']['remaining'], 75)
        self.assertEqual(response.data['usage']['aiCredits']['percentUsed'], 25)
        self.assertEqual(response.data['usage']['aiProposals']['used'], 2)
        self.assertEqual(response.data['usage']['aiProposals']['limit'], 0)
        self.assertEqual(response.data['usage']['aiProposals']['percentUsed'], 100)
        self.assertEqual(response.data['creditCosts']['proposal']['creditsPerUnit'], 20)

    def test_zero_cap_with_no_usage_reports_zero_percent(self):
        self.client.force_authenticate(user=self.admin)

        response = self.client.get(reverse('billing-usage'))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['usage']['aiProposals']['used'], 0)
        self.assertEqual(response.data['usage']['aiProposals']['percentUsed'], 0)


class ChangePlanTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.company = Company.objects.create(
            name='Plan Change Co',
            plan='pro',
            billing_interval='monthly',
            stripe_customer_id='cus_plan_change',
            stripe_subscription_id='sub_plan_change',
        )
        self.admin = User.objects.create_user(
            email='plan-change@example.com',
            first_name='Plan',
            last_name='Admin',
            role=User.Role.COMPANY_ADMIN,
            status=User.Status.ACTIVE,
            company=self.company,
        )
        self.client.force_authenticate(user=self.admin)

    @patch('payment.stripe_client.update_subscription_plan')
    @patch('payment.stripe_client.price_id_for_plan_change', return_value='price_plus_yearly')
    @patch('payment.stripe_client.retrieve_subscription')
    def test_paid_plan_change_updates_stripe_subscription(
        self, mock_retrieve, _mock_price, mock_update
    ):
        mock_retrieve.return_value = {
            'id': 'sub_plan_change',
            'customer': 'cus_plan_change',
            'items': {'data': [{'id': 'si_plan_change', 'price': {'id': 'price_pro_monthly'}}]},
        }
        mock_update.return_value = {
            'id': 'sub_plan_change',
            'customer': 'cus_plan_change',
            'status': 'active',
            'items': {'data': [{'id': 'si_plan_change', 'price': {'id': 'price_plus_yearly'}}]},
            'metadata': {'plan': 'plus', 'interval': 'yearly'},
        }

        response = self.client.post(
            '/api/payment/change-plan/',
            {'plan': 'plus', 'interval': 'yearly'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data['plan'], 'plus')
        self.assertEqual(response.data['interval'], 'yearly')
        mock_update.assert_called_once_with(
            'sub_plan_change',
            'si_plan_change',
            'price_plus_yearly',
            plan='plus',
            interval='yearly',
            company_id=self.company.pk,
        )
        self.company.refresh_from_db()
        self.assertEqual(self.company.plan, 'plus')
        self.assertEqual(self.company.billing_interval, 'yearly')

    @patch('payment.stripe_client.cancel_subscription')
    def test_free_downgrade_schedules_period_end_cancellation(self, mock_cancel):
        response = self.client.post(
            '/api/payment/change-plan/',
            {'plan': 'free'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['effective'], 'current_period_end')
        mock_cancel.assert_called_once_with('sub_plan_change')
        self.company.refresh_from_db()
        self.assertEqual(self.company.plan, 'pro')

    @patch('payment.stripe_client.create_checkout_session')
    @patch('payment.stripe_client.create_customer')
    def test_paid_plan_change_without_subscription_returns_checkout_url(
        self, mock_create_customer, mock_create_checkout
    ):
        self.company.stripe_subscription_id = ''
        self.company.stripe_customer_id = ''
        self.company.plan = 'free'
        self.company.save(update_fields=['stripe_subscription_id', 'stripe_customer_id', 'plan'])
        mock_create_customer.return_value.id = 'cus_new_checkout'
        mock_create_checkout.return_value = {'url': 'https://checkout.stripe.com/test'}

        response = self.client.post(
            '/api/payment/change-plan/',
            {'plan': 'pro', 'interval': 'monthly'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['checkoutRequired'])
        self.assertEqual(response.data['checkoutUrl'], 'https://checkout.stripe.com/test')
        mock_create_checkout.assert_called_once()


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
        self.assertEqual(current, 5)

        # Refund chat turn
        services.refund_credits(self.company.pk, 'chat_turn')
        current = services.current_count(self.company.pk, UsageCounter.Kind.AI_CREDITS)
        self.assertEqual(current, 0)

    def test_database_credit_cost_updates_apply_without_code_changes(self):
        rate = CreditCost.objects.get(operation='chat_turn')
        rate.credits_per_unit = 9
        rate.save(update_fields=['credits_per_unit', 'updated_at'])

        services.consume_credits(self.company.pk, 'chat_turn')

        self.assertEqual(
            services.current_count(self.company.pk, UsageCounter.Kind.AI_CREDITS),
            9,
        )

    def test_proposal_and_call_minute_default_rates(self):
        self.assertEqual(services.credit_cost_for('ai_proposal'), 20)
        self.assertEqual(services.credit_cost_for('calling_agent_call'), 20)

        services.consume_credits(self.company.pk, 'ai_proposal')
        services.consume_credits(
            self.company.pk,
            'calling_agent_call',
            quantity=2,
        )

        self.assertEqual(
            services.current_count(self.company.pk, UsageCounter.Kind.AI_CREDITS),
            60,
        )


class DevelopmentPlanBypassTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.company = Company.objects.create(name='Development Co', plan='free')
        self.company_admin = User.objects.create_user(
            email='owner@development.example',
            first_name='Company',
            last_name='Owner',
            company=self.company,
            role=User.Role.COMPANY_ADMIN,
        )
        self.superadmin = User.objects.create_user(
            email='superadmin@development.example',
            first_name='Super',
            last_name='Admin',
            company=self.company,
            role=User.Role.SUPERADMIN,
            is_superuser=True,
        )
        self.url = reverse('development-plan-bypass')

    @override_settings(DEBUG=True)
    @patch('payment.views.enable_development_plan_bypass', return_value=True)
    def test_superadmin_can_enable_indefinite_bypass_for_company_email(self, enable_bypass):
        self.client.force_authenticate(user=self.superadmin)

        response = self.client.post(
            self.url,
            {'company_email': self.company_admin.email},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['enabled'])
        enable_bypass.assert_called_once_with(self.company.pk)

    @override_settings(DEBUG=True)
    def test_company_admin_cannot_enable_bypass(self):
        self.client.force_authenticate(user=self.company_admin)

        response = self.client.post(
            self.url,
            {'company_email': self.company_admin.email},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_settings(DEBUG=False)
    def test_bypass_endpoint_is_hidden_outside_debug(self):
        self.client.force_authenticate(user=self.superadmin)

        response = self.client.post(
            self.url,
            {'company_email': self.company_admin.email},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @patch('payment.plan_limits.development_plan_bypass_is_active', return_value=True)
    def test_active_bypass_removes_plan_caps_and_enables_features(self, _active):
        limits = services.limits_for('free', company_id=self.company.pk)

        self.assertIsNone(limits['ai_proposals'])
        self.assertIsNone(limits['agents'])
        self.assertTrue(limits['features']['ai_proposal_generation'])


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

    @patch('users.tasks.notify_admins')
    def test_referral_link_signup_awards_100_credits_once(self, _mock_notify):
        inviter_company = Company.objects.create(name='Referring Co', plan='free')
        inviter = User.objects.create_user(
            email='referrer@example.com',
            first_name='Referring',
            last_name='Admin',
            role=User.Role.COMPANY_ADMIN,
            status=User.Status.ACTIVE,
            company=inviter_company,
        )
        self.client.force_authenticate(user=inviter)
        invitation_response = self.client.post('/api/payment/referrals/generate-url/', {}, format='json')

        self.assertEqual(invitation_response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(list(invitation_response.data), ['referral_url'])
        listing_response = self.client.get('/api/payment/referrals/')
        self.assertEqual(listing_response.data['summary']['people_invited'], 1)
        self.assertEqual(listing_response.data['summary']['pending'], 1)
        self.assertEqual(listing_response.data['summary']['credits_earned'], 0)
        self.assertEqual(len(listing_response.data['invitations']), 1)
        referral_code = listing_response.data['invitations'][0]['referral_code']
        self.assertIn(referral_code, invitation_response.data['referral_url'])

        self.client.force_authenticate(user=None)
        signup_payload = {
            'company_name': 'Referred Co',
            'countries': ['Algeria'],
            'first_name': 'New',
            'last_name': 'Admin',
            'email': 'new-admin@example.com',
            'password': 'StrongPass!234',
            'plan': 'free',
            'referral_code': referral_code,
        }
        response = self.client.post('/api/auth/register-company/', signup_payload, format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        invitation = ReferralInvitation.objects.get(referral_code=referral_code)
        self.assertEqual(invitation.status, ReferralInvitation.Status.JOINED)
        self.assertEqual(invitation.reward_credits, 100)
        self.assertEqual(
            services.limits_for('free', company_id=inviter_company.pk)['ai_credits'],
            200,
        )
        self.assertEqual(
            services.usage_snapshot(inviter_company.pk)['ai_credits']['remaining'],
            200,
        )
        joined_company = Company.objects.get(name='Referred Co')
        self.assertEqual(
            services.limits_for('free', company_id=joined_company.pk)['ai_credits'],
            200,
        )

        repeated_signup = self.client.post('/api/auth/register-company/', {
            **signup_payload,
            'company_name': 'Second Referred Co',
            'email': 'second-admin@example.com',
        }, format='json')
        self.assertEqual(repeated_signup.status_code, status.HTTP_400_BAD_REQUEST)

    @patch('users.tasks.send_referral_invitation_email.delay')
    def test_email_referral_invitation_sends_signup_link(self, mock_send_invitation):
        company = Company.objects.create(name='Email Referring Co', plan='free')
        admin = User.objects.create_user(
            email='email-referrer@example.com',
            first_name='Email',
            last_name='Admin',
            role=User.Role.COMPANY_ADMIN,
            status=User.Status.ACTIVE,
            company=company,
        )
        self.client.force_authenticate(user=admin)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                '/api/payment/referrals/',
                {'invited_email': 'invitee@example.com'},
                format='json',
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['source'], 'email')
        mock_send_invitation.assert_called_once_with(
            'invitee@example.com',
            'Email Admin',
            'Email Referring Co',
            response.data['referral_url'],
        )

        from django.core import mail
        from django.test import override_settings
        from users.tasks import send_referral_invitation_email

        with override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend'):
            mail.outbox.clear()
            send_referral_invitation_email.run(
                'invitee@example.com',
                'Email Admin',
                'Email Referring Co',
                response.data['referral_url'],
            )

        sent_email = mail.outbox[-1]
        self.assertIn('Email Admin', sent_email.body)
        self.assertIn('both your company', sent_email.body.lower())
        self.assertIn(response.data['referral_code'], sent_email.body)
        self.assertIn('100 credits each', sent_email.alternatives[0][0])

    def test_referral_list_create_delete_and_generate_url_endpoints(self):
        company = Company.objects.create(name='CRUD Referring Co', plan='free')
        admin = User.objects.create_user(
            email='crud-referrer@example.com',
            first_name='CRUD',
            last_name='Admin',
            role=User.Role.COMPANY_ADMIN,
            status=User.Status.ACTIVE,
            company=company,
        )
        self.client.force_authenticate(user=admin)

        created = self.client.post(
            '/api/payment/referrals/generate-url/', {}, format='json'
        )
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)
        invitation_id = self.client.get('/api/payment/referrals/').data['invitations'][0]['id']
        detail_url = f'/api/payment/referrals/{invitation_id}/'

        listing = self.client.get('/api/payment/referrals/')
        self.assertEqual(listing.status_code, status.HTTP_200_OK)
        self.assertEqual(listing.data['invitations'][0]['id'], invitation_id)
        self.assertEqual(self.client.get(detail_url).status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
        self.assertEqual(
            self.client.patch(detail_url, {'invited_email': 'updated-invitee@example.com'}).status_code,
            status.HTTP_405_METHOD_NOT_ALLOWED,
        )
        self.assertEqual(
            self.client.put(detail_url, {'invited_email': 'updated-invitee@example.com'}).status_code,
            status.HTTP_405_METHOD_NOT_ALLOWED,
        )

        email_invitation = self.client.post(
            '/api/payment/referrals/',
            {'invited_email': 'invitee@example.com'},
            format='json',
        )
        self.assertEqual(email_invitation.status_code, status.HTTP_201_CREATED)
        self.assertEqual(email_invitation.data['source'], 'email')

        deleted = self.client.delete(detail_url)
        self.assertEqual(deleted.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(ReferralInvitation.objects.filter(pk=invitation_id).exists())


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

