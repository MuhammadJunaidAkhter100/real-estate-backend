from django.db import models


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

    ``task_id`` is the Celery task id (``self.request.id``), which makes the
    charge idempotent across task retries. Refunding deletes the receipt so a
    retry re-charges cleanly instead of refunding twice.
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
                name='unique_usage_counter_per_period',
            ),
        ]
        verbose_name = 'Usage counter'
        verbose_name_plural = 'Usage counters'

    def __str__(self):
        return f"{self.company_id} {self.kind} {self.period_start:%Y-%m} = {self.used}"