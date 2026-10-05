from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from calling_agent.exceptions import (
    CallConflictError,
    ToolActionRejectedError,
    ToolContextUnavailableError,
)
from calling_agent.models import Call, CallGeneratedAction
from calling_agent.services import _select_for_update_rows
from projects.models import Project
from users.models import Lead, Task, User

ACTIVE_TOOL_STATUSES = {
    Call.Status.RINGING,
    Call.Status.IN_PROGRESS,
}
CLOSED_STAGE = Lead.Stage.CLOSED


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (date, Decimal)):
        return str(value)
    return value


def build_tool_idempotency_key(
    action_type: str,
    call: Call,
    payload: dict[str, Any],
) -> uuid.UUID:
    """Derive a stable key from call + action + payload (no client UUID)."""
    digest = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            default=str,
            separators=(',', ':'),
        ).encode('utf-8')
    ).hexdigest()
    return uuid.uuid5(
        uuid.NAMESPACE_URL,
        f'{action_type}:{call.public_id}:{digest}',
    )


def _lock_active_call(call: Call) -> Call:
    locked_call = (
        Call.objects.select_for_update(of=('self',))
        .select_related(
            'lead',
            'lead__created_by',
            'lead__assigned_to',
            'context_user',
        )
        .filter(pk=call.pk)
        .first()
    )
    if (
        locked_call is None
        or locked_call.status not in ACTIVE_TOOL_STATUSES
        or locked_call.lead is None
        or locked_call.lead.created_by.company_id != locked_call.company_id
    ):
        raise ToolContextUnavailableError(
            'The call no longer has valid write context.'
        )
    return locked_call


def _task_owner(call: Call) -> User:
    lead = call.lead
    if lead is None:
        raise ToolContextUnavailableError(
            'The call no longer has valid lead context.'
        )
    if (
        lead.assigned_to is not None
        and lead.assigned_to.company_id == call.company_id
    ):
        return lead.assigned_to
    if lead.created_by.company_id == call.company_id:
        return lead.created_by
    raise ToolContextUnavailableError(
        'The call no longer has a valid task owner.'
    )


def _validate_existing_action(
    action: CallGeneratedAction,
    *,
    call: Call,
    action_type: str,
    payload: dict[str, Any],
) -> CallGeneratedAction:
    if (
        action.call_id != call.pk
        or action.action_type != action_type
        or action.payload != payload
    ):
        raise CallConflictError(
            'The idempotency key was already used for another action.'
        )
    if action.status != CallGeneratedAction.Status.COMPLETED:
        raise CallConflictError('The existing action is not complete.')
    return action


def _find_existing_action(
    *,
    call: Call,
    action_type: str,
    idempotency_key: uuid.UUID,
    payload: dict[str, Any],
) -> CallGeneratedAction | None:
    action = CallGeneratedAction.objects.select_related('task').filter(
        idempotency_key=idempotency_key
    ).first()
    if action is None:
        return None
    return _validate_existing_action(
        action,
        call=call,
        action_type=action_type,
        payload=payload,
    )


class TaskActionService:
    def create_task(
        self,
        *,
        call: Call,
        title: str,
        priority: str,
        reason: str,
        scheduled_at: datetime | None = None,
        due_date: date | None = None,
        task_type: str | None = None,
        open_ended: bool = False,
    ) -> tuple[CallGeneratedAction, Task, bool]:
        if open_ended:
            scheduled_at = None
            due_date = None
        resolved_due_date = due_date
        if resolved_due_date is None and scheduled_at is not None:
            resolved_due_date = timezone.localtime(scheduled_at).date()
        resolved_type = task_type or Task.Type.CALLBACK
        return self._create_task(
            call=call,
            action_type=CallGeneratedAction.ActionType.CREATE_TASK,
            title=title,
            priority=priority,
            due_date=resolved_due_date,
            scheduled_at=scheduled_at,
            task_type=resolved_type,
            reason=reason,
            open_ended=open_ended,
        )

    def create_transfer_fallback(
        self,
        *,
        call: Call,
        reason: str,
    ) -> tuple[CallGeneratedAction, Task, bool]:
        return self._create_task(
            call=call,
            action_type=CallGeneratedAction.ActionType.TRANSFER_FALLBACK,
            title='Follow up on requested call transfer',
            priority=Task.Priority.HIGH,
            due_date=None,
            scheduled_at=None,
            task_type=None,
            reason=reason,
            open_ended=False,
        )

    def create_follow_up_task(
        self,
        *,
        call: Call,
        title: str,
        priority: str,
        due_date: date | None,
        reason: str,
    ) -> tuple[CallGeneratedAction, Task, bool]:
        """Create a non-callback CRM task (e.g. proposal follow-up)."""
        return self._create_task(
            call=call,
            action_type=CallGeneratedAction.ActionType.CREATE_TASK,
            title=title,
            priority=priority,
            due_date=due_date,
            scheduled_at=None,
            task_type=None,
            reason=reason,
            open_ended=False,
        )

    def _create_task(
        self,
        *,
        call: Call,
        action_type: str,
        title: str,
        priority: str,
        due_date: date | None,
        scheduled_at: datetime | None,
        task_type: str | None,
        reason: str,
        open_ended: bool = False,
    ) -> tuple[CallGeneratedAction, Task, bool]:
        payload = {
            'title': title,
            'priority': priority,
            'due_date': _json_value(due_date),
            'scheduled_at': _json_value(scheduled_at),
            'type': task_type,
            'reason': reason,
            'open_ended': open_ended,
        }
        idempotency_key = build_tool_idempotency_key(
            action_type,
            call,
            payload,
        )
        existing = _find_existing_action(
            call=call,
            action_type=action_type,
            idempotency_key=idempotency_key,
            payload=payload,
        )
        if existing is not None:
            if existing.task is None:
                raise CallConflictError(
                    'The existing action has no linked task.'
                )
            return existing, existing.task, False

        with transaction.atomic():
            locked_call = _lock_active_call(call)
            try:
                with transaction.atomic():
                    action = CallGeneratedAction.objects.create(
                        call=locked_call,
                        action_type=action_type,
                        title=title,
                        payload=payload,
                        idempotency_key=idempotency_key,
                    )
            except IntegrityError:
                existing = _find_existing_action(
                    call=locked_call,
                    action_type=action_type,
                    idempotency_key=idempotency_key,
                    payload=payload,
                )
                if existing is None or existing.task is None:
                    raise
                return existing, existing.task, False

            owner = _task_owner(locked_call)
            task = Task.objects.create(
                name=title,
                associated_country=(
                    locked_call.lead.desired_country
                    or locked_call.lead.country
                ),
                priority=priority,
                type=task_type,
                due_date=due_date,
                scheduled_at=scheduled_at,
                open_ended=open_ended,
                created_by=owner,
                related_lead=locked_call.lead,
                related_call=locked_call,
            )
            action.task = task
            action.status = CallGeneratedAction.Status.COMPLETED
            action.save(
                update_fields=['task', 'status', 'updated_at']
            )
        return action, task, True


class LeadUpdateActionService:
    def update(
        self,
        *,
        call: Call,
        changes: dict[str, Any],
        reason: str,
        assigned_project: Project | None = None,
    ) -> tuple[CallGeneratedAction, Lead, bool]:
        payload_changes = {
            field: _json_value(value)
            for field, value in sorted(changes.items())
        }
        if assigned_project is not None:
            payload_changes['assigned_project_id'] = assigned_project.pk
        payload = {
            'changes': payload_changes,
            'reason': reason,
        }
        idempotency_key = build_tool_idempotency_key(
            CallGeneratedAction.ActionType.UPDATE_LEAD,
            call,
            payload,
        )
        existing = _find_existing_action(
            call=call,
            action_type=CallGeneratedAction.ActionType.UPDATE_LEAD,
            idempotency_key=idempotency_key,
            payload=payload,
        )
        if existing is not None:
            lead = call.lead
            if lead is None:
                raise ToolContextUnavailableError(
                    'The call no longer has valid lead context.'
                )
            lead.refresh_from_db()
            return existing, lead, False

        with transaction.atomic():
            locked_call = _lock_active_call(call)
            lead = _select_for_update_rows(
                Lead.objects.filter(pk=locked_call.lead_id)
            ).get()
            self._validate_changes(
                lead=lead,
                changes=changes,
                assigned_project=assigned_project,
            )
            try:
                with transaction.atomic():
                    action = CallGeneratedAction.objects.create(
                        call=locked_call,
                        action_type=(
                            CallGeneratedAction.ActionType.UPDATE_LEAD
                        ),
                        title='Lead updated during call',
                        payload=payload,
                        idempotency_key=idempotency_key,
                    )
            except IntegrityError:
                existing = _find_existing_action(
                    call=locked_call,
                    action_type=(
                        CallGeneratedAction.ActionType.UPDATE_LEAD
                    ),
                    idempotency_key=idempotency_key,
                    payload=payload,
                )
                if existing is None:
                    raise
                lead.refresh_from_db()
                return existing, lead, False

            update_fields = list(changes.keys())
            for field, value in changes.items():
                setattr(lead, field, value)
            if assigned_project is not None:
                lead.project = assigned_project
                update_fields.append('project')
            if update_fields:
                lead.save(update_fields=[*update_fields, 'updated_at'])
            elif assigned_project is None:
                lead.save(update_fields=['updated_at'])
            if assigned_project is not None:
                lead.projects.add(assigned_project)
            action.status = CallGeneratedAction.Status.COMPLETED
            action.save(update_fields=['status', 'updated_at'])
        return action, lead, True

    @staticmethod
    def _validate_changes(
        *,
        lead: Lead,
        changes: dict[str, Any],
        assigned_project: Project | None,
    ) -> None:
        requested_status = changes.get('status')
        requested_dnc = changes.get('do_not_contact')

        if lead.do_not_contact:
            only_reinforce_dnc = (
                set(changes.keys()) <= {'do_not_contact'}
                and requested_dnc is True
                and assigned_project is None
            )
            if not only_reinforce_dnc:
                raise ToolActionRejectedError(
                    'Lead is marked do-not-contact and cannot be updated.'
                )

        if requested_dnc is False:
            raise ToolActionRejectedError(
                'do_not_contact cannot be cleared by the calling agent.'
            )

        current_stage = Lead.STATUS_TO_STAGE.get(lead.status, Lead.Stage.NEW)
        if (
            requested_status is not None
            and current_stage == CLOSED_STAGE
        ):
            raise ToolActionRejectedError(
                'Closed leads cannot be reopened by the calling agent.'
            )

        if requested_status is not None:
            if requested_status not in Lead.AI_SETTABLE_STATUSES:
                raise ToolActionRejectedError(
                    'The requested lead status is not allowed during a call.'
                )

        if assigned_project is not None and lead.do_not_contact:
            raise ToolActionRejectedError(
                'Lead is marked do-not-contact and cannot be updated.'
            )
