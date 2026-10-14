from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase
from rest_framework.test import APITestCase

from notifications.services import _has_proposal_images, _required_image_keys
from projects.models import Project, Unit
from users.models import Company

User = get_user_model()

PROJECTS_URL = '/api/projects/development_portfolio/'
UNITS_URL = '/api/projects/units/'


class ProjectFormFieldsTests(APITestCase):
    """What the Create Development form sends since its rework."""

    def setUp(self):
        self.company = Company.objects.create(name='Acme Realty', operating_countries=['UK'])
        self.admin = User.objects.create_user(
            email='admin@acme.test',
            password='password123',
            first_name='Admin',
            last_name='User',
            company=self.company,
            role=User.Role.COMPANY_ADMIN,
            countries=['UK'],
        )
        self.client.force_authenticate(user=self.admin)

    def test_an_industrial_project_for_rent_without_optional_fields(self):
        response = self.client.post(PROJECTS_URL, {
            'title': 'Thames Logistics Park',
            'location': 'Dartford',
            'project_type': 'industrial',
            'property_category': 'Warehouse',
            'purpose': 'rent',
            'starting_price': '950000',
        }, format='json')

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['project_type'], 'industrial')
        self.assertEqual(response.data['purpose'], 'rent')
        self.assertEqual(response.data['description'], '')
        self.assertEqual(response.data['developer'], '')
        self.assertIsNone(response.data['yield_percentage'])
        project = Project.objects.get(pk=response.data['id'])
        self.assertEqual(project.purpose, Project.Purpose.RENT)
        self.assertIsNone(project.yield_percentage)

    def test_the_form_sent_as_multipart_works_too(self):
        response = self.client.post(PROJECTS_URL, {
            'title': 'Marina Offices',
            'project_type': 'commercial',
            'purpose': 'sale',
            'yield_percentage': '',
        }, format='multipart')

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['purpose'], 'sale')
        self.assertIsNone(response.data['yield_percentage'])

    def test_clearing_and_setting_fields_on_edit(self):
        project = Project.objects.create(
            title='Riverside', description='Old text', location='London', developer='Acme',
            starting_price=100, yield_percentage=Decimal('6.50'),
            project_type=Project.ProjectType.RESIDENTIAL, created_by=self.admin,
        )

        response = self.client.patch(f'{PROJECTS_URL}{project.pk}/', {
            'yield_percentage': '',
            'description': '',
            'developer': '',
            'purpose': 'sale',
        }, format='json')

        self.assertEqual(response.status_code, 200, response.data)
        project.refresh_from_db()
        self.assertIsNone(project.yield_percentage)
        self.assertEqual(project.description, '')
        self.assertEqual(project.developer, '')
        self.assertEqual(project.purpose, 'sale')

        response = self.client.patch(f'{PROJECTS_URL}{project.pk}/', {'yield_percentage': '7.2'}, format='json')
        self.assertEqual(response.data['yield_percentage'], '7.20')

    def test_a_draft_from_the_form_keeps_an_empty_yield(self):
        # The form first creates a draft, then fills it in without a yield.
        draft = self.client.post(f'{PROJECTS_URL}init_project/', {}, format='json')
        self.assertEqual(draft.status_code, 201, draft.data)

        response = self.client.patch(f"{PROJECTS_URL}{draft.data['id']}/", {
            'title': 'Harbour View', 'location': 'Leeds', 'starting_price': '300000',
            'project_status': 'live',
        }, format='json')

        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(response.data['yield_percentage'])

    def test_unknown_type_or_purpose_is_refused(self):
        bad_type = self.client.post(PROJECTS_URL, {'title': 'X', 'project_type': 'agricultural'}, format='json')
        bad_purpose = self.client.post(PROJECTS_URL, {'title': 'X', 'purpose': 'lease'}, format='json')

        self.assertEqual(bad_type.status_code, 400)
        self.assertIn('project_type', bad_type.data)
        self.assertEqual(bad_purpose.status_code, 400)
        self.assertIn('purpose', bad_purpose.data)

    def test_projects_can_be_filtered_by_purpose(self):
        for title, purpose in (('For sale', 'sale'), ('For rent', 'rent')):
            self.client.post(PROJECTS_URL, {'title': title, 'purpose': purpose}, format='json')

        response = self.client.get(PROJECTS_URL, {'purpose': 'rent'})

        self.assertEqual([p['title'] for p in response.data['results']], ['For rent'])

    def test_units_keep_their_bathrooms(self):
        created = self.client.post(PROJECTS_URL, {
            'title': 'Riverside Homes',
            'units_to_add': [{'label': 'A-101', 'category': '2 Bed', 'bathrooms': '2', 'list_price': '250000', 'status': 'available'}],
        }, format='json')
        self.assertEqual(created.status_code, 201, created.data)
        project_id = created.data['id']

        added = self.client.post(UNITS_URL, {
            'project': project_id, 'label': 'A-102', 'category': '10+ Bed', 'bathrooms': '6+',
            'list_price': '900000', 'status': 'available',
        }, format='json')
        self.assertEqual(added.status_code, 201, added.data)
        self.assertEqual(added.data['bathrooms'], '6+')

        edited = self.client.patch(f"{UNITS_URL}{added.data['id']}/", {'bathrooms': '5'}, format='json')
        self.assertEqual(edited.status_code, 200, edited.data)

        units = {u.label: u.bathrooms for u in Unit.objects.filter(project_id=project_id)}
        self.assertEqual(units, {'A-101': '2', 'A-102': '5'})
        detail = self.client.get(f'{PROJECTS_URL}{project_id}/')
        self.assertEqual(sorted(u['bathrooms'] for u in detail.data['units']), ['2', '5'])


class ProposalImageRulesTests(SimpleTestCase):
    def test_bedroom_types_need_kitchen_bedroom_and_bath_photos(self):
        self.assertEqual(
            _required_image_keys('1_bed'),
            ['1_bed_kitchen_dining', '1_bed_master_bedroom', '1_bed_bath'],
        )
        self.assertEqual(_required_image_keys('studio')[-1], 'studio_bath')
        # The form cannot add photos for "10+ Bed", so none are asked for.
        self.assertEqual(_required_image_keys('10+_bed'), [])

    def test_other_unit_types_need_none(self):
        for prefix in ('office', 'office_suite', 'warehouse', 'recording_studio', ''):
            self.assertEqual(_required_image_keys(prefix), [], prefix)

    def test_an_older_sample_layout_photo_counts_as_the_bath_photo(self):
        images = {'2_bed_sample_layout': ['a.jpg'], '3_bed_bath': ['b.jpg']}

        self.assertTrue(_has_proposal_images(images, '2_bed_bath'))
        self.assertTrue(_has_proposal_images(images, '3_bed_bath'))
        self.assertFalse(_has_proposal_images(images, '1_bed_bath'))
        self.assertFalse(_has_proposal_images(images, '2_bed_kitchen_dining'))
