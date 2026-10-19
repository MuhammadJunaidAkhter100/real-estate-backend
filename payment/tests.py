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

    def test_get_plans_public(self):
        url = reverse('plans')
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        plans = response.data.get('plans', [])
        plan_ids = [p['id'] for p in plans]
        self.assertIn('free', plan_ids)
        self.assertIn('pro', plan_ids)
        self.assertIn('plus', plan_ids)
        self.assertIn('top_tier', plan_ids)

        pro = next(p for p in plans if p['id'] == 'pro')
        self.assertEqual(pro['monthlyPrice'], 49)
        self.assertEqual(pro['yearlyPrice'], 39)
        self.assertEqual(pro['aiCredits'], 250)
        self.assertEqual(pro['agents'], 10)

        plus = next(p for p in plans if p['id'] == 'plus')
        self.assertEqual(plus['monthlyPrice'], 99)
        self.assertEqual(plus['yearlyPrice'], 79)
        self.assertEqual(plus['aiCredits'], 500)
        self.assertEqual(plus['agents'], 20)

        top = next(p for p in plans if p['id'] == 'top_tier')
        self.assertEqual(top['monthlyPrice'], 199)
        self.assertEqual(top['yearlyPrice'], 159)
        self.assertEqual(top['aiCredits'], 1000)
        self.assertIsNone(top['agents'])


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
