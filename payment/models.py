from django.db import models
import uuid


def generate_referral_code():
    return uuid.uuid4().hex[:12]


class ProcessedStripeEvent(models.Model):
    """Idempotency ledger for inbound Stripe webhook deliveries.

    A unique ``event_id`` makes webhook handling exactly-once: the row is
    inserted (inside a transaction) before the handler runs, so a redelivery
    hits an IntegrityError and is acknowledged without being reprocessed.
    """

    event_id = models.CharField(max_length=255, unique=True)
    event_type = models.CharField(max_length=255, blank=True, default='')
    processed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-processed_at']
        verbose_name = 'Processed Stripe event'
        verbose_name_plural = 'Processed Stripe events'

    def __str__(self):
        return f"{self.event_type or 'event'} {self.event_id}"


class CreditCharge(models.Model):
    """Receipt for a single AI-credit deduction.

    ``task_id`` is the Celery task id (``self.request.id``) or an idempotency key,
    which makes the charge idempotent across retries. Refunding deletes the receipt
    so a retry re-charges cleanly instead of refunding twice.
    """

    task_id = models.CharField(max_length=255, unique=True)
    company = models.ForeignKey(
        'users.Company',
        on_delete=models.CASCADE,
        related_name='credit_charges',
    )
    kind = models.CharField(max_length=50)
    cost = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Credit charge'
        verbose_name_plural = 'Credit charges'

    def __str__(self):
        return f"{self.task_id} ({self.kind}, {self.cost})"


class UsageCounter(models.Model):
    """Per-company, per-kind usage for one billing period.

    ``used`` is only ever mutated under a row lock so concurrent requests
    cannot push a company past its plan limit.
    """

    class Kind(models.TextChoices):
        USERS = 'users', 'Users'
        AGENTS = 'agents', 'Agents'
        TEAM_MANAGERS = 'team_managers', 'Team Managers'
        AI_PROPOSALS = 'ai_proposals', 'AI Proposals'
        AI_CREDITS = 'ai_credits', 'AI Credits'

    company = models.ForeignKey(
        'users.Company',
        on_delete=models.CASCADE,
        related_name='usage_counters',
    )
    kind = models.CharField(max_length=50, choices=Kind.choices)
    period_start = models.DateTimeField()
    used = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['company', 'kind', '-period_start']
        constraints = [
            models.UniqueConstraint(
                fields=['company', 'kind', 'period_start'],
                name='unique_payment_usage_counter_per_period',
            ),
        ]
        verbose_name = 'Usage counter'
        verbose_name_plural = 'Usage counters'

    def __str__(self):
        return f"{self.company_id} {self.kind} {self.period_start:%Y-%m} = {self.used}"


class ReferralInvitation(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        JOINED = 'joined', 'Joined'

    inviter_company = models.ForeignKey(
        'users.Company',
        on_delete=models.CASCADE,
        related_name='referral_invitations',
    )
    inviter_user = models.ForeignKey(
        'users.User',
        on_delete=models.CASCADE,
        related_name='referral_invitations',
    )
    invited_email = models.EmailField(blank=True, default='')
    referral_code = models.CharField(
        max_length=12,
        unique=True,
        default=generate_referral_code,
        editable=False,
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    joined_company = models.OneToOneField(
        'users.Company',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='referral_invitation',
    )
    reward_credits = models.PositiveIntegerField(default=0)
    joined_reward_credits = models.PositiveIntegerField(default=0)
    reward_period_start = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    @property
    def source(self):
        return 'email' if self.invited_email else 'link'

    def __str__(self):
        return f"{self.referral_code} ({self.status})"


class SubscriptionPlan(models.Model):
    """Dynamic subscription plans created and managed by admins."""

    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=100, unique=True, blank=True)
    description = models.TextField(blank=True, default='')
    icon = models.CharField(max_length=100, blank=True, default='')
    badge = models.CharField(max_length=100, blank=True, default='')

    # Pricing
    monthly_price = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    yearly_price = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    currency = models.CharField(max_length=10, default='USD')

    # Software limits
    ai_credits = models.PositiveIntegerField(null=True, blank=True, default=100)
    is_credits_unlimited = models.BooleanField(default=False)

    agents = models.PositiveIntegerField(null=True, blank=True, default=5)
    is_agents_unlimited = models.BooleanField(default=False)

    team_managers = models.PositiveIntegerField(null=True, blank=True, default=1)
    is_team_managers_unlimited = models.BooleanField(default=False)

    # Software features
    features = models.JSONField(default=dict, blank=True)
    custom_features = models.JSONField(default=list, blank=True)

    # Status & order
    is_active = models.BooleanField(default=True)
    order = models.IntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['order', 'id']
        verbose_name = 'Subscription Plan'
        verbose_name_plural = 'Subscription Plans'

    def __str__(self):
        return f"{self.name} (${self.monthly_price}/mo)"

    def save(self, *args, **kwargs):
        from django.utils.text import slugify

        if not self.slug:
            base_slug = slugify(self.name) or 'plan'
            slug = base_slug
            idx = 1
            while SubscriptionPlan.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base_slug}-{idx}"
                idx += 1
            self.slug = slug

        if not self.pk and not self.order:
            from django.db.models import Max
            max_order = SubscriptionPlan.objects.aggregate(m=Max('order'))['m'] or 0
            self.order = max_order + 1

        if self.is_credits_unlimited:
            self.ai_credits = None
        if self.is_agents_unlimited:
            self.agents = None
        if self.is_team_managers_unlimited:
            self.team_managers = None

        super().save(*args, **kwargs)

    @property
    def users_limit(self):
        """Total users = managers + agents, or None if any is unlimited."""
        if self.is_agents_unlimited or self.is_team_managers_unlimited:
            return None
        return (self.team_managers or 0) + (self.agents or 0)


class StripePlanPrice(models.Model):
    class Interval(models.TextChoices):
        MONTHLY = 'monthly', 'Monthly'
        YEARLY = 'yearly', 'Yearly'

    plan_slug = models.CharField(max_length=100)
    interval = models.CharField(max_length=10, choices=Interval.choices)
    stripe_price_id = models.CharField(max_length=255, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['plan_slug', 'interval'],
                name='unique_stripe_price_per_plan_interval',
            ),
        ]

    def __str__(self):
        return f'{self.plan_slug} ({self.interval}): {self.stripe_price_id}'

