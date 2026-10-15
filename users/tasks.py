import io
import csv
import logging
from urllib.parse import urlparse
from django.core.files.storage import default_storage
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.contrib.auth import get_user_model
from django.conf import settings

from celery import shared_task
from django.db import transaction

logger = logging.getLogger(__name__)

from billing.exceptions import PlanLimitReached
from users.serializers import LeadSerializer, UserManagementSerializer

User = get_user_model()


def _notification_recipients(user) -> list[str]:
    """
    Addresses for a notification email meant for ``user``. Mail for a
    superadmin goes to the admin inbox (ADMIN_NOTIFICATION_EMAILS) instead of
    the superadmin account's address, when one is configured.
    Not for login emails: an OTP or password must only reach its owner.
    """
    if user.role == User.Role.SUPERADMIN:
        admin_inbox = [
            address.strip()
            for address in getattr(settings, 'ADMIN_NOTIFICATION_EMAILS', [])
            if address.strip()
        ]
        if admin_inbox:
            return admin_inbox
    return [user.email] if user.email else []


def send_otp_email(user, otp):
    """Send password reset OTP. Called synchronously from ForgotPasswordView."""
    context = {
        "first_name": user.first_name,
        "otp": otp,
    }
    html_body = render_to_string("emails/otp_email.html", context)
    plain_body = (
        f"Hi {user.first_name},\n\n"
        f"Your OTP for password reset is: {otp}\n\n"
        f"This OTP is valid for 5 minutes.\n\n"
        f"If you did not request this, please ignore this email.\n\n"
        f"Thanks,\nThe Axiyon Team"
    )
    msg = EmailMultiAlternatives(
        subject="Password Reset OTP – Axiyon",
        body=plain_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[user.email],
    )
    msg.attach_alternative(html_body, "text/html")
    msg.send(fail_silently=False)


# def send_superadmin_created_account_email(user, plain_password):
#     """
#     Notify a user that their account was created by an admin.
#     Emails them their auto-generated password.
#     """
#     send_mail(
#         subject="Your account has been created",
#         message=(
#             f"Hi {user.first_name},\n\n"
#             f"An admin has created an account for you.\n\n"
#             f"Here are your login credentials:\n\n"
#             f"    Email:    {user.email}\n"
#             f"    Password: {plain_password}\n\n"
#             f"Please log in and change your password as soon as possible.\n\n"
#             f"Thanks,\n"
#             f"The Team"
#         ),
#         from_email=settings.DEFAULT_FROM_EMAIL,
#         recipient_list=[user.email],
#         fail_silently=False,
#     )


def send_superadmin_created_account_email(user, plain_password, created_by):
    role_display = dict(User.Role.choices).get(user.role, user.role)
    created_by_role_display = dict(User.Role.choices).get(created_by.role, created_by.role)

    company = user.company
    team = user.team

    # Build initials for the creator avatar in the HTML template
    created_by_initials = "".join(
        part[0].upper()
        for part in (created_by.full_name or "").split()
        if part
    )[:2] or "?"

    context = {
        "first_name": user.first_name,
        "email": user.email,
        "plain_password": plain_password,
        "role": role_display,
        "countries": ", ".join(user.countries) if user.countries else None,
        "created_by_name": created_by.full_name,
        "created_by_role": created_by_role_display,
        "created_by_initials": created_by_initials,
        "company_name": company.name if company else None,
        "company_countries": ", ".join(company.operating_countries) if company and company.operating_countries else None,
        "team_name": team.name if team else None,
        "login_url": settings.FRONTEND_LOGIN_URL,
    }

    html_body = render_to_string("emails/account_created_email.html", context)

    plain_body = (
        f"Hi {user.first_name},\n\n"
        f"{created_by.full_name} ({created_by_role_display}) has created an account for you.\n\n"
        f"Email:    {user.email}\n"
        f"Password: {plain_password}\n"
        f"Role:     {role_display}\n\n"
        f"Please log in and change your password immediately:\n"
        f"{settings.FRONTEND_LOGIN_URL}\n\n"
        f"Thanks,\nThe Axiyon Team"
    )

    msg = EmailMultiAlternatives(
        subject="Your Axiyon Account is Ready",
        body=plain_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[user.email],
    )
    msg.attach_alternative(html_body, "text/html")
    msg.send(fail_silently=False)


@shared_task(bind=True)
def scrape_company_website_task(self, company_id: int, website: str):
    """Fetch a company website, extract project cards and detail pages, and store the structured result."""
    from users.models import Company
    from users.website_scraper import scrape_company_website
    from projects.models import Project

    logger.info('scrape_company_website_task started: company_id=%s website=%s', company_id, website)

    company = Company.objects.filter(pk=company_id).first()
    if company is None:
        logger.warning('scrape_company_website_task: company not found for company_id=%s', company_id)
        return {'status': 'company_not_found', 'projects': []}

    normalized_website = (website or company.website or '').strip().strip('"\'')
    if normalized_website and not normalized_website.startswith(('https://', 'http://')):
        normalized_website = f'https://{normalized_website}'
    parsed_website = urlparse(normalized_website)
    if parsed_website.scheme not in ('http', 'https') or not parsed_website.netloc:
        logger.warning('scrape_company_website_task: missing website for company_id=%s', company_id)
        company.website_scrape_status = 'empty'
        company.save(update_fields=['website_scrape_status'])
        return {'status': 'missing_website', 'projects': []}

    # The same website also feeds the knowledge base ("Website Data.txt").
    # Queued as its own task so a problem there never stops the project import.
    try:
        from chatbot.tasks import import_company_website_knowledge

        import_company_website_knowledge.delay(company.pk, normalized_website)
    except Exception:
        logger.exception(
            'Failed to queue website knowledge import for company_id=%s', company_id)

    report = scrape_company_website(normalized_website)
    company_admin = company.users.filter(role='company_admin').order_by('pk').first()
    project_creator = company_admin or company.users.order_by('pk').first()
    saved_project_ids = []
    seen_source_urls = set()

    for scraped_project in report.get('projects', []):
        if not isinstance(scraped_project, dict):
            continue

        title = scraped_project.get('name') or scraped_project.get('title')
        source_url = scraped_project.get('detail_page_url') or scraped_project.get('url')
        if not isinstance(title, str) or not isinstance(source_url, str):
            continue

        title = title.strip()[:255]
        source_url = source_url.strip()
        parsed_source_url = urlparse(source_url)
        if (
            not title
            or parsed_source_url.scheme not in ('http', 'https')
            or not parsed_source_url.netloc
            or source_url in seen_source_urls
        ):
            continue

        seen_source_urls.add(source_url)
        project = Project.objects.filter(
            visible_to_companies=company,
            source_url=source_url,
        ).first()
        image_url = scraped_project.get('image_url')

        if project is None:
            project = Project.objects.create(
                title=title,
                source_url=source_url,
                description='',
                location='',
                developer='',
                starting_price=0,
                yield_percentage=None,
                project_type=Project.ProjectType.RESIDENTIAL,
                status=Project.Status.PLANNED,
                project_status=Project.ProjectStatus.DRAFT,
                created_by=project_creator,
            )
        else:
            fields_to_update = []
            if project.title != title:
                project.title = title
                fields_to_update.append('title')
            if fields_to_update:
                project.save(update_fields=fields_to_update)

        project.visible_to_companies.add(company)
        if isinstance(image_url, str) and image_url.startswith(('http://', 'https://')):
            from projects.tasks import import_scraped_proposal_image_task

            try:
                image_task = import_scraped_proposal_image_task.delay(project.pk, image_url)
                scraped_project['exterior_image_task_id'] = image_task.id
            except Exception:
                logger.exception(
                    'Failed to queue exterior image import for project_id=%s',
                    project.pk,
                )
        scraped_project['project_id'] = project.pk
        saved_project_ids.append(project.pk)

    report['project_ids'] = saved_project_ids
    report['projects_created_or_updated'] = len(saved_project_ids)
    company.website = normalized_website
    company.website_data = report
    company.website_scrape_status = 'completed' if report.get('projects') else 'empty'
    company.save(update_fields=['website', 'website_data', 'website_scrape_status'])
    logger.info('scrape_company_website_task finished: company_id=%s status=%s projects=%s', company_id, report.get('status'), len(report.get('projects', [])))
    return report


@shared_task(bind=True)
def import_users_csv(self, s3_key: str, requested_by_id: int):
    """
    Process CSV import of users.
    Runs the same serializer validations as the API.
    Creates new users or updates existing ones based on email.

    s3_key: temporary S3 path written by the view before queuing this task.
    The file is deleted from S3 in the finally block after processing.
    """
    requested_by = User.objects.select_related('company', 'managed_team').get(pk=requested_by_id)

    try:
        try:
            with default_storage.open(s3_key) as f:
                file_content = f.read().decode('utf-8')
        except Exception:
            logger.exception('import_users_csv: failed to read CSV from S3 key=%s', s3_key)
            return {'created': 0, 'updated': 0, 'errors': ['Failed to read uploaded CSV file.']}

        reader = csv.DictReader(io.StringIO(file_content))

        results = {
            'created': 0,
            'updated': 0,
            'errors': [],
        }

        required_columns = {'email', 'first_name', 'last_name', 'role', 'countries'}
        if not required_columns.issubset(set(reader.fieldnames or [])):
            missing = required_columns - set(reader.fieldnames or [])
            return {
                'created': 0,
                'updated': 0,
                'errors': [f"Missing required columns: {', '.join(sorted(missing))}"],
            }

        for row_num, row in enumerate(reader, start=2):  # start=2 accounting for header
            email = row.get('email', '').strip().lower()
            if not email:
                results['errors'].append({'row': row_num, 'error': 'Email is required.'})
                continue

            # Parse countries — CSV stores as comma separated string
            raw_countries = row.get('countries', '')
            countries = [c.strip() for c in raw_countries.split(',') if c.strip()]

            payload = {
                'email': email,
                'first_name': row.get('first_name', '').strip(),
                'last_name': row.get('last_name', '').strip(),
                'role': row.get('role', '').strip(),
                'countries': countries,
            }

            # Optional team column
            team_id = row.get('team', '').strip()
            if team_id:
                payload['team'] = team_id

            # Resolve company based on requesting user's role
            if requested_by.role in (User.Role.AXIYON_ADMIN, User.Role.COMPANY_ADMIN):
                payload['company'] = requested_by.company.pk
            elif requested_by.role == User.Role.TEAM_MANAGER:
                payload['company'] = requested_by.company.pk
            else:
                # superadmin — company must be in the CSV
                company_id = row.get('company', '').strip()
                if not company_id:
                    results['errors'].append({'row': row_num, 'email': email, 'error': 'Company is required for superadmin imports.'})
                    continue
                payload['company'] = company_id

            # Check if user already exists — update vs create
            existing_user = User.objects.filter(email=email).first()

            context = {'request': _MockRequest(requested_by)}

            if existing_user:
                serializer = UserManagementSerializer(
                    existing_user,
                    data=payload,
                    partial=True,
                    context=context,
                )
            else:
                serializer = UserManagementSerializer(
                    data=payload,
                    context=context,
                )

            if serializer.is_valid():
                try:
                    # Per-row transaction: the seat check and the write commit
                    # together, and one over-limit row does not abort the rest
                    # of the import.
                    with transaction.atomic():
                        if existing_user:
                            serializer.save()
                            updated = True
                        else:
                            from .utils import generate_password
                            plain_password = generate_password()
                            user = serializer.save()
                            user.set_password(plain_password)
                            user.save(update_fields=['password'])
                            updated = False
                except PlanLimitReached as exc:
                    results['errors'].append({
                        'row': row_num,
                        'email': email,
                        'error': str(exc.detail),
                        'code': exc.default_code,
                        'kind': 'teamManagers' if 'manager' in exc.kind else 'users',
                        'limit': exc.limit,
                        'current': exc.current,
                    })
                    continue
                except Exception as exc:
                    logger.exception(
                        'import_users_csv: row %s failed for %s', row_num, email)
                    results['errors'].append({
                        'row': row_num,
                        'email': email,
                        'error': str(exc),
                    })
                    continue

                if updated:
                    results['updated'] += 1
                else:
                    send_superadmin_created_account_email(user, plain_password, requested_by)
                    results['created'] += 1
            else:
                results['errors'].append({
                    'row': row_num,
                    'email': email,
                    'error': serializer.errors,
                })

        return results
    finally:
        try:
            default_storage.delete(s3_key)
        except Exception:
            logger.warning('import_users_csv: failed to delete temp S3 file: %s', s3_key)


class _MockRequest:
    """
    Minimal request mock to satisfy serializer context expectations.
    Celery tasks have no real HTTP request — this bridges that gap.
    """
    def __init__(self, user):
        self.user = user


@shared_task(bind=True)
def import_leads_csv(self, file_content, requested_by_id):
    """
    Process CSV import of leads.
    Runs the same serializer validations as the API.
    Creates new leads for the requesting user's company.
    """
    requested_by = User.objects.select_related('company').get(pk=requested_by_id)

    reader = csv.DictReader(io.StringIO(file_content))

    results = {
        'created': 0,
        'errors': [],
    }

    HEADER_ALIASES = {
        'name': 'name', 'full name': 'name', 'lead name': 'name',
        'email': 'email', 'e-mail': 'email',
        'phone': 'phone_no', 'phone no': 'phone_no', 'phone number': 'phone_no',
        'phone_no': 'phone_no', 'mobile': 'phone_no', 'contact': 'phone_no',
        'source': 'source', 'lead source': 'source',
        'country': 'country',
        'desired country': 'desired_country', 'desired_country': 'desired_country',
        'desired location': 'desired_location', 'desired_location': 'desired_location',
        'budget': 'estimated_budget',
        'estimated budget': 'estimated_budget',
        'estimated_budget': 'estimated_budget',
        'category': 'category',
        'type': 'type',
        'other type': 'other_type', 'other_type': 'other_type',
        'status': 'status',
        'scheduled at': 'scheduled_at', 'scheduled_at': 'scheduled_at',
        'project': 'project', 'project id': 'project', 'project_id': 'project',
        'assigned to': 'assigned_to', 'assigned_to': 'assigned_to',
    }

    def _canonical(raw_key):
        if raw_key is None:
            return None
        cleaned = str(raw_key).strip().lower().replace('-', ' ').replace('_', ' ')
        cleaned = ' '.join(cleaned.split())  # collapse extra whitespace
        return HEADER_ALIASES.get(cleaned) or HEADER_ALIASES.get(cleaned.replace(' ', '_'))

    header_map = {raw: _canonical(raw) for raw in (reader.fieldnames or [])}
    canonical_headers = {v for v in header_map.values() if v}

    required_fields = ['name', 'phone_no', 'estimated_budget']
    missing_columns = [f for f in required_fields if f not in canonical_headers]
    if missing_columns:
        return {
            'created': 0,
            'errors': [
                f"Missing required columns: {', '.join(missing_columns)}. "
                f"Required: name, phone_no, estimated_budget"
            ],
        }

    for row_num, row in enumerate(reader, start=2):
        try:
            normalized_row = {}
            for raw_key, value in row.items():
                canonical_key = header_map.get(raw_key)
                if not canonical_key:
                    continue
                normalized_row[canonical_key] = (value or '').strip()

            # Skip completely empty rows silently.
            if not any(normalized_row.values()):
                continue

            # Per-row required field check → skip row, continue with the rest.
            missing_fields = [
                field for field in required_fields
                if not normalized_row.get(field, '').strip()
            ]
            if missing_fields:
                results['errors'].append({
                    'row': row_num,
                    'name': normalized_row.get('name', ''),
                    'email': normalized_row.get('email', ''),
                    'error': (
                        f"Row {row_num} skipped: missing required field(s) "
                        f"{', '.join(missing_fields)}."
                    ),
                    'missing_fields': missing_fields,
                })
                continue

            payload = {
                key: value
                for key, value in {
                    'name': normalized_row.get('name', ''),
                    'email': normalized_row.get('email', ''),
                    'phone_no': normalized_row.get('phone_no', ''),
                    'source': normalized_row.get('source', ''),
                    'country': normalized_row.get('country', ''),
                    'desired_country': normalized_row.get('desired_country', ''),
                    'desired_location': normalized_row.get('desired_location', ''),
                    'estimated_budget': normalized_row.get('estimated_budget', ''),
                    'category': normalized_row.get('category', ''),
                    'type': normalized_row.get('type', ''),
                    'other_type': normalized_row.get('other_type', ''),
                    'status': normalized_row.get('status', ''),
                    'scheduled_at': normalized_row.get('scheduled_at', ''),
                    'project': normalized_row.get('project', ''),
                    'assigned_to': normalized_row.get('assigned_to', ''),
                }.items()
                if value != ''
            }

            serializer = LeadSerializer(data=payload)
            if not serializer.is_valid():
                results['errors'].append({
                    'row': row_num,
                    'name': normalized_row.get('name', ''),
                    'email': normalized_row.get('email', ''),
                    'error': f"Row {row_num} skipped: validation failed.",
                    'errors': serializer.errors,
                })
                continue

            serializer.save(created_by=requested_by)
            results['created'] += 1

        except Exception as exc:  # noqa: BLE001
            # Keep going for the remaining rows even if one row blows up
            # (DB integrity errors, unexpected data, etc.).
            logger.exception("import_leads_csv: row %s failed", row_num)
            results['errors'].append({
                'row': row_num,
                'error': f"Row {row_num} skipped: {exc}",
            })

    results['failed'] = len(results['errors'])
    return results

@shared_task(bind=True)
def send_lead_project_missing_images_email(
    self,
    user_id,
    lead_id,
    project_id,
    missing_categories,
    missing_image_keys,
    is_assigner=False,
):
    """
    Email a recipient that the project linked to an assigned lead is missing
    proposal images. ``is_assigner`` tailors the message for the agent/team
    manager who performed the assignment (asking them to contact the super
    admin) vs. admins who can add the images themselves.
    """
    from projects.models import Project
    from users.models import Lead

    user = User.objects.filter(pk=user_id).first()
    recipients = _notification_recipients(user) if user else []
    if not recipients:
        return

    lead = Lead.objects.filter(pk=lead_id).first()
    project = Project.objects.filter(pk=project_id).first()
    if lead is None or project is None:
        return

    categories_display = ', '.join(
        c.replace('_', ' ') for c in (missing_categories or [])
    )
    keys_display = ', '.join(missing_image_keys or [])

    if is_assigner:
        subject = f"Action needed: images missing for '{project.title}'"
        body = (
            f"Hi {user.first_name},\n\n"
            f"The project '{project.title}' linked to lead '{lead.name}' is "
            f"missing proposal images for these unit categories: "
            f"{categories_display}.\n\n"
            f"These units will not appear in the generated proposal. Please "
            f"connect with the super admin so the relevant unit images can be "
            f"added.\n\n"
            f"Missing images: {keys_display}\n\n"
            f"Thanks,\nThe Axiyon Team"
        )
    else:
        subject = f"Missing proposal images for project '{project.title}'"
        body = (
            f"Hi {user.first_name},\n\n"
            f"Lead '{lead.name}' was assigned on project '{project.title}', but "
            f"proposal images are missing for these unit categories: "
            f"{categories_display}.\n\n"
            f"These units will not appear in the generated proposal until the "
            f"images are uploaded. Please add the missing images.\n\n"
            f"Missing images: {keys_display}\n\n"
            f"Thanks,\nThe Axiyon Team"
        )

    msg = EmailMultiAlternatives(
        subject=subject,
        body=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=recipients,
    )
    try:
        msg.send(fail_silently=False)
    except Exception:  # noqa: BLE001
        logger.exception(
            "Failed to send lead-project missing-images email to %s", recipients
        )


def send_task_expiring_24h_email(task):
    """
    Send an email notification to the task creator that the task will expire in 24 hours.
    """
    user = task.created_by
    recipients = _notification_recipients(user) if user else []
    if not recipients:
        return

    due_date_display = None
    if task.scheduled_at:
        due_date_display = task.scheduled_at.strftime("%B %d, %Y at %I:%M %p")
    elif task.due_date:
        due_date_display = task.due_date.strftime("%B %d, %Y")

    lead_name = task.related_lead.name if task.related_lead else None

    context = {
        "first_name": user.first_name or "User",
        "task_name": task.name,
        "due_date_display": due_date_display,
        "priority": task.get_priority_display() if hasattr(task, 'get_priority_display') else task.priority,
        "lead_name": lead_name,
        "login_url": getattr(settings, 'FRONTEND_LOGIN_URL', ''),
    }

    html_body = render_to_string("emails/task_expiring_24h_email.html", context)
    plain_body = (
        f"Hi {user.first_name or 'User'},\n\n"
        f"Your task '{task.name}' is scheduled to expire in 24 hours.\n"
        f"Due Date/Time: {due_date_display or 'N/A'}\n\n"
        f"Please log in to review and complete your task.\n\n"
        f"Thanks,\nThe Axiyon Team"
    )

    msg = EmailMultiAlternatives(
        subject=f"Task Expiring in 24 Hours: '{task.name}' – Axiyon",
        body=plain_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=recipients,
    )
    msg.attach_alternative(html_body, "text/html")
    try:
        msg.send(fail_silently=False)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to send 24h task expiration email for task_id=%s to %s", task.id, recipients)


@shared_task
def check_and_update_task_expirations():
    """
    Periodic task to check pending and in_progress tasks:
    1. Send a warning notification and email 24 hours before expiration if not already sent.
    2. Send a warning notification 1 hour before expiration if not already sent.
    3. Change status to EXPIRED when due time/date has passed, and send task expired notification.
    """
    from datetime import datetime, time
    from django.utils import timezone
    from users.models import Task
    from notifications.models import Notification
    from notifications.services import send_notification

    now = timezone.now()
    twenty_four_hours_later = now + timezone.timedelta(hours=24)
    one_hour_later = now + timezone.timedelta(hours=1)

    active_tasks = Task.objects.filter(
        status__in=[Task.Status.PENDING, Task.Status.IN_PROGRESS]
    ).select_related('created_by', 'related_lead')

    expired_count = 0
    warning_24h_count = 0
    warning_1h_count = 0

    for task in active_tasks:
        if task.open_ended:
            continue

        target_dt = None
        if task.scheduled_at:
            target_dt = task.scheduled_at
        elif task.due_date:
            naive_dt = datetime.combine(task.due_date, time(23, 59, 59))
            target_dt = timezone.make_aware(naive_dt, timezone.get_current_timezone())

        if not target_dt:
            continue

        # 1. Check if task has passed target_dt -> Expire it
        if target_dt <= now:
            task.status = Task.Status.EXPIRED
            task.save(update_fields=['status', 'updated_at'])
            expired_count += 1

            if task.created_by:
                send_notification(
                    recipient=task.created_by,
                    type=Notification.Type.TASK_EXPIRED,
                    title="Task Expired",
                    message=f"Your Task '{task.name}' has expired.",
                    data={
                        "task_id": task.id,
                        "task_name": task.name,
                        "status": Task.Status.EXPIRED,
                    },
                )

        else:
            # 2. Check if task is expiring within 24 hours and 24h warning not sent yet
            if now < target_dt <= twenty_four_hours_later and not task.expiry_24h_warning_sent:
                task.expiry_24h_warning_sent = True
                task.save(update_fields=['expiry_24h_warning_sent', 'updated_at'])
                warning_24h_count += 1

                if task.created_by:
                    send_notification(
                        recipient=task.created_by,
                        type=Notification.Type.TASK_EXPIRING_AFTER_24H,
                        title="Task Expiring in 24 Hours",
                        message=f"Your Task '{task.name}' is expiring in 24 hours, please check.",
                        data={
                            "task_id": task.id,
                            "task_name": task.name,
                            "hours_remaining": 24,
                            "due_date": str(task.due_date) if task.due_date else None,
                            "scheduled_at": task.scheduled_at.isoformat() if task.scheduled_at else None,
                        },
                    )
                    send_task_expiring_24h_email(task)

            # 3. Check if task is expiring within 1 hour and 1h warning not sent yet
            if now < target_dt <= one_hour_later and not task.expiry_warning_sent:
                task.expiry_warning_sent = True
                task.save(update_fields=['expiry_warning_sent', 'updated_at'])
                warning_1h_count += 1

                if task.created_by:
                    send_notification(
                        recipient=task.created_by,
                        type=Notification.Type.TASK_EXPIRING_SOON,
                        title="Task Expiring Soon",
                        message=f"Your Task '{task.name}' is expiring soon, please check.",
                        data={
                            "task_id": task.id,
                            "task_name": task.name,
                            "hours_remaining": 1,
                            "due_date": str(task.due_date) if task.due_date else None,
                            "scheduled_at": task.scheduled_at.isoformat() if task.scheduled_at else None,
                        },
                    )

    logger.info(
        "check_and_update_task_expirations: processed active tasks. "
        "Expired: %d, 24h Warnings Sent: %d, 1h Warnings Sent: %d",
        expired_count,
        warning_24h_count,
        warning_1h_count,
    )
    return {
        "expired_count": expired_count,
        "warning_24h_count": warning_24h_count,
        "warning_1h_count": warning_1h_count,
        "warning_count": warning_1h_count + warning_24h_count,
    }
