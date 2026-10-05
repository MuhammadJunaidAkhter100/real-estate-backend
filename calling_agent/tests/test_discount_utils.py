from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from calling_agent.discount_utils import (
    build_project_promotion_block,
    compute_budget_fit,
    unit_pricing_block,
)
from calling_agent.tests.factories import create_company, create_project, create_user
from projects.models import Promotion


class DiscountUtilsTests(TestCase):
    def setUp(self) -> None:
        self.company = create_company()
        self.user = create_user(company=self.company)
        self.project = create_project(user=self.user, title='Promo Tower')

    def test_compute_budget_fit(self) -> None:
        self.assertEqual(
            compute_budget_fit(Decimal('300000'), Decimal('250000')),
            'within',
        )
        self.assertEqual(
            compute_budget_fit(Decimal('200000'), Decimal('250000')),
            'over',
        )
        self.assertEqual(compute_budget_fit(None, Decimal('250000')), 'unknown')

    def test_active_promotion_exposed(self) -> None:
        now = timezone.now()
        Promotion.objects.create(
            project=self.project,
            title='Spring Sale',
            discount=10,
            start_date=now - timedelta(days=1),
            end_date=now + timedelta(days=14),
            status=Promotion.Status.ACTIVE,
            created_by=self.user,
        )
        block = build_project_promotion_block(self.project)
        self.assertEqual(block['status'], 'active')
        self.assertEqual(block['title'], 'Spring Sale')
        self.assertEqual(block['discount_percent'], 10)

    def test_near_expiring_promotion(self) -> None:
        now = timezone.now()
        Promotion.objects.create(
            project=self.project,
            title='Ending Soon',
            discount=5,
            start_date=now - timedelta(days=5),
            end_date=now + timedelta(days=2),
            status=Promotion.Status.ACTIVE,
            created_by=self.user,
        )
        block = build_project_promotion_block(self.project)
        self.assertEqual(block['status'], 'near_expiring')
        self.assertTrue(block['is_near_expiring'])

    def test_expired_promotion_not_exposed(self) -> None:
        now = timezone.now()
        Promotion.objects.create(
            project=self.project,
            title='Old Promo',
            discount=15,
            start_date=now - timedelta(days=30),
            end_date=now - timedelta(days=1),
            status=Promotion.Status.EXPIRED,
            created_by=self.user,
        )
        block = build_project_promotion_block(self.project)
        self.assertEqual(block['status'], 'none')
        self.assertIsNone(block['title'])

    def test_unit_pricing_block_discount_source(self) -> None:
        pricing = unit_pricing_block(
            list_price=Decimal('300000'),
            discounted_price=Decimal('270000'),
            effective_price=Decimal('270000'),
            has_active_promotion=True,
        )
        self.assertEqual(pricing['discount_source'], 'both')
        self.assertTrue(pricing['has_discount'])

    def test_lowest_available_unit_prices_batch(self) -> None:
        from calling_agent.discount_utils import (
            lowest_available_unit_prices_for_projects,
        )
        from calling_agent.tests.factories import create_unit

        project_b = create_project(user=self.user, title='Batch Project B')
        create_unit(project=self.project, price=Decimal('250000.00'))
        create_unit(project=project_b, price=Decimal('180000.00'))
        create_unit(
            project=project_b,
            label='B-2',
            price=Decimal('220000.00'),
            discounted_price=Decimal('200000.00'),
        )

        prices = lowest_available_unit_prices_for_projects(
            [self.project.pk, project_b.pk]
        )
        self.assertEqual(prices[self.project.pk], Decimal('250000.00'))
        self.assertEqual(prices[project_b.pk], Decimal('180000.00'))
