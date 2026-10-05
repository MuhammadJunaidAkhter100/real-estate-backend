"""
Signal handlers that emit notifications for domain events.

Currently handled:
  - When a Lead is (re)saved with an ``assigned_to`` agent but no
    ``ProjectAgentAssignment`` (commission split) exists for that agent on
    the lead's project, notify the agent's company admin(s).
"""
from __future__ import annotations

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from .services import notify_lead_missing_commission

logger = logging.getLogger(__name__)


@receiver(post_save, sender='users.Lead')
def _lead_post_save(sender, instance, created, **kwargs):
    try:
        notify_lead_missing_commission(instance)
    except Exception:  # noqa: BLE001
        logger.exception("Notification dispatch failed for Lead %s", instance.pk)
