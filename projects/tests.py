from datetime import timedelta
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase
from projects.models import Project, Promotion, Unit

User = get_user_model()


class PromotionTests(APITestCase):

    def setUp(self):
        self.user = User.objects.create_superuser(
            email='testadmin@example.com',
            password='password123',
            first_name='Test',
            last_name='Admin',
        )
        self.client.force_authenticate(user=self.user)

        self.project = Project.objects.create(
            title='Forum House',
            description='Test description',
            location='London',
            developer='Test Developer',
            status=Project.Status.READY,
            project_status=Project.ProjectStatus.LIVE,
            starting_price=Decimal('100000.00'),
            yield_percentage=Decimal('7.50'),
            project_type=Project.ProjectType.RESIDENTIAL,
        )

        # Unit 1: no pre-existing discounted price
        self.unit1 = Unit.objects.create(
            project=self.project,
            label='Unit 101',
            list_price=Decimal('100000.00'),
            discounted_price=None,
            status=Unit.UnitStatus.AVAILABLE,
        )

        # Unit 2: pre-existing discounted price of 110000
        self.unit2 = Unit.objects.create(
            project=self.project,
            label='Unit 102',
            list_price=Decimal('120000.00'),
            discounted_price=Decimal('110000.00'),
            status=Unit.UnitStatus.AVAILABLE,
        )

    def test_promotion_status_upcoming(self):
        now = timezone.now()
        start = now + timedelta(days=1)
        end = now + timedelta(days=5)

        promo = Promotion.objects.create(
            project=self.project,
            title='Summer Move-in Special',
            discount=10,
            start_date=start,
            end_date=end,
        )
        promo.sync_status()

        self.assertEqual(promo.status, Promotion.Status.UPCOMING)
        self.unit1.refresh_from_db()
        self.unit2.refresh_from_db()
        self.assertIsNone(self.unit1.discounted_price)
        self.assertEqual(self.unit2.discounted_price, Decimal('110000.00'))

    def test_promotion_activation_and_price_discount(self):
        now = timezone.now()
        start = now - timedelta(hours=1)
        end = now + timedelta(days=2)

        promo = Promotion.objects.create(
            project=self.project,
            title='Summer Move-in Special',
            discount=10,
            start_date=start,
            end_date=end,
        )
        promo.sync_status()

        self.assertEqual(promo.status, Promotion.Status.ACTIVE)
        self.unit1.refresh_from_db()
        self.unit2.refresh_from_db()

        # Unit 1: 100,000 * 0.9 = 90,000
        self.assertEqual(self.unit1.discounted_price, Decimal('90000.00'))
        # Unit 2: base price 110,000 * 0.9 = 99,000
        self.assertEqual(self.unit2.discounted_price, Decimal('99000.00'))

    def test_promotion_expiration_reverts_prices(self):
        now = timezone.now()
        start = now - timedelta(days=5)
        end = now - timedelta(days=1)

        promo = Promotion.objects.create(
            project=self.project,
            title='Expired Special',
            discount=10,
            start_date=start,
            end_date=end,
        )
        # Manually activate to create price snapshot
        promo.activate()

        promo.sync_status()
        self.assertEqual(promo.status, Promotion.Status.EXPIRED)

        self.unit1.refresh_from_db()
        self.unit2.refresh_from_db()
        # Unit 1 should revert to None
        self.assertIsNone(self.unit1.discounted_price)
        # Unit 2 should revert to 110,000
        self.assertEqual(self.unit2.discounted_price, Decimal('110000.00'))

    def test_promotion_deletion_reverts_prices(self):
        now = timezone.now()
        start = now - timedelta(hours=1)
        end = now + timedelta(days=2)

        promo = Promotion.objects.create(
            project=self.project,
            title='ToDelete Special',
            discount=10,
            start_date=start,
            end_date=end,
        )
        promo.sync_status()

        url = f'/api/projects/promotions/{promo.id}/'
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

        self.unit1.refresh_from_db()
        self.unit2.refresh_from_db()
        self.assertIsNone(self.unit1.discounted_price)
        self.assertEqual(self.unit2.discounted_price, Decimal('110000.00'))

    def test_promotion_crud_api(self):
        now = timezone.now()
        start_str = (now + timedelta(days=1)).isoformat()
        end_str = (now + timedelta(days=3)).isoformat()

        # CREATE
        payload = {
            'project': self.project.id,
            'promotion_title': 'Summer Offer',
            'discount_percentage': 15,
            'start_date': start_str,
            'end_date': end_str,
        }
        res = self.client.post('/api/projects/promotions/', payload, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        promo_id = res.data['id']
        self.assertEqual(res.data['promotion_title'], 'Summer Offer')
        self.assertEqual(res.data['discount_percentage'], 15)

        # LIST
        res_list = self.client.get('/api/projects/promotions/')
        self.assertEqual(res_list.status_code, status.HTTP_200_OK)
        self.assertIn('stats', res_list.data)
        self.assertIn('active_promotions', res_list.data['stats'])
        self.assertIn('upcoming_promotions', res_list.data['stats'])
        self.assertIn('expired_promotions', res_list.data['stats'])
        self.assertIn('total_leads_generated', res_list.data['stats'])
        self.assertEqual(res_list.data['results'][0]['leads_generated'], 0)

        # UPDATE
        res_update = self.client.patch(f'/api/projects/promotions/{promo_id}/', {'discount_percentage': 20}, format='json')
        self.assertEqual(res_update.status_code, status.HTTP_200_OK)
        self.assertEqual(res_update.data['discount_percentage'], 20)

        # DELETE
        res_del = self.client.delete(f'/api/projects/promotions/{promo_id}/')
        self.assertEqual(res_del.status_code, status.HTTP_204_NO_CONTENT)

    def test_project_promotion_fields_in_development_portfolio(self):
        now = timezone.now()
        start = now - timedelta(hours=1)
        end = now + timedelta(days=2)

        # Before promotion creation
        res_detail_before = self.client.get(f'/api/projects/development_portfolio/{self.project.id}/')
        self.assertEqual(res_detail_before.status_code, status.HTTP_200_OK)
        self.assertFalse(res_detail_before.data['has_active_promotion'])
        self.assertIsNone(res_detail_before.data['active_promotion_discount'])

        # Create active promotion
        promo = Promotion.objects.create(
            project=self.project,
            title='Active Special',
            discount=25,
            start_date=start,
            end_date=end,
        )
        promo.sync_status()

        # Check detail API
        res_detail = self.client.get(f'/api/projects/development_portfolio/{self.project.id}/')
        self.assertEqual(res_detail.status_code, status.HTTP_200_OK)
        self.assertTrue(res_detail.data['has_active_promotion'])
        self.assertEqual(res_detail.data['active_promotion_discount'], 25)

        # Check list API
        res_list = self.client.get('/api/projects/development_portfolio/')
        self.assertEqual(res_list.status_code, status.HTTP_200_OK)
        project_data = next(item for item in res_list.data['results'] if item['id'] == self.project.id)
        self.assertTrue(project_data['has_active_promotion'])
        self.assertEqual(project_data['active_promotion_discount'], 25)

