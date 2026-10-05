"""Smoke checks for signup, guard, status and webhook wiring."""

import csv
import json
from datetime import timedelta
from unittest.mock import patch

import stripe
from django.conf import settings
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from billing.models import CreditCharge, ProcessedStripeEvent, UsageCounter
from users.models import Company, User

BASIC_SIGNUP = {
    'company_name': 'Sunrise Realty',
    'countries': ['UAE', 'UK'],
    'first_name': 'Sara',
    'last_name': 'Khan',
    'email': 'sara@sunrise.test',
    'password': 'Str0ngPass!23',
    'plan': 'basic',
    'interval': 'monthly',
}


class _RecordingHTTPClient(stripe._http_client.HTTPClient):
    """Captures outbound Stripe calls instead of performing them.

    The rest of the billing suite patches ``stripe_client`` wholesale, which
    means a call shaped wrongly for the installed stripe-python SDK would pass
    every test and only explode against the live API. Driving the real SDK with
    a stubbed transport keeps the parameter binding real while staying offline.
    """

    name = 'recording'
    cert = None
    verify = None

    def __init__(self):
        super().__init__()
        self.calls = []

    def request_with_retries(self, method, url, headers, post_data=None,
                             max_network_retries=None, *, _usage=None):
        self.calls.append({'method': method, 'url': url, 'post_data': post_data})
        return json.dumps({
            'id': 'obj_1',
            'object': 'list',
            'data': [],
            'has_more': False,
            'url': url,
        }), 200, {}


class StripeClientSdkShapeTests(SimpleTestCase):
    """Every stripe_client call must bind against the installed SDK."""

    def setUp(self):
        from billing import stripe_client

        self.module = stripe_client
        self.http = _RecordingHTTPClient()
        real_client_cls = stripe.StripeClient

        def _patched_client(secret_key, **kwargs):
            return real_client_cls(secret_key, http_client=self.http)

        patcher = patch.object(stripe, 'StripeClient', _patched_client)
        patcher.start()
        self.addCleanup(patcher.stop)

        # Pagination builds its own global requestor, which does not inherit the
        # client's transport. Redirect that one at the recorder too, otherwise
        # the list helpers would try to reach api.stripe.com for real.
        self.http.name = 'recording'
        global_http = patch.object(
            stripe._api_requestor._APIRequestor,
            '_get_http_client',
            lambda self_: self.http,
        )
        global_http.start()
        self.addCleanup(global_http.stop)

        # Pagination builds its own requestor, which reads the module-level key.
        key_patcher = patch.object(stripe, 'api_key', 'sk_test_offline_probe')
        key_patcher.start()
        self.addCleanup(key_patcher.stop)

    def test_create_customer_binds_and_serialises(self):
        self.module.create_customer(
            email='ops@example.com',
            name='Sunrise Realty',
            metadata={'company_id': '42'},
        )
        call = self.http.calls[-1]
        self.assertEqual(call['method'], 'post')
        self.assertIn('email=ops%40example.com', call['post_data'])
        self.assertIn('name=Sunrise+Realty', call['post_data'])
        self.assertIn('metadata[company_id]=42', call['post_data'])

    def test_create_checkout_session_binds_and_serialises(self):
        self.module.create_checkout_session(
            customer_id='cus_1',
            price_id='price_1',
            client_reference_id=7,
            success_url='https://app.test/billing/success',
            cancel_url='https://app.test/billing/cancel',
            metadata={'company_id': '7'},
        )
        post = self.http.calls[-1]['post_data']
        self.assertIn('mode=subscription', post)
        self.assertIn('customer=cus_1', post)
        self.assertIn('price_1', post)
        self.assertIn('client_reference_id=7', post)
        self.assertIn('metadata[company_id]=7', post)

    def test_create_portal_session_binds_and_serialises(self):
        self.module.create_portal_session('cus_1', 'https://app.test/subscription')
        post = self.http.calls[-1]['post_data']
        self.assertIn('customer=cus_1', post)
        self.assertIn('return_url=', post)

    def test_list_helpers_bind_and_serialise(self):
        self.module.list_checkout_sessions('cus_1')
        call = self.http.calls[-1]
        self.assertEqual(call['method'], 'get')
        # Query params travel in the URL for GET, not in a request body.
        self.assertIn('customer=cus_1', call['url'])
        self.assertIn('limit=100', call['url'])

        self.module.list_subscriptions('cus_1')
        call = self.http.calls[-1]
        self.assertEqual(call['method'], 'get')
        self.assertIn('customer=cus_1', call['url'])
        self.assertIn('status=all', call['url'])

    def test_retrieve_and_delete_take_positional_ids(self):
        self.module.retrieve_checkout_session('cs_1')
        self.assertEqual(self.http.calls[-1]['method'], 'get')
        self.module.retrieve_subscription('sub_1')
        self.assertEqual(self.http.calls[-1]['method'], 'get')
        self.module.delete_customer('cus_1')
        self.assertEqual(self.http.calls[-1]['method'], 'delete')


class RegisterCompanyTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.url = '/api/auth/register-company/'
        # Signup is IP-throttled; clear the cache so tests do not inherit a
        # previous test's request count.
        from django.core.cache import cache

        cache.clear()

    def test_basic_signup_activates_and_returns_tokens(self):
        response = self.client.post(self.url, BASIC_SIGNUP, format='json')
        self.assertEqual(response.status_code, 201, response.data)

        company = Company.objects.get(name='Sunrise Realty')
        self.assertEqual(company.plan, Company.Plan.BASIC)
        self.assertEqual(company.status, Company.Status.ACTIVE)

        admin = User.objects.get(email='sara@sunrise.test')
        self.assertEqual(admin.role, User.Role.COMPANY_ADMIN)
        self.assertTrue(admin.check_password('Str0ngPass!23'))
        self.assertIn('access', response.data)
        self.assertIn('refresh', response.data)
        self.assertNotIn('checkoutUrl', response.data)

    def test_professional_signup_is_pending_and_returns_checkout_url(self):
        customer_id = 'cus_test_123'
        with patch('billing.views.stripe_client.create_customer') as create_customer, \
             patch('billing.views.stripe_client.price_id_for', return_value='price_x'), \
             patch('billing.views.stripe_client.create_checkout_session') as create_session:
            create_customer.return_value = type('C', (), {'id': customer_id})()
            create_session.return_value = type('S', (), {'url': 'https://stripe.test/checkout'})()

            payload = dict(BASIC_SIGNUP, plan='professional', email='pro@sunrise.test')
            response = self.client.post(self.url, payload, format='json')

        self.assertEqual(response.status_code, 201, response.data)
        company = Company.objects.get(name='Sunrise Realty')
        self.assertEqual(company.status, Company.Status.PENDING_PAYMENT)
        self.assertEqual(company.plan, Company.Plan.PROFESSIONAL)
        self.assertEqual(company.stripe_customer_id, customer_id)
        self.assertEqual(response.data['checkoutUrl'], 'https://stripe.test/checkout')

    def test_annual_professional_signup_uses_annual_stripe_price(self):
        customer_id = 'cus_annual_test'
        with patch('billing.views.stripe_client.create_customer') as create_customer, \
             patch('billing.views.stripe_client.price_id_for', return_value='price_annual') as price_id_for, \
             patch('billing.views.stripe_client.create_checkout_session') as create_session:
            create_customer.return_value = type('C', (), {'id': customer_id})()
            create_session.return_value = type('S', (), {'url': 'https://stripe.test/annual'})()

            payload = dict(
                BASIC_SIGNUP,
                plan='professional',
                interval='annual',
                email='annual@sunrise.test',
            )
            response = self.client.post(self.url, payload, format='json')

        self.assertEqual(response.status_code, 201, response.data)
        price_id_for.assert_called_once_with(Company.BillingInterval.ANNUAL)
        self.assertEqual(create_session.call_args.kwargs['price_id'], 'price_annual')

    def test_tampered_body_is_rejected(self):
        for extra in (
            {'status': 'active'},
            {'role': 'superadmin'},
            {'companyId': 999},
            {'priceId': 'price_attacker'},
            {'stripe_customer_id': 'cus_hax'},
        ):
            payload = dict(BASIC_SIGNUP, **extra)
            response = self.client.post(self.url, payload, format='json')
            self.assertEqual(response.status_code, 400, f'{extra} was accepted: {response.data}')
        self.assertFalse(Company.objects.exists())
        self.assertFalse(User.objects.exists())

    def test_duplicate_email_and_name_conflict(self):
        self.client.post(self.url, BASIC_SIGNUP, format='json')

        dup_email = self.client.post(
            self.url, dict(BASIC_SIGNUP, company_name='Other Co'), format='json')
        self.assertEqual(dup_email.status_code, 409, dup_email.data)

        dup_name = self.client.post(
            self.url, dict(BASIC_SIGNUP, email='other@sunrise.test'), format='json')
        self.assertEqual(dup_name.status_code, 409, dup_name.data)


class SubscriptionGuardTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def _company_user(self, **company_kwargs):
        company = Company.objects.create(name='Guard Co', **company_kwargs)
        user = User.objects.create_user(
            email='admin@guard.test', first_name='A', last_name='B',
            password='Str0ngPass!23', company=company,
            role=User.Role.COMPANY_ADMIN, status=User.Status.ACTIVE,
        )
        return company, user

    def _auth(self, user):
        self.client.force_authenticate(user=user)

    def test_pending_payment_blocks_protected_routes_with_402(self):
        _, user = self._company_user(
            plan=Company.Plan.PROFESSIONAL, status=Company.Status.PENDING_PAYMENT)
        self._auth(user)
        response = self.client.get('/api/users/manage_users/')
        self.assertEqual(response.status_code, 402, response.content)
        body = response.json()
        self.assertEqual(body['code'], 'PAYMENT_REQUIRED')
        self.assertFalse(body['allowed'])
        self.assertEqual(body['status'], Company.Status.PENDING_PAYMENT)

    def test_suspended_blocks(self):
        _, user = self._company_user(
            plan=Company.Plan.PROFESSIONAL, status=Company.Status.SUSPENDED)
        self._auth(user)
        self.assertEqual(self.client.get('/api/users/manage_users/').status_code, 402)

    def test_past_due_allowed_within_grace_then_blocked(self):
        from datetime import timedelta

        from django.utils import timezone

        from billing import services

        _, user = self._company_user(plan=Company.Plan.BASIC)
        self._auth(user)
        company = user.company

        company.status = Company.Status.PAST_DUE
        company.grace_until = timezone.now() + timedelta(days=1)
        company.save()
        services.invalidate(company.pk)
        self.assertEqual(self.client.get('/api/users/manage_users/').status_code, 200)

        company.refresh_from_db()
        company.grace_until = timezone.now() - timedelta(days=1)
        company.save()
        services.invalidate(company.pk)
        self.assertEqual(self.client.get('/api/users/manage_users/').status_code, 402)

    def test_active_and_manual_are_allowed(self):
        _, active_user = self._company_user(plan=Company.Plan.BASIC, status=Company.Status.ACTIVE)
        self._auth(active_user)
        self.assertEqual(self.client.get('/api/users/manage_users/').status_code, 200)

        self.client.force_authenticate(user=None)
        self.client.logout()
        manual_company = Company.objects.create(name='Manual Co')
        manual_user = User.objects.create_user(
            email='admin@manual.test', first_name='M', last_name='A',
            password='Str0ngPass!23', company=manual_company,
            role=User.Role.COMPANY_ADMIN, status=User.Status.ACTIVE)
        self._auth(manual_user)
        self.assertEqual(self.client.get('/api/users/manage_users/').status_code, 200)

    def test_billing_and_auth_routes_stay_reachable(self):
        _, user = self._company_user(
            plan=Company.Plan.PROFESSIONAL, status=Company.Status.PENDING_PAYMENT)
        self._auth(user)

        for url in (
            '/api/billing/status/',
            '/api/billing/verify-session/?session_id=cs_1',
            '/api/auth/me/',
        ):
            response = self.client.get(url)
            self.assertNotEqual(response.status_code, 402, f'{url} was blocked')

    def test_super_admin_is_never_blocked(self):
        superadmin = User.objects.create_superuser(
            email='root@test.test', password='Str0ngPass!23',
            first_name='R', last_name='O')
        self._auth(superadmin)
        self.assertNotEqual(self.client.get('/api/users/manage_users/').status_code, 402)


class BillingStatusTests(TestCase):
    def test_status_reports_limits_and_usage(self):
        company = Company.objects.create(name='Status Co', plan=Company.Plan.BASIC)
        user = User.objects.create_user(
            email='s@status.test', first_name='S', last_name='T',
            password='Str0ngPass!23', company=company,
            role=User.Role.COMPANY_ADMIN, status=User.Status.ACTIVE)
        client = APIClient()
        client.force_authenticate(user=user)

        response = client.get('/api/billing/status/')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['limits']['users'], 10)
        self.assertEqual(response.data['usage']['users'], 1)
        self.assertIn('ai_credits', response.data['usage'])


class StripeWebhookTests(TestCase):
    URL = '/webhooks/stripe/'
    NO_SLASH_URL = '/webhooks/stripe'

    @staticmethod
    def _signature(payload: bytes, secret: str, timestamp: int | None = None) -> str:
        """Build a Stripe-style signature header without needing the SDK helper."""
        import hashlib
        import hmac
        import time

        timestamp = timestamp if timestamp is not None else int(time.time())
        signed = f'{timestamp}.{payload.decode()}'.encode()
        digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
        return f't={timestamp},v1={digest}'

    def _post(self, payload, signature='sig-good'):
        return self.client.post(
            self.URL,
            data=json.dumps(payload),
            content_type='application/json',
            headers={'stripe-signature': signature},
        )

    def test_bad_signature_is_rejected(self):
        from django.test import override_settings

        with override_settings(STRIPE_WEBHOOK_SECRET='whsec_test'):
            response = self._post({'id': 'evt_1', 'type': 'invoice.paid'}, signature='bad')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(ProcessedStripeEvent.objects.exists())

    def test_slashless_url_accepts_signed_post_without_redirect(self):
        from django.test import override_settings

        secret = 'whsec_test'
        payload = {'id': 'evt_slashless', 'type': 'invoice.paid'}
        body = json.dumps(payload).encode()
        with override_settings(STRIPE_WEBHOOK_SECRET=secret):
            response = self.client.post(
                self.NO_SLASH_URL,
                data=body,
                content_type='application/json',
                headers={'stripe-signature': self._signature(body, secret)},
            )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {'received': True})

    def test_missing_webhook_secret_is_rejected(self):
        from django.test import override_settings

        with override_settings(STRIPE_WEBHOOK_SECRET=''):
            response = self._post({'id': 'evt_x', 'type': 'invoice.paid'})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(ProcessedStripeEvent.objects.exists())

    def test_valid_event_activates_and_is_processed_once(self):
        from django.test import override_settings

        secret = 'whsec_test'
        overrides = override_settings(
            STRIPE_WEBHOOK_SECRET=secret,
            STRIPE_PRICE_PRO_MONTHLY='price_pro',
            STRIPE_PRICE_PRO_ANNUAL='price_pro_annual',
        )
        with overrides:
            company = Company.objects.create(
                name='Hook Co', plan=Company.Plan.PROFESSIONAL,
                status=Company.Status.PENDING_PAYMENT, stripe_customer_id='cus_1')
            payload = {
                'id': 'evt_2',
                'type': 'checkout.session.completed',
                'data': {'object': {
                    'id': 'cs_1', 'customer': 'cus_1',
                    'client_reference_id': str(company.pk),
                    'subscription': 'sub_1',
                    'line_items': {'data': [{'price': {'id': 'price_pro'}}]},
                }},
            }
            body = json.dumps(payload).encode()
            signature = self._signature(body, secret)

            first = self.client.post(
                self.URL, data=body, content_type='application/json',
                headers={'stripe-signature': signature})
            self.assertEqual(first.status_code, 200, first.content)

            company.refresh_from_db()
            self.assertEqual(company.status, Company.Status.ACTIVE)
            self.assertEqual(company.plan, Company.Plan.PROFESSIONAL)
            self.assertEqual(company.stripe_subscription_id, 'sub_1')

            second = self.client.post(
                self.URL, data=body, content_type='application/json',
                headers={'stripe-signature': signature})
            self.assertEqual(second.status_code, 200)
            self.assertTrue(second.json().get('duplicate'))

        self.assertEqual(ProcessedStripeEvent.objects.filter(event_id='evt_2').count(), 1)

    def test_invoice_failed_marks_past_due_with_grace(self):
        from datetime import timedelta

        from django.test import override_settings
        from django.utils import timezone

        company = Company.objects.create(
            name='Fail Co', plan=Company.Plan.PROFESSIONAL,
            status=Company.Status.ACTIVE, stripe_customer_id='cus_9')
        from billing import services

        with override_settings(STRIPE_WEBHOOK_SECRET='whsec_test'):
            payload = {
                'id': 'evt_3',
                'type': 'invoice.payment_failed',
                'data': {'object': {'id': 'in_1', 'customer': 'cus_9'}},
            }
            body = json.dumps(payload).encode()
            response = self.client.post(
                self.URL, data=body, content_type='application/json',
                headers={'stripe-signature': self._signature(body, 'whsec_test')})
        self.assertEqual(response.status_code, 200, response.content)

        company.refresh_from_db()
        self.assertEqual(company.status, Company.Status.PAST_DUE)
        self.assertGreaterEqual(
            company.grace_until, timezone.now() + timedelta(days=4))

    def test_subscription_deleted_downgrades_to_basic(self):
        from django.test import override_settings

        company = Company.objects.create(
            name='Cancel Co', plan=Company.Plan.PROFESSIONAL,
            status=Company.Status.ACTIVE, stripe_customer_id='cus_10',
            stripe_subscription_id='sub_10')

        with override_settings(
            STRIPE_WEBHOOK_SECRET='whsec_test',
            STRIPE_PRICE_PRO_MONTHLY='price_pro',
            STRIPE_PRICE_PRO_ANNUAL='price_pro_annual',
        ):
            payload = {
                'id': 'evt_4',
                'type': 'customer.subscription.deleted',
                'data': {'object': {
                    'id': 'sub_10', 'customer': 'cus_10', 'status': 'canceled',
                    'items': {'data': [{'price': {'id': 'price_pro'}}]},
                }},
            }
            body = json.dumps(payload).encode()
            response = self.client.post(
                self.URL, data=body, content_type='application/json',
                headers={'stripe-signature': self._signature(body, 'whsec_test')})
        self.assertEqual(response.status_code, 200, response.content)

        company.refresh_from_db()
        self.assertEqual(company.plan, Company.Plan.BASIC)
        self.assertEqual(company.status, Company.Status.ACTIVE)


class ProcessedStripeEventModelTests(TestCase):
    def test_event_id_is_unique(self):
        from django.db import IntegrityError, transaction

        ProcessedStripeEvent.objects.create(event_id='evt_dup', event_type='invoice.paid')
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ProcessedStripeEvent.objects.create(
                    event_id='evt_dup', event_type='invoice.paid')


class PlanLimitTests(TestCase):
    def setUp(self):
        from billing import services

        self.services = services
        self.company = Company.objects.create(name='Limits Co', plan=Company.Plan.BASIC)

    def _add_user(self, email, status=User.Status.ACTIVE, role=User.Role.AGENT):
        return User.objects.create_user(
            email=email, first_name='L', last_name='M', password='Passw0rd!23',
            company=self.company, status=status, role=role)

    def test_roster_counts_include_invited_and_exclude_inactive(self):
        self._add_user('a@limits.test')
        self._add_user('b@limits.test', status=User.Status.INVITED)
        self._add_user('c@limits.test', status=User.Status.INACTIVE)
        self._add_user('d@limits.test', role=User.Role.TEAM_MANAGER)

        counts = self.services._roster_counts(self.company.pk)
        self.assertEqual(counts['users'], 3)
        self.assertEqual(counts['team_managers'], 1)

    def test_assert_can_add_blocks_at_ten_users(self):
        from django.db import transaction

        for index in range(10):
            self._add_user(f'user{index}@limits.test')

        with self.assertRaises(self.services.PlanLimitReached) as ctx:
            with transaction.atomic():
                self.services.PlanLimitsService(self.company.pk).assert_can_add('users')
        self.assertEqual(ctx.exception.limit, 10)
        self.assertEqual(ctx.exception.current, 10)

        # Within a real create-transaction the eleventh user is refused.
        from django.db import IntegrityError  # noqa: F401

        with self.assertRaises(self.services.PlanLimitReached):
            with transaction.atomic():
                self.services.PlanLimitsService(self.company.pk).assert_can_add('users')
                self._add_user('eleven@limits.test')

    def test_team_manager_limit(self):
        from django.db import transaction

        self._add_user('m1@limits.test', role=User.Role.TEAM_MANAGER)
        self._add_user('m2@limits.test', role=User.Role.TEAM_MANAGER)

        with self.assertRaises(self.services.PlanLimitReached):
            with transaction.atomic():
                self.services.PlanLimitsService(self.company.pk).assert_can_add(
                    'team_managers')

    def test_unlimited_plans_pass_every_check(self):
        self.company.plan = Company.Plan.PROFESSIONAL
        self.company.save()
        service = self.services.PlanLimitsService(self.company.pk)
        self.assertIsNone(service.limits()['users'])
        service.assert_can_add('users', 5000)

    def test_manual_plans_pass_every_check(self):
        self.company.plan = Company.Plan.MANUAL
        self.company.save()
        self.services.PlanLimitsService(self.company.pk).assert_can_add('users', 5000)


class CreditAccountingTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='Credits Co', plan=Company.Plan.BASIC)

    def test_weights_come_from_central_config(self):
        from billing import plan_limits

        self.assertEqual(plan_limits.CREDIT_COSTS['chat_turn'], 1)
        self.assertEqual(plan_limits.CREDIT_COSTS['ai_proposal'], 10)
        self.assertEqual(plan_limits.CREDIT_COSTS['calling_agent_call'], 5)
        self.assertEqual(plan_limits.CREDIT_COSTS['transcript_processing'], 1)
        self.assertEqual(plan_limits.CREDIT_COSTS['kb_document_extraction'], 1)

    def test_credits_accumulate_and_block_at_the_cap(self):
        from django.db import transaction

        from billing import services

        # 50 chat turns = 50 credits.
        for _ in range(50):
            services.consume_credits(self.company.pk, 'chat_turn')
        self.assertEqual(services.current_count(self.company.pk, 'ai_credits'), 50)

        # 45 more proposals = 450 credits, bringing the total to exactly 500.
        for _ in range(45):
            services.consume_credits(self.company.pk, 'ai_proposal')
        self.assertEqual(services.current_count(self.company.pk, 'ai_credits'), 500)

        # One more proposal cannot fit.
        with self.assertRaises(services.PlanLimitReached) as ctx:
            with transaction.atomic():
                services.consume_credits(self.company.pk, 'ai_proposal')
        self.assertEqual(ctx.exception.limit, 500)
        self.assertEqual(ctx.exception.current, 500)
        self.assertEqual(services.current_count(self.company.pk, 'ai_credits'), 500)

    def test_proposal_costs_ten_credits(self):
        from billing import services

        services.consume_credits(self.company.pk, 'ai_proposal')
        self.assertEqual(services.current_count(self.company.pk, 'ai_credits'), 10)

    def test_receipt_makes_a_charge_idempotent(self):
        from billing import services

        first = services.consume_credits(self.company.pk, 'ai_proposal', receipt_id='task-1')
        second = services.consume_credits(self.company.pk, 'ai_proposal', receipt_id='task-1')

        self.assertTrue(first)
        self.assertFalse(second, 'a retried task must not be charged twice')
        self.assertEqual(services.current_count(self.company.pk, 'ai_credits'), 10)
        self.assertEqual(CreditCharge.objects.filter(task_id='task-1').count(), 1)

    def test_refund_releases_the_receipt_for_a_retry(self):
        from billing import services

        services.consume_credits(self.company.pk, 'ai_proposal', receipt_id='task-2')
        services.refund_credits(self.company.pk, 'ai_proposal', receipt_id='task-2')

        self.assertEqual(services.current_count(self.company.pk, 'ai_credits'), 0)
        self.assertFalse(CreditCharge.objects.filter(task_id='task-2').exists())

        # The retry charges again, exactly once.
        self.assertTrue(
            services.consume_credits(self.company.pk, 'ai_proposal', receipt_id='task-2'))
        self.assertEqual(services.current_count(self.company.pk, 'ai_credits'), 10)

    def test_unlimited_plan_records_usage_without_raising(self):
        from billing import services

        self.company.plan = Company.Plan.PROFESSIONAL
        self.company.save()

        for _ in range(5):
            self.assertTrue(services.consume_credits(self.company.pk, 'ai_proposal'))
        self.assertEqual(services.current_count(self.company.pk, 'ai_credits'), 50)

    def test_usage_resets_in_a_new_period(self):
        from billing import plan_limits, services

        services.consume_credits(self.company.pk, 'ai_proposal')
        self.assertEqual(services.current_count(self.company.pk, 'ai_credits'), 10)

        next_period = plan_limits.current_period_start(now=timezone.now() + timedelta(days=40))
        self.assertNotEqual(next_period, plan_limits.current_period_start())
        self.assertEqual(
            UsageCounter.objects.filter(company_id=self.company.pk, period_start=next_period)
            .count(), 0)


class ErrorShapeTests(TestCase):
    """The frontend branches on these bodies, so their shape is a contract."""

    def test_plan_limit_error_body(self):
        from rest_framework.settings import api_settings

        from billing.exceptions import PlanLimitReached

        response = api_settings.EXCEPTION_HANDLER(
            PlanLimitReached(kind='ai_credits', limit=500, current=500), {})
        self.assertEqual(response.status_code, 403)
        body = response.data
        self.assertEqual(body['code'], 'PLAN_LIMIT_REACHED')
        self.assertEqual(body['kind'], 'aiCredits')
        self.assertEqual(body['limit'], 500)
        self.assertEqual(body['current'], 500)
        self.assertIn('detail', body)

    def test_payment_required_error_body(self):
        from rest_framework.settings import api_settings

        from billing.exceptions import PaymentRequired

        response = api_settings.EXCEPTION_HANDLER(
            PaymentRequired(company_status='suspended'), {})
        self.assertEqual(response.status_code, 402)
        self.assertEqual(response.data['code'], 'PAYMENT_REQUIRED')
        self.assertEqual(response.data['status'], 'suspended')
        self.assertFalse(response.data['allowed'])

    def test_non_billing_errors_keep_default_handling(self):
        from rest_framework.exceptions import NotFound
        from rest_framework.settings import api_settings

        response = api_settings.EXCEPTION_HANDLER(NotFound(), {})
        self.assertEqual(response.status_code, 404)
        self.assertIn('detail', response.data)


class CleanupPendingCompaniesTests(TestCase):
    def setUp(self):
        from django.core.cache import cache

        cache.clear()

    def _pending_company(self, name, age_hours, customer_id='cus_pending'):
        company = Company.objects.create(
            name=name, plan=Company.Plan.PROFESSIONAL,
            status=Company.Status.PENDING_PAYMENT,
            stripe_customer_id=customer_id,
        )
        Company.objects.filter(pk=company.pk).update(
            created_at=timezone.now() - timedelta(hours=age_hours))
        return company

    def test_deletes_stale_pending_company_with_no_stripe_history(self):
        from billing import tasks

        company = self._pending_company('Abandoned Co', age_hours=72)
        with patch('billing.stripe_client.list_checkout_sessions', return_value=[]), \
             patch('billing.stripe_client.list_subscriptions', return_value=[]), \
             patch('billing.stripe_client.delete_customer') as delete_customer:
            result = tasks.cleanup_pending_companies()

        self.assertEqual(result['deleted'], 1)
        self.assertFalse(Company.objects.filter(pk=company.pk).exists())
        delete_customer.assert_called_once_with('cus_pending')

    def test_keeps_company_with_a_paid_checkout(self):
        from billing import tasks

        company = self._pending_company('Paid Co', age_hours=72)
        with patch('billing.stripe_client.list_checkout_sessions',
                   return_value=[{'id': 'cs_1', 'payment_status': 'paid', 'status': 'complete'}]), \
             patch('billing.stripe_client.delete_customer') as delete_customer:
            result = tasks.cleanup_pending_companies()

        self.assertEqual(result['deleted'], 0)
        self.assertEqual(result['kept_paid'], 1)
        self.assertTrue(Company.objects.filter(pk=company.pk).exists())
        delete_customer.assert_not_called()

    def test_keeps_company_with_a_live_subscription(self):
        from billing import tasks

        company = self._pending_company('Live Co', age_hours=72)
        with patch('billing.stripe_client.list_checkout_sessions', return_value=[]), \
             patch('billing.stripe_client.list_subscriptions',
                   return_value=[{'id': 'sub_1', 'status': 'active'}]), \
             patch('billing.stripe_client.delete_customer') as delete_customer:
            result = tasks.cleanup_pending_companies()

        self.assertEqual(result['kept_paid'], 1)
        self.assertTrue(Company.objects.filter(pk=company.pk).exists())
        delete_customer.assert_not_called()

    def test_keeps_everything_when_stripe_is_unreachable(self):
        from billing import tasks

        company = self._pending_company('Unverified Co', age_hours=72)
        with patch('billing.stripe_client.list_checkout_sessions',
                   side_effect=RuntimeError('stripe is down')), \
             patch('billing.stripe_client.delete_customer') as delete_customer:
            result = tasks.cleanup_pending_companies()

        self.assertEqual(result['deleted'], 0)
        self.assertEqual(result['kept_unverified'], 1)
        self.assertTrue(Company.objects.filter(pk=company.pk).exists())
        delete_customer.assert_not_called()

    def test_keeps_companies_with_a_subscription_id(self):
        from billing import tasks

        company = self._pending_company('Subscribed Co', age_hours=72)
        company.stripe_subscription_id = 'sub_9'
        company.save(update_fields=['stripe_subscription_id'])

        with patch('billing.stripe_client.list_checkout_sessions', return_value=[]), \
             patch('billing.stripe_client.delete_customer') as delete_customer:
            result = tasks.cleanup_pending_companies()

        self.assertEqual(result['deleted'], 0)
        self.assertTrue(Company.objects.filter(pk=company.pk).exists())
        delete_customer.assert_not_called()

    def test_keeps_recent_pending_companies(self):
        from billing import tasks

        company = self._pending_company('Fresh Co', age_hours=2)
        with patch('billing.stripe_client.list_checkout_sessions', return_value=[]), \
             patch('billing.stripe_client.delete_customer'):
            result = tasks.cleanup_pending_companies()

        self.assertEqual(result['deleted'], 0)
        self.assertTrue(Company.objects.filter(pk=company.pk).exists())


class SuspendOverdueCompaniesTests(TestCase):
    def test_suspends_only_past_due_past_its_grace(self):
        from billing import tasks

        overdue = Company.objects.create(
            name='Overdue Co', plan=Company.Plan.PROFESSIONAL,
            status=Company.Status.PAST_DUE,
            grace_until=timezone.now() - timedelta(hours=1))
        within_grace = Company.objects.create(
            name='Grace Co', plan=Company.Plan.PROFESSIONAL,
            status=Company.Status.PAST_DUE,
            grace_until=timezone.now() + timedelta(days=1))
        suspended = Company.objects.create(
            name='Already Co', plan=Company.Plan.PROFESSIONAL,
            status=Company.Status.SUSPENDED,
            grace_until=timezone.now() - timedelta(hours=5))

        with patch('billing.stripe_client.list_checkout_sessions', return_value=[]):
            result = tasks.suspend_overdue_companies()

        self.assertEqual(result['suspended'], 1)
        overdue.refresh_from_db()
        within_grace.refresh_from_db()
        suspended.refresh_from_db()
        self.assertEqual(overdue.status, Company.Status.SUSPENDED)
        self.assertEqual(within_grace.status, Company.Status.PAST_DUE)
        self.assertEqual(suspended.status, Company.Status.SUSPENDED)


class SeatLimitEnforcementTests(TestCase):
    """Every path that adds a seat must respect the plan cap.

    Exercised through the real HTTP endpoints and the real CSV import task, not
    by calling the limiter directly.
    """

    def setUp(self):
        self.company = Company.objects.create(
            name='Seats Co', plan=Company.Plan.BASIC,
            operating_countries=['UAE'])
        self.admin = User.objects.create_user(
            email='admin@seats.test', first_name='S', last_name='A',
            password='Passw0rd!23', company=self.company,
            role=User.Role.COMPANY_ADMIN, status=User.Status.ACTIVE)
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)

    def _invite(self, index, role=User.Role.AGENT):
        response = self.client.post(
            '/api/users/manage_users/',
            {
                'email': f'user{index}@seats.test',
                'first_name': 'U', 'last_name': 'X',
                'role': role, 'countries': ['UAE'],
                'company': self.company.pk,
            },
            format='json',
        )
        return response

    def test_invites_ten_users_then_returns_403(self):
        # The company admin already holds one seat.
        for index in range(9):
            response = self._invite(index)
            self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            User.objects.filter(company=self.company)
            .exclude(status=User.Status.INACTIVE).count(), 10)

        blocked = self._invite(99)
        self.assertEqual(blocked.status_code, 403, blocked.data)
        self.assertEqual(blocked.data['code'], 'PLAN_LIMIT_REACHED')
        self.assertEqual(blocked.data['kind'], 'users')
        self.assertEqual(blocked.data['limit'], 10)
        self.assertEqual(blocked.data['current'], 10)
        self.assertFalse(
            User.objects.filter(email='user99@seats.test').exists(),
            'the rejected user must not exist')

    def test_inactive_users_do_not_consume_seats(self):
        for index in range(9):
            self.assertEqual(self._invite(index).status_code, 201)

        spare = User.objects.get(email='user0@seats.test')
        spare.status = User.Status.INACTIVE
        spare.save(update_fields=['status'])

        self.assertEqual(self._invite(99).status_code, 201)

    def test_third_team_manager_is_refused(self):
        self.assertEqual(
            self._invite(1, role=User.Role.TEAM_MANAGER).status_code, 201)
        self.assertEqual(
            self._invite(2, role=User.Role.TEAM_MANAGER).status_code, 201)

        blocked = self._invite(3, role=User.Role.TEAM_MANAGER)
        self.assertEqual(blocked.status_code, 403, blocked.data)
        self.assertEqual(blocked.data['kind'], 'teamManagers')
        self.assertEqual(blocked.data['limit'], 2)

    def test_role_promotion_respects_the_manager_cap(self):
        for index in range(9):
            self.assertEqual(self._invite(index).status_code, 201)

        promoted = []
        for index in (1, 2):
            user = User.objects.get(email=f'user{index}@seats.test')
            response = self.client.patch(
                f'/api/users/manage_users/{user.pk}/',
                {'role': User.Role.TEAM_MANAGER},
                format='json',
            )
            self.assertEqual(response.status_code, 200, response.data)
            promoted.append(user)

        third = User.objects.get(email='user3@seats.test')
        blocked = self.client.patch(
            f'/api/users/manage_users/{third.pk}/',
            {'role': User.Role.TEAM_MANAGER},
            format='json',
        )
        self.assertEqual(blocked.status_code, 403, blocked.data)
        self.assertEqual(blocked.data['kind'], 'teamManagers')
        third.refresh_from_db()
        self.assertEqual(third.role, User.Role.AGENT)

    def test_demoting_a_manager_frees_a_seat(self):
        self.assertEqual(
            self._invite(1, role=User.Role.TEAM_MANAGER).status_code, 201)
        self.assertEqual(
            self._invite(2, role=User.Role.TEAM_MANAGER).status_code, 201)

        first = User.objects.get(email='user1@seats.test')
        demote = self.client.patch(
            f'/api/users/manage_users/{first.pk}/', {'role': User.Role.AGENT},
            format='json')
        self.assertEqual(demote.status_code, 200, demote.data)

        self.assertEqual(
            self._invite(3, role=User.Role.TEAM_MANAGER).status_code, 201)

    def test_professional_plan_is_unlimited(self):
        self.company.plan = Company.Plan.PROFESSIONAL
        self.company.save(update_fields=['plan'])

        for index in range(15):
            self.assertEqual(
                self._invite(index, role=User.Role.TEAM_MANAGER).status_code, 201)
        self.assertEqual(
            User.objects.filter(company=self.company, role=User.Role.TEAM_MANAGER)
            .count(), 15)

    def test_manual_companies_are_unlimited(self):
        self.company.plan = Company.Plan.MANUAL
        self.company.save(update_fields=['plan'])
        for index in range(12):
            self.assertEqual(self._invite(index).status_code, 201)


class CsvImportLimitTests(TestCase):
    """Bulk import must not be a way around the seat cap."""

    def setUp(self):
        import io

        self.company = Company.objects.create(
            name='Csv Co', plan=Company.Plan.BASIC, operating_countries=['UAE'])
        self.admin = User.objects.create_user(
            email='admin@csv.test', first_name='C', last_name='A',
            password='Passw0rd!23', company=self.company,
            role=User.Role.COMPANY_ADMIN, status=User.Status.ACTIVE)
        self.io = io

    def _import(self, rows):
        import uuid

        from django.core.files.storage import default_storage

        from users.tasks import import_users_csv

        buffer = self.io.StringIO()
        writer = csv.DictWriter(
            buffer,
            fieldnames=['email', 'first_name', 'last_name', 'role', 'countries'],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
        key = f'imports/csv/test/{uuid.uuid4()}/users.csv'
        default_storage.save(key, self.io.BytesIO(buffer.getvalue().encode()))
        with patch('users.tasks.send_superadmin_created_account_email'):
            return import_users_csv(key, self.admin.pk)

    def test_import_stops_at_the_cap_and_reports_the_error(self):
        rows = [
            {'email': f'csv{i}@co.test', 'first_name': 'C', 'last_name': 'U',
             'role': User.Role.AGENT, 'countries': 'UAE'}
            for i in range(15)
        ]
        result = self._import(rows)

        # Admin holds one seat, so only nine more fit.
        self.assertEqual(result['created'], 9)
        self.assertEqual(len(result['errors']), 6)
        self.assertEqual(result['errors'][0]['code'], 'PLAN_LIMIT_REACHED')
        self.assertEqual(result['errors'][0]['kind'], 'users')
        self.assertEqual(result['errors'][0]['limit'], 10)
        self.assertEqual(
            User.objects.filter(company=self.company)
            .exclude(status=User.Status.INACTIVE).count(), 10)

    def test_import_caps_team_managers(self):
        rows = [
            {'email': f'mgr{i}@co.test', 'first_name': 'C', 'last_name': 'M',
             'role': User.Role.TEAM_MANAGER, 'countries': 'UAE'}
            for i in range(5)
        ]
        result = self._import(rows)

        self.assertEqual(result['created'], 2)
        self.assertEqual(len(result['errors']), 3)
        self.assertEqual(result['errors'][0]['kind'], 'teamManagers')
        self.assertEqual(result['errors'][0]['limit'], 2)

    def test_existing_user_updates_are_not_charged_a_seat(self):
        self.assertEqual(self._import([
            {'email': 'csv0@co.test', 'first_name': 'C', 'last_name': 'U',
             'role': User.Role.AGENT, 'countries': 'UAE'},
        ])['created'], 1)

        # Re-importing the same row updates rather than creating, so it still
        # succeeds even when the company sits exactly at its cap.
        for i in range(1, 10):
            self._import([
                {'email': f'filler{i}@co.test', 'first_name': 'F', 'last_name': 'L',
                 'role': User.Role.AGENT, 'countries': 'UAE'},
            ])

        result = self._import([
            {'email': 'csv0@co.test', 'first_name': 'C', 'last_name': 'Renamed',
             'role': User.Role.AGENT, 'countries': 'UAE'},
        ])
        self.assertEqual(result['updated'], 1)
        self.assertEqual(result['errors'], [])