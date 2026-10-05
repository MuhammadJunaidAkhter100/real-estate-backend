from django.contrib.auth.base_user import BaseUserManager
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models
from django.conf import settings
from django.utils import timezone
import uuid

from api.constants import AVAILABLE_COUNTRIES
from projects.models import Project


def profile_image_upload_path(instance, filename):
    return f'profile_images/{instance.id}/{filename}'


class UserManager(BaseUserManager):
    def create_user(self, email, first_name, last_name, password=None, **extra_fields):
        if not email:
            raise ValueError("Users must have an email address")
        email = self.normalize_email(email)
        user = self.model(email=email, first_name=first_name, last_name=last_name, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, first_name, last_name, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('status', User.Status.ACTIVE)
        extra_fields.setdefault('role', User.Role.SUPERADMIN)
        extra_fields.setdefault('countries', AVAILABLE_COUNTRIES)

        # create company for superuser
        company = Company(name="SuperAdmin Company", operating_countries=AVAILABLE_COUNTRIES)
        company.save()
        extra_fields.setdefault('company', company)
        return self.create_user(email, first_name, last_name, password, **extra_fields)


class Company(models.Model):
    class Status(models.TextChoices):
        PENDING_PAYMENT = 'pending_payment', 'Pending Payment'
        ACTIVE = 'active', 'Active'
        PAST_DUE = 'past_due', 'Past Due'
        CANCELED = 'canceled', 'Canceled'
        SUSPENDED = 'suspended', 'Suspended'

    class Plan(models.TextChoices):
        BASIC = 'basic', 'Basic'
        PROFESSIONAL = 'professional', 'Professional'
        MANUAL = 'manual', 'Manual'

    class BillingInterval(models.TextChoices):
        MONTHLY = 'monthly', 'Monthly'
        ANNUAL = 'annual', 'Annual'

    name = models.CharField(max_length=255)
    operating_countries = models.JSONField(default=list, blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    # 'manual' keeps companies created by the super-admin "Add New Company"
    # flow (and every other non-billing creation path) outside of billing.
    plan = models.CharField(
        max_length=20,
        choices=Plan.choices,
        default=Plan.MANUAL,
    )
    billing_interval = models.CharField(
        max_length=20,
        choices=BillingInterval.choices,
        blank=True,
        default='',
    )
    stripe_customer_id = models.CharField(max_length=255, blank=True, default='')
    stripe_subscription_id = models.CharField(max_length=255, blank=True, default='')
    current_period_end = models.DateTimeField(null=True, blank=True)
    grace_until = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


class Team(models.Model):
    name = models.CharField(max_length=255)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='teams')
    manager = models.OneToOneField(
        'User',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='managed_team',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} ({self.company.name})"


class User(AbstractBaseUser, PermissionsMixin):

    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        INACTIVE = 'inactive', 'Inactive'
        INVITED = 'invited', 'Invited'

    class Role(models.TextChoices):
        SUPERADMIN = 'superadmin', 'Super Admin'
        AXIYON_ADMIN = 'axiyon_admin', 'Axiyon Admin'
        COMPANY_ADMIN = 'company_admin', 'Company Admin'
        TEAM_MANAGER = 'team_manager', 'Team Manager'
        AGENT = 'agent', 'Agent'

    email = models.EmailField(unique=True)
    first_name = models.CharField(max_length=150)
    last_name = models.CharField(max_length=150)

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.INVITED,
    )
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='users')
    team = models.ForeignKey(
        'Team',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='members',
    )
    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.AGENT,
    )
    countries = models.JSONField(default=list)
    current_country = models.CharField(max_length=100, blank=True, default='')
    profile_image = models.ImageField(upload_to=profile_image_upload_path, null=True, blank=True)

    # Keep these for Django internals / admin panel
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserManager()

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['first_name', 'last_name']

    def __str__(self):
        return f"{self.email} ({self.role})"

    @property
    def is_approved(self):
        return self.status == self.Status.ACTIVE

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"


class PasswordResetOTP(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    otp = models.CharField(max_length=6)
    created_at = models.DateTimeField(default=timezone.now)
    resend_allowed_at = models.DateTimeField(default=timezone.now)
    is_verified = models.BooleanField(default=False)
    reset_uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)

    def is_expired(self):
        return (timezone.now() - self.created_at).total_seconds() > 300  # 5 minutes

    def can_resend(self):
        return timezone.now() >= self.resend_allowed_at

    def __str__(self):
        return f"PasswordResetOTP({self.user.email})"


class Task(models.Model):
    class Priority(models.TextChoices):
        HIGH = 'high', 'High'
        MEDIUM = 'medium', 'Medium'
        LOW = 'low', 'Low'

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        IN_PROGRESS = 'in_progress', 'In Progress'
        COMPLETED = 'completed', 'Completed'
        EXPIRED = 'expired', 'Expired'

    class Type(models.TextChoices):
        CALLBACK = 'callback', 'Callback'

    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default='')
    associated_country = models.CharField(max_length=100, blank=True, default='')
    priority = models.CharField(
        max_length=10,
        choices=Priority.choices,
        default=Priority.MEDIUM,
    )
    type = models.CharField(
        max_length=32,
        choices=Type.choices,
        null=True,
        blank=True,
    )
    due_date = models.DateField(null=True, blank=True)
    scheduled_at = models.DateTimeField(null=True, blank=True)
    open_ended = models.BooleanField(
        default=False,
        help_text='When true, task is not auto-expired by the expiry cron.',
    )
    expiry_warning_sent = models.BooleanField(default=False)
    expiry_24h_warning_sent = models.BooleanField(default=False)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='tasks',
    )
    related_lead = models.ForeignKey(
        'Lead',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='tasks',
    )
    related_call = models.ForeignKey(
        'calling_agent.Call',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='tasks',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['created_by', '-created_at']),
            models.Index(fields=['due_date']),
            models.Index(fields=['status']),
            models.Index(fields=['priority']),
            models.Index(fields=['related_lead']),
            models.Index(fields=['scheduled_at']),
            models.Index(fields=['type', 'scheduled_at']),
        ]

    def __str__(self):
        return f"{self.name} ({self.status})"


class Lead(models.Model):
    class Stage(models.TextChoices):
        NEW = 'new', 'New'
        CONTACT_ATTEMPT = 'contact_attempt', 'Contact Attempt'
        QUALIFICATION = 'qualification', 'Qualification'
        NURTURING = 'nurturing', 'Nurturing'
        SALES_PROCESS = 'sales_process', 'Sales Process'
        CLOSED = 'closed', 'Closed'

    class Status(models.TextChoices):
        # New
        NEW = 'new', 'New Lead'
        # Contact Attempt
        CALL_PENDING = 'call_pending', 'Call Pending'
        CONTACTED = 'contacted', 'Contacted'
        NO_ANSWER = 'no_answer', 'No Answer'
        CALL_BACK_REQUESTED = 'call_back_requested', 'Call Back Requested'
        WRONG_NUMBER = 'wrong_number', 'Wrong Number'
        UNREACHABLE = 'unreachable', 'Unreachable'
        # Qualification
        INTERESTED = 'interested', 'Interested'
        HIGHLY_INTERESTED = 'highly_interested', 'Highly Interested'
        NEED_MORE_INFORMATION = 'need_more_information', 'Need More Information'
        SITE_VISIT_REQUESTED = 'site_visit_requested', 'Site Visit Requested'
        BUDGET_MISMATCH = 'budget_mismatch', 'Budget Mismatch'
        LOCATION_MISMATCH = 'location_mismatch', 'Location Mismatch'
        NOT_INTERESTED = 'not_interested', 'Not Interested'
        # Nurturing
        FOLLOW_UP_REQUIRED = 'follow_up_required', 'Follow-up Required'
        BROCHURE_SENT = 'brochure_sent', 'Brochure Sent'
        WHATSAPP_FOLLOW_UP = 'whatsapp_follow_up', 'WhatsApp Follow-up'
        EMAIL_SENT = 'email_sent', 'Email Sent'
        # Sales Process
        SITE_VISIT_SCHEDULED = 'site_visit_scheduled', 'Site Visit Scheduled'
        SITE_VISIT_COMPLETED = 'site_visit_completed', 'Site Visit Completed'
        NEGOTIATION_ONGOING = 'negotiation_ongoing', 'Negotiation Ongoing'
        DOCUMENTATION_IN_PROGRESS = (
            'documentation_in_progress',
            'Documentation in Progress',
        )
        BOOKING_AMOUNT_RECEIVED = (
            'booking_amount_received',
            'Booking Amount Received',
        )
        UNIT_RESERVED = 'unit_reserved', 'Unit Reserved'
        # Closed
        CONVERTED_WON = 'converted_won', 'Converted / Won'
        LOST_TO_COMPETITOR = 'lost_to_competitor', 'Lost to Competitor'
        LOST_NO_RESPONSE = 'lost_no_response', 'Lost - No Response'
        LOST_BUDGET_ISSUE = 'lost_budget_issue', 'Lost - Budget Issue'
        FUTURE_PROSPECT = 'future_prospect', 'Future Prospect'
        DUPLICATE_LEAD = 'duplicate_lead', 'Duplicate Lead'

    STATUS_TO_STAGE = {
        Status.NEW: Stage.NEW,
        Status.CALL_PENDING: Stage.CONTACT_ATTEMPT,
        Status.CONTACTED: Stage.CONTACT_ATTEMPT,
        Status.NO_ANSWER: Stage.CONTACT_ATTEMPT,
        Status.CALL_BACK_REQUESTED: Stage.CONTACT_ATTEMPT,
        Status.WRONG_NUMBER: Stage.CONTACT_ATTEMPT,
        Status.UNREACHABLE: Stage.CONTACT_ATTEMPT,
        Status.INTERESTED: Stage.QUALIFICATION,
        Status.HIGHLY_INTERESTED: Stage.QUALIFICATION,
        Status.NEED_MORE_INFORMATION: Stage.QUALIFICATION,
        Status.SITE_VISIT_REQUESTED: Stage.QUALIFICATION,
        Status.BUDGET_MISMATCH: Stage.QUALIFICATION,
        Status.LOCATION_MISMATCH: Stage.QUALIFICATION,
        Status.NOT_INTERESTED: Stage.QUALIFICATION,
        Status.FOLLOW_UP_REQUIRED: Stage.NURTURING,
        Status.BROCHURE_SENT: Stage.NURTURING,
        Status.WHATSAPP_FOLLOW_UP: Stage.NURTURING,
        Status.EMAIL_SENT: Stage.NURTURING,
        Status.SITE_VISIT_SCHEDULED: Stage.SALES_PROCESS,
        Status.SITE_VISIT_COMPLETED: Stage.SALES_PROCESS,
        Status.NEGOTIATION_ONGOING: Stage.SALES_PROCESS,
        Status.DOCUMENTATION_IN_PROGRESS: Stage.SALES_PROCESS,
        Status.BOOKING_AMOUNT_RECEIVED: Stage.SALES_PROCESS,
        Status.UNIT_RESERVED: Stage.SALES_PROCESS,
        Status.CONVERTED_WON: Stage.CLOSED,
        Status.LOST_TO_COMPETITOR: Stage.CLOSED,
        Status.LOST_NO_RESPONSE: Stage.CLOSED,
        Status.LOST_BUDGET_ISSUE: Stage.CLOSED,
        Status.FUTURE_PROSPECT: Stage.CLOSED,
        Status.DUPLICATE_LEAD: Stage.CLOSED,
    }

    # Statuses the calling agent may set via update_lead or post-call analysis.
    AI_SETTABLE_STATUSES = frozenset({
        Status.CONTACTED,
        Status.NO_ANSWER,
        Status.CALL_BACK_REQUESTED,
        Status.WRONG_NUMBER,
        Status.UNREACHABLE,
        Status.INTERESTED,
        Status.HIGHLY_INTERESTED,
        Status.NEED_MORE_INFORMATION,
        Status.SITE_VISIT_REQUESTED,
        Status.BUDGET_MISMATCH,
        Status.LOCATION_MISMATCH,
        Status.NOT_INTERESTED,
        Status.FOLLOW_UP_REQUIRED,
        Status.BROCHURE_SENT,
        Status.WHATSAPP_FOLLOW_UP,
        Status.EMAIL_SENT,
        Status.SITE_VISIT_SCHEDULED,
        Status.SITE_VISIT_COMPLETED,
        Status.NEGOTIATION_ONGOING,
        Status.DOCUMENTATION_IN_PROGRESS,
        Status.BOOKING_AMOUNT_RECEIVED,
        Status.UNIT_RESERVED,
        Status.CONVERTED_WON,
        Status.FUTURE_PROSPECT,
        Status.LOST_NO_RESPONSE,
        Status.LOST_BUDGET_ISSUE,
        Status.LOST_TO_COMPETITOR,
        Status.DUPLICATE_LEAD,
    })

    # Auto-dial / scheduler skips these dispositions.
    NON_CALLABLE_STATUSES = frozenset({
        Status.WRONG_NUMBER,
        Status.UNREACHABLE,
        Status.CONVERTED_WON,
        Status.LOST_TO_COMPETITOR,
        Status.LOST_NO_RESPONSE,
        Status.LOST_BUDGET_ISSUE,
        Status.FUTURE_PROSPECT,
        Status.DUPLICATE_LEAD,
    })

    # Lead information
    name = models.CharField(max_length=255)
    email = models.EmailField(blank=True, default='')
    phone_no = models.CharField(max_length=32, blank=True, default='')
    source = models.CharField(max_length=255, blank=True, default='')
    country = models.CharField(max_length=100, blank=True, default='')

    # Lead requirements
    desired_country = models.CharField(max_length=100, blank=True, default='')
    desired_location = models.CharField(max_length=255, blank=True, default='')
    estimated_budget = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    category = models.CharField(max_length=255, blank=True, default='')
    type = models.CharField(max_length=255, blank=True, default='')
    other_type = models.CharField(max_length=255, blank=True, default='')

    # suggested projects
    project = models.ForeignKey(Project, on_delete=models.CASCADE, null=True, blank=True, related_name="leads")
    projects = models.ManyToManyField(Project, blank=True, related_name="leads_multi")
    
    # scheduled at
    scheduled_at = models.DateTimeField(null=True, blank=True)

    # lead status
    status = models.CharField(max_length=40, choices=Status.choices, default=Status.NEW)
    do_not_contact = models.BooleanField(default=False)

    # Unit relations
    unit = models.ForeignKey('projects.Unit', on_delete=models.SET_NULL, null=True, blank=True, related_name='leads')
    units = models.ManyToManyField('projects.Unit', blank=True, related_name='leads_multi')

    # extra fields for database queries and relations
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='leads')
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='assigned_leads')
    is_assign = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['created_by', '-created_at']),
            models.Index(fields=['status']),
            models.Index(fields=['scheduled_at']),
            models.Index(fields=['email']),
            models.Index(fields=['do_not_contact']),
        ]

    @property
    def stage(self) -> str:
        return self.STATUS_TO_STAGE.get(self.status, self.Stage.NEW)

    def is_callable(self) -> bool:
        if self.do_not_contact:
            return False
        return self.status not in self.NON_CALLABLE_STATUSES

    def is_manually_callable(self) -> bool:
        if self.do_not_contact:
            return False
        blocked = self.NON_CALLABLE_STATUSES - {self.Status.UNREACHABLE}
        return self.status not in blocked

    def save(self, *args, **kwargs):
        self.is_assign = bool(self.assigned_to_id)
        status_changed_by_hook = False
        if self.scheduled_at and (
            not self.status
            or self.status in (self.Status.NEW, self.Status.CALL_PENDING)
        ):
            if self.status != self.Status.CALL_PENDING:
                self.status = self.Status.CALL_PENDING
                status_changed_by_hook = True

        update_fields = kwargs.get('update_fields')
        if update_fields is not None:
            update_fields = set(update_fields)
            update_fields.add('is_assign')
            if status_changed_by_hook:
                update_fields.add('status')
            kwargs['update_fields'] = list(update_fields)
        super().save(*args, **kwargs)


class AgentCommission(models.Model):
    lead = models.ForeignKey(
        Lead,
        on_delete=models.CASCADE,
        related_name='commissions',
    )
    agent = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='agent_commissions',
    )
    project = models.ForeignKey(
        'projects.Project',
        on_delete=models.CASCADE,
        related_name='commissions',
    )
    unit = models.ForeignKey(
        'projects.Unit',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='commissions',
    )
    unit_details = models.JSONField(default=dict, blank=True)
    list_price = models.DecimalField(max_digits=15, decimal_places=2)
    agent_split = models.DecimalField(max_digits=5, decimal_places=2)
    company_split = models.DecimalField(max_digits=5, decimal_places=2)
    agent_commission = models.DecimalField(max_digits=15, decimal_places=2)
    company_commission = models.DecimalField(max_digits=15, decimal_places=2)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['lead'],
                name='unique_lead_commission',
            ),
        ]

    def __str__(self):
        return f"Commission for {self.agent.email} - Lead #{self.lead_id} ({self.agent_commission})"
