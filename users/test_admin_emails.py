"""Notification emails meant for the superadmin go to the admin inbox."""

from datetime import date, timedelta
from unittest.mock import patch

from django.core import mail
from django.test import TestCase, override_settings

from projects.models import Project
from users.models import Company, Lead, Task, User
from users.tasks import (
    _notification_recipients,
    send_lead_project_missing_images_email,
    send_otp_email,
    send_task_expiring_24h_email,
)
from whatsapp.models import WhatsAppAccount
from whatsapp.services import WhatsAppService
from whatsapp.tasks import send_company_admin_welcome_task

ADMIN_INBOX = 'admin-inbox@techfy.test'


@override_settings(ADMIN_NOTIFICATION_EMAILS=[ADMIN_INBOX])
class AdminNotificationEmailTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='Acme Realty')
        self.superadmin = User.objects.create_superuser(
            email='root@techfy.test', first_name='Root', last_name='User', password='password123')
        self.company_admin = User.objects.create_user(
            email='admin@acme.test', first_name='Ann', last_name='Admin', password='password123',
            company=self.company, role=User.Role.COMPANY_ADMIN, status=User.Status.ACTIVE)
        self.project = Project.objects.create(
            title='Acme Tower', description='', location='Dubai', developer='Dev',
            starting_price=100000, project_type=Project.ProjectType.RESIDENTIAL,
            created_by=self.company_admin)
        self.lead = Lead.objects.create(
            name='Lead One', email='lead@example.com', project=self.project,
            created_by=self.company_admin, assigned_to=self.company_admin)

    def test_superadmin_mail_goes_to_the_admin_inbox(self):
        self.assertEqual(_notification_recipients(self.superadmin), [ADMIN_INBOX])

    def test_company_users_keep_only_their_own_address(self):
        self.assertEqual(_notification_recipients(self.company_admin), ['admin@acme.test'])

    @override_settings(ADMIN_NOTIFICATION_EMAILS=[])
    def test_no_admin_inbox_configured(self):
        self.assertEqual(_notification_recipients(self.superadmin), ['root@techfy.test'])

    def test_missing_images_email_reaches_the_admin_inbox(self):
        for user in (self.superadmin, self.company_admin):
            send_lead_project_missing_images_email.run(
                user_id=user.pk, lead_id=self.lead.pk, project_id=self.project.pk,
                missing_categories=['2_bed'], missing_image_keys=['2_bed_bath'])

        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(mail.outbox[0].to, [ADMIN_INBOX])
        self.assertEqual(mail.outbox[1].to, ['admin@acme.test'])

    @patch('users.tasks.render_to_string', return_value='<p>html</p>')
    def test_task_expiring_email_reaches_the_admin_inbox(self, _render):
        task = Task.objects.create(
            name='Call back', created_by=self.superadmin, due_date=date.today() + timedelta(days=1))

        send_task_expiring_24h_email(task)

        self.assertEqual(mail.outbox[0].to, [ADMIN_INBOX])

    @patch('users.tasks.render_to_string', return_value='<p>html</p>')
    def test_login_emails_are_never_copied(self, _render):
        send_otp_email(self.superadmin, '123456')

        self.assertEqual(mail.outbox[0].to, ['root@techfy.test'])


SITE = 'https://site.example'


@override_settings(
    DEFAULT_FROM_EMAIL='noreply@mail.example', EMAIL_BRAND_NAME='Nexusproperty',
    EMAIL_SITE_URL=SITE, EMAIL_LOGO_URL=f'{SITE}/logo.png', FRONTEND_LOGIN_URL=f'{SITE}/login',
    EMAIL_SUPPORT_ADDRESS='hello@site.example', ADMIN_NOTIFICATION_EMAILS=[ADMIN_INBOX])
class EmailBrandingTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='Acme Realty')
        self.admin = User.objects.create_user(
            email='admin@acme.test', first_name='Ann', last_name='Admin', password='password123',
            company=self.company, role=User.Role.COMPANY_ADMIN, status=User.Status.ACTIVE)

    def assert_branded(self, message):
        self.assertEqual(message.from_email, 'Nexusproperty <noreply@mail.example>')
        self.assertIn('Nexusproperty', message.subject)
        self.assertNotIn('xiyon', message.subject + message.body)
        self.assertIn(SITE, message.body)
        self.assertIn('hello@site.example', message.body)
        for html, _mime in message.alternatives:
            self.assertNotIn('xiyon', html)
            self.assertIn(f'src="{SITE}/logo.png"', html)
            self.assertIn(f'href="{SITE}"', html)
            self.assertIn('mailto:hello@site.example', html)
            self.assertNotIn('href="#"', html)

    def test_password_reset_email(self):
        send_otp_email(self.admin, '123456')
        self.assert_branded(mail.outbox[0])
        self.assertIn('123456', mail.outbox[0].alternatives[0][0])

    def test_account_created_email(self):
        from users.tasks import send_superadmin_created_account_email

        send_superadmin_created_account_email(self.admin, 'Temp#1234', self.admin)
        self.assert_branded(mail.outbox[0])
        self.assertIn('Your Nexusproperty Account is Ready', mail.outbox[0].subject)
        self.assertIn(f'href="{SITE}/login"', mail.outbox[0].alternatives[0][0])

    def test_task_expiring_email(self):
        task = Task.objects.create(
            name='Call back', created_by=self.admin, due_date=date.today() + timedelta(days=1))
        send_task_expiring_24h_email(task)
        self.assert_branded(mail.outbox[0])

    def test_missing_images_email(self):
        project = Project.objects.create(
            title='Acme Tower', description='', location='Dubai', developer='Dev',
            starting_price=100000, project_type=Project.ProjectType.RESIDENTIAL,
            created_by=self.admin)
        lead = Lead.objects.create(
            name='Lead One', email='lead@example.com', project=project,
            created_by=self.admin, assigned_to=self.admin)
        send_lead_project_missing_images_email.run(
            user_id=self.admin.pk, lead_id=lead.pk, project_id=project.pk,
            missing_categories=['2_bed'], missing_image_keys=['2_bed_bath'])
        message = mail.outbox[0]
        self.assertEqual(message.from_email, 'Nexusproperty <noreply@mail.example>')
        self.assertNotIn('xiyon', message.body)
        self.assertIn(f'{SITE}/login', message.body)


@override_settings(ADMIN_NOTIFICATION_EMAILS=[ADMIN_INBOX], EMAIL_BRAND_NAME='Nexusproperty')
class AdminInfoEmailTests(TestCase):
    """New companies and new users are reported to the admin inbox."""

    def setUp(self):
        from django.core.cache import cache
        from rest_framework.test import APIClient

        cache.clear()
        self.client = APIClient()
        self.superadmin = User.objects.create_superuser(
            email='root@techfy.test', first_name='Root', last_name='User', password='password123')

    def admin_mails(self):
        return [m for m in mail.outbox if m.to == [ADMIN_INBOX]]

    @patch('whatsapp.tasks.send_company_admin_welcome_task.delay')
    @patch('users.tasks.scrape_company_website_task.delay')
    def test_company_signup_is_reported(self, _scrape, welcome_task):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post('/api/auth/register-company/', {
                'company_name': 'Sunrise Realty', 'countries': ['UAE', 'UK'],
                'first_name': 'Sara', 'last_name': 'Khan', 'email': 'sara@sunrise.test',
                'password': 'Str0ngPass!23', 'plan': 'basic', 'interval': 'monthly',
                'phone': '+971 50 123 4567',
            }, format='json')

        self.assertEqual(response.status_code, 201, response.data)
        admin = User.objects.get(email='sara@sunrise.test')
        welcome_task.assert_called_once_with(admin.company_id, admin.pk, None)
        [message] = self.admin_mails()
        self.assertEqual(message.subject, 'New company signed up – Nexusproperty')
        self.assertIn('Company: Sunrise Realty', message.body)
        self.assertIn('Admin: Sara Khan <sara@sunrise.test>', message.body)
        self.assertIn('Countries: UAE, UK', message.body)
        self.assertIn('Status: Active', message.body)

    @patch('whatsapp.tasks.send_company_admin_welcome_task.delay')
    @patch('users.views.send_superadmin_created_account_email')
    def test_company_created_by_superadmin_is_reported(self, _welcome_email, welcome_task):
        self.client.force_authenticate(self.superadmin)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post('/api/users/super_admin_companies/', {
                'name': 'Beta Homes', 'phone': '+971501234567', 'operating_countries': ['UAE'],
                'admin': {'email': 'admin@beta.test', 'first_name': 'Bob', 'last_name': 'Beta'},
            }, format='json')

        self.assertEqual(response.status_code, 201, response.data)
        admin = User.objects.get(email='admin@beta.test')
        welcome_task.assert_called_once_with(admin.company_id, admin.pk, self.superadmin.pk)
        [message] = self.admin_mails()
        self.assertEqual(message.subject, 'New company created – Nexusproperty')
        self.assertIn('Company: Beta Homes', message.body)
        self.assertIn('Admin: Bob Beta <admin@beta.test>', message.body)
        self.assertIn('Created by: Root User <root@techfy.test>', message.body)

    @patch.object(WhatsAppService, 'send_text_message')
    @patch.object(WhatsAppService, 'check_number_exists', return_value={
        'status': 'success', 'numberExists': True, 'chatId': '971501234567@c.us',
    })
    def test_company_welcome_checks_number_and_sends_from_superadmin(self, check_number, send_text):
        company = Company.objects.create(name='Welcome Realty', phone='+971 50 123 4567')
        admin = User.objects.create_user(
            email='welcome@realty.test', first_name='Wafa', last_name='Admin',
            password='password123', company=company, role=User.Role.COMPANY_ADMIN,
        )
        WhatsAppAccount.objects.create(
            user=self.superadmin, session_name='root-wa', session_status='WORKING',
        )

        result = send_company_admin_welcome_task.run(company.pk, admin.pk, self.superadmin.pk)

        self.assertEqual(result['status'], 'sent')
        check_number.assert_called_once_with(self.superadmin, company.phone)
        send_text.assert_called_once_with(
            user=self.superadmin,
            payload={
                'chat_id': '971501234567@c.us',
                'text': 'Hi Wafa, welcome to Nexusproperty! Your company admin account for '
                'Welcome Realty is ready. You can now log in and get started.',
            },
        )

    @patch('users.views.send_superadmin_created_account_email')
    def test_user_added_is_reported(self, _welcome):
        company = Company.objects.create(name='Acme Realty', operating_countries=['UAE'])
        admin = User.objects.create_user(
            email='admin@acme.test', first_name='Ann', last_name='Admin', password='password123',
            company=company, role=User.Role.COMPANY_ADMIN, status=User.Status.ACTIVE,
            countries=['UAE'])
        self.client.force_authenticate(admin)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post('/api/users/manage_users/', {
                'email': 'agent@acme.test', 'first_name': 'Al', 'last_name': 'Agent',
                'role': 'agent', 'countries': ['UAE'], 'company': company.pk,
            }, format='json')

        self.assertEqual(response.status_code, 201, response.data)
        [message] = self.admin_mails()
        self.assertEqual(message.subject, 'New user added – Nexusproperty')
        self.assertIn('User: Al Agent <agent@acme.test>', message.body)
        self.assertIn('Company: Acme Realty', message.body)
        self.assertIn('Added by: Ann Admin <admin@acme.test> (Company Admin)', message.body)

    def test_a_mail_failure_never_breaks_the_action(self):
        from users.tasks import send_admin_info_email

        with patch('users.tasks.EmailMultiAlternatives.send', side_effect=OSError('smtp down')):
            send_admin_info_email.run('New user added', ['User: x'])  # must not raise

    @override_settings(ADMIN_NOTIFICATION_EMAILS=[])
    def test_nothing_is_sent_without_an_admin_inbox(self):
        from users.tasks import send_admin_info_email

        send_admin_info_email.run('New user added', ['User: x'])
        self.assertEqual(mail.outbox, [])
