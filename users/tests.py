import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.core import mail
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from notifications.models import Notification
from users.models import Company, Lead, Task
from users.tasks import check_and_update_task_expirations

User = get_user_model()


class LoginResponseTestCase(TestCase):
    def test_login_omits_company_website_data_from_response_and_tokens(self):
        from django.urls import reverse
        from rest_framework.test import APIClient
        from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

        company = Company.objects.create(
            name='Login Test Company',
            website_data={
                'projects': [{'name': 'Private Project Details'}],
                'total_projects': 1,
            },
        )
        user = User.objects.create_user(
            email='login-test@example.com',
            first_name='Login',
            last_name='Tester',
            password='password123',
            company=company,
            role=User.Role.COMPANY_ADMIN,
            status=User.Status.ACTIVE,
        )

        response = APIClient().post(
            reverse('token_obtain_pair'),
            {'email': user.email, 'password': 'password123'},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn('website_data', response.data['user']['company'])
        self.assertNotIn('website_data', AccessToken(response.data['access'])['user']['company'])
        self.assertNotIn('website_data', RefreshToken(response.data['refresh'])['user']['company'])

    def test_superadmin_login_resets_country_selection_to_all(self):
        from django.urls import reverse
        from rest_framework.test import APIClient

        superadmin = User.objects.create_superuser(
            email='login-superadmin@example.com',
            password='password123',
            first_name='Login',
            last_name='Superadmin',
        )
        superadmin.current_country = 'UAE'
        superadmin.save(update_fields=['current_country'])

        response = APIClient().post(
            reverse('token_obtain_pair'),
            {'email': superadmin.email, 'password': 'password123'},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['user']['current_country'], '')
        superadmin.refresh_from_db()
        self.assertEqual(superadmin.current_country, '')


class CompanyCreationTestCase(TestCase):
    def test_reuses_existing_admin_for_new_company(self):
        from users.serializers import CompanyCreateSerializer
        from users.serializers import CompanyDetailSerializer

        existing_company = Company.objects.create(name='Existing Company', website='https://example.com')
        existing_admin = User.objects.create_user(
            email='admin@example.com',
            first_name='Existing',
            last_name='Admin',
            password='password123',
            company=existing_company,
            role=User.Role.COMPANY_ADMIN,
            status=User.Status.ACTIVE,
            countries=['UAE'],
        )

        payload = {
            'name': 'New Company',
            'phone': '+97144398500',
            'operating_countries': ['UAE'],
            'website': 'https://legacyinvestmentconsultancy.com/',
            'admin': {
                'email': 'admin@example.com',
                'first_name': 'Existing',
                'last_name': 'Admin',
            },
        }

        serializer = CompanyCreateSerializer(data=payload)
        self.assertTrue(serializer.is_valid(), serializer.errors)

        company = serializer.save()

        self.assertEqual(User.objects.filter(email='admin@example.com').count(), 1)
        self.assertEqual(company.phone, '+97144398500')
        self.assertEqual(CompanyDetailSerializer(company).data['phone'], '+97144398500')
        self.assertEqual(company.users.get(email='admin@example.com').company_id, company.id)
        self.assertEqual(company.users.get(email='admin@example.com').role, User.Role.COMPANY_ADMIN)

    def test_manage_users_serializer_does_not_expose_company_website(self):
        from users.serializers import UserManagementSerializer

        serializer = UserManagementSerializer()

        self.assertNotIn('website', serializer.fields)
        self.assertNotIn('company_website', serializer.fields)


class CurrentCountrySelectionTestCase(TestCase):
    def test_superadmin_can_set_iso_country_code_outside_legacy_assignment(self):
        from users.serializers import MeUpdateSerializer

        superadmin = User.objects.create_superuser(
            email='country-root@example.com',
            password='password123',
            first_name='Country',
            last_name='Admin',
        )
        superadmin.countries = ['UAE', 'UK', 'Oman', 'Saudi Arabia', 'Indonesia']
        superadmin.save(update_fields=['countries'])

        serializer = MeUpdateSerializer(
            superadmin,
            data={'current_country': 'ZW'},
            partial=True,
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        updated_user = serializer.save()
        self.assertEqual(updated_user.current_country, 'Zimbabwe')

    def test_regular_user_remains_limited_to_assigned_countries(self):
        from users.serializers import MeUpdateSerializer

        company = Company.objects.create(name='Country Restricted Company')
        user = User.objects.create_user(
            email='country-agent@example.com',
            first_name='Country',
            last_name='Agent',
            password='password123',
            company=company,
            countries=['UK'],
            role=User.Role.COMPANY_ADMIN,
        )

        serializer = MeUpdateSerializer(
            user,
            data={'current_country': 'ZW'},
            partial=True,
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn('current_country', serializer.errors)

    def test_me_profile_serializer_accepts_and_returns_social_links(self):
        from users.serializers import MeUpdateSerializer, UserDetailSerializer

        company = Company.objects.create(name='Social Links Company')
        user = User.objects.create_user(
            email='social-links@example.com',
            first_name='Social',
            last_name='Links',
            password='password123',
            company=company,
            countries=['UAE'],
            role=User.Role.COMPANY_ADMIN,
        )

        payload = {
            'facebook': 'https://facebook.com/yourpage',
            'instagram': 'https://instagram.com/yourhandle',
            'linkedin': 'https://linkedin.com/in/yourprofile',
            'twitter': 'https://x.com/yourhandle',
            'website': 'https://yourdomain.com',
        }

        serializer = MeUpdateSerializer(user, data=payload, partial=True)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        updated_user = serializer.save()

        self.assertEqual(updated_user.facebook, payload['facebook'])
        self.assertEqual(updated_user.instagram, payload['instagram'])
        self.assertEqual(updated_user.linkedin, payload['linkedin'])
        self.assertEqual(updated_user.twitter, payload['twitter'])
        self.assertEqual(updated_user.website, payload['website'])

        rendered = UserDetailSerializer(updated_user).data
        for field_name, field_value in payload.items():
            self.assertEqual(rendered[field_name], field_value)


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
        unassigned_lead = Lead.objects.create(
            name='Unassigned Lead',
            email='unassigned@example.com',
            project=self.project,
            created_by=self.agent,
            assigned_to=None,
            status=Lead.Status.CONVERTED_WON,
            estimated_budget=173000.0,
        )
        admin_user = User.objects.create_user(
            email='admin_unassigned@example.com',
            first_name='Admin',
            last_name='Check',
            password='password123',
            company=self.company,
            role=User.Role.COMPANY_ADMIN,
        )
        self.client.force_authenticate(user=admin_user)

        res_latest = self.client.get('/api/users/commissions/latest/?currency=USD')
        self.assertEqual(res_latest.status_code, 200)
        lead_ids = [c['lead_id'] for c in res_latest.data['latest_commissions']]
        self.assertNotIn(unassigned_lead.id, lead_ids)

        res_analytics = self.client.get('/api/users/commissions/analytics/?timeframe=1Y&currency=USD')
        self.assertEqual(res_analytics.status_code, 200)
        self.assertEqual(res_analytics.data['summary']['total_deals_won'], 1)
        self.assertEqual(res_analytics.data['summary']['total_sales'], 450000.0)


class WebsiteScraperTestCase(TestCase):
    def test_extract_real_estate_projects_from_html(self):
        from users.website_scraper import extract_projects_from_html

        html = '''
        <html>
            <body>
                <div class="project-card">
                    <a href="/projects/harbor-view">
                        <img src="https://example.com/images/harbor.jpg" alt="Harbor View" />
                    </a>
                    <h3>Harbor View</h3>
                    <p>Waterfront living in a prime city location.</p>
                </div>
                <div class="project-card">
                    <a href="https://example.com/projects/skyline-residences">
                        <img src="https://example.com/images/skyline.jpg" alt="Skyline Residences" />
                    </a>
                    <h3>Skyline Residences</h3>
                    <p>Luxury residences with panoramic views.</p>
                </div>
            </body>
        </html>
        '''

        project_cards = extract_projects_from_html(html, 'https://example.com')

        self.assertEqual(len(project_cards), 2)
        self.assertEqual(project_cards[0]['title'], 'Harbor View')
        self.assertEqual(project_cards[0]['url'], 'https://example.com/projects/harbor-view')
        self.assertEqual(project_cards[0]['image'], 'https://example.com/images/harbor.jpg')
        self.assertIn('Waterfront', project_cards[0]['description'])

    def test_normalize_website_url(self):
        from users.website_scraper import normalize_website_url

        self.assertEqual(
            normalize_website_url('https://legacyinvestmentconsultancy.com'),
            'https://legacyinvestmentconsultancy.com'
        )
        self.assertEqual(
            normalize_website_url('legacyinvestmentconsultancy.com'),
            'https://legacyinvestmentconsultancy.com'
        )

    @patch('users.website_scraper.OpenAI')
    def test_scrape_company_website_uses_ai_for_real_estate_pages(self, mock_openai):
        from users.website_scraper import scrape_company_website

        class DummyResponse:
            def __init__(self, payload):
                self.choices = [type('Choice', (), {'message': type('Message', (), {'content': json.dumps(payload)})()})()]

        mock_openai.return_value.chat.completions.create.return_value = DummyResponse({
            'is_real_estate_project': True,
            'title': 'Harbor View',
            'developer': 'Harbor Developments',
            'location': 'Dubai',
            'description': 'Luxury waterfront apartments',
            'status': 'Off plan',
            'price_from': 'AED 1.2M',
            'bedrooms': '2 - 3 bedrooms',
            'image_url': 'https://example.com/harbor.jpg',
            'project_type': 'Apartments',
        })

        with patch('users.website_scraper.requests.get') as mock_get:
            mock_get.return_value.text = '''
                <html>
                    <body>
                        <h1>Harbor View</h1>
                        <p>Luxury apartments and waterfront living in Dubai.</p>
                        <a href="/projects/harbor-view">View Details</a>
                    </body>
                </html>
            '''
            mock_get.return_value.raise_for_status.return_value = None

            result = scrape_company_website('https://example.com')

        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['projects'][0]['title'], 'Harbor View')
        self.assertEqual(result['projects'][0]['ai_structured_data']['developer'], 'Harbor Developments')

    @patch('users.website_scraper.OpenAI')
    def test_scrape_company_website_uses_ai_to_select_homepage_project_cards(self, mock_openai):
        from users.website_scraper import scrape_company_website

        class DummyResponse:
            def __init__(self, payload):
                self.choices = [type('Choice', (), {'message': type('Message', (), {'content': json.dumps(payload)})()})()]

        mock_openai.return_value.chat.completions.create.return_value = DummyResponse({
            'selected': [1]
        })

        homepage_html = '''
            <html>
                <body>
                    <div class="project-card">
                        <a href="/projects/alpha">Alpha Residences</a>
                    </div>
                    <div class="project-card">
                        <a href="/projects/beta">Beta Blog</a>
                    </div>
                    <div class="project-card">
                        <a href="/projects/gamma">Gamma Villas</a>
                    </div>
                </body>
            </html>
        '''

        detail_html = '''
            <html>
                <body>
                    <h1>Beta Blog</h1>
                    <p>Luxury villa community with apartments and residences for sale in Dubai.</p>
                </body>
            </html>
        '''

        with patch('users.website_scraper.requests.get') as mock_get:
            mock_get.side_effect = [
                MagicMock(text=homepage_html, raise_for_status=lambda: None),
                MagicMock(text=detail_html, raise_for_status=lambda: None),
            ]

            result = scrape_company_website('https://example.com')

        self.assertEqual(len(result['projects']), 1)
        self.assertEqual(result['projects'][0]['title'], 'Beta Blog')
        homepage_prompt = mock_openai.return_value.chat.completions.create.call_args_list[0].kwargs['messages'][1]['content']
        self.assertIn('Alpha Residences [URL: https://example.com/projects/alpha]', homepage_prompt)

    def test_scrape_company_website_returns_empty_when_no_project_cards_are_found(self):
        from users.website_scraper import scrape_company_website

        with patch('users.website_scraper.requests.get') as mock_get:
            mock_get.return_value.text = '<html><body><h1>Welcome to Our Blog</h1><p>We write about travel and food.</p></body></html>'
            mock_get.return_value.raise_for_status.return_value = None

            result = scrape_company_website('https://example.com')

        self.assertEqual(result['status'], 'empty')
        self.assertEqual(result['projects'], [])


class ScrapedProjectCreationTestCase(TestCase):
    @patch('chatbot.tasks.import_company_website_knowledge.delay')
    @patch('projects.tasks.import_scraped_proposal_image_task.delay')
    @patch('users.website_scraper.scrape_company_website')
    def test_scrape_task_creates_company_visible_draft_projects_idempotently(self, mock_scrape, mock_image_task, mock_kb_import):
        from projects.models import Project
        from users.tasks import scrape_company_website_task

        company = Company.objects.create(
            name='Legacy Investment Consultancy',
            website='https://legacyinvestmentconsultancy.com/',
        )
        admin = User.objects.create_user(
            email='legacy-admin@example.com',
            first_name='Legacy',
            last_name='Admin',
            password='password123',
            company=company,
            role=User.Role.COMPANY_ADMIN,
            status=User.Status.ACTIVE,
        )
        mock_scrape.return_value = {
            'website': company.website,
            'status': 'completed',
            'projects': [
                {
                    'name': 'London House',
                    'detail_page_url': 'https://legacyinvestmentconsultancy.com/developments/london-house/',
                    'image_url': 'https://legacyinvestmentconsultancy.com/img/london-house.jpg',
                },
            ],
            'total_projects': 1,
        }
        mock_image_task.return_value.id = 'image-import-task-id'

        first_report = scrape_company_website_task.run(company.pk, company.website)
        second_report = scrape_company_website_task.run(company.pk, company.website)

        self.assertEqual(Project.objects.filter(source_url__contains='/london-house/').count(), 1)
        project = Project.objects.get(source_url__contains='/london-house/')
        self.assertEqual(project.title, 'London House')
        self.assertEqual(project.image, [])
        self.assertEqual(project.project_status, Project.ProjectStatus.DRAFT)
        self.assertEqual(project.created_by, admin)
        self.assertTrue(project.visible_to_companies.filter(pk=company.pk).exists())
        self.assertEqual(first_report['project_ids'], [project.pk])
        self.assertEqual(second_report['project_ids'], [project.pk])
        self.assertEqual(mock_image_task.call_count, 2)
        mock_image_task.assert_called_with(
            project.pk,
            'https://legacyinvestmentconsultancy.com/img/london-house.jpg',
            'exterior',
        )

    @patch('projects.tasks.default_storage.exists', return_value=False)
    @patch('projects.tasks.default_storage.save')
    @patch('projects.tasks.requests.get')
    def test_scraped_image_is_saved_as_exterior_proposal_image(self, mock_get, mock_storage_save, mock_exists):
        import hashlib

        from projects.models import Project
        from projects.tasks import import_scraped_proposal_image_task

        company = Company.objects.create(name='Image Import Company')
        creator = User.objects.create_user(
            email='image-admin@example.com',
            first_name='Image',
            last_name='Admin',
            password='password123',
            company=company,
            role=User.Role.COMPANY_ADMIN,
        )
        project = Project.objects.create(
            title='London House',
            description='',
            location='',
            developer='',
            starting_price=0,
            yield_percentage=0,
            project_type=Project.ProjectType.RESIDENTIAL,
            created_by=creator,
        )
        image_url = 'https://example.com/london-house.jpg'
        image_hash = hashlib.sha256(image_url.encode('utf-8')).hexdigest()
        expected_path = (
            f'projects/{project.pk}_london_house/proposal_images/exterior/'
            f'{image_hash}.jpg'
        )
        mock_storage_save.return_value = expected_path
        mock_get.return_value.headers = {'Content-Type': 'image/jpeg'}
        mock_get.return_value.content = b'image-bytes'
        mock_get.return_value.raise_for_status.return_value = None

        result = import_scraped_proposal_image_task.run(project.pk, image_url)
        retry_result = import_scraped_proposal_image_task.run(project.pk, image_url)

        project.refresh_from_db()
        self.assertTrue(result['success'])
        self.assertTrue(retry_result['success'])
        self.assertEqual(retry_result['uploaded_count'], 0)
        self.assertEqual(project.image, [])
        self.assertEqual(
            project.proposal_images['exterior'],
            [expected_path],
        )
        mock_exists.assert_called_once()
        mock_get.assert_called_once()
        mock_storage_save.assert_called_once()


class CountryCurrencyTestCase(TestCase):
    def test_every_country_has_a_currency(self):
        from api.constants import COUNTRY_CURRENCY_MAP, COUNTRY_NAMES

        for name in COUNTRY_NAMES:
            currency = COUNTRY_CURRENCY_MAP.get(name, '')
            self.assertRegex(currency, r'^[A-Z]{3}$', name)

    def test_currencies_match_the_country(self):
        from api.constants import COUNTRY_CURRENCY_MAP

        expected = {
            'Andorra': 'EUR',
            'Pakistan': 'PKR',
            'United Kingdom': 'GBP',
            'UK': 'GBP',
            'United Arab Emirates': 'AED',
            'UAE': 'AED',
            'Oman': 'OMR',
            'Saudi Arabia': 'SAR',
            'Indonesia': 'USD',
        }
        for country, currency in expected.items():
            self.assertEqual(COUNTRY_CURRENCY_MAP.get(country), currency, country)

    def test_commissions_use_the_users_country_currency(self):
        from users.commission_service import CommissionAnalyticsService

        company = Company.objects.create(name='Andorra Homes', operating_countries=['Andorra'])
        admin = User.objects.create_user(
            email='admin@andorra.test',
            first_name='Andorra',
            last_name='Admin',
            password='password123',
            company=company,
            role=User.Role.COMPANY_ADMIN,
            status=User.Status.ACTIVE,
            countries=['Andorra'],
        )
        admin.current_country = 'Andorra'

        service = CommissionAnalyticsService(admin)

        self.assertEqual(service.currency, 'EUR')
        self.assertEqual(service.currency_symbol, '€')

    def test_superadmin_commissions_are_all_countries_by_default_and_filter_by_country(self):
        from calling_agent.tests.factories import create_project, create_user
        from users.commission_service import CommissionAnalyticsService
        from users.models import Lead

        company = Company.objects.create(name='Sales Company')
        agent = create_user(company=company, email='sales-agent@example.com')
        uae_project = create_project(user=agent, title='UAE Sale')
        uae_project.associated_country = 'UAE'
        uae_project.save(update_fields=['associated_country'])
        Lead.objects.create(
            name='UAE Won Lead',
            created_by=agent,
            assigned_to=agent,
            project=uae_project,
            status=Lead.Status.CONVERTED_WON,
        )

        uk_project = create_project(user=agent, title='UK Sale')
        uk_project.associated_country = 'UK'
        uk_project.save(update_fields=['associated_country'])
        Lead.objects.create(
            name='UK Won Lead',
            created_by=agent,
            assigned_to=agent,
            project=uk_project,
            status=Lead.Status.CONVERTED_WON,
        )

        superadmin = User.objects.create_superuser(
            email='sales-superadmin@example.com',
            first_name='Sales',
            last_name='Superadmin',
            password='password123',
        )
        service = CommissionAnalyticsService(superadmin)
        self.assertEqual(service.get_scoped_leads_queryset().count(), 2)

        superadmin.current_country = 'UAE'
        service = CommissionAnalyticsService(superadmin)
        self.assertEqual(service.get_scoped_leads_queryset().count(), 1)
