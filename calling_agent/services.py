from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from time import monotonic
from typing import TYPE_CHECKING

import httpx
import phonenumbers
from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.db.models import Exists, OuterRef, Q, QuerySet
from django.utils import timezone

from calling_agent.auth import CallContextTokenService
from calling_agent.elevenlabs import (
    AgentConfigurationResolver,
    ElevenLabsClient,
)
from calling_agent.exceptions import (
    CallAuthorizationError,
    CallConflictError,
    CallingConfigurationError,
    ElevenLabsRequestError,
    ElevenLabsResponseError,
    ElevenLabsTransientError,
    InvalidPhoneNumberError,
    LeadNotCallableError,
)
from calling_agent.models import Call, CallGeneratedAction
from calling_agent.outbound_context import OutboundDynamicVariablesBuilder
from users.models import Lead, Task, User

if TYPE_CHECKING:
    from calling_agent.elevenlabs import AgentConfiguration

logger = logging.getLogger(__name__)


def _select_for_update_rows(
    queryset: QuerySet,
    *,
    skip_locked: bool = False,
) -> QuerySet:
    """Lock only base rows; PostgreSQL rejects FOR UPDATE on outer-join sides or with DISTINCT."""
    qs = queryset.all()
    qs.query.distinct = False
    qs.query.distinct_fields = ()
    kwargs: dict[str, object] = {'of': ('self',)}
    if skip_locked and connection.features.has_select_for_update_skip_locked:
        kwargs['skip_locked'] = True
    return qs.select_for_update(**kwargs)



def leads_visible_to_user(user: User) -> QuerySet[Lead]:
    queryset = Lead.objects.select_related(
        'created_by',
        'created_by__company',
        'assigned_to',
        'project',
    )
    if user.role == User.Role.SUPERADMIN:
        return queryset
    if user.role in (User.Role.AXIYON_ADMIN, User.Role.COMPANY_ADMIN):
        return queryset.filter(created_by__company=user.company)
    if user.role == User.Role.TEAM_MANAGER:
        managed_team = getattr(user, 'managed_team', None)
        if managed_team:
            return queryset.filter(
                Q(assigned_to__team=managed_team)
                | Q(created_by__team=managed_team)
                | Q(assigned_to=user)
            ).distinct()
        return queryset.filter(
            Q(assigned_to=user) | Q(created_by=user)
        ).distinct()
    return queryset.filter(
        Q(assigned_to=user) | Q(created_by=user)
    ).distinct()


def calls_visible_to_user(user: User) -> QuerySet[Call]:
    queryset = Call.objects.select_related(
        'company',
        'lead',
        'lead__project',
        'context_user',
    )
    if user.role == User.Role.SUPERADMIN:
        return queryset
    if user.role in (User.Role.AXIYON_ADMIN, User.Role.COMPANY_ADMIN, 'axiyon_admin', 'company_admin'):
        if user.company_id:
            return queryset.filter(
                Q(company_id=user.company_id)
                | Q(context_user__company_id=user.company_id)
                | Q(context_user__team__company_id=user.company_id)
                | Q(lead__created_by__company_id=user.company_id)
                | Q(lead__assigned_to__company_id=user.company_id)
                | Q(context_user=user)
                | Q(lead__created_by=user)
                | Q(lead__assigned_to=user)
            ).distinct()
        return queryset.filter(
            Q(context_user=user)
            | Q(lead__created_by=user)
            | Q(lead__assigned_to=user)
        ).distinct()
    if user.role == User.Role.TEAM_MANAGER:
        managed_team = getattr(user, 'managed_team', None)
        if managed_team:
            return queryset.filter(
                Q(context_user__team=managed_team)
                | Q(context_user=user)
                | Q(lead__assigned_to__team=managed_team)
                | Q(lead__created_by__team=managed_team)
                | Q(lead__assigned_to=user)
                | Q(lead__created_by=user)
            ).distinct()
        return queryset.filter(
            Q(context_user=user)
            | Q(lead__assigned_to=user)
            | Q(lead__created_by=user)
        ).distinct()
    return queryset.filter(
        Q(context_user=user)
        | Q(lead__assigned_to=user)
        | Q(lead__created_by=user)
    ).distinct()


def is_call_owned_by_user(call: Call, user: User) -> bool:
    """Check whether a call record is directly owned by the authenticated user.

    Every authenticated user (including company admins and team managers)
    can only delete calls that they own directly.
    Company admins and team managers cannot delete their agents' or other team managers' call records.
    """
    if call.context_user_id is not None:
        return call.context_user_id == user.id
    if call.lead_id is not None and call.lead is not None:
        return (
            call.lead.assigned_to_id == user.id
            or call.lead.created_by_id == user.id
        )
    return False



def normalize_phone_number(raw_number: str) -> str:
    try:
        parsed = phonenumbers.parse(
            raw_number,
            settings.DEFAULT_PHONE_REGION or None,
        )
    except phonenumbers.NumberParseException as exc:
        raise InvalidPhoneNumberError(
            'The lead phone number is not valid.'
        ) from exc

    if (
        not phonenumbers.is_possible_number(parsed)
        or not phonenumbers.is_valid_number(parsed)
    ):
        raise InvalidPhoneNumberError('The lead phone number is not valid.')
    return phonenumbers.format_number(
        parsed,
        phonenumbers.PhoneNumberFormat.E164,
    )


class CallProviderInitiationService:
    """Initiate a prepared call and persist the normalized provider outcome."""

    def __init__(
        self,
        provider_client: ElevenLabsClient | None = None,
    ) -> None:
        self.provider_client = provider_client

    def initiate(
        self,
        call: Call,
        configuration: AgentConfiguration,
    ) -> bool:
        call_id = call.pk
        call = (
            Call.objects.select_related(
                'lead',
                'lead__assigned_to',
                'lead__created_by',
                'context_user',
                'company',
            )
            .filter(pk=call_id)
            .first()
        )
        if call is None or call.lead is None:
            Call.objects.filter(
                pk=call_id,
                status=Call.Status.INITIATING,
            ).update(
                status=Call.Status.CANCELLED,
                cancelled_at=timezone.now(),
                claim_expires_at=None,
                failure_code='lead_unavailable',
                failure_detail='The lead was removed before call initiation.',
                updated_at=timezone.now(),
            )
            return False

        client = self.provider_client or ElevenLabsClient(configuration)
        try:
            result = client.initiate_outbound_call(
                to_number=call.phone_number,
                call_public_id=str(call.public_id),
                lead_id=call.lead_id,
                lead_name=call.lead_name,
                company_id=call.company_id,
                call_context_token=CallContextTokenService.issue(call),
                extra_dynamic_variables=(
                    OutboundDynamicVariablesBuilder.build(call)
                ),
            )
        except (ElevenLabsRequestError, ElevenLabsResponseError) as exc:
            Call.objects.filter(
                pk=call.pk,
                status=Call.Status.INITIATING,
            ).update(
                status=Call.Status.FAILED,
                claim_expires_at=None,
                failure_code='provider_rejected',
                failure_detail=str(exc),
                updated_at=timezone.now(),
            )
            raise
        except ElevenLabsTransientError as exc:
            Call.objects.filter(
                pk=call.pk,
                status=Call.Status.INITIATING,
            ).update(
                status=Call.Status.INITIATION_UNKNOWN,
                claim_expires_at=None,
                failure_code='provider_response_unknown',
                failure_detail=str(exc),
                updated_at=timezone.now(),
            )
            raise

        updated_count = Call.objects.filter(
            pk=call.pk,
            status=Call.Status.INITIATING,
        ).update(
            status=Call.Status.RINGING,
            provider_conversation_id=result.conversation_id,
            provider_call_id=result.provider_call_id,
            initiated_at=timezone.now(),
            claim_expires_at=None,
            failure_code='',
            failure_detail='',
            updated_at=timezone.now(),
        )
        if updated_count:
            logger.info(
                'Outbound call initiated call_id=%s trigger=%s '
                'conversation_id=%s provider_call_id=%s lead_id=%s '
                'company_id=%s',
                call.public_id,
                call.trigger,
                result.conversation_id,
                result.provider_call_id,
                call.lead_id,
                call.company_id,
            )
        return updated_count == 1


class ManualCallInitiationService:
    def __init__(
        self,
        *,
        configuration_resolver: AgentConfigurationResolver | None = None,
        provider_client: ElevenLabsClient | None = None,
    ) -> None:
        self.configuration_resolver = (
            configuration_resolver or AgentConfigurationResolver()
        )
        self.provider_initiation = CallProviderInitiationService(provider_client)

    def initiate(
        self,
        *,
        user: User,
        lead_id: int,
        idempotency_key: uuid.UUID,
        agent_config_key: str = 'default',
    ) -> tuple[Call, bool]:
        configuration = self.configuration_resolver.resolve(agent_config_key)
        existing_call = Call.objects.filter(
            initiation_key=idempotency_key
        ).first()
        if existing_call:
            if not calls_visible_to_user(user).filter(pk=existing_call.pk).exists():
                raise CallAuthorizationError(
                    'The idempotency key belongs to another call.'
                )
            if existing_call.lead_id != lead_id:
                raise CallConflictError(
                    'The idempotency key was already used for another lead.'
                )
            return existing_call, False

        invalid_phone_error: InvalidPhoneNumberError | None = None
        with transaction.atomic():
            lead = (
                _select_for_update_rows(leads_visible_to_user(user))
                .filter(pk=lead_id)
                .first()
            )
            if lead is None:
                raise CallAuthorizationError(
                    'The lead does not exist or is not available to this user.'
                )
            if not lead.is_manually_callable():
                raise LeadNotCallableError(
                    'This lead cannot be called (do-not-contact or '
                    'non-callable status).'
                )

            context_user = lead.assigned_to or lead.created_by
            try:
                phone_number = normalize_phone_number(lead.phone_no)
            except InvalidPhoneNumberError as exc:
                from calling_agent.webhook_services import (
                    update_lead_status_on_call_outcome,
                )

                try:
                    failed_call = Call.objects.create(
                        company=lead.created_by.company,
                        lead=lead,
                        context_user=context_user,
                        lead_name=lead.name,
                        phone_number=lead.phone_no,
                        outbound_number=configuration.outbound_phone_number,
                        agent_config_key=configuration.key,
                        provider_agent_id=configuration.agent_id,
                        provider_phone_number_id=configuration.phone_number_id,
                        initiation_key=idempotency_key,
                        trigger=Call.Trigger.MANUAL,
                        status=Call.Status.FAILED,
                        failure_code='invalid_phone_number',
                        failure_detail='The lead phone number is not valid.',
                    )
                except IntegrityError as integrity_exc:
                    raise CallConflictError(
                        'A call with this idempotency key already exists.'
                    ) from integrity_exc
                update_lead_status_on_call_outcome(failed_call)
                invalid_phone_error = exc
            else:
                try:
                    call = Call.objects.create(
                        company=lead.created_by.company,
                        lead=lead,
                        context_user=context_user,
                        lead_name=lead.name,
                        phone_number=phone_number,
                        outbound_number=configuration.outbound_phone_number,
                        agent_config_key=configuration.key,
                        provider_agent_id=configuration.agent_id,
                        provider_phone_number_id=configuration.phone_number_id,
                        initiation_key=idempotency_key,
                        trigger=Call.Trigger.MANUAL,
                        status=Call.Status.INITIATING,
                    )
                except IntegrityError as exc:
                    raise CallConflictError(
                        'A call with this idempotency key already exists.'
                    ) from exc

        if invalid_phone_error is not None:
            raise invalid_phone_error

        self.provider_initiation.initiate(call, configuration)
        call.refresh_from_db()
        charge_call_credits(call.company, f"manual-call:{call.initiation_key}")
        return call, True


def charge_call_credits(company, receipt):
    """Bill 5 AI credits for a call that was actually placed.

    Called after provider initiation succeeds, so a call that never dialled out
    costs nothing. The receipt is keyed on the call's idempotency key, so a
    retried request is never charged twice.
    """
    if company is None:
        return False
    from billing.services import consume_credits

    return consume_credits(company.pk, 'calling_agent_call', receipt_id=receipt)


def charge_transcript_credits(call):
    """Bill 1 AI credit for analysing one call's transcript.

    Keyed on the call, so whichever path runs first pays and the other is free.
    Both the Celery task and the signal's direct fallback call this, which keeps
    a broker outage from becoming unmetered AI work.
    """
    if call.company_id is None:
        return False
    from billing.services import consume_credits

    return consume_credits(
        call.company_id, 'transcript_processing',
        receipt_id=f'transcript:{call.pk}',
    )


TERMINABLE_CALL_STATUSES = {
    Call.Status.INITIATING,
    Call.Status.INITIATION_UNKNOWN,
    Call.Status.RINGING,
    Call.Status.IN_PROGRESS,
}


class TwilioCallHangupService:
    """End an active Twilio call by setting its status to completed."""

    def hangup(self, call_sid: str) -> None:
        account_sid = (settings.TWILIO_ACCOUNT_SID or '').strip()
        auth_token = (settings.TWILIO_AUTH_TOKEN or '').strip()
        if not account_sid or not auth_token:
            raise CallingConfigurationError(
                'Twilio is not configured to hang up the phone call. '
                'Set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN.'
            )

        timeout_seconds = int(
            getattr(settings, 'ELEVENLABS_REQUEST_TIMEOUT_SECONDS', 20)
        )
        url = (
            f'https://api.twilio.com/2010-04-01/Accounts/{account_sid}'
            f'/Calls/{call_sid}.json'
        )
        started_at = monotonic()
        try:
            response = httpx.post(
                url,
                data={'Status': 'completed'},
                auth=(account_sid, auth_token),
                timeout=timeout_seconds,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            logger.warning(
                'Twilio hangup transport failure call_sid=%s latency_ms=%d',
                call_sid,
                int((monotonic() - started_at) * 1000),
            )
            raise ElevenLabsTransientError(
                'The phone hangup response is unknown after a transport failure.'
            ) from exc

        if response.status_code in {200, 201}:
            logger.info(
                'Twilio call hung up call_sid=%s latency_ms=%d',
                call_sid,
                int((monotonic() - started_at) * 1000),
            )
            return
        if response.status_code == 404:
            logger.info(
                'Twilio call already ended call_sid=%s latency_ms=%d',
                call_sid,
                int((monotonic() - started_at) * 1000),
            )
            return
        if 400 <= response.status_code < 500:
            logger.warning(
                'Twilio rejected hangup call_sid=%s status=%s',
                call_sid,
                response.status_code,
            )
            raise ElevenLabsRequestError('Twilio rejected the hangup request.')
        logger.warning(
            'Twilio hangup unexpected status call_sid=%s status=%s',
            call_sid,
            response.status_code,
        )
        raise ElevenLabsTransientError('The phone hangup response is unknown.')


class CallTerminationService:
    """Hang up a live Twilio call and mark the local call completed."""

    def __init__(
        self,
        *,
        hangup_service: TwilioCallHangupService | None = None,
    ) -> None:
        self.hangup_service = hangup_service or TwilioCallHangupService()

    def terminate(
        self,
        *,
        user: User,
        public_id: uuid.UUID,
        reason: str = '',
    ) -> Call:
        call_sid = self._lock_active_call(user=user, public_id=public_id)
        self.hangup_service.hangup(call_sid)
        return self._complete_terminated_call(
            user=user,
            public_id=public_id,
            reason=reason,
        )

    def _lock_active_call(
        self,
        *,
        user: User,
        public_id: uuid.UUID,
    ) -> str:
        with transaction.atomic():
            call = (
                _select_for_update_rows(calls_visible_to_user(user))
                .filter(public_id=public_id)
                .first()
            )
            if call is None:
                raise CallAuthorizationError(
                    'The call does not exist or is not available to this user.'
                )
            call_sid = (call.provider_call_id or '').strip()
            if call.status not in TERMINABLE_CALL_STATUSES or not call_sid:
                raise CallConflictError('The call is not active.')
            return call_sid

    def _complete_terminated_call(
        self,
        *,
        user: User,
        public_id: uuid.UUID,
        reason: str,
    ) -> Call:
        from calling_agent.webhook_services import (
            TERMINAL_STATUSES,
            CallStatusTransitionService,
        )

        with transaction.atomic():
            call = (
                _select_for_update_rows(calls_visible_to_user(user))
                .filter(public_id=public_id)
                .first()
            )
            if call is None:
                raise CallAuthorizationError(
                    'The call does not exist or is not available to this user.'
                )
            if call.status in TERMINAL_STATUSES:
                return call

            ended_at = timezone.now()
            CallStatusTransitionService().apply(
                call,
                Call.Status.COMPLETED,
                failure_code='user_terminated',
                failure_detail=reason.strip(),
                ended_at=ended_at,
                duration_seconds=_termination_duration_seconds(call, ended_at),
            )
            call.refresh_from_db()
            return call


def _termination_duration_seconds(call: Call, ended_at: datetime) -> int | None:
    started_at = call.answered_at or call.initiated_at
    if started_at is None:
        return None
    return max(int((ended_at - started_at).total_seconds()), 0)


class ScheduledCallService:
    """Claim due lead and callback-task schedules, then initiate outbound calls."""

    def __init__(
        self,
        *,
        configuration_resolver: AgentConfigurationResolver | None = None,
        provider_client: ElevenLabsClient | None = None,
    ) -> None:
        self.configuration_resolver = (
            configuration_resolver or AgentConfigurationResolver()
        )
        self.provider_initiation = CallProviderInitiationService(provider_client)

    @staticmethod
    def _lock_queryset(queryset: QuerySet) -> QuerySet:
        return _select_for_update_rows(queryset, skip_locked=True)

    def _build_claim_defaults(
        self,
        *,
        lead: Lead,
        configuration: AgentConfiguration,
        current_time: datetime,
        claim_expires_at: datetime,
        related_task: Task | None = None,
    ) -> dict[str, object]:
        context_user = lead.assigned_to or lead.created_by
        defaults: dict[str, object] = {
            'company': lead.created_by.company,
            'context_user': context_user,
            'lead_name': lead.name,
            'phone_number': lead.phone_no,
            'outbound_number': configuration.outbound_phone_number,
            'trigger': Call.Trigger.SCHEDULED,
            'agent_config_key': configuration.key,
            'provider_agent_id': configuration.agent_id,
            'provider_phone_number_id': configuration.phone_number_id,
            'claimed_at': current_time,
            'claim_expires_at': claim_expires_at,
            'status': Call.Status.CLAIMED,
            'related_task': related_task,
        }
        try:
            defaults['phone_number'] = normalize_phone_number(lead.phone_no)
        except InvalidPhoneNumberError:
            defaults.update(
                status=Call.Status.FAILED,
                claim_expires_at=None,
                failure_code='invalid_phone_number',
                failure_detail='The lead phone number is not valid.',
            )
        return defaults

    def claim_due_calls(
        self,
        *,
        now: datetime | None = None,
        batch_size: int | None = None,
        agent_config_key: str = 'default',
    ) -> list[int]:
        current_time = now or timezone.now()
        claim_batch_size = batch_size or settings.CALLING_AGENT_CLAIM_BATCH_SIZE
        claim_expires_at = current_time + timedelta(
            seconds=settings.CALLING_AGENT_CLAIM_TTL_SECONDS
        )
        configuration = self.configuration_resolver.resolve(agent_config_key)
        scheduled_in_flight = {
            Call.Status.CLAIMED,
            Call.Status.INITIATING,
            Call.Status.INITIATION_UNKNOWN,
            Call.Status.RINGING,
            Call.Status.IN_PROGRESS,
        }
        existing_call = Call.objects.filter(
            lead_id=OuterRef('pk'),
            trigger=Call.Trigger.SCHEDULED,
            scheduled_for=OuterRef('scheduled_at'),
        )
        inflight_scheduled = Call.objects.filter(
            lead_id=OuterRef('pk'),
            trigger=Call.Trigger.SCHEDULED,
            status__in=scheduled_in_flight,
        )
        existing_task_call = Call.objects.filter(
            lead_id=OuterRef('related_lead_id'),
            trigger=Call.Trigger.SCHEDULED,
            scheduled_for=OuterRef('scheduled_at'),
        )
        inflight_task_lead = Call.objects.filter(
            lead_id=OuterRef('related_lead_id'),
            trigger=Call.Trigger.SCHEDULED,
            status__in=scheduled_in_flight,
        )

        claimed_call_ids: list[int] = []
        with transaction.atomic():
            remaining = claim_batch_size
            due_leads = (
                Lead.objects.filter(
                    created_by__company__isnull=False,
                    scheduled_at__isnull=False,
                    scheduled_at__lte=current_time,
                    do_not_contact=False,
                )
                .exclude(status__in=Lead.NON_CALLABLE_STATUSES)
                .annotate(
                    has_scheduled_call=Exists(existing_call),
                    has_inflight_scheduled=Exists(inflight_scheduled),
                )
                .filter(
                    has_scheduled_call=False,
                    has_inflight_scheduled=False,
                )
                .order_by('scheduled_at', 'pk')
            )
            locked_leads = list(self._lock_queryset(due_leads)[:remaining])

            for lead in locked_leads:
                defaults = self._build_claim_defaults(
                    lead=lead,
                    configuration=configuration,
                    current_time=current_time,
                    claim_expires_at=claim_expires_at,
                )
                call, created = Call.objects.get_or_create(
                    lead=lead,
                    scheduled_for=lead.scheduled_at,
                    attempt_number=1,
                    defaults=defaults,
                )
                if created and call.status == Call.Status.CLAIMED:
                    claimed_call_ids.append(call.pk)

            remaining = claim_batch_size - len(claimed_call_ids)
            if remaining > 0:
                due_tasks = (
                    Task.objects.filter(
                        type=Task.Type.CALLBACK,
                        scheduled_at__isnull=False,
                        scheduled_at__lte=current_time,
                        status__in=[
                            Task.Status.PENDING,
                            Task.Status.IN_PROGRESS,
                        ],
                        related_lead__isnull=False,
                        related_lead__created_by__company__isnull=False,
                        related_lead__do_not_contact=False,
                    )
                    .exclude(
                        related_lead__status__in=Lead.NON_CALLABLE_STATUSES,
                    )
                    .select_related(
                        'related_lead',
                        'related_lead__created_by',
                        'related_lead__assigned_to',
                    )
                    .annotate(
                        has_scheduled_call=Exists(existing_task_call),
                        has_inflight_scheduled=Exists(inflight_task_lead),
                    )
                    .filter(
                        has_scheduled_call=False,
                        has_inflight_scheduled=False,
                    )
                    .order_by('scheduled_at', 'pk')
                )
                locked_tasks = list(self._lock_queryset(due_tasks)[:remaining])

                for task in locked_tasks:
                    lead = task.related_lead
                    if lead is None:
                        continue
                    defaults = self._build_claim_defaults(
                        lead=lead,
                        configuration=configuration,
                        current_time=current_time,
                        claim_expires_at=claim_expires_at,
                        related_task=task,
                    )
                    call, created = Call.objects.get_or_create(
                        lead=lead,
                        scheduled_for=task.scheduled_at,
                        attempt_number=1,
                        defaults=defaults,
                    )
                    if created and call.status == Call.Status.CLAIMED:
                        claimed_call_ids.append(call.pk)
                        if task.status != Task.Status.IN_PROGRESS:
                            task.status = Task.Status.IN_PROGRESS
                            task.save(update_fields=['status', 'updated_at'])

        logger.info(
            'Claimed due scheduled calls count=%s batch_size=%s',
            len(claimed_call_ids),
            claim_batch_size,
        )
        return claimed_call_ids

    def reclaim_expired_claims(
        self,
        *,
        now: datetime | None = None,
        batch_size: int | None = None,
    ) -> list[int]:
        current_time = now or timezone.now()
        claim_batch_size = batch_size or settings.CALLING_AGENT_CLAIM_BATCH_SIZE
        claim_expires_at = current_time + timedelta(
            seconds=settings.CALLING_AGENT_CLAIM_TTL_SECONDS
        )

        with transaction.atomic():
            expired_claims = Call.objects.filter(
                status=Call.Status.CLAIMED,
                claim_expires_at__lte=current_time,
                provider_conversation_id__isnull=True,
            ).order_by('claim_expires_at', 'pk')
            calls = list(
                self._lock_queryset(expired_claims)[:claim_batch_size]
            )
            call_ids = [call.pk for call in calls]
            if call_ids:
                Call.objects.filter(pk__in=call_ids).update(
                    claimed_at=current_time,
                    claim_expires_at=claim_expires_at,
                    updated_at=current_time,
                )

        if call_ids:
            logger.warning(
                'Reclaimed expired scheduled calls count=%s',
                len(call_ids),
            )
        return call_ids

    def mark_stale_initiations_unknown(
        self,
        *,
        now: datetime | None = None,
        batch_size: int | None = None,
    ) -> list[int]:
        current_time = now or timezone.now()
        claim_batch_size = batch_size or settings.CALLING_AGENT_CLAIM_BATCH_SIZE

        with transaction.atomic():
            stale_initiations = Call.objects.filter(
                status=Call.Status.INITIATING,
                trigger=Call.Trigger.SCHEDULED,
                claim_expires_at__lte=current_time,
                provider_conversation_id__isnull=True,
            ).order_by('claim_expires_at', 'pk')
            calls = list(
                self._lock_queryset(stale_initiations)[:claim_batch_size]
            )
            call_ids = [call.pk for call in calls]
            if call_ids:
                Call.objects.filter(pk__in=call_ids).update(
                    status=Call.Status.INITIATION_UNKNOWN,
                    claim_expires_at=None,
                    failure_code='stale_initiation',
                    failure_detail=(
                        'Provider acceptance is unknown after task interruption.'
                    ),
                    updated_at=current_time,
                )

        if call_ids:
            logger.error(
                'Marked stale call initiations unknown count=%s',
                len(call_ids),
            )
        return call_ids

    def initiate_claimed_call(self, call_id: int) -> tuple[Call | None, bool]:
        with transaction.atomic():
            call = self._lock_queryset(
                Call.objects.filter(pk=call_id)
            ).first()
            if call is None:
                return None, False
            if call.status != Call.Status.CLAIMED:
                return call, False

            lead = None
            if call.lead_id:
                lead = _select_for_update_rows(
                    Lead.objects.filter(pk=call.lead_id)
                ).first()
            cancellation_code = self._cancellation_code(call, lead)
            if cancellation_code:
                call.status = Call.Status.CANCELLED
                call.cancelled_at = timezone.now()
                call.claim_expires_at = None
                call.failure_code = cancellation_code
                call.failure_detail = (
                    'The lead or schedule is no longer valid for initiation.'
                )
                call.save(
                    update_fields=[
                        'status',
                        'cancelled_at',
                        'claim_expires_at',
                        'failure_code',
                        'failure_detail',
                        'updated_at',
                    ]
                )
                return call, False

            assert lead is not None
            try:
                phone_number = normalize_phone_number(lead.phone_no)
            except InvalidPhoneNumberError:
                from calling_agent.webhook_services import update_lead_status_on_call_outcome

                call.status = Call.Status.FAILED
                call.claim_expires_at = None
                call.failure_code = 'invalid_phone_number'
                call.failure_detail = 'The lead phone number is not valid.'
                call.save(
                    update_fields=[
                        'status',
                        'claim_expires_at',
                        'failure_code',
                        'failure_detail',
                        'updated_at',
                    ]
                )
                update_lead_status_on_call_outcome(call)
                return call, False

            configuration = self.configuration_resolver.resolve(
                call.agent_config_key
            )
            call.status = Call.Status.INITIATING
            call.phone_number = phone_number
            call.lead_name = lead.name
            call.context_user = lead.assigned_to or lead.created_by
            call.provider_agent_id = configuration.agent_id
            call.provider_phone_number_id = configuration.phone_number_id
            call.outbound_number = configuration.outbound_phone_number
            call.save(
                update_fields=[
                    'status',
                    'phone_number',
                    'lead_name',
                    'context_user',
                    'provider_agent_id',
                    'provider_phone_number_id',
                    'outbound_number',
                    'updated_at',
                ]
            )

        if not self._schedule_is_current(call):
            Call.objects.filter(
                pk=call.pk,
                status=Call.Status.INITIATING,
            ).update(
                status=Call.Status.CANCELLED,
                cancelled_at=timezone.now(),
                claim_expires_at=None,
                failure_code='schedule_changed',
                failure_detail=(
                    'The schedule changed before provider initiation.'
                ),
                updated_at=timezone.now(),
            )
            call.refresh_from_db()
            return call, False

        initiated = self.provider_initiation.initiate(call, configuration)
        call.refresh_from_db()
        if initiated:
            charge_call_credits(call.company, f"scheduled-call:{call.pk}")
        return call, initiated

    @staticmethod
    def _schedule_is_current(call: Call) -> bool:
        if call.related_task_id:
            return Task.objects.filter(
                pk=call.related_task_id,
                type=Task.Type.CALLBACK,
                scheduled_at=call.scheduled_for,
                related_lead_id=call.lead_id,
                status__in=[
                    Task.Status.PENDING,
                    Task.Status.IN_PROGRESS,
                ],
            ).exists()
        return Lead.objects.filter(
            pk=call.lead_id,
            scheduled_at=call.scheduled_for,
            created_by__company_id=call.company_id,
        ).exists()

    @staticmethod
    def _cancellation_code(call: Call, lead: Lead | None) -> str:
        if lead is None:
            return 'lead_unavailable'
        if lead.created_by.company_id != call.company_id:
            return 'company_changed'

        if call.related_task_id:
            task = Task.objects.filter(pk=call.related_task_id).first()
            if task is None:
                return 'schedule_cancelled'
            if task.type != Task.Type.CALLBACK:
                return 'schedule_cancelled'
            if task.scheduled_at is None:
                return 'schedule_cancelled'
            if task.scheduled_at != call.scheduled_for:
                return 'schedule_changed'
            if task.related_lead_id != lead.pk:
                return 'schedule_changed'
            if task.status == Task.Status.COMPLETED:
                return 'schedule_cancelled'
            return ''

        if lead.scheduled_at is None:
            return 'schedule_cancelled'
        if lead.scheduled_at != call.scheduled_for:
            return 'schedule_changed'
        return ''


class CallAnalyticsService:
    """Aggregate tenant-scoped call metrics for dashboard reporting."""

    TERMINAL_ATTEMPT_STATUSES = {
        Call.Status.COMPLETED,
        Call.Status.FAILED,
        Call.Status.NO_ANSWER,
        Call.Status.BUSY,
        Call.Status.CANCELLED,
    }

    def build_summary(self, queryset: QuerySet[Call]) -> dict[str, object]:
        total_calls = queryset.count()
        completed_calls = queryset.filter(status=Call.Status.COMPLETED).count()
        failed_calls = queryset.filter(status=Call.Status.FAILED).count()
        no_answer_calls = queryset.filter(status=Call.Status.NO_ANSWER).count()
        busy_calls = queryset.filter(status=Call.Status.BUSY).count()
        in_progress_calls = queryset.filter(
            status__in={
                Call.Status.RINGING,
                Call.Status.IN_PROGRESS,
                Call.Status.INITIATING,
            }
        ).count()

        terminal_count = queryset.filter(
            status__in=self.TERMINAL_ATTEMPT_STATUSES
        ).count()
        answered_count = completed_calls
        answer_rate = (
            round(answered_count / terminal_count, 4)
            if terminal_count
            else 0.0
        )
        failure_rate = (
            round(failed_calls / terminal_count, 4)
            if terminal_count
            else 0.0
        )

        durations = list(
            queryset.filter(duration_seconds__isnull=False).values_list(
                'duration_seconds',
                flat=True,
            )
        )
        average_duration_seconds = (
            round(sum(durations) / len(durations), 2)
            if durations
            else None
        )

        sentiment_breakdown: dict[str, int] = {}
        for sentiments in queryset.values_list('key_sentiments', flat=True):
            if not isinstance(sentiments, list):
                continue
            for sentiment in sentiments:
                label = str(sentiment).strip()
                if not label:
                    continue
                sentiment_breakdown[label] = sentiment_breakdown.get(label, 0) + 1

        task_conversion_count = CallGeneratedAction.objects.filter(
            call__in=queryset,
            action_type=CallGeneratedAction.ActionType.CREATE_TASK,
            status=CallGeneratedAction.Status.COMPLETED,
        ).count()

        return {
            'total_calls': total_calls,
            'completed_calls': completed_calls,
            'failed_calls': failed_calls,
            'no_answer_calls': no_answer_calls,
            'busy_calls': busy_calls,
            'in_progress_calls': in_progress_calls,
            'answer_rate': answer_rate,
            'failure_rate': failure_rate,
            'average_duration_seconds': average_duration_seconds,
            'sentiment_breakdown': sentiment_breakdown,
            'task_conversion_count': task_conversion_count,
        }
