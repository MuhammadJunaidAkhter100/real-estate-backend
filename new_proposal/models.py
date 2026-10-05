from django.conf import settings
from django.db import models

from projects.models import Project, Unit
from users.models import Lead


def _proposal_upload_path(instance, filename):
    return f"new_proposals/project_{instance.project_id}/unit_{instance.unit_id}/{filename}"


class GeneratedProposal(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="generated_proposals"
    )
    lead = models.ForeignKey(
        Lead, on_delete=models.CASCADE, related_name="generated_proposals"
    )
    unit = models.ForeignKey(
        Unit, on_delete=models.CASCADE, related_name="generated_proposals"
    )
    generated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="generated_proposals",
    )
    file = models.FileField(upload_to=_proposal_upload_path, blank=True, null=True)
    hosted_url = models.URLField(max_length=1000, blank=True, default="")
    ai_facts = models.JSONField(default=dict, blank=True)

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING,
    )
    error = models.TextField(blank=True, default="")
    task_id = models.CharField(max_length=255, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Proposal #{self.pk} - project {self.project_id} / unit {self.unit_id}"
