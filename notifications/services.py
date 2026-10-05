"""
Notification dispatch helper.

`send_notification(...)` persists a row in the DB and (best-effort) pushes it
over the WebSocket channel layer to a per-user group `notifications_<user_id>`.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from django.contrib.auth import get_user_model

from .models import Notification
from .serializers import NotificationSerializer

logger = logging.getLogger(__name__)

User = get_user_model()


def user_group_name(user_id: int) -> str:
    return f"notifications_{user_id}"


def send_notification(
    recipient,
    type: str,
    title: str,
    message: str = "",
    data: Optional[dict[str, Any]] = None,
) -> Notification:
    """
    Create a Notification row and push it over WebSocket to the recipient.

    `recipient` may be a User instance or a user id.
    Returns the created Notification.
    """
    if hasattr(recipient, 'pk'):
        recipient_id = recipient.pk
    else:
        recipient_id = int(recipient)

    notification = Notification.objects.create(
        recipient_id=recipient_id,
        type=type,
        title=title,
        message=message or "",
        data=data or {},
    )

    _broadcast(notification)
    return notification


def notify_lead_missing_commission(lead, extra_recipients=None) -> list[Notification]:
    """
    If ``lead.assigned_to`` is set but there is no ``ProjectAgentAssignment``
    (commission split) for that agent on ``lead.project``, notify the relevant
    company admins (plus ``extra_recipients``) so they can set the commission.

    Returns the list of Notification rows actually created (may be empty when
    everything is already configured or a duplicate unread notification
    already exists).
    """
    if lead is None:
        return []

    agent = getattr(lead, 'assigned_to', None)
    if agent is None:
        return []

    project = getattr(lead, 'project', None)

    # If a project is set, only notify when commission is actually missing.
    if project is not None:
        from projects.models import ProjectAgentAssignment
        has_commission = ProjectAgentAssignment.objects.filter(
            agent=agent, project=project,
        ).exists()
        if has_commission:
            return []

    # Recipients: company_admins of the agent's company + any extras (e.g. the
    # user who initiated the assignment, so they see the alert immediately).
    recipient_ids: set[int] = set()

    company_id = getattr(agent, 'company_id', None)
    if company_id:
        admin_ids = User.objects.filter(
            company_id=company_id,
            role=User.Role.COMPANY_ADMIN,
            is_active=True,
        ).values_list('pk', flat=True)
        recipient_ids.update(admin_ids)

    for candidate in extra_recipients or []:
        if candidate is None:
            continue
        pk = getattr(candidate, 'pk', None) or getattr(candidate, 'id', None)
        if pk:
            recipient_ids.add(int(pk))

    if not recipient_ids:
        return []

    project_title = project.title if project is not None else ''
    project_id = project.pk if project is not None else None
    agent_name = agent.full_name or agent.email

    if project is not None:
        message = (
            f"Lead '{lead.name}' was assigned to {agent_name} on project "
            f"'{project_title}', but no commission split is configured for "
            f"this agent on this project."
        )
    else:
        message = (
            f"Lead '{lead.name}' was assigned to {agent_name}, but no "
            f"commission split is configured for this agent on any project yet."
        )

    data = {
        "lead_id": lead.pk,
        "lead_name": lead.name,
        "project_id": project_id,
        "project_title": project_title,
        "agent_id": agent.pk,
        "agent_email": agent.email,
        "agent_name": agent_name,
    }
    title = "Commission missing for assigned lead"

    created: list[Notification] = []
    for admin_id in recipient_ids:
        # Dedupe: avoid spamming the same recipient for the same
        # lead+agent+project combo if this is called more than once.
        exists = Notification.objects.filter(
            recipient_id=admin_id,
            type=Notification.Type.LEAD_ASSIGNED_NO_COMMISSION,
            is_read=False,
            data__lead_id=lead.pk,
            data__agent_id=agent.pk,
            data__project_id=project_id,
        ).exists()
        if exists:
            continue

        created.append(
            send_notification(
                recipient=admin_id,
                type=Notification.Type.LEAD_ASSIGNED_NO_COMMISSION,
                title=title,
                message=message,
                data=data,
            )
        )
    return created


def _unit_image_prefix(category: str) -> str:
    """Map a unit category (e.g. '3 Bed') to its proposal_images key prefix ('3_bed')."""
    category = (category or '').strip().lower()
    if not category:
        return ''
    _MAP = {
        '1 bed': '1_bed',
        '2 bed': '2_bed',
        '3 bed': '3_bed',
        'studio': 'studio',
    }
    return _MAP.get(category, category.replace(' ', '_'))


def _required_image_keys(prefix: str) -> list[str]:
    """Return the proposal_images keys required for a unit category prefix."""
    return [
        f'{prefix}_sample_layout',
        f'{prefix}_kitchen_dining',
        f'{prefix}_master_bedroom',
    ]


def get_project_missing_unit_images(project) -> dict[str, list[str]]:
    """
    Inspect a project's units and its ``proposal_images`` and return the
    proposal image keys that are missing per unit category prefix.

    Returns a dict like::

        {"3_bed": ["3_bed_sample_layout", "3_bed_kitchen_dining"], ...}

    An empty dict means every unit category has all its required images.
    """
    if project is None:
        return {}

    proposal_images = getattr(project, 'proposal_images', None) or {}

    def _has_images(key: str) -> bool:
        value = proposal_images.get(key)
        return isinstance(value, list) and any(value)

    missing: dict[str, list[str]] = {}
    seen_prefixes: set[str] = set()
    for unit in project.units.all():
        prefix = _unit_image_prefix(getattr(unit, 'category', ''))
        if not prefix or prefix in seen_prefixes:
            continue
        seen_prefixes.add(prefix)

        missing_keys = [k for k in _required_image_keys(prefix) if not _has_images(k)]
        if missing_keys:
            missing[prefix] = missing_keys
    return missing


def notify_missing_unit_images(unit) -> list[Notification]:
    """
    When a Unit is created/updated, verify the parent project has proposal
    images for that unit's bed category. If the required images are missing,
    notify all super admins so they can upload them.

    Required image keys per unit category prefix (e.g. '3_bed'):
        - <prefix>_sample_layout
        - <prefix>_kitchen_dining
        - <prefix>_master_bedroom

    Returns the list of Notification rows created (may be empty when the
    images already exist or a duplicate unread notification exists).
    """
    if unit is None:
        return []

    project = getattr(unit, 'project', None)
    if project is None:
        return []

    prefix = _unit_image_prefix(getattr(unit, 'category', ''))
    if not prefix:
        return []

    proposal_images = getattr(project, 'proposal_images', None) or {}

    required_keys = _required_image_keys(prefix)

    def _has_images(key: str) -> bool:
        value = proposal_images.get(key)
        return isinstance(value, list) and any(value)

    missing_keys = [key for key in required_keys if not _has_images(key)]
    if not missing_keys:
        return []

    recipient_ids = set(
        User.objects.filter(
            role=User.Role.SUPERADMIN,
            is_active=True,
        ).values_list('pk', flat=True)
    )
    if not recipient_ids:
        return []

    category_label = (getattr(unit, 'category', '') or prefix.replace('_', ' ')).strip()
    project_title = project.title
    title = f"Missing {category_label} images for '{project_title}'"
    message = (
        f"A '{category_label}' unit ('{unit.label}') was added to project "
        f"'{project_title}', but the following proposal images are missing: "
        f"{', '.join(missing_keys)}. Please upload the {category_label} images."
    )
    data = {
        'project_id': project.pk,
        'project_title': project_title,
        'unit_id': unit.pk,
        'unit_label': unit.label,
        'unit_category': category_label,
        'category_prefix': prefix,
        'missing_image_keys': missing_keys,
    }

    created: list[Notification] = []
    for admin_id in recipient_ids:
        # Dedupe: avoid spamming the same super admin for the same
        # project + category combo while an unread alert already exists.
        exists = Notification.objects.filter(
            recipient_id=admin_id,
            type=Notification.Type.UNIT_MISSING_PROPOSAL_IMAGES,
            is_read=False,
            data__project_id=project.pk,
            data__category_prefix=prefix,
        ).exists()
        if exists:
            continue

        created.append(
            send_notification(
                recipient=admin_id,
                type=Notification.Type.UNIT_MISSING_PROPOSAL_IMAGES,
                title=title,
                message=message,
                data=data,
            )
        )
    return created


def notify_lead_project_missing_images(lead, assigned_by=None) -> list[Notification]:
    """
    When a lead that references a project is assigned to an agent/team manager,
    check whether that project's units all have their required proposal images.

    If images are missing:
      * Notify all super admins and the assigned agent's company admins
        (they can add the images) — via notification + email.
      * Notify the person who assigned the lead (``assigned_by``) when they are
        an agent or team manager, telling them to contact the super admin
        because the missing images won't appear in the proposal.

    Returns the list of Notification rows created.
    """
    if lead is None:
        return []

    project = getattr(lead, 'project', None)
    if project is None:
        return []

    missing = get_project_missing_unit_images(project)
    if not missing:
        return []

    agent = getattr(lead, 'assigned_to', None)
    project_title = project.title

    # Flatten the missing image keys for messaging / data payload.
    missing_categories = sorted(missing.keys())
    missing_keys_flat = sorted({k for keys in missing.values() for k in keys})
    categories_display = ', '.join(c.replace('_', ' ') for c in missing_categories)

    agent_name = ''
    if agent is not None:
        agent_name = agent.full_name or agent.email

    base_data = {
        'lead_id': lead.pk,
        'lead_name': lead.name,
        'project_id': project.pk,
        'project_title': project_title,
        'agent_id': agent.pk if agent is not None else None,
        'agent_name': agent_name,
        'missing_categories': missing_categories,
        'missing_image_keys': missing_keys_flat,
    }

    # ---- Admin recipients (super admins + agent's company admins) ----
    admin_ids: set[int] = set(
        User.objects.filter(
            role=User.Role.SUPERADMIN,
            is_active=True,
        ).values_list('pk', flat=True)
    )

    company_id = getattr(agent, 'company_id', None) if agent is not None else None
    if company_id:
        admin_ids.update(
            User.objects.filter(
                company_id=company_id,
                role=User.Role.COMPANY_ADMIN,
                is_active=True,
            ).values_list('pk', flat=True)
        )

    admin_title = f"Missing proposal images for project '{project_title}'"
    admin_message = (
        f"Lead '{lead.name}' was assigned to {agent_name or 'an agent'} on "
        f"project '{project_title}', but proposal images are missing for the "
        f"following unit categories: {categories_display}. These units will not "
        f"appear in the proposal until the images are added. Please upload the "
        f"missing images ({', '.join(missing_keys_flat)})."
    )

    created: list[Notification] = []
    for admin_id in admin_ids:
        exists = Notification.objects.filter(
            recipient_id=admin_id,
            type=Notification.Type.LEAD_PROJECT_MISSING_IMAGES,
            is_read=False,
            data__lead_id=lead.pk,
            data__project_id=project.pk,
        ).exists()
        if exists:
            continue

        created.append(
            send_notification(
                recipient=admin_id,
                type=Notification.Type.LEAD_PROJECT_MISSING_IMAGES,
                title=admin_title,
                message=admin_message,
                data={**base_data, 'role': 'admin'},
            )
        )

    # ---- Notify the assigner if they are an agent / team manager ----
    assigner_role = getattr(assigned_by, 'role', None)
    if assigned_by is not None and assigner_role in (
        User.Role.AGENT,
        User.Role.TEAM_MANAGER,
    ):
        assigner_id = assigned_by.pk
        already = Notification.objects.filter(
            recipient_id=assigner_id,
            type=Notification.Type.LEAD_PROJECT_MISSING_IMAGES,
            is_read=False,
            data__lead_id=lead.pk,
            data__project_id=project.pk,
        ).exists()
        if not already:
            assigner_title = (
                f"Action needed: images missing for project '{project_title}'"
            )
            assigner_message = (
                f"The project '{project_title}' linked to lead '{lead.name}' is "
                f"missing proposal images for these unit categories: "
                f"{categories_display}. These units will not appear in the "
                f"proposal. Please connect with the super admin to have the "
                f"relevant unit images added."
            )
            created.append(
                send_notification(
                    recipient=assigner_id,
                    type=Notification.Type.LEAD_PROJECT_MISSING_IMAGES,
                    title=assigner_title,
                    message=assigner_message,
                    data={**base_data, 'role': 'assigner'},
                )
            )

    # ---- Send emails (best-effort, async) ----
    try:
        from users.tasks import send_lead_project_missing_images_email
        recipient_email_ids = list(admin_ids)
        if (
            assigned_by is not None
            and assigner_role in (User.Role.AGENT, User.Role.TEAM_MANAGER)
        ):
            recipient_email_ids.append(assigned_by.pk)

        for uid in set(recipient_email_ids):
            send_lead_project_missing_images_email.delay(
                user_id=uid,
                lead_id=lead.pk,
                project_id=project.pk,
                missing_categories=missing_categories,
                missing_image_keys=missing_keys_flat,
                is_assigner=(
                    assigned_by is not None and uid == assigned_by.pk
                ),
            )
    except Exception:  # noqa: BLE001
        logger.exception(
            "Failed to enqueue lead-project missing-images emails for lead %s",
            lead.pk,
        )

    return created


def _broadcast(notification: Notification) -> None:
    try:
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync
    except ImportError:
        logger.debug("channels not installed; skipping WebSocket broadcast")
        return

    channel_layer = get_channel_layer()
    if channel_layer is None:
        logger.debug("No channel layer configured; skipping WebSocket broadcast")
        return

    payload = NotificationSerializer(notification).data
    try:
        async_to_sync(channel_layer.group_send)(
            user_group_name(notification.recipient_id),
            {"type": "notification.new", "payload": payload},
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to broadcast notification %s", notification.pk)
