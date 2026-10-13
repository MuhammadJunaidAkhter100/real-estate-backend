from __future__ import annotations

import shutil
import socket
import tempfile
from datetime import timedelta
from unittest.mock import MagicMock, patch

import requests
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from chatbot.models import KnowledgeBaseDocument
from chatbot.pinecone_service import PineconeServiceError
from chatbot.tasks import import_company_website_knowledge
from chatbot.website_knowledge import (
    MAX_PAGES,
    WEBSITE_DOCUMENT_NAME,
    WebsiteContent,
    WebsiteImportError,
    WebsitePage,
    crawl_website,
    fetch_page,
    is_public_url,
    normalize_website_url,
)
from users.models import Company, User

MEDIA_ROOT = tempfile.mkdtemp(prefix='kb-website-tests-')
WEBSITE_URL = '/api/chatbot/knowledge-base/website/'

NAV = '<nav><a href="/">Home</a> <a href="/about">About</a> <a href="/contact">Contact</a></nav>'
FOOTER = '<footer>Call us on +971 4 000 0000</footer>'


def page(title, body):
    return (
        f'<html><head><title>{title}</title>'
        f'<meta name="description" content="{title} summary"></head>'
        f'<body>{NAV}{body}{FOOTER}<script>ignored()</script></body></html>'
    ).encode()


def fake_site(pages, redirects=None):
    """A `fetch_page` stand-in serving `pages` ({url: html}) and recording calls."""
    fetched = []

    def fetch(url):
        fetched.append(url)
        if redirects and url in redirects:
            url = redirects[url]
        if url not in pages:
            raise requests.HTTPError(f'404 for {url}')
        return url, pages[url]

    fetch.fetched = fetched
    return fetch


def addresses(*ips):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 0)) for ip in ips]


def make_user(email, company, role):
    return User.objects.create_user(
        email=email,
        first_name='Test',
        last_name='User',
        password='password123',
        company=company,
        role=role,
        status=User.Status.ACTIVE,
    )


class WebsiteUrlTests(SimpleTestCase):
    def test_normalize_website_url(self):
        self.assertEqual(normalize_website_url('acme.test'), 'https://acme.test')
        self.assertEqual(normalize_website_url(' "http://acme.test/home" '), 'http://acme.test/home')
        for bad in ('', None, 'not a site', 'ftp://acme.test', 'https://localhost'):
            self.assertEqual(normalize_website_url(bad), '', bad)

    def test_only_public_addresses_are_allowed(self):
        with patch('chatbot.website_knowledge.socket.getaddrinfo') as resolve:
            resolve.return_value = addresses('93.184.216.34')
            self.assertTrue(is_public_url('https://acme.test/about'))

            for private in ('127.0.0.1', '10.0.0.5', '192.168.1.10', '169.254.169.254'):
                resolve.return_value = addresses(private)
                self.assertFalse(is_public_url('https://acme.test/'), private)

            # One private address among public ones is still refused.
            resolve.return_value = addresses('93.184.216.34', '10.0.0.5')
            self.assertFalse(is_public_url('https://acme.test/'))

            resolve.return_value = addresses('93.184.216.34')
            self.assertFalse(is_public_url('https://acme.test:8080/'))
            self.assertFalse(is_public_url('ftp://acme.test/'))

            resolve.side_effect = socket.gaierror('no such host')
            self.assertFalse(is_public_url('https://acme.test/'))

    def test_fetch_refuses_a_redirect_to_a_private_address(self):
        redirect = MagicMock(status_code=302, headers={'Location': 'http://internal.test/secret'})
        redirect.__enter__.return_value = redirect

        def resolve(host, *_):
            return addresses('10.0.0.5' if host == 'internal.test' else '93.184.216.34')

        with patch('chatbot.website_knowledge.socket.getaddrinfo', side_effect=resolve), \
             patch('chatbot.website_knowledge.requests.get', return_value=redirect) as get:
            with self.assertRaises(WebsiteImportError):
                fetch_page('https://acme.test/')

        # The private address was never requested.
        get.assert_called_once()
        self.assertEqual(get.call_args.args[0], 'https://acme.test/')


class CrawlWebsiteTests(SimpleTestCase):
    def crawl(self, pages, redirects=None, website='acme.test'):
        fetch = fake_site(pages, redirects)
        with patch('chatbot.website_knowledge.fetch_page', side_effect=fetch):
            return crawl_website(website), fetch.fetched

    def test_reads_the_home_page_and_same_site_pages(self):
        home = page('Acme Realty', (
            '<h1>Homes in Dubai Marina and Business Bay since 2010</h1>'
            '<p>Acme Realty sells and rents apartments, villas and offices across the UAE '
            'with a team of forty advisers.</p>'
            '<a href="https://other.test/partner">Partner</a>'
            '<a href="/brochure.pdf">Brochure</a><a href="/login">Login</a>'
            '<a href="/about#team">Team</a>'
        ))
        about = page('About Acme', '<p>Founded in 2010 by two brokers from Abu Dhabi.</p>')
        contact = page('Contact Acme', '<p>Office 12, Marina Plaza, Dubai. Open Sunday to Thursday.</p>')

        content, fetched = self.crawl({
            'https://acme.test': home,
            'https://acme.test/about': about,
            'https://acme.test/contact': contact,
        })

        self.assertEqual(
            [p.url for p in content.pages],
            ['https://acme.test', 'https://acme.test/about', 'https://acme.test/contact'],
        )
        # Other sites, files and login pages are never requested.
        self.assertEqual(
            fetched, ['https://acme.test', 'https://acme.test/about', 'https://acme.test/contact'])

        text = content.as_text()
        self.assertTrue(text.startswith('Website: https://acme.test'))
        self.assertIn('==== About Acme ====', text)
        self.assertIn('Summary: Contact Acme summary', text)
        self.assertIn('Founded in 2010 by two brokers', text)
        self.assertNotIn('ignored()', text)
        # The menu and footer repeat on every page but are kept once.
        self.assertEqual(text.count('Call us on +971 4 000 0000'), 1)

    def test_stops_at_the_page_limit(self):
        links = ''.join(f'<a href="/area-{n}">Area {n}</a>' for n in range(40))
        pages = {'https://acme.test': page('Acme', f'<p>{"Welcome to Acme Realty. " * 12}</p>{links}')}
        for n in range(40):
            pages[f'https://acme.test/area-{n}'] = page(f'Area {n}', f'<p>Guide to living in area number {n}.</p>')

        content, _ = self.crawl(pages)

        self.assertEqual(len(content.pages), MAX_PAGES)

    def test_a_page_that_redirects_off_the_site_is_left_out(self):
        home = page('Acme', f'<p>{"Acme Realty sells homes in Dubai. " * 10}</p><a href="/portal">Portal</a>')
        content, _ = self.crawl(
            {'https://acme.test': home, 'https://other.test/app': page('Other', '<p>Another company.</p>')},
            redirects={'https://acme.test/portal': 'https://other.test/app'},
        )

        self.assertEqual([p.url for p in content.pages], ['https://acme.test'])
        self.assertNotIn('Another company', content.as_text())

    def test_a_site_with_no_readable_text_is_rejected(self):
        empty = b'<html><body><div id="root"></div><script>render()</script></body></html>'
        with self.assertRaisesMessage(WebsiteImportError, 'No readable text'):
            self.crawl({'https://acme.test': empty})

    def test_a_site_that_cannot_be_opened_is_rejected(self):
        with self.assertRaisesMessage(WebsiteImportError, 'could not be opened'):
            self.crawl({})

    def test_an_invalid_address_is_rejected_without_a_request(self):
        fetch = fake_site({})
        with patch('chatbot.website_knowledge.fetch_page', side_effect=fetch):
            with self.assertRaisesMessage(WebsiteImportError, 'not valid'):
                crawl_website('not a site')
        self.assertEqual(fetch.fetched, [])


def site_content(marker='Acme Realty sells homes in Dubai.'):
    return WebsiteContent(
        website='https://acme.test/',
        pages=[WebsitePage(url='https://acme.test/', title='Acme', description='', lines=[marker])],
    )


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class WebsiteKnowledgeTestCase(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        self.company = Company.objects.create(name='Acme Realty', website='https://acme.test')
        self.admin = make_user('admin@acme.test', self.company, User.Role.COMPANY_ADMIN)
        self.agent = make_user('agent@acme.test', self.company, User.Role.AGENT)

    def documents(self):
        return KnowledgeBaseDocument.objects.filter(source=KnowledgeBaseDocument.Source.WEBSITE)


class ImportWebsiteTaskTests(WebsiteKnowledgeTestCase):
    def run_import(self, content=None, error=None, **kwargs):
        with patch('chatbot.tasks.crawl_website') as crawl, \
             patch('chatbot.tasks.PineconeService') as pinecone:
            crawl.side_effect = error
            crawl.return_value = content or site_content()
            pinecone.return_value.upsert_document.return_value = 3
            result = import_company_website_knowledge.run(self.company.pk, **kwargs)
        return result, crawl, pinecone.return_value

    def test_files_the_website_under_the_company_admin(self):
        result, crawl, pinecone = self.run_import()

        self.assertTrue(result['success'])
        crawl.assert_called_once_with('https://acme.test')

        doc = self.documents().get()
        self.assertEqual(doc.user, self.admin)
        self.assertEqual(doc.original_filename, WEBSITE_DOCUMENT_NAME)
        self.assertEqual(doc.file_type, 'txt')
        self.assertEqual(doc.status, KnowledgeBaseDocument.Status.COMPLETED)
        self.assertEqual(doc.source_url, 'https://acme.test/')
        self.assertTrue(doc.hosted_url)
        with doc.file.open('rb') as stored:
            self.assertIn('Acme Realty sells homes in Dubai.', stored.read().decode())

        upsert = pinecone.upsert_document.call_args.kwargs
        self.assertEqual(upsert['document_id'], doc.pk)
        self.assertIn('Acme Realty sells homes in Dubai.', upsert['text'])
        self.assertEqual(upsert['metadata']['user_id'], self.admin.pk)
        self.assertEqual(upsert['metadata']['company_id'], self.company.pk)
        self.assertEqual(upsert['metadata']['source'], 'website')

    def test_running_again_replaces_the_same_document(self):
        self.run_import()
        first = self.documents().get()

        _, _, pinecone = self.run_import(content=site_content('Acme Realty now also sells in Abu Dhabi.'))

        doc = self.documents().get()
        self.assertEqual(doc.pk, first.pk)
        with doc.file.open('rb') as stored:
            text = stored.read().decode()
        self.assertIn('Abu Dhabi', text)
        self.assertNotIn('sells homes in Dubai', text)
        # Chunks the shorter text no longer covers are cleared.
        cleared = pinecone.delete_document_chunks.call_args.args
        self.assertEqual(cleared[:2], (doc.pk, 3))

    def test_a_website_that_cannot_be_read_marks_the_document_failed(self):
        result, _, pinecone = self.run_import(
            error=WebsiteImportError('No readable text was found on the website.'))

        self.assertFalse(result['success'])
        doc = self.documents().get()
        self.assertEqual(doc.status, KnowledgeBaseDocument.Status.FAILED)
        self.assertEqual(doc.error, 'No readable text was found on the website.')
        pinecone.upsert_document.assert_not_called()

    def test_a_failed_reimport_keeps_the_previous_file(self):
        self.run_import()
        self.run_import(error=WebsiteImportError('The website could not be opened.'))

        doc = self.documents().get()
        self.assertEqual(doc.status, KnowledgeBaseDocument.Status.FAILED)
        with doc.file.open('rb') as stored:
            self.assertIn('Acme Realty sells homes in Dubai.', stored.read().decode())

    def test_an_indexing_failure_marks_the_document_failed(self):
        with patch('chatbot.tasks.crawl_website', return_value=site_content()), \
             patch('chatbot.tasks.PineconeService') as pinecone:
            pinecone.return_value.upsert_document.side_effect = PineconeServiceError('index down')
            result = import_company_website_knowledge.run(self.company.pk)

        self.assertFalse(result['success'])
        self.assertEqual(self.documents().get().status, KnowledgeBaseDocument.Status.FAILED)

    def test_a_company_without_a_website_gets_no_document(self):
        self.company.website = ''
        self.company.save(update_fields=['website'])

        result, crawl, _ = self.run_import()

        self.assertFalse(result['success'])
        crawl.assert_not_called()
        self.assertFalse(self.documents().exists())

    def test_the_requesting_admin_owns_a_new_document(self):
        second_admin = make_user('second@acme.test', self.company, User.Role.COMPANY_ADMIN)

        self.run_import(requested_by_id=second_admin.pk)

        self.assertEqual(self.documents().get().user, second_admin)

    @patch('chatbot.tasks.import_company_website_knowledge.delay')
    @patch('users.website_scraper.scrape_company_website')
    def test_the_signup_scrape_also_queues_the_knowledge_import(self, scrape, queue_import):
        from users.tasks import scrape_company_website_task

        scrape.return_value = {'website': 'https://acme.test', 'status': 'completed', 'projects': []}

        scrape_company_website_task.run(self.company.pk, 'acme.test')

        queue_import.assert_called_once_with(self.company.pk, 'https://acme.test')

    @patch('chatbot.tasks.import_company_website_knowledge.delay', side_effect=RuntimeError('broker down'))
    @patch('users.website_scraper.scrape_company_website')
    def test_the_signup_scrape_survives_a_queueing_failure(self, scrape, _queue_import):
        from users.tasks import scrape_company_website_task

        scrape.return_value = {'website': 'https://acme.test', 'status': 'completed', 'projects': []}

        report = scrape_company_website_task.run(self.company.pk, 'acme.test')

        self.assertEqual(report['status'], 'completed')


class WebsiteKnowledgeApiTests(WebsiteKnowledgeTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        patcher = patch('chatbot.views.import_company_website_knowledge.delay')
        self.queue_import = patcher.start()
        self.queue_import.return_value.id = 'task-1'
        self.addCleanup(patcher.stop)

    def test_status_before_any_import(self):
        response = self.client.get(WEBSITE_URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {
            'website': 'https://acme.test',
            'status': 'not_started',
            'error': '',
            'document': None,
        })

    def test_post_queues_the_import_and_returns_the_pending_document(self):
        response = self.client.post(WEBSITE_URL, {}, format='json')

        self.assertEqual(response.status_code, 202, response.data)
        doc = self.documents().get()
        self.assertEqual(doc.user, self.admin)
        self.assertEqual(doc.status, KnowledgeBaseDocument.Status.PENDING)
        self.assertEqual(doc.task_id, 'task-1')
        self.queue_import.assert_called_once_with(self.company.pk, 'https://acme.test', self.admin.pk)
        self.assertEqual(response.data['status'], 'pending')
        self.assertEqual(response.data['document']['id'], doc.pk)
        self.assertEqual(response.data['document']['original_filename'], 'Website Data.txt')
        self.assertEqual(response.data['document']['source'], 'website')

    def test_post_with_a_new_address_saves_it_on_the_company(self):
        response = self.client.post(WEBSITE_URL, {'website': 'newacme.test'}, format='json')

        self.assertEqual(response.status_code, 202, response.data)
        self.company.refresh_from_db()
        self.assertEqual(self.company.website, 'https://newacme.test')
        self.assertEqual(response.data['website'], 'https://newacme.test')
        self.queue_import.assert_called_once_with(self.company.pk, 'https://newacme.test', self.admin.pk)

    def test_post_rejects_an_invalid_address(self):
        response = self.client.post(WEBSITE_URL, {'website': 'not a site'}, format='json')

        self.assertEqual(response.status_code, 400)
        self.assertIn('website', response.data)
        self.queue_import.assert_not_called()

    def test_post_without_any_website_is_rejected(self):
        self.company.website = ''
        self.company.save(update_fields=['website'])

        response = self.client.post(WEBSITE_URL, {}, format='json')

        self.assertEqual(response.status_code, 400)
        self.assertIn('website', response.data)
        self.queue_import.assert_not_called()

    def test_post_while_an_import_is_running_answers_409(self):
        self.client.post(WEBSITE_URL, {}, format='json')
        self.queue_import.reset_mock()

        response = self.client.post(WEBSITE_URL, {}, format='json')

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['status'], 'pending')
        self.queue_import.assert_not_called()
        self.assertEqual(self.documents().count(), 1)

    def test_an_import_stuck_for_too_long_can_be_started_again(self):
        self.client.post(WEBSITE_URL, {}, format='json')
        self.documents().update(updated_at=timezone.now() - timedelta(hours=1))
        self.queue_import.reset_mock()

        response = self.client.post(WEBSITE_URL, {}, format='json')

        self.assertEqual(response.status_code, 202)
        self.queue_import.assert_called_once()
        self.assertEqual(self.documents().count(), 1)

    def test_the_document_shows_in_the_knowledge_base_list(self):
        self.client.post(WEBSITE_URL, {}, format='json')

        response = self.client.get('/api/chatbot/knowledge-base/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['original_filename'], 'Website Data.txt')
        self.assertEqual(response.data[0]['source'], 'website')
        self.assertEqual(response.data[0]['source_url'], 'https://acme.test')

    def test_another_company_does_not_see_the_document(self):
        self.client.post(WEBSITE_URL, {}, format='json')
        other = Company.objects.create(name='Beta Homes', website='https://beta.test')
        self.client.force_authenticate(make_user('admin@beta.test', other, User.Role.COMPANY_ADMIN))

        response = self.client.get(WEBSITE_URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'not_started')
        self.assertIsNone(response.data['document'])
        self.assertEqual(self.client.get('/api/chatbot/knowledge-base/').data, [])

    def test_only_company_admins_may_use_it(self):
        manager = make_user('manager@acme.test', self.company, User.Role.TEAM_MANAGER)
        superadmin = User.objects.create_superuser(
            email='root@techfy.test', first_name='Root', last_name='User', password='password123')

        for user in (self.agent, manager, superadmin):
            self.client.force_authenticate(user)
            self.assertEqual(self.client.get(WEBSITE_URL).status_code, 403, user.email)
            self.assertEqual(self.client.post(WEBSITE_URL, {}, format='json').status_code, 403, user.email)

        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(WEBSITE_URL).status_code, 401)
        self.queue_import.assert_not_called()
