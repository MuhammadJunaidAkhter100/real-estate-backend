import uuid

from django.db import models
from django.conf import settings


class ChatSession(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='chat_sessions',
    )
    thread_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    title = models.CharField(max_length=255, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return f"Session {self.id} ({self.thread_id}) - {self.user.email}"


def _knowledge_base_upload_path(instance, filename):
    return f"knowledge_base/user_{instance.user_id}/{filename}"


class KnowledgeBaseDocument(models.Model):
    """A user-uploaded document whose text is extracted asynchronously."""

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        PROCESSING = 'processing', 'Processing'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='knowledge_base_documents',
    )
    file = models.FileField(upload_to=_knowledge_base_upload_path)
    hosted_url = models.URLField(max_length=1000, blank=True, default='')
    original_filename = models.CharField(max_length=255)
    file_type = models.CharField(max_length=10, blank=True, default='')
    associated_country = models.CharField(max_length=100, blank=True, default='')

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING,
    )
    error = models.TextField(blank=True, default='')
    task_id = models.CharField(max_length=255, blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"KBDoc #{self.pk} - {self.original_filename} ({self.status})"
