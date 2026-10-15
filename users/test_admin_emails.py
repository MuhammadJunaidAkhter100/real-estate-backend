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
