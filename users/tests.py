from datetime import timedelta
from django.core import mail
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from notifications.models import Notification
from users.models import Company, Lead, Task
from users.tasks import check_and_update_task_expirations

User = get_user_model()


class TaskExpirationTestCase(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='Test Company')
        self.user = User.objects.create_user(
            email='test@example.com',
            first_name='Test',
            last_name='User',
            password='password123',
            company=self.company,
        )

    def test_past_task_expires_and_notifies_user(self):
        past_time = timezone.now() - timedelta(minutes=15)
        task = Task.objects.create(
            name='Past Task',
            scheduled_at=past_time,
            status=Task.Status.PENDING,
            created_by=self.user,
        )

        res = check_and_update_task_expirations()
        self.assertEqual(res['expired_count'], 1)

        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.EXPIRED)

        notification = Notification.objects.filter(
            recipient=self.user,
            type=Notification.Type.TASK_EXPIRED,
        ).first()
        self.assertIsNotNone(notification)
        self.assertIn("Past Task", notification.message)

    def test_task_expiring_within_24_hours_sends_warning_and_email(self):
        within_24h = timezone.now() + timedelta(hours=12)
        task = Task.objects.create(
            name='Expiring 24h Task',
            scheduled_at=within_24h,
            status=Task.Status.PENDING,
            created_by=self.user,
            expiry_24h_warning_sent=False,
            expiry_warning_sent=False,
        )

        res = check_and_update_task_expirations()
        self.assertEqual(res['warning_24h_count'], 1)
        self.assertEqual(res['warning_1h_count'], 0)

        task.refresh_from_db()
        self.assertTrue(task.expiry_24h_warning_sent)
        self.assertFalse(task.expiry_warning_sent)
        self.assertEqual(task.status, Task.Status.PENDING)

        notification = Notification.objects.filter(
            recipient=self.user,
            type=Notification.Type.TASK_EXPIRING_AFTER_24H,
            title="Task Expiring in 24 Hours",
        ).first()
        self.assertIsNotNone(notification)
        self.assertIn("Expiring 24h Task", notification.message)

        # Check email sent
        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertEqual(email.to, [self.user.email])
        self.assertIn("Expiring 24h Task", email.subject)
        self.assertIn("24 hours", email.body)

    def test_task_expiring_within_1_hour_sends_warning_notification(self):
        soon_time = timezone.now() + timedelta(minutes=30)
        task = Task.objects.create(
            name='Expiring Soon Task',
            scheduled_at=soon_time,
            status=Task.Status.PENDING,
            created_by=self.user,
            expiry_24h_warning_sent=True,  # 24h warning already sent
            expiry_warning_sent=False,
        )

        res = check_and_update_task_expirations()
        self.assertEqual(res['warning_1h_count'], 1)

        task.refresh_from_db()
        self.assertTrue(task.expiry_warning_sent)
        self.assertEqual(task.status, Task.Status.PENDING)

        notification = Notification.objects.filter(
            recipient=self.user,
            type=Notification.Type.TASK_EXPIRING_SOON,
            title="Task Expiring Soon",
        ).first()
        self.assertIsNotNone(notification)
        self.assertIn("Expiring Soon Task", notification.message)

    def test_future_task_beyond_24h_unaffected(self):
        future_time = timezone.now() + timedelta(hours=30)
        task = Task.objects.create(
            name='Future Task',
            scheduled_at=future_time,
            status=Task.Status.PENDING,
            created_by=self.user,
        )

        res = check_and_update_task_expirations()
        self.assertEqual(res['expired_count'], 0)
        self.assertEqual(res['warning_24h_count'], 0)
        self.assertEqual(res['warning_1h_count'], 0)

        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.PENDING)
        self.assertFalse(task.expiry_24h_warning_sent)
        self.assertFalse(task.expiry_warning_sent)


class CommissionTestCase(TestCase):
    def setUp(self):
        from rest_framework.test import APIClient
        from projects.models import Project, ProjectAgentAssignment, Unit
        from users.models import Lead

        self.client = APIClient()
        self.company = Company.objects.create(name='Acme Corp')
        self.agent = User.objects.create_user(
            email='agent@example.com',
            first_name='Agent',
            last_name='Smith',
            password='password123',
            company=self.company,
            role='agent',
        )
        self.client.force_authenticate(user=self.agent)

        self.project = Project.objects.create(
            title='Fountain Court',
            description='Luxury Apartments',
            location='Downtown',
            developer='Acme Dev',
            status=Project.Status.PLANNED,
            starting_price=200000,
            yield_percentage=5,
            project_type=Project.ProjectType.RESIDENTIAL,
            created_by=self.agent,
        )

        # 10% Agent, 90% Company split
        self.assignment = ProjectAgentAssignment.objects.create(
            agent=self.agent,
            project=self.project,
            agent_split=10,
            company_split=90,
        )

        self.unit = Unit.objects.create(
            project=self.project,
            label='G-01',
            floor='Ground Floor',
            category='2 bed',
            list_price=500000.00,
            discounted_price=450000.00,
            status=Unit.UnitStatus.AVAILABLE,
        )
        self.lead = Lead.objects.create(
            name='John Doe',
            email='john@example.com',
            project=self.project,
            unit=self.unit,
            created_by=self.agent,
            assigned_to=self.agent,
            status=Lead.Status.CONVERTED_WON,
        )

    def test_commission_calculation_endpoint(self):
        res = self.client.get('/api/users/leads/commissions/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['total_won_leads'], 1)
        # Discounted price takes priority over list price (450,000 vs 500,000)
        self.assertEqual(res.data['total_deal_volume'], 450000.0)
        # 10% of 450,000 = 45,000
        self.assertEqual(res.data['total_agent_commissions'], 45000.0)
        # 90% of 450,000 = 405,000
        self.assertEqual(res.data['total_company_commissions'], 405000.0)
        self.assertEqual(res.data['leads'][0]['unit_id'], self.unit.id)

    def test_commission_analytics_agent(self):
        res = self.client.get('/api/users/commissions/analytics/?timeframe=1Y&currency=USD')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['role'], 'agent')
        self.assertEqual(res.data['timeframe'], '1Y')
        self.assertEqual(res.data['summary']['total_sales'], 450000.0)
        self.assertEqual(res.data['summary']['total_commission'], 45000.0)
        self.assertEqual(res.data['summary']['total_deals_won'], 1)
        self.assertEqual(len(res.data['chart_data']), 12)
        self.assertIn('total_sales_formatted', res.data['summary'])
        self.assertIn('total_commission_formatted', res.data['summary'])

    def test_commission_analytics_default_1d(self):
        res = self.client.get('/api/users/commissions/analytics/?currency=USD')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['timeframe'], '1D')
        self.assertEqual(len(res.data['chart_data']), 24)

    def test_commission_analytics_superadmin(self):
        super_user = User.objects.create_superuser(
            email='super_test@example.com',
            first_name='Super',
            last_name='Admin',
            password='password123',
        )
        self.client.force_authenticate(user=super_user)
        res = self.client.get('/api/users/commissions/analytics/?timeframe=1Y&currency=USD')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['role'], 'superadmin')
        self.assertEqual(res.data['summary']['total_sales'], 450000.0)
        self.assertIn('companies_breakdown', res.data)

    def test_latest_commissions_agent(self):
        res = self.client.get('/api/users/commissions/latest/?currency=USD')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['widget_title'], 'Commission Intelligence')
        self.assertEqual(res.data['widget_subtitle'], 'EARNINGS OUTLOOK')
        self.assertEqual(res.data['role'], 'agent')
        self.assertEqual(len(res.data['latest_commissions']), 1)
        item = res.data['latest_commissions'][0]
        self.assertEqual(item['amount'], 45000.0)
        self.assertEqual(item['developer'], 'Acme Dev')
        self.assertEqual(item['title'], 'Acme Dev')
        self.assertEqual(item['rate'], 10.0)
        self.assertEqual(item['rate_badge'], '10%')
        self.assertEqual(item['tier_label'], 'PREMIUM RATE')

    def test_latest_commissions_company_admin(self):
        admin_user = User.objects.create_user(
            email='admin_latest@example.com',
            first_name='Admin',
            last_name='User',
            password='password123',
            company=self.company,
            role=User.Role.COMPANY_ADMIN,
        )
        self.client.force_authenticate(user=admin_user)
        res = self.client.get('/api/users/commissions/latest/?currency=USD')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['role'], 'company_admin')
        self.assertEqual(len(res.data['latest_commissions']), 1)
        item = res.data['latest_commissions'][0]
        # Company commission is 90% = 405,000
        self.assertEqual(item['amount'], 405000.0)
        self.assertEqual(item['agent_name'], 'Agent Smith')

    def test_latest_commissions_superadmin(self):
        super_user = User.objects.create_superuser(
            email='super_latest@example.com',
            first_name='Super',
            last_name='Admin',
            password='password123',
        )
        self.client.force_authenticate(user=super_user)
        res = self.client.get('/api/users/commissions/latest/?currency=USD')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['role'], 'superadmin')
        self.assertEqual(len(res.data['latest_commissions']), 1)
        item = res.data['latest_commissions'][0]
        self.assertEqual(item['company_name'], 'Acme Corp')

    def test_unassigned_lead_excluded_from_commissions(self):
        # Create an unassigned won lead in the same company
        unassigned_lead = Lead.objects.create(
            name='Unassigned Lead',
            email='unassigned@example.com',
            project=self.project,
            created_by=self.agent,
            assigned_to=None,
            status=Lead.Status.CONVERTED_WON,
            estimated_budget=173000.0,
        )
        # Authenticate as company admin
        admin_user = User.objects.create_user(
            email='admin_unassigned@example.com',
            first_name='Admin',
            last_name='Check',
            password='password123',
            company=self.company,
            role=User.Role.COMPANY_ADMIN,
        )
        self.client.force_authenticate(user=admin_user)

        # Check latest commissions: must not include unassigned lead
        res_latest = self.client.get('/api/users/commissions/latest/?currency=USD')
        self.assertEqual(res_latest.status_code, 200)
        lead_ids = [c['lead_id'] for c in res_latest.data['latest_commissions']]
        self.assertNotIn(unassigned_lead.id, lead_ids)

        # Check analytics: total_deals_won should only be 1 (assigned lead), not 2
        res_analytics = self.client.get('/api/users/commissions/analytics/?timeframe=1Y&currency=USD')
        self.assertEqual(res_analytics.status_code, 200)
        self.assertEqual(res_analytics.data['summary']['total_deals_won'], 1)
        self.assertEqual(res_analytics.data['summary']['total_sales'], 450000.0)




