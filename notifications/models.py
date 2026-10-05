from django.conf import settings
from django.db import models


class Notification(models.Model):
    class Type(models.TextChoices):
        LEAD_ASSIGNED_NO_COMMISSION = (
            'lead_assigned_no_commission',
            'Lead Assigned Without Commission',
        )
        UNIT_MISSING_PROPOSAL_IMAGES = (
            'unit_missing_proposal_images',
            'Unit Missing Proposal Images',
        )
        LEAD_PROJECT_MISSING_IMAGES = (
            'lead_project_missing_images',
            'Lead Project Missing Proposal Images',
        )
        TASK_EXPIRING_SOON = (
            'task_expiring_soon',
            'Task Expiring Soon',
        )
        TASK_EXPIRING_AFTER_24H = (
            'task_expiring_after_24h',
            'Task Expiring After 24 Hours',
        )
        TASK_EXPIRED = (
            'task_expired',
            'Task Expired',
        )
        GENERIC = 'generic', 'Generic'

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='notifications',
    )
    type = models.CharField(
        max_length=64,
        choices=Type.choices,
        default=Type.GENERIC,
    )
    title = models.CharField(max_length=255)
    message = models.TextField(blank=True, default='')
    data = models.JSONField(default=dict, blank=True)
    is_read = models.BooleanField(default=False)
    read_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['recipient', 'is_read', '-created_at']),
        ]

    def __str__(self):
        return f"{self.recipient_id} - {self.type} - {self.title}"
