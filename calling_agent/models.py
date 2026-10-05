from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class Call(models.Model):
    class Direction(models.TextChoices):
        OUTBOUND = 'outbound', 'Outbound'

    class Trigger(models.TextChoices):
        MANUAL = 'manual', 'Manual'
        SCHEDULED = 'scheduled', 'Scheduled'

    class Status(models.TextChoices):
        SCHEDULED = 'scheduled', 'Scheduled'
        CLAIMED = 'claimed', 'Claimed'
        INITIATING = 'initiating', 'Initiating'
        INITIATION_UNKNOWN = 'initiation_unknown', 'Initiation Unknown'
        RINGING = 'ringing', 'Ringing'
        IN_PROGRESS = 'in_progress', 'In Progress'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'
        CANCELLED = 'cancelled', 'Cancelled'
        NO_ANSWER = 'no_answer', 'No Answer'
        BUSY = 'busy', 'Busy'

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    company = models.ForeignKey(
        'users.Company',
        on_delete=models.PROTECT,
        related_name='calls',
    )
    lead = models.ForeignKey(
        'users.Lead',
        on_delete=models.SET_NULL,
        related_name='calls',
        null=True,
        blank=True,
    )
    related_task = models.ForeignKey(
        'users.Task',
        on_delete=models.SET_NULL,
        related_name='scheduled_calls',
        null=True,
        blank=True,
    )
    context_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name='context_calls',
        null=True,
        blank=True,
    )

    lead_name = models.CharField(max_length=255, blank=True, default='')
    phone_number = models.CharField(max_length=32, blank=True, default='')
    outbound_number = models.CharField(max_length=32, blank=True, default='')
    direction = models.CharField(
        max_length=16,
        choices=Direction.choices,
        default=Direction.OUTBOUND,
    )
    trigger = models.CharField(
        max_length=16,
        choices=Trigger.choices,
        default=Trigger.MANUAL,
    )

    agent_config_key = models.CharField(max_length=64, default='default')
    provider_agent_id = models.CharField(max_length=255, blank=True, default='')
    provider_phone_number_id = models.CharField(max_length=255, blank=True, default='')
    provider_conversation_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        unique=True,
    )
    provider_call_id = models.CharField(max_length=255, blank=True, default='')
    initiation_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)

    scheduled_for = models.DateTimeField(null=True, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    claim_expires_at = models.DateTimeField(null=True, blank=True)
    attempt_number = models.PositiveSmallIntegerField(default=1)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    initiated_at = models.DateTimeField(null=True, blank=True)
    answered_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.PositiveIntegerField(null=True, blank=True)
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.SCHEDULED,
    )
    failure_code = models.CharField(max_length=64, blank=True, default='')
    failure_detail = models.CharField(max_length=500, blank=True, default='')

    recording_storage_key = models.CharField(max_length=1000, blank=True, default='')
    recording_content_type = models.CharField(max_length=100, blank=True, default='')
    recording_size_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    recording_available = models.BooleanField(default=False)
    recording_fetched_at = models.DateTimeField(null=True, blank=True)

    transcript_data = models.JSONField(default=list, blank=True)
    summary = models.TextField(blank=True, default="")
    key_sentiments = models.JSONField(default=list, blank=True)
    detected_intents = models.JSONField(default=list, blank=True)
    provider_analysis = models.JSONField(default=dict, blank=True)
    call_insights = models.JSONField(
        default=dict,
        blank=True,
        help_text='Structured post-call insights: objections, preferences, etc.',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['lead', 'scheduled_for', 'attempt_number'],
                condition=models.Q(
                    lead__isnull=False,
                    scheduled_for__isnull=False,
                ),
                name='unique_scheduled_call_attempt',
            ),
        ]
        indexes = [
            models.Index(
                fields=['status', 'scheduled_for'],
                name='calling_status_sched_idx',
            ),
            models.Index(
                fields=['company', '-created_at'],
                name='calling_company_created_idx',
            ),
            models.Index(
                fields=['lead', '-created_at'],
                name='calling_lead_created_idx',
            ),
            models.Index(
                fields=['provider_call_id'],
                name='calling_provider_call_idx',
            ),
        ]

    @property
    def duration(self) -> str:
        if self.duration_seconds is None:
            return ''
        minutes, seconds = divmod(self.duration_seconds, 60)
        return f'{minutes:02d}:{seconds:02d}'

    def __str__(self) -> str:
        return f"Call with {self.lead_name} ({self.phone_number}) - {self.status}"


class CallGeneratedAction(models.Model):
    class ActionType(models.TextChoices):
        CREATE_TASK = 'create_task', 'Create Task'
        UPDATE_LEAD = 'update_lead', 'Update Lead'
        TRANSFER_FALLBACK = 'transfer_fallback', 'Transfer Fallback'

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'

    call = models.ForeignKey(
        Call,
        on_delete=models.CASCADE,
        related_name='generated_actions',
    )
    action_type = models.CharField(
        max_length=32,
        choices=ActionType.choices,
    )
    title = models.CharField(max_length=255)
    payload = models.JSONField(default=dict, blank=True)
    idempotency_key = models.UUIDField(unique=True)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    task = models.ForeignKey(
        'users.Task',
        on_delete=models.SET_NULL,
        related_name='generated_call_actions',
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(
                fields=['call', 'action_type', '-created_at'],
                name='calling_action_lookup_idx',
            ),
        ]

    def __str__(self) -> str:
        return f'{self.action_type} for call {self.call_id}'


class CallWebhookEvent(models.Model):
    class EventType(models.TextChoices):
        POST_CALL_TRANSCRIPTION = (
            'post_call_transcription',
            'Post Call Transcription',
        )
        POST_CALL_AUDIO = 'post_call_audio', 'Post Call Audio'
        CALL_INITIATION_FAILURE = (
            'call_initiation_failure',
            'Call Initiation Failure',
        )
        UNKNOWN = 'unknown', 'Unknown'

    class ProcessStatus(models.TextChoices):
        PENDING = 'pending', 'Pending'
        PROCESSING = 'processing', 'Processing'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'
        IGNORED = 'ignored', 'Ignored'

    event_key = models.CharField(max_length=255, unique=True)
    event_type = models.CharField(
        max_length=64,
        choices=EventType.choices,
        default=EventType.UNKNOWN,
    )
    conversation_id = models.CharField(
        max_length=255,
        blank=True,
        default='',
        db_index=True,
    )
    call = models.ForeignKey(
        Call,
        on_delete=models.SET_NULL,
        related_name='webhook_events',
        null=True,
        blank=True,
    )
    body_hash = models.CharField(max_length=64)
    payload = models.JSONField(default=dict, blank=True)
    process_status = models.CharField(
        max_length=16,
        choices=ProcessStatus.choices,
        default=ProcessStatus.PENDING,
    )
    retry_count = models.PositiveSmallIntegerField(default=0)
    last_error = models.CharField(max_length=500, blank=True, default='')
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-received_at']
        indexes = [
            models.Index(
                fields=['process_status', 'received_at'],
                name='calling_webhook_status_idx',
            ),
        ]

    def __str__(self) -> str:
        return f'{self.event_type} ({self.event_key})'


class CallTranscriptTurn(models.Model):
    class Speaker(models.TextChoices):
        AGENT = 'agent', 'Agent'
        CUSTOMER = 'customer', 'Customer'
        SYSTEM = 'system', 'System'
        UNKNOWN = 'unknown', 'Unknown'

    call = models.ForeignKey(
        Call,
        on_delete=models.CASCADE,
        related_name='transcript_turns',
    )
    turn_index = models.PositiveIntegerField()
    speaker = models.CharField(
        max_length=16,
        choices=Speaker.choices,
        default=Speaker.UNKNOWN,
    )
    message = models.TextField(blank=True, default='')
    started_at = models.DateTimeField(null=True, blank=True)
    raw = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['turn_index']
        constraints = [
            models.UniqueConstraint(
                fields=['call', 'turn_index'],
                name='unique_call_transcript_turn',
            ),
        ]

    def __str__(self) -> str:
        return f'Turn {self.turn_index} ({self.speaker})'
