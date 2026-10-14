from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from users.models import Company

User = get_user_model()

ALL_URL = '/api/users/countries/'
COMPANY_URL = '/api/users/company/countries/'


def make_user(email, company, role, countries, current_country=''):
    return User.objects.create_user(
        email=email,
        first_name='Test',
        last_name='User',
        password='password123',
        company=company,
        role=role,
        status=User.Status.ACTIVE,
        countries=countries,
        current_country=current_country,
    )


class CountryListTests(TestCase):
    def test_lists_every_country_with_its_currency_without_login(self):
        response = APIClient().get(ALL_URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 199)
        by_name = {entry['name']: entry for entry in response.data}
        self.assertEqual(by_name['Andorra'], {'name': 'Andorra', 'code': 'AD', 'dial_code': '+376', 'currency': 'EUR'})
        # UAE and the UK keep the short names the app stores.
        self.assertEqual(by_name['UK']['currency'], 'GBP')
        self.assertEqual(by_name['UAE']['currency'], 'AED')
        self.assertNotIn('United Kingdom', by_name)
        self.assertTrue(all(len(entry['currency']) == 3 for entry in response.data))

    def test_accented_names_sort_as_plain_letters(self):
        names = [entry['name'] for entry in APIClient().get(ALL_URL).data]

        self.assertEqual(names[names.index('Costa Rica') + 1], "Côte d'Ivoire")

    def test_a_bad_or_expired_token_does_not_block_the_list(self):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION='Bearer not-a-real-token')

        response = client.get(ALL_URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 199)


class CompanyCountriesTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.company = Company.objects.create(name='Acme Realty', operating_countries=['UK', 'Andorra'])
        self.admin = make_user('admin@acme.test', self.company, User.Role.COMPANY_ADMIN, ['UK', 'Andorra'], 'UK')
        self.agent = make_user('agent@acme.test', self.company, User.Role.AGENT, ['UK', 'Andorra'], 'Andorra')
        self.manager = make_user('manager@acme.test', self.company, User.Role.TEAM_MANAGER, ['UK'], 'UK')
        self.other = Company.objects.create(name='Beta Homes', operating_countries=['UAE'])
        self.other_admin = make_user('admin@beta.test', self.other, User.Role.COMPANY_ADMIN, ['UAE'], 'UAE')

    def names(self, response):
        return [entry['name'] for entry in response.data['countries']]

    def test_any_company_user_can_read_the_company_countries(self):
        self.client.force_authenticate(self.agent)

        response = self.client.get(COMPANY_URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['countries'], [
            {'name': 'UK', 'code': 'GB', 'dial_code': '+44', 'currency': 'GBP'},
            {'name': 'Andorra', 'code': 'AD', 'dial_code': '+376', 'currency': 'EUR'},
        ])

    def test_company_admin_adds_a_country(self):
        self.client.force_authenticate(self.admin)

        response = self.client.post(COMPANY_URL, {'country': 'Pakistan'}, format='json')

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.names(response), ['UK', 'Andorra', 'Pakistan'])
        self.company.refresh_from_db()
        self.admin.refresh_from_db()
        self.agent.refresh_from_db()
        self.assertEqual(self.company.operating_countries, ['UK', 'Andorra', 'Pakistan'])
        # The admin can now switch to it; other users are given it by their admin.
        self.assertIn('Pakistan', self.admin.countries)
        self.assertNotIn('Pakistan', self.agent.countries)
        self.other.refresh_from_db()
        self.assertEqual(self.other.operating_countries, ['UAE'])

    def test_a_country_can_be_given_by_iso_code_or_full_name(self):
        self.client.force_authenticate(self.admin)

        self.assertEqual(self.client.post(COMPANY_URL, {'country': 'pk'}, format='json').status_code, 201)
        response = self.client.post(COMPANY_URL, {'country': 'United Arab Emirates'}, format='json')

        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.names(response)[-2:], ['Pakistan', 'UAE'])

    def test_adding_a_country_twice_or_an_unknown_one_is_refused(self):
        self.client.force_authenticate(self.admin)

        duplicate = self.client.post(COMPANY_URL, {'country': 'United Kingdom'}, format='json')
        unknown = self.client.post(COMPANY_URL, {'country': 'Atlantis'}, format='json')
        missing = self.client.post(COMPANY_URL, {}, format='json')

        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(missing.status_code, 400)
        self.company.refresh_from_db()
        self.assertEqual(self.company.operating_countries, ['UK', 'Andorra'])

    def test_company_admin_removes_a_country(self):
        self.client.force_authenticate(self.admin)

        response = self.client.delete(COMPANY_URL + 'Andorra/')

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.names(response), ['UK'])
        self.company.refresh_from_db()
        self.admin.refresh_from_db()
        self.agent.refresh_from_db()
        self.assertEqual(self.company.operating_countries, ['UK'])
        self.assertEqual(self.admin.countries, ['UK'])
        self.assertEqual(self.agent.countries, ['UK'])
        # The agent was working in Andorra and moves to the country they have left.
        self.assertEqual(self.agent.current_country, 'UK')

    def test_a_country_can_be_removed_by_iso_code(self):
        self.client.force_authenticate(self.admin)

        response = self.client.delete(COMPANY_URL + 'AD/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.names(response), ['UK'])

    def test_add_and_remove_return_the_callers_countries(self):
        self.client.force_authenticate(self.admin)

        added = self.client.post(COMPANY_URL, {'country': 'Pakistan'}, format='json')
        removed = self.client.delete(COMPANY_URL + 'UK/')

        self.assertEqual(added.data['user'], {'countries': ['UK', 'Andorra', 'Pakistan'], 'current_country': 'UK'})
        self.assertEqual(removed.status_code, 400)  # the team manager only has UK
        self.manager.countries = ['UK', 'Andorra']
        self.manager.save(update_fields=['countries'])
        removed = self.client.delete(COMPANY_URL + 'UK/')
        self.assertEqual(removed.status_code, 200, removed.data)
        self.assertEqual(removed.data['user'], {'countries': ['Andorra', 'Pakistan'], 'current_country': 'Andorra'})

    def test_an_admin_with_fewer_countries_is_given_the_remaining_ones(self):
        # Older admins can hold fewer countries than their company.
        self.admin.countries = ['UK']
        self.admin.save(update_fields=['countries'])
        self.manager.countries = ['UK', 'Andorra']
        self.manager.save(update_fields=['countries'])
        self.client.force_authenticate(self.admin)

        response = self.client.delete(COMPANY_URL + 'UK/')

        self.assertEqual(response.status_code, 200, response.data)
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.countries, ['Andorra'])
        self.assertEqual(self.admin.current_country, 'Andorra')

    def test_a_country_stored_twice_is_listed_once(self):
        self.company.operating_countries = ['UK', 'United Kingdom', 'Andorra']
        self.company.save(update_fields=['operating_countries'])
        self.client.force_authenticate(self.agent)

        response = self.client.get(COMPANY_URL)

        self.assertEqual(self.names(response), ['UK', 'Andorra'])

    def test_older_spellings_count_as_the_same_country(self):
        self.company.operating_countries = ['USA']
        self.company.save(update_fields=['operating_countries'])
        self.client.force_authenticate(self.admin)

        response = self.client.post(COMPANY_URL, {'country': 'US'}, format='json')

        self.assertEqual(response.status_code, 400)

    def test_the_last_country_cannot_be_removed(self):
        self.client.force_authenticate(self.other_admin)

        response = self.client.delete(COMPANY_URL + 'UAE/')

        self.assertEqual(response.status_code, 400)
        self.other.refresh_from_db()
        self.assertEqual(self.other.operating_countries, ['UAE'])

    def test_a_country_some_user_only_works_in_cannot_be_removed(self):
        self.client.force_authenticate(self.admin)

        response = self.client.delete(COMPANY_URL + 'UK/')

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['users'], ['manager@acme.test'])
        self.company.refresh_from_db()
        self.assertEqual(self.company.operating_countries, ['UK', 'Andorra'])

    def test_removing_a_country_the_company_does_not_have_answers_404(self):
        self.client.force_authenticate(self.admin)

        self.assertEqual(self.client.delete(COMPANY_URL + 'Pakistan/').status_code, 404)

    def test_the_superadmins_company_is_not_changed_here(self):
        root = User.objects.create_superuser(
            email='root@techfy.test', first_name='Root', last_name='User', password='password123')
        platform = root.company or Company.objects.create(name='Platform', operating_countries=['UK', 'UAE'])
        if root.company_id is None:
            root.company = platform
            root.save(update_fields=['company'])
        admin = make_user('ops@techfy.test', platform, User.Role.AXIYON_ADMIN, ['UK'], 'UK')
        before = list(Company.objects.get(pk=platform.pk).operating_countries)
        self.client.force_authenticate(admin)

        self.assertEqual(self.client.post(COMPANY_URL, {'country': 'Pakistan'}, format='json').status_code, 403)
        self.assertEqual(self.client.delete(COMPANY_URL + 'UK/').status_code, 403)
        self.assertEqual(Company.objects.get(pk=platform.pk).operating_countries, before)

    def test_only_company_admins_may_change_countries(self):
        superadmin = User.objects.create_superuser(
            email='root@techfy.test', first_name='Root', last_name='User', password='password123')

        for user in (self.agent, self.manager, superadmin):
            self.client.force_authenticate(user)
            self.assertEqual(
                self.client.post(COMPANY_URL, {'country': 'Pakistan'}, format='json').status_code, 403, user.email)
            self.assertEqual(self.client.delete(COMPANY_URL + 'Andorra/').status_code, 403, user.email)

        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(COMPANY_URL).status_code, 401)
        self.company.refresh_from_db()
        self.assertEqual(self.company.operating_countries, ['UK', 'Andorra'])
