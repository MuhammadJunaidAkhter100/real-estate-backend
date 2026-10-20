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
    """A knowledge-base document whose text is extracted asynchronously.

    Most are files a user uploaded. One per company can instead be built from
    the company's own website (source="website").
    """

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        PROCESSING = 'processing', 'Processing'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'

    class Source(models.TextChoices):
        UPLOAD = 'upload', 'Upload'
        WEBSITE = 'website', 'Website'

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
    source = models.CharField(
        max_length=20, choices=Source.choices, default=Source.UPLOAD,
    )
    # The website a source="website" document was built from.
    source_url = models.URLField(max_length=1000, blank=True, default='')

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


def _public_kb_upload_path(instance, filename):
    return f"public_knowledge_base/{filename}"


class PublicKnowledgeBaseDocument(models.Model):
    """A public knowledge-base document whose content is vectorized in Pinecone

    for the unauthenticated website chatbot (/api/chatbot/public/stream/).
    Managed exclusively by administrators.
    """

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        PROCESSING = 'processing', 'Processing'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='public_knowledge_base_documents',
    )
    file = models.FileField(upload_to=_public_kb_upload_path)
    hosted_url = models.URLField(max_length=1000, blank=True, default='')
    original_filename = models.CharField(max_length=255)
    file_type = models.CharField(max_length=10, blank=True, default='')
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING,
    )
    error = models.TextField(blank=True, default='')
    task_id = models.CharField(max_length=255, blank=True, default='')
    chunk_count = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"PublicKBDoc #{self.pk} - {self.original_filename} ({self.status})"
