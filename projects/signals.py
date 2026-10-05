from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from projects.models import Project, Unit


def _refresh_project_counts(project_id):
    if not project_id:
        return
    project = Project.objects.filter(pk=project_id).first()
    if project is not None:
        project.recalculate_unit_counts(save=True)


@receiver(post_save, sender=Unit)
def _unit_saved(sender, instance, **kwargs):
    _refresh_project_counts(instance.project_id)

    # Notify super admins when the project is missing proposal images for
    # this unit's bed category (e.g. a 3 Bed unit added but no 3 bed images).
    try:
        from notifications.services import notify_missing_unit_images
        notify_missing_unit_images(instance)
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).exception(
            "Failed to check/notify missing proposal images for unit %s",
            instance.pk,
        )


@receiver(post_delete, sender=Unit)
def _unit_deleted(sender, instance, **kwargs):
    _refresh_project_counts(instance.project_id)
