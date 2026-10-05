from __future__ import annotations

import logging

from celery import Task, shared_task

from calling_agent.exceptions import (
    CallingConfigurationError,
    ElevenLabsError,
)
from calling_agent.models import Call
from calling_agent.services import ScheduledCallService
from calling_agent.transcript import analyze_and_process_call
from calling_agent.webhook_services import CallWebhookProcessingService

logger = logging.getLogger(__name__)


@shared_task(bind=True, name='calling_agent.process_call_transcript')
def process_call_transcript_task(
    self: Task,
    call_id: int,
) -> dict[str, object]:
    """Background task to analyze and process a call transcript."""
    call_obj = Call.objects.filter(pk=call_id).first()
    if not call_obj:
        return {'status': 'error', 'detail': 'Call not found', 'call_id': call_id}

    # AI transcript analysis costs 1 credit, charged once per call.
    charged_here = False
    if call_obj.company_id:
        from billing.exceptions import PlanLimitReached

        from .services import charge_transcript_credits

        try:
            charged_here = charge_transcript_credits(call_obj)
        except PlanLimitReached:
            logger.warning(
                'Skipping transcript analysis for call=%s: AI credit limit reached',
                call_id,
            )
            return {
                'status': 'error',
                'detail': 'AI credit limit reached.',
                'code': 'PLAN_LIMIT_REACHED',
                'call_id': call_id,
            }

    try:
        analyze_and_process_call(call_obj)
    except Exception:
        if charged_here:
            from billing.services import refund_credits

            refund_credits(
                call_obj.company_id, 'transcript_processing',
                receipt_id=f'transcript:{call_obj.pk}',
            )
        raise
    return {'status': 'success', 'call_id': call_id}


@shared_task(
    bind=True,
    ignore_result=True,
    name='calling_agent.process_webhook_event',
)
def process_webhook_event_task(
    self: Task,
    event_id: int,
) -> dict[str, object]:
    """Process one verified ElevenLabs webhook event."""
    logger.info(
        'Celery webhook task started event_id=%s task_id=%s',
        event_id,
        getattr(self.request, 'id', None),
    )
    try:
        event = CallWebhookProcessingService().process(event_id)
    except Exception:
        logger.exception('Webhook event processing failed event_id=%s', event_id)
        return {
            'status': 'error',
            'event_id': event_id,
        }
    logger.info(
        'Celery webhook task finished event_id=%s process_status=%s '
        'event_type=%s conversation_id=%s',
        event_id,
        event.process_status,
        event.event_type,
        event.conversation_id,
    )
    return {
        'status': 'success',
        'event_id': event_id,
        'process_status': event.process_status,
    }


def _dispatch_call_tasks(call_ids: list[int]) -> int:
    for call_id in call_ids:
        initiate_scheduled_call_task.apply_async(
            args=[call_id],
            queue='calling',
        )
    return len(call_ids)


@shared_task(
    bind=True,
    ignore_result=True,
    name='calling_agent.claim_due_calls',
)
def claim_due_calls_task(self: Task) -> dict[str, object]:
    """Claim due lead schedules and dispatch one initiation task per call."""
    try:
        call_ids = ScheduledCallService().claim_due_calls()
    except CallingConfigurationError:
        logger.exception('Scheduled calling is not configured')
        return {
            'status': 'error',
            'detail': 'Scheduled calling is not configured.',
            'claimed_count': 0,
        }

    dispatched_count = _dispatch_call_tasks(call_ids)
    return {
        'status': 'success',
        'claimed_count': len(call_ids),
        'dispatched_count': dispatched_count,
    }


@shared_task(
    bind=True,
    ignore_result=True,
    name='calling_agent.initiate_scheduled_call',
)
def initiate_scheduled_call_task(
    self: Task,
    call_id: int,
) -> dict[str, object]:
    """Revalidate and initiate one previously claimed scheduled call."""
    try:
        call, initiated = ScheduledCallService().initiate_claimed_call(call_id)
    except CallingConfigurationError:
        logger.exception(
            'Scheduled call configuration unavailable call_id=%s',
            call_id,
        )
        return {
            'status': 'error',
            'detail': 'Scheduled calling is not configured.',
            'call_id': call_id,
        }
    except ElevenLabsError as exc:
        logger.warning(
            'Scheduled call provider failure call_id=%s error_type=%s',
            call_id,
            type(exc).__name__,
        )
        return {
            'status': 'error',
            'detail': type(exc).__name__,
            'call_id': call_id,
        }

    if call is None:
        return {
            'status': 'ignored',
            'detail': 'Call not found.',
            'call_id': call_id,
        }
    return {
        'status': 'success' if initiated else 'ignored',
        'call_id': call_id,
        'call_status': call.status,
    }


@shared_task(
    bind=True,
    ignore_result=True,
    name='calling_agent.reconcile_scheduled_calls',
)
def reconcile_scheduled_calls_task(self: Task) -> dict[str, object]:
    """Recover abandoned claims without redialing ambiguous initiations."""
    service = ScheduledCallService()
    unknown_ids = service.mark_stale_initiations_unknown()
    reclaimed_ids = service.reclaim_expired_claims()
    dispatched_count = _dispatch_call_tasks(reclaimed_ids)
    return {
        'status': 'success',
        'marked_unknown_count': len(unknown_ids),
        'reclaimed_count': len(reclaimed_ids),
        'dispatched_count': dispatched_count,
    }
