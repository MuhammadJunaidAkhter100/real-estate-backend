from datetime import timedelta
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase
from projects.models import Project, Promotion, Unit
from users.models import Company

User = get_user_model()


PROJECTS_URL = '/api/projects/development_portfolio/'
UNITS_URL = '/api/projects/units/'
PROMOTIONS_URL = '/api/projects/promotions/'


def make_user(email, company, role):
    return User.objects.create_user(
        email=email,
        password='password123',
        first_name=email.split('@')[0],
        last_name='User',
        company=company,
        role=role,
        countries=[],
    )


def make_project(title, created_by, project_status=Project.ProjectStatus.LIVE):
    return Project.objects.create(
        title=title,
        description='',
        location='',
        developer='',
        starting_price=0,
        yield_percentage=0,
        project_type=Project.ProjectType.RESIDENTIAL,
        project_status=project_status,
        created_by=created_by,
    )


class CompanyProjectCreationTests(APITestCase):
    def test_superadmin_cannot_create_project(self):
        superadmin = User.objects.create_superuser(
            email='project-superadmin@example.com',
            password='password123',
            first_name='Project',
            last_name='Superadmin',
        )
        self.client.force_authenticate(user=superadmin)

        response = self.client.post(
            PROJECTS_URL,
            {'title': 'Superadmin Draft'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, response.data)
        self.assertFalse(Project.objects.filter(title='Superadmin Draft').exists())

    def test_company_admin_can_create_title_only_project_and_only_sees_own_company_projects(self):
        own_company = Company.objects.create(name='Own Company')
        other_company = Company.objects.create(name='Other Company')
        company_admin = User.objects.create_user(
            email='company-admin@example.com',
            password='password123',
            first_name='Company',
            last_name='Admin',
            company=own_company,
            role=User.Role.COMPANY_ADMIN,
            countries=[],
            current_country='UK',
        )
        other_admin = User.objects.create_user(
            email='other-admin@example.com',
            password='password123',
            first_name='Other',
            last_name='Admin',
            company=other_company,
            role=User.Role.COMPANY_ADMIN,
            countries=[],
        )

        self.client.force_authenticate(user=company_admin)
        create_response = self.client.post(
            '/api/projects/development_portfolio/',
            {'title': 'My Draft Project'},
            format='json',
        )

        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED, create_response.data)
        self.assertEqual(create_response.data['title'], 'My Draft Project')
        self.assertEqual(create_response.data['project_status'], Project.ProjectStatus.DRAFT)
        self.assertEqual(create_response.data['starting_price'], '0.00')
        self.assertEqual(create_response.data['description'], '')

        Project.objects.create(
            title='Own Company UAE Project',
            description='',
            location='',
            developer='',
            associated_country='UAE',
            starting_price=0,
            yield_percentage=0,
            project_type=Project.ProjectType.RESIDENTIAL,
            created_by=company_admin,
        )
        Project.objects.create(
            title='Other Company Project',
            description='',
            location='',
            developer='',
            starting_price=0,
            yield_percentage=0,
            project_type=Project.ProjectType.RESIDENTIAL,
            created_by=other_admin,
        )

        list_response = self.client.get('/api/projects/development_portfolio/')
        self.assertEqual(list_response.status_code, status.HTTP_200_OK)
        listed_titles = [project['title'] for project in list_response.data['results']]
        self.assertCountEqual(
            listed_titles,
            ['My Draft Project', 'Own Company UAE Project'],
        )


class CompanyScopedProjectTests(APITestCase):
    """Projects belong to companies: each company sees and edits only its own,
    and only its admins may write."""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name='Alpha')
        cls.other_company = Company.objects.create(name='Beta')
        cls.admin = make_user('admin@alpha.test', cls.company, User.Role.COMPANY_ADMIN)
        cls.manager = make_user('manager@alpha.test', cls.company, User.Role.TEAM_MANAGER)
        cls.agent = make_user('agent@alpha.test', cls.company, User.Role.AGENT)
        cls.other_admin = make_user('admin@beta.test', cls.other_company, User.Role.COMPANY_ADMIN)
        cls.other_agent = make_user('agent@beta.test', cls.other_company, User.Role.AGENT)
        cls.superadmin = User.objects.create_superuser(
            email='root@platform.test',
            password='password123',
            first_name='Root',
            last_name='Admin',
        )
        cls.project = make_project('Alpha Tower', cls.admin)
        cls.other_project = make_project('Beta Tower', cls.other_admin)

    def detail_url(self, project):
        return f'{PROJECTS_URL}{project.pk}/'

    def listed_titles(self, user):
        self.client.force_authenticate(user=user)
        response = self.client.get(PROJECTS_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return [item['title'] for item in response.data['results']]

    # ── Reads ────────────────────────────────────────────────────────────────

    def test_every_company_role_lists_only_its_own_company_projects(self):
        for user in (self.admin, self.manager, self.agent):
            with self.subTest(role=user.role):
                self.assertEqual(self.listed_titles(user), ['Alpha Tower'])

    def test_other_company_project_is_not_found(self):
        for user in (self.admin, self.manager, self.agent):
            with self.subTest(role=user.role):
                self.client.force_authenticate(user=user)
                response = self.client.get(self.detail_url(self.other_project))
                self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_manager_and_agent_can_read_their_company_project(self):
        for user in (self.manager, self.agent):
            with self.subTest(role=user.role):
                self.client.force_authenticate(user=user)
                response = self.client.get(self.detail_url(self.project))
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(response.data['title'], 'Alpha Tower')

    def test_superadmin_still_reads_every_project(self):
        self.assertCountEqual(
            self.listed_titles(self.superadmin),
            ['Alpha Tower', 'Beta Tower'],
        )

    # ── Writes ───────────────────────────────────────────────────────────────

    def test_manager_agent_and_superadmin_get_403_on_every_write(self):
        image = SimpleUploadedFile('a.png', b'not-really-a-png', content_type='image/png')
        writes = [
            ('post', PROJECTS_URL, {'title': 'Nope'}),
            ('patch', self.detail_url(self.project), {'title': 'Renamed'}),
            ('delete', self.detail_url(self.project), None),
            ('post', f'{PROJECTS_URL}init_project/', {}),
            ('post', f'{self.detail_url(self.project)}upload_images/', {'image': image}),
            (
                'post',
                f'{PROJECTS_URL}upload_proposal_images/',
                {'project_id': self.project.pk, 'exterior': image},
            ),
            ('delete', f'{self.detail_url(self.project)}remove_image/?index=0', None),
            ('post', f'{self.detail_url(self.project)}toggle_visibility/', {}),
        ]
        for user in (self.manager, self.agent, self.superadmin):
            self.client.force_authenticate(user=user)
            for method, url, data in writes:
                with self.subTest(role=user.role, method=method, url=url):
                    image.seek(0)
                    response = getattr(self.client, method)(url, data, format='multipart')
                    self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        self.project.refresh_from_db()
        self.assertEqual(self.project.title, 'Alpha Tower')
        self.assertEqual(Project.objects.count(), 2)

    def test_company_admin_can_create_edit_and_delete_own_project(self):
        self.client.force_authenticate(user=self.admin)

        created = self.client.post(PROJECTS_URL, {'title': 'Alpha Gardens'}, format='json')
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)

        edited = self.client.patch(
            self.detail_url(self.project), {'title': 'Alpha Tower II'}, format='json',
        )
        self.assertEqual(edited.status_code, status.HTTP_200_OK, edited.data)
        self.assertEqual(edited.data['title'], 'Alpha Tower II')

        deleted = self.client.delete(self.detail_url(self.project))
        self.assertEqual(deleted.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Project.objects.filter(pk=self.project.pk).exists())

    def test_company_admin_cannot_touch_another_company_project(self):
        self.client.force_authenticate(user=self.admin)
        image = SimpleUploadedFile('a.png', b'not-really-a-png', content_type='image/png')

        patched = self.client.patch(
            self.detail_url(self.other_project), {'title': 'Hijacked'}, format='json',
        )
        self.assertEqual(patched.status_code, status.HTTP_404_NOT_FOUND)

        deleted = self.client.delete(self.detail_url(self.other_project))
        self.assertEqual(deleted.status_code, status.HTTP_404_NOT_FOUND)

        uploaded = self.client.post(
            f'{PROJECTS_URL}upload_proposal_images/',
            {'project_id': self.other_project.pk, 'exterior': image},
            format='multipart',
        )
        self.assertEqual(uploaded.status_code, status.HTTP_404_NOT_FOUND)

        self.other_project.refresh_from_db()
        self.assertEqual(self.other_project.title, 'Beta Tower')
        self.assertEqual(self.other_project.proposal_images, {})

    def test_agents_from_another_company_cannot_be_assigned(self):
        self.client.force_authenticate(user=self.admin)
        url = f'{self.detail_url(self.project)}assign_agents/'

        rejected = self.client.post(
            url,
            {'assignments': [{'agent': self.other_agent.pk, 'agent_split': '60.00', 'company_split': '40.00'}]},
            format='json',
        )
        self.assertEqual(rejected.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(self.project.agent_assignments.exists())

        accepted = self.client.post(
            url,
            {'assignments': [{'agent': self.agent.pk, 'agent_split': '60.00', 'company_split': '40.00'}]},
            format='json',
        )
        self.assertEqual(accepted.status_code, status.HTTP_200_OK, accepted.data)
        self.assertEqual(self.project.agent_assignments.count(), 1)

    # ── Units ────────────────────────────────────────────────────────────────

    def test_units_follow_the_same_rules(self):
        own_unit = Unit.objects.create(project=self.project, label='A-1', list_price=100)
        Unit.objects.create(project=self.other_project, label='B-1', list_price=100)
        payload = {'project': self.project.pk, 'label': 'A-2', 'list_price': '200', 'status': 'available'}

        for user in (self.manager, self.agent):
            with self.subTest(role=user.role):
                self.client.force_authenticate(user=user)
                listed = self.client.get(UNITS_URL)
                self.assertEqual(listed.status_code, status.HTTP_200_OK)
                self.assertEqual([unit['label'] for unit in listed.data], ['A-1'])
                self.assertEqual(
                    self.client.post(UNITS_URL, payload, format='json').status_code,
                    status.HTTP_403_FORBIDDEN,
                )
                self.assertEqual(
                    self.client.delete(f'{UNITS_URL}{own_unit.pk}/').status_code,
                    status.HTTP_403_FORBIDDEN,
                )

        self.client.force_authenticate(user=self.admin)
        created = self.client.post(UNITS_URL, payload, format='json')
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)

        foreign = self.client.post(
            UNITS_URL, {**payload, 'project': self.other_project.pk}, format='json',
        )
        self.assertEqual(foreign.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.other_project.units.count(), 1)

        csv_file = SimpleUploadedFile('units.csv', b'Unit Label/Name\n', content_type='text/csv')
        imported = self.client.post(
            f'{UNITS_URL}import-from-csv/',
            {'project_id': self.other_project.pk, 'file': csv_file},
            format='multipart',
        )
        self.assertEqual(imported.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(imported.data['detail'], 'Project not found.')

    # ── Promotions ───────────────────────────────────────────────────────────

    def test_promotions_follow_the_same_rules(self):
        now = timezone.now()
        dates = {
            'start_date': (now + timedelta(days=1)).isoformat(),
            'end_date': (now + timedelta(days=3)).isoformat(),
        }
        own = Promotion.objects.create(
            project=self.project, title='Alpha Offer', discount=10,
            start_date=now + timedelta(days=1), end_date=now + timedelta(days=3),
        )
        foreign = Promotion.objects.create(
            project=self.other_project, title='Beta Offer', discount=10,
            start_date=now + timedelta(days=1), end_date=now + timedelta(days=3),
        )
        payload = {'project': self.project.pk, 'promotion_title': 'New', 'discount_percentage': 5, **dates}

        for user in (self.admin, self.manager, self.agent):
            with self.subTest(role=user.role):
                self.client.force_authenticate(user=user)
                listed = self.client.get(PROMOTIONS_URL)
                self.assertEqual(listed.status_code, status.HTTP_200_OK)
                self.assertEqual(
                    [promo['promotion_title'] for promo in listed.data['results']],
                    ['Alpha Offer'],
                )
                self.assertEqual(listed.data['stats']['total_promotions'], 1)

        for user in (self.manager, self.agent, self.superadmin):
            with self.subTest(role=user.role):
                self.client.force_authenticate(user=user)
                self.assertEqual(
                    self.client.post(PROMOTIONS_URL, payload, format='json').status_code,
                    status.HTTP_403_FORBIDDEN,
                )
                self.assertEqual(
                    self.client.delete(f'{PROMOTIONS_URL}{own.pk}/').status_code,
                    status.HTTP_403_FORBIDDEN,
                )

        self.client.force_authenticate(user=self.admin)
        self.assertEqual(
            self.client.post(PROMOTIONS_URL, payload, format='json').status_code,
            status.HTTP_201_CREATED,
        )
        self.assertEqual(
            self.client.post(
                PROMOTIONS_URL, {**payload, 'project': self.other_project.pk}, format='json',
            ).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(
            self.client.patch(
                f'{PROMOTIONS_URL}{foreign.pk}/', {'discount_percentage': 90}, format='json',
            ).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertEqual(
            self.client.delete(f'{PROMOTIONS_URL}{foreign.pk}/').status_code,
            status.HTTP_404_NOT_FOUND,
        )
        foreign.refresh_from_db()
        self.assertEqual(foreign.discount, 10)

    # ── Projects handed out by the users app ─────────────────────────────────

    def test_user_assignment_list_and_dashboard_only_cover_own_company(self):
        self.client.force_authenticate(user=self.admin)

        assignable = self.client.get(f'/api/users/manage_users/{self.agent.pk}/projects/')
        self.assertEqual(assignable.status_code, status.HTTP_200_OK, assignable.data)
        self.assertEqual(
            [item['title'] for item in assignable.data['results']],
            ['Alpha Tower'],
        )

        foreign = self.client.get(
            f'/api/users/manage_users/{self.agent.pk}/projects/{self.other_project.pk}/',
        )
        self.assertEqual(foreign.status_code, status.HTTP_404_NOT_FOUND)

        summary = self.client.get('/api/users/dashboard/summary/')
        self.assertEqual(summary.status_code, status.HTTP_200_OK, summary.data)
        self.assertEqual(summary.data['total_active_projects'], 1)


class PromotionTests(APITestCase):

    def setUp(self):
        # Promotions are managed by the company that owns the project.
        self.company = Company.objects.create(name='Promo Company')
        self.user = make_user('testadmin@example.com', self.company, User.Role.COMPANY_ADMIN)
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
            created_by=self.user,
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

