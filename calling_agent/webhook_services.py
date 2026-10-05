from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import logging
import uuid
from datetime import date, datetime, timedelta, timezone as dt_timezone
from typing import Any

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from elevenlabs import ElevenLabs
from elevenlabs.core.api_error import ApiError

from calling_agent.elevenlabs import (
    AgentConfigurationResolver,
    ElevenLabsClient,
)
from calling_agent.exceptions import (
    WebhookProcessingError,
    WebhookVerificationError,
)
from calling_agent.models import (
    Call,
    CallGeneratedAction,
    CallTranscriptTurn,
    CallWebhookEvent,
)
from calling_agent.storage import get_call_recording_storage
from calling_agent.transcript import (
    analyze_transcript_with_openai,
    format_transcript_as_text,
    normalize_speaker,
)
from notifications.services import send_notification
from users.models import Task, User

logger = logging.getLogger(__name__)

CALL_STATUS_RANK = {
    Call.Status.SCHEDULED: 0,
    Call.Status.CLAIMED: 1,
    Call.Status.INITIATING: 2,
    Call.Status.INITIATION_UNKNOWN: 3,
    Call.Status.RINGING: 4,
    Call.Status.IN_PROGRESS: 5,
    Call.Status.COMPLETED: 6,
    Call.Status.FAILED: 6,
    Call.Status.CANCELLED: 6,
    Call.Status.NO_ANSWER: 6,
    Call.Status.BUSY: 6,
}
TERMINAL_STATUSES = {
    Call.Status.COMPLETED,
    Call.Status.FAILED,
    Call.Status.CANCELLED,
    Call.Status.NO_ANSWER,
    Call.Status.BUSY,
}


def _parse_event_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # Relative offsets such as ElevenLabs time_in_call_secs are small.
        # Absolute unix timestamps used by webhook envelopes are much larger.
        if value < 1_000_000_000:
            return None
        return datetime.fromtimestamp(value, tz=dt_timezone.utc)
    if isinstance(value, str):
        parsed = parse_datetime(value)
        if parsed is not None:
            if timezone.is_naive(parsed):
                return timezone.make_aware(parsed, dt_timezone.utc)
            return parsed
    return None


def _redact_dynamic_variables(variables: dict[str, Any]) -> None:
    for key in list(variables):
        if key == 'system__conversation_history' or key.startswith('secret__'):
            variables[key] = '[redacted]'


def _slim_transcript_tool_entries(entries: Any) -> list[Any]:
    if not isinstance(entries, list):
        return []
    slimmed: list[Any] = []
    for entry in entries:
        if not isinstance(entry, dict):
            slimmed.append(entry)
            continue
        slim: dict[str, Any] = {
            'tool_name': entry.get('tool_name'),
            'is_error': entry.get('is_error'),
            'type': entry.get('type'),
        }
        request_id = entry.get('request_id')
        if request_id is not None:
            slim['request_id'] = request_id
        result_value = entry.get('result_value')
        if isinstance(result_value, str) and len(result_value) > 500:
            slim['result_value'] = result_value[:500] + '...[truncated]'
        elif result_value is not None:
            slim['result_value'] = result_value
        # Intentionally drop tool_details / params_as_json / headers / body.
        slimmed.append(slim)
    return slimmed


def _slim_transcript_items(transcript: Any) -> Any:
    if not isinstance(transcript, list):
        return transcript
    slimmed_items: list[Any] = []
    for item in transcript:
        if not isinstance(item, dict):
            slimmed_items.append(item)
            continue
        slim_item = dict(item)
        if 'tool_calls' in slim_item:
            slim_item['tool_calls'] = _slim_transcript_tool_entries(
                slim_item.get('tool_calls')
            )
        if 'tool_results' in slim_item:
            slim_item['tool_results'] = _slim_transcript_tool_entries(
                slim_item.get('tool_results')
            )
        slimmed_items.append(slim_item)
    return slimmed_items


def _filter_webhook_payload(event: dict[str, Any]) -> dict[str, Any]:
    filtered = copy.deepcopy(event)
    data = filtered.get('data')
    if not isinstance(data, dict):
        return filtered

    for key in ('full_audio', 'audio_base64', 'recording_url', 'audio_url'):
        if key in data:
            data[key] = '[redacted]'

    audio = data.get('audio')
    if isinstance(audio, str) and len(audio) > 200:
        data['audio'] = '[redacted]'

    initiation = data.get('conversation_initiation_client_data')
    if isinstance(initiation, dict):
        variables = initiation.get('dynamic_variables')
        if isinstance(variables, dict):
            _redact_dynamic_variables(variables)

    if 'transcript' in data:
        data['transcript'] = _slim_transcript_items(data.get('transcript'))
    if 'conversation' in data:
        data['conversation'] = _slim_transcript_items(data.get('conversation'))

    return filtered


def _decode_full_audio_base64(value: Any) -> bytes | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return base64.b64decode(value, validate=False)
    except (binascii.Error, ValueError) as exc:
        logger.warning(
            'Webhook full_audio base64 decode failed error=%s',
            type(exc).__name__,
        )
        return None


def _initiation_failure_status(failure_reason: str) -> str:
    normalized = failure_reason.strip().lower().replace('_', '-')
    if normalized in {'busy'}:
        return Call.Status.BUSY
    if normalized in {'no-answer', 'noanswer', 'no answer'}:
        return Call.Status.NO_ANSWER
    return Call.Status.FAILED


def _initiation_failure_detail(data: dict[str, Any], failure_reason: str) -> str:
    parts = [failure_reason.strip() or 'Call initiation failed.']
    metadata = data.get('metadata') if isinstance(data.get('metadata'), dict) else {}
    meta_type = metadata.get('type')
    if meta_type:
        parts.append(f'type={meta_type}')
    body = metadata.get('body') if isinstance(metadata.get('body'), dict) else {}
    if meta_type == 'sip':
        for key in ('sip_status_code', 'error_reason', 'sip_status'):
            value = body.get(key)
            if value is not None and str(value).strip():
                parts.append(f'{key}={value}')
    elif meta_type == 'twilio':
        for key in ('CallStatus', 'ErrorCode', 'ErrorMessage'):
            value = body.get(key)
            if value is not None and str(value).strip():
                parts.append(f'{key}={value}')
    return '; '.join(parts)[:500]


def build_event_key(event: dict[str, Any]) -> str:
    event_type = str(event.get('type') or CallWebhookEvent.EventType.UNKNOWN)
    data = event.get('data') if isinstance(event.get('data'), dict) else {}
    event_id = event.get('event_id') or data.get('event_id')
    if event_id:
        return f'{event_type}:{event_id}'

    conversation_id = str(data.get('conversation_id') or '')
    timestamp = event.get('event_timestamp') or event.get('timestamp') or ''
    fingerprint = hashlib.sha256(
        f'{event_type}:{conversation_id}:{timestamp}'.encode('utf-8')
    ).hexdigest()[:16]
    return f'{event_type}:{conversation_id}:{timestamp}:{fingerprint}'



class ElevenLabsWebhookVerifier:
    def verify(self, *, raw_body: str, signature_header: str) -> dict[str, Any]:
        secret = settings.ELEVENLABS_WEBHOOK_SECRET
        if not secret:
            logger.error('Webhook verification failed: secret not configured')
            raise WebhookVerificationError(
                'ElevenLabs webhook secret is not configured.'
            )
        if not signature_header:
            logger.warning('Webhook verification failed: missing signature header')
            raise WebhookVerificationError('Missing ElevenLabs signature header.')

        logger.info(
            'Webhook signature verification started body_bytes=%d',
            len(raw_body.encode('utf-8')),
        )
        client = ElevenLabs(api_key=settings.ELEVENLABS_API_KEY or 'unused')
        try:
            event = client.webhooks.construct_event(
                rawBody=raw_body,
                sig_header=signature_header,
                secret=secret,
            )
        except ApiError as exc:
            logger.warning(
                'Webhook signature verification failed api_error=%s '
                'status_code=%s detail=%s signature_prefix=%s',
                type(exc).__name__,
                getattr(exc, 'status_code', None),
                getattr(exc, 'body', None) or str(exc),
                signature_header[:32] if signature_header else '',
            )
            raise WebhookVerificationError(
                'ElevenLabs webhook signature verification failed.'
            ) from exc

        logger.info(
            'Webhook signature verification succeeded event_type=%s',
            event.get('type') if isinstance(event, dict) else type(event).__name__,
        )
        return event


class CallWebhookIngestionService:
    def __init__(
        self,
        *,
        verifier: ElevenLabsWebhookVerifier | None = None,
        recording_service: CallRecordingFetchService | None = None,
    ) -> None:
        self.verifier = verifier or ElevenLabsWebhookVerifier()
        self.recording_service = recording_service or CallRecordingFetchService()

    def ingest(
        self,
        *,
        raw_body: str,
        signature_header: str,
    ) -> tuple[CallWebhookEvent, bool]:
        logger.info('Webhook ingest step=verify')
        event = self.verifier.verify(
            raw_body=raw_body,
            signature_header=signature_header,
        )
        if not isinstance(event, dict):
            logger.error(
                'Webhook ingest rejected: payload type=%s',
                type(event).__name__,
            )
            raise WebhookVerificationError('Webhook payload must be a JSON object.')

        event_type = str(event.get('type') or CallWebhookEvent.EventType.UNKNOWN)
        data = event.get('data') if isinstance(event.get('data'), dict) else {}
        conversation_id = str(data.get('conversation_id') or '')
        event_key = build_event_key(event)
        body_hash = hashlib.sha256(raw_body.encode('utf-8')).hexdigest()

        logger.info(
            'Webhook ingest step=resolve_call event_type=%s conversation_id=%s '
            'event_key=%s',
            event_type,
            conversation_id,
            event_key,
        )
        call = None
        if conversation_id:
            call = Call.objects.filter(
                provider_conversation_id=conversation_id
            ).first()
        if call is None and conversation_id:
            logger.warning(
                'Webhook ingest: no Call matched conversation_id=%s',
                conversation_id,
            )
        elif call is not None:
            logger.info(
                'Webhook ingest: matched call_id=%s public_id=%s status=%s',
                call.pk,
                call.public_id,
                call.status,
            )

        recording_storage_key = ''
        recording_size_bytes: int | None = None
        if event_type == CallWebhookEvent.EventType.POST_CALL_AUDIO:
            audio_bytes = _decode_full_audio_base64(data.get('full_audio'))
            if audio_bytes:
                recording_size_bytes = len(audio_bytes)
                stored_key = self.recording_service.store_bytes(
                    audio_bytes=audio_bytes,
                    call=call,
                    conversation_id=conversation_id,
                )
                recording_storage_key = stored_key or ''
                if call is not None:
                    call.refresh_from_db()
            else:
                logger.warning(
                    'Webhook ingest post_call_audio missing/invalid full_audio '
                    'conversation_id=%s',
                    conversation_id,
                )

        payload = _filter_webhook_payload(event)
        payload_data = (
            payload.get('data') if isinstance(payload.get('data'), dict) else None
        )
        if payload_data is not None and recording_storage_key:
            payload_data['recording_storage_key'] = recording_storage_key
            if recording_size_bytes is not None:
                payload_data['recording_size_bytes'] = recording_size_bytes

        try:
            with transaction.atomic():
                webhook_event = CallWebhookEvent.objects.create(
                    event_key=event_key,
                    event_type=event_type,
                    conversation_id=conversation_id,
                    call=call,
                    body_hash=body_hash,
                    payload=payload,
                )
        except IntegrityError:
            existing = CallWebhookEvent.objects.filter(event_key=event_key).first()
            if existing is None:
                logger.exception(
                    'Webhook ingest IntegrityError without existing event '
                    'event_key=%s',
                    event_key,
                )
                raise
            logger.info(
                'Webhook ingest duplicate event_key=%s existing_event_id=%s '
                'process_status=%s',
                event_key,
                existing.pk,
                existing.process_status,
            )
            return existing, False

        logger.info(
            'Webhook ingest created event_id=%s event_type=%s conversation_id=%s '
            'call_id=%s',
            webhook_event.pk,
            webhook_event.event_type,
            webhook_event.conversation_id,
            webhook_event.call_id,
        )
        return webhook_event, True


def _slugify_lead_status(raw: str) -> str:
    normalized = (
        raw.lower()
        .replace('–', ' ')
        .replace('—', ' ')
        .replace('-', ' ')
        .replace('/', ' ')
        .replace('_', ' ')
    )
    cleaned = ''.join(
        char if char.isalnum() or char.isspace() else ' ' for char in normalized
    )
    return '_'.join(cleaned.split())


def normalize_post_call_lead_status(raw: Any) -> tuple[str | None, bool]:
    """Map analyzer labels/codes to Lead.Status values. Returns (status, do_not_contact)."""
    from users.models import Lead

    if raw is None:
        return None, False
    text = str(raw).strip()
    if not text or text.lower() in ('null', 'none', 'n/a'):
        return None, False

    slug = _slugify_lead_status(text)
    dnc_aliases = {
        'do_not_call',
        'dont_call',
        'dnc',
        'do_not_contact',
        'remove_number',
        'remove_my_number',
    }
    set_dnc = slug in dnc_aliases
    aliases = {
        'qualified': Lead.Status.INTERESTED,
        'in_negotiation': Lead.Status.NEGOTIATION_ONGOING,
        'callback': Lead.Status.CALL_BACK_REQUESTED,
        'call_back': Lead.Status.CALL_BACK_REQUESTED,
        'hot': Lead.Status.HIGHLY_INTERESTED,
        'wrong_person': Lead.Status.WRONG_NUMBER,
        'do_not_call': Lead.Status.NOT_INTERESTED,
        'dont_call': Lead.Status.NOT_INTERESTED,
        'dnc': Lead.Status.NOT_INTERESTED,
        'do_not_contact': Lead.Status.NOT_INTERESTED,
        'remove_number': Lead.Status.NOT_INTERESTED,
        'remove_my_number': Lead.Status.NOT_INTERESTED,
        'new_lead': Lead.Status.NEW,
        'converted': Lead.Status.CONVERTED_WON,
        'won': Lead.Status.CONVERTED_WON,
    }

    for value, label in Lead.Status.choices:
        if slug == value or slug == _slugify_lead_status(label):
            return value, set_dnc

    mapped = aliases.get(slug)
    if mapped:
        return mapped, set_dnc
    return None, False


def _release_scheduled_slot_after_no_connect(call: Call, lead) -> None:
    if call.trigger != Call.Trigger.SCHEDULED:
        return
    if call.status not in (
        Call.Status.BUSY,
        Call.Status.NO_ANSWER,
        Call.Status.CANCELLED,
    ):
        return

    if lead.scheduled_at is not None:
        lead.scheduled_at = None
        lead.save(update_fields=['scheduled_at', 'updated_at'])

    Task.objects.filter(
        related_lead_id=lead.pk,
        type=Task.Type.CALLBACK,
        status__in=[Task.Status.PENDING, Task.Status.IN_PROGRESS],
        scheduled_at__lte=timezone.now(),
    ).update(status=Task.Status.COMPLETED, updated_at=timezone.now())


def update_lead_status_on_call_outcome(call: Call) -> None:
    """
    Automatically updates the associated Lead's status based on call outcome:
    - Call.Status.COMPLETED -> Lead.Status.CONTACTED ('contacted') if status in (NEW, CALL_PENDING)
    - Wrong number / invalid phone failure -> Lead.Status.WRONG_NUMBER ('wrong_number')
    - No answer / busy / cancelled(decline) -> Lead.Status.NO_ANSWER ('no_answer')
    - Multiple failed call attempts (>= 3) -> Lead.Status.UNREACHABLE ('unreachable')
    """
    from users.models import Lead

    if call.lead_id is None:
        return

    lead = Lead.objects.get(pk=call.lead_id)
    call.lead = lead

    if lead.do_not_contact or lead.stage == Lead.Stage.CLOSED:
        return

    code_lower = (call.failure_code or '').lower()
    detail_lower = (call.failure_detail or '').lower()
    early_statuses = (Lead.Status.NEW, Lead.Status.CALL_PENDING)

    if (
        code_lower in ('invalid_phone_number', 'invalid_phone', 'wrong_number')
        or 'invalid phone' in detail_lower
        or 'wrong number' in detail_lower
        or 'invalid_phone' in code_lower
    ):
        if lead.status != Lead.Status.WRONG_NUMBER:
            lead.status = Lead.Status.WRONG_NUMBER
            lead.save(update_fields=['status', 'updated_at'])
        return

    if call.status == Call.Status.COMPLETED:
        if lead.status in early_statuses:
            lead.status = Lead.Status.CONTACTED
            lead.save(update_fields=['status', 'updated_at'])
        return

    if call.status in TERMINAL_STATUSES:
        failed_attempts = Call.objects.filter(
            lead=lead,
            status__in=[
                Call.Status.NO_ANSWER,
                Call.Status.BUSY,
                Call.Status.FAILED,
                Call.Status.CANCELLED,
            ],
        ).count()

        if failed_attempts >= 3:
            if lead.status not in (
                Lead.Status.UNREACHABLE,
                Lead.Status.WRONG_NUMBER,
            ) and lead.stage in (Lead.Stage.NEW, Lead.Stage.CONTACT_ATTEMPT):
                lead.status = Lead.Status.UNREACHABLE
                lead.save(update_fields=['status', 'updated_at'])
            _release_scheduled_slot_after_no_connect(call, lead)
            return

        is_no_connect = (
            call.status in (
                Call.Status.NO_ANSWER,
                Call.Status.BUSY,
                Call.Status.CANCELLED,
            )
            or 'no-answer' in detail_lower
            or 'no_answer' in detail_lower
            or 'busy' in detail_lower
            or 'no answer' in detail_lower
            or 'declined' in detail_lower
            or 'cancelled' in detail_lower
            or 'canceled' in detail_lower
        )
        if is_no_connect and lead.status in early_statuses:
            lead.status = Lead.Status.NO_ANSWER
            lead.save(update_fields=['status', 'updated_at'])

        _release_scheduled_slot_after_no_connect(call, lead)


class CallStatusTransitionService:
    def apply(
        self,
        call: Call,
        new_status: str,
        *,
        failure_code: str = '',
        failure_detail: str = '',
        answered_at: datetime | None = None,
        ended_at: datetime | None = None,
        duration_seconds: int | None = None,
    ) -> bool:
        previous_status = call.status
        current_rank = CALL_STATUS_RANK.get(call.status, -1)
        new_rank = CALL_STATUS_RANK.get(new_status, -1)
        if call.status in TERMINAL_STATUSES and new_status not in TERMINAL_STATUSES:
            logger.info(
                'Call status transition skipped call_id=%s public_id=%s '
                'from=%s to=%s reason=terminal_to_non_terminal',
                call.pk,
                call.public_id,
                previous_status,
                new_status,
            )
            return False
        if (
            call.status in TERMINAL_STATUSES
            and new_status in TERMINAL_STATUSES
            and call.status == new_status
        ):
            logger.info(
                'Call status transition skipped call_id=%s public_id=%s '
                'status=%s reason=already_terminal',
                call.pk,
                call.public_id,
                previous_status,
            )
            return False
        if new_rank < current_rank and new_status not in TERMINAL_STATUSES:
            logger.info(
                'Call status transition skipped call_id=%s public_id=%s '
                'from=%s to=%s reason=non_monotonic',
                call.pk,
                call.public_id,
                previous_status,
                new_status,
            )
            return False

        update_fields = ['status', 'updated_at']
        call.status = new_status
        if failure_code:
            call.failure_code = failure_code
            update_fields.append('failure_code')
        if failure_detail:
            call.failure_detail = failure_detail[:500]
            update_fields.append('failure_detail')
        if answered_at is not None and call.answered_at is None:
            call.answered_at = answered_at
            update_fields.append('answered_at')
        if ended_at is not None:
            call.ended_at = ended_at
            update_fields.append('ended_at')
        if duration_seconds is not None:
            call.duration_seconds = duration_seconds
            update_fields.append('duration_seconds')

        call.save(update_fields=update_fields)
        logger.info(
            'Call status transition applied call_id=%s public_id=%s '
            'from=%s to=%s duration_seconds=%s failure_code=%s',
            call.pk,
            call.public_id,
            previous_status,
            new_status,
            duration_seconds,
            failure_code or '',
        )
        if new_status in TERMINAL_STATUSES:
            from calling_agent.live_transcript import broadcast_call_ended

            broadcast_call_ended(call)
            update_lead_status_on_call_outcome(call)
        return True


class CallTranscriptNormalizationService:
    def upsert_turns(
        self,
        call: Call,
        transcript_items: list[Any],
    ) -> list[CallTranscriptTurn]:
        logger.info(
            'Transcript normalization started call_id=%s public_id=%s '
            'raw_items=%d',
            call.pk,
            call.public_id,
            len(transcript_items),
        )
        turns: list[CallTranscriptTurn] = []
        for index, item in enumerate(transcript_items):
            if isinstance(item, dict):
                speaker = normalize_speaker(
                    item.get('speaker')
                    or item.get('role')
                    or item.get('name')
                    or item.get('from')
                )
                message = (
                    item.get('message')
                    or item.get('text')
                    or item.get('content')
                    or item.get('transcript')
                    or ''
                )
                # Prefer absolute timestamps only. time_in_call_secs is a
                # relative offset and must not be parsed as unix epoch.
                started_at = _parse_event_timestamp(
                    item.get('timestamp')
                    or item.get('start_time')
                )
                raw = item
                if isinstance(raw, dict):
                    raw = dict(raw)
                    if 'tool_calls' in raw:
                        raw['tool_calls'] = _slim_transcript_tool_entries(
                            raw.get('tool_calls')
                        )
                    if 'tool_results' in raw:
                        raw['tool_results'] = _slim_transcript_tool_entries(
                            raw.get('tool_results')
                        )
            elif isinstance(item, str):
                speaker = CallTranscriptTurn.Speaker.UNKNOWN
                message = item
                started_at = None
                raw = {'text': item}
            else:
                logger.debug(
                    'Transcript item skipped call_id=%s index=%s type=%s',
                    call.pk,
                    index,
                    type(item).__name__,
                )
                continue

            turn, _created = CallTranscriptTurn.objects.update_or_create(
                call=call,
                turn_index=index,
                defaults={
                    'speaker': speaker,
                    'message': str(message),
                    'started_at': started_at,
                    'raw': raw if isinstance(raw, dict) else {'value': raw},
                },
            )
            turns.append(turn)

        transcript_data_list: list[dict[str, Any]] = []
        for turn in turns:
            item_data: dict[str, Any] = {
                'turn_index': turn.turn_index,
                'speaker': turn.speaker,
                'message': turn.message,
            }
            if isinstance(turn.raw, dict):
                time_in_call_secs = turn.raw.get('time_in_call_secs')
                if isinstance(time_in_call_secs, (int, float)):
                    item_data['time_in_call_secs'] = time_in_call_secs
            transcript_data_list.append(item_data)

        call.transcript_data = transcript_data_list
        call.save(update_fields=['transcript_data', 'updated_at'])
        logger.info(
            'Transcript normalization finished call_id=%s public_id=%s '
            'turns=%d',
            call.pk,
            call.public_id,
            len(turns),
        )
        return turns


class CallRecordingFetchService:
    def __init__(
        self,
        *,
        client_factory: type[ElevenLabsClient] | None = None,
        configuration_resolver: AgentConfigurationResolver | None = None,
    ) -> None:
        self.client_factory = client_factory or ElevenLabsClient
        self.configuration_resolver = (
            configuration_resolver or AgentConfigurationResolver()
        )

    def store_bytes(
        self,
        *,
        audio_bytes: bytes,
        call: Call | None = None,
        conversation_id: str = '',
    ) -> str | None:
        """Persist MP3 bytes from a post_call_audio webhook and update Call if known."""
        if not audio_bytes:
            logger.warning(
                'Recording store skipped conversation_id=%s reason=empty_audio',
                conversation_id,
            )
            return None
        if not settings.ELEVENLABS_CALL_RECORDING_ENABLED:
            logger.info(
                'Recording store skipped conversation_id=%s reason=recording_disabled',
                conversation_id,
            )
            return None

        if call is not None and call.recording_available and call.recording_storage_key:
            logger.info(
                'Recording store skipped call_id=%s public_id=%s '
                'reason=already_available',
                call.pk,
                call.public_id,
            )
            return call.recording_storage_key

        if call is not None:
            relative_name = f'{call.company_id}/{call.public_id}.mp3'
        elif conversation_id:
            safe_conversation = conversation_id.replace('/', '_')
            relative_name = f'pending/{safe_conversation}.mp3'
        else:
            logger.warning('Recording store skipped reason=missing_call_and_conversation')
            return None

        storage = get_call_recording_storage()
        storage_key = storage.save(relative_name, ContentFile(audio_bytes))
        if call is not None:
            call.recording_storage_key = storage_key
            call.recording_content_type = 'audio/mpeg'
            call.recording_size_bytes = len(audio_bytes)
            call.recording_available = True
            call.recording_fetched_at = timezone.now()
            call.save(
                update_fields=[
                    'recording_storage_key',
                    'recording_content_type',
                    'recording_size_bytes',
                    'recording_available',
                    'recording_fetched_at',
                    'updated_at',
                ]
            )
        logger.info(
            'Recording stored from webhook bytes=%d storage_key=%s call_id=%s '
            'conversation_id=%s',
            len(audio_bytes),
            storage_key,
            getattr(call, 'pk', None),
            conversation_id,
        )
        return storage_key

    def attach_pending_recording(
        self,
        call: Call,
        *,
        storage_key: str,
        size_bytes: int | None = None,
    ) -> bool:
        if call.recording_available and call.recording_storage_key:
            return False
        call.recording_storage_key = storage_key
        call.recording_content_type = 'audio/mpeg'
        call.recording_size_bytes = size_bytes
        call.recording_available = True
        call.recording_fetched_at = timezone.now()
        call.save(
            update_fields=[
                'recording_storage_key',
                'recording_content_type',
                'recording_size_bytes',
                'recording_available',
                'recording_fetched_at',
                'updated_at',
            ]
        )
        return True

    def fetch_and_store(self, call: Call) -> bool:
        if call.recording_available:
            logger.info(
                'Recording fetch skipped call_id=%s public_id=%s '
                'reason=already_available',
                call.pk,
                call.public_id,
            )
            return False
        if not call.provider_conversation_id:
            logger.warning(
                'Recording fetch skipped call_id=%s public_id=%s '
                'reason=missing_conversation_id',
                call.pk,
                call.public_id,
            )
            return False
        if not settings.ELEVENLABS_CALL_RECORDING_ENABLED:
            logger.info(
                'Recording fetch skipped call_id=%s public_id=%s '
                'reason=recording_disabled',
                call.pk,
                call.public_id,
            )
            return False

        logger.info(
            'Recording fetch started call_id=%s public_id=%s conversation_id=%s',
            call.pk,
            call.public_id,
            call.provider_conversation_id,
        )
        configuration = self.configuration_resolver.resolve(
            call.agent_config_key
        )
        client = self.client_factory(configuration)
        audio_bytes = client.fetch_conversation_audio(
            call.provider_conversation_id
        )
        storage_key = self.store_bytes(
            audio_bytes=audio_bytes,
            call=call,
            conversation_id=call.provider_conversation_id,
        )
        return bool(storage_key)


def _post_call_task_owner(call: Call) -> User | None:
    if (
        call.context_user is not None
        and call.context_user.company_id == call.company_id
    ):
        return call.context_user
    lead = call.lead
    if lead is not None:
        if (
            lead.assigned_to is not None
            and lead.assigned_to.company_id == call.company_id
        ):
            return lead.assigned_to
        if lead.created_by and lead.created_by.company_id == call.company_id:
            return lead.created_by

    if getattr(call, 'agent', None) and getattr(call.agent, 'created_by', None):
        if call.agent.created_by.company_id == call.company_id:
            return call.agent.created_by

    if call.company_id:
        from django.contrib.auth import get_user_model
        User = get_user_model()
        user = User.objects.filter(company_id=call.company_id, is_active=True).first()
        if user is not None:
            return user

    return None


class PostCallAnalysisService:
    def apply_provider_analysis(
        self,
        call: Call,
        analysis: dict[str, Any],
    ) -> dict[str, Any]:
        summary = (
            analysis.get('summary')
            or analysis.get('call_summary')
            or analysis.get('transcript_summary')
            or ''
        )
        sentiments = (
            analysis.get('key_sentiments')
            or analysis.get('sentiments')
            or []
        )
        if not sentiments:
            sentiment_analysis = analysis.get('sentiment_analysis')
            if isinstance(sentiment_analysis, list):
                sentiments = sentiment_analysis
            elif isinstance(sentiment_analysis, dict):
                label = (
                    sentiment_analysis.get('label')
                    or sentiment_analysis.get('overall')
                    or sentiment_analysis.get('sentiment')
                )
                if label:
                    sentiments = [label]
        intents = (
            analysis.get('detected_intents')
            or analysis.get('intents')
            or analysis.get('action_items')
            or []
        )
        tasks = analysis.get('tasks') or analysis.get('follow_up_tasks') or []
        return {
            'summary': summary,
            'key_sentiments': sentiments if isinstance(sentiments, list) else [],
            'detected_intents': intents if isinstance(intents, list) else [],
            'tasks': tasks if isinstance(tasks, list) else [],
        }

    def analyze_call(self, call: Call) -> dict[str, Any] | None:
        provider_analysis = call.provider_analysis or {}
        normalized: dict[str, Any] = {}
        if provider_analysis:
            normalized = self.apply_provider_analysis(call, provider_analysis)

        transcript_text = format_transcript_as_text(call.transcript_data)

        # Check if provider analysis is missing key_sentiments, detected_intents, or tasks
        need_openai = (
            not normalized.get('summary')
            or not normalized.get('key_sentiments')
            or not normalized.get('detected_intents')
            or not normalized.get('tasks')
        )

        if need_openai and transcript_text.strip():
            logger.info(
                'Post-call analysis invoking OpenAI to extract missing metadata/tasks '
                'call_id=%s public_id=%s',
                call.pk,
                call.public_id,
            )
            openai_analysis = analyze_transcript_with_openai(transcript_text)
            if openai_analysis and isinstance(openai_analysis, dict):
                if not normalized.get('summary') and openai_analysis.get('summary'):
                    normalized['summary'] = openai_analysis['summary']
                if not normalized.get('key_sentiments') and openai_analysis.get('key_sentiments'):
                    normalized['key_sentiments'] = openai_analysis['key_sentiments']
                if not normalized.get('detected_intents') and openai_analysis.get('detected_intents'):
                    normalized['detected_intents'] = openai_analysis['detected_intents']
                if not normalized.get('tasks') and openai_analysis.get('tasks'):
                    normalized['tasks'] = openai_analysis['tasks']
                if openai_analysis.get('lead_status'):
                    normalized['lead_status'] = openai_analysis['lead_status']

        if not normalized.get('summary') and transcript_text.strip():
            normalized['summary'] = (
                transcript_text[:300] + '...' if len(transcript_text) > 300 else transcript_text
            )

        return normalized if normalized else None

    def persist_analysis(self, call: Call, analysis: dict[str, Any]) -> Call:
        call.summary = analysis.get('summary', '') or call.summary
        call.key_sentiments = analysis.get('key_sentiments', []) or []
        call.detected_intents = analysis.get('detected_intents', []) or []
        call.call_insights = {
            'objections': analysis.get('objections', []) or [],
            'preferences': analysis.get('preferences', []) or [],
            'requirements_changed': bool(
                analysis.get('requirements_changed', False)
            ),
        }
        call.save(
            update_fields=[
                'summary',
                'key_sentiments',
                'detected_intents',
                'call_insights',
                'updated_at',
            ]
        )
        detected_raw = analysis.get('lead_status')
        if detected_raw and call.lead_id:
            from users.models import Lead

            lead = Lead.objects.get(pk=call.lead_id)
            call.lead = lead
            detected_status, set_dnc = normalize_post_call_lead_status(detected_raw)
            generic_statuses = {Lead.Status.CONTACTED, Lead.Status.NO_ANSWER}
            live_status_update = CallGeneratedAction.objects.filter(
                call=call,
                action_type=CallGeneratedAction.ActionType.UPDATE_LEAD,
                status=CallGeneratedAction.Status.COMPLETED,
                payload__changes__has_key='status',
            ).exists()
            if live_status_update:
                logger.info(
                    'Post-call analysis skipped lead status call_id=%s lead_id=%s '
                    'reason=live_update_lead detected_status=%s current_status=%s',
                    call.pk,
                    call.lead_id,
                    detected_status,
                    lead.status,
                )
            is_valid_status = (
                detected_status is not None
                and detected_status in Lead.AI_SETTABLE_STATUSES
            )
            not_dnc = not lead.do_not_contact
            is_currently_closed = lead.stage == Lead.Stage.CLOSED
            detected_stage = Lead.STATUS_TO_STAGE.get(detected_status)
            would_clobber = (
                detected_status in generic_statuses
                and lead.status not in (
                    Lead.Status.NEW,
                    Lead.Status.CALL_PENDING,
                    Lead.Status.CONTACTED,
                    Lead.Status.NO_ANSWER,
                )
            )

            can_update = (
                is_valid_status
                and not_dnc
                and not live_status_update
                and not would_clobber
                and (not is_currently_closed or detected_stage == Lead.Stage.CLOSED)
            )

            if can_update:
                lead.status = detected_status
                update_fields = ['status', 'updated_at']
                if set_dnc:
                    lead.do_not_contact = True
                    update_fields.append('do_not_contact')
                lead.save(update_fields=update_fields)
                logger.info(
                    'Post-call analysis updated lead status from transcript call_id=%s lead_id=%s new_status=%s stage=%s dnc=%s',
                    call.pk,
                    call.lead_id,
                    detected_status,
                    lead.stage,
                    lead.do_not_contact,
                )

        logger.info(
            'Post-call analysis persisted call_id=%s public_id=%s '
            'summary_chars=%d sentiments=%d intents=%d',
            call.pk,
            call.public_id,
            len(call.summary or ''),
            len(call.key_sentiments or []),
            len(call.detected_intents or []),
        )
        return call

    def materialize_tasks(
        self,
        call: Call,
        tasks: list[dict[str, Any]],
    ) -> int:
        owner = _post_call_task_owner(call)
        if owner is None or not tasks:
            logger.info(
                'Post-call task materialization skipped call_id=%s '
                'public_id=%s owner_found=%s task_count=%d',
                call.pk,
                call.public_id,
                owner is not None,
                len(tasks),
            )
            return 0

        live_task_exists = CallGeneratedAction.objects.filter(
            call=call,
            action_type=CallGeneratedAction.ActionType.CREATE_TASK,
            status=CallGeneratedAction.Status.COMPLETED,
        ).exists()
        if live_task_exists:
            logger.info(
                'Post-call task materialization skipped call_id=%s '
                'public_id=%s reason=live_create_task_exists requested=%d',
                call.pk,
                call.public_id,
                len(tasks),
            )
            return 0

        created_count = 0
        today = timezone.localdate()
        for index, item in enumerate(tasks):
            title = item.get('name') or item.get('title') or 'Call Follow-up Task'
            idempotency_key = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f'post-call-task:{call.public_id}:{index}:{title}',
            )
            priority = str(item.get('priority', Task.Priority.MEDIUM)).lower()
            if priority not in {
                Task.Priority.HIGH,
                Task.Priority.MEDIUM,
                Task.Priority.LOW,
            }:
                priority = Task.Priority.MEDIUM

            due_days = item.get('due_days')
            due_date = None
            open_ended = False
            if isinstance(due_days, int):
                due_date = today + timedelta(days=due_days)
            elif item.get('due_date'):
                parsed_due = parse_datetime(str(item['due_date']))
                if parsed_due is not None:
                    due_date = parsed_due.date()
            else:
                open_ended = True

            description = str(item.get('description') or '').strip()
            payload = {
                'title': title,
                'description': description,
                'priority': priority,
                'due_date': due_date.isoformat() if isinstance(due_date, date) else None,
                'open_ended': open_ended,
                'source': 'post_call_analysis',
            }
            existing = CallGeneratedAction.objects.filter(
                idempotency_key=idempotency_key
            ).first()
            if existing is not None:
                logger.info(
                    'Post-call task already exists call_id=%s index=%s '
                    'action_id=%s',
                    call.pk,
                    index,
                    existing.pk,
                )
                continue

            try:
                with transaction.atomic():
                    action = CallGeneratedAction.objects.create(
                        call=call,
                        action_type=CallGeneratedAction.ActionType.CREATE_TASK,
                        title=title,
                        payload=payload,
                        idempotency_key=idempotency_key,
                    )
                    task = Task.objects.create(
                        name=title,
                        description=description,
                        status=Task.Status.PENDING,
                        priority=priority,
                        due_date=due_date,
                        open_ended=open_ended,
                        created_by=owner,
                        related_call=call,
                        related_lead=call.lead,
                        associated_country=(
                            call.lead.desired_country or call.lead.country
                            if call.lead is not None
                            else ''
                        ),
                    )
                    action.task = task
                    action.status = CallGeneratedAction.Status.COMPLETED
                    action.save(
                        update_fields=['task', 'status', 'updated_at']
                    )
            except IntegrityError:
                logger.info(
                    'Post-call task create raced call_id=%s index=%s title=%s',
                    call.pk,
                    index,
                    title,
                )
                continue
            created_count += 1
        logger.info(
            'Post-call task materialization finished call_id=%s public_id=%s '
            'created=%d requested=%d owner_id=%s',
            call.pk,
            call.public_id,
            created_count,
            len(tasks),
            owner.pk,
        )
        return created_count


def get_friendly_failure_notification(call: Call) -> tuple[str, str]:
    lead_name = call.lead_name or 'lead'
    phone_no = call.phone_number or ''
    phone_str = f' ({phone_no})' if phone_no else ''

    detail_lower = (call.failure_detail or '').lower()
    code_lower = (call.failure_code or '').lower()
    status_lower = (call.status or '').lower()

    if (
        'no-answer' in detail_lower
        or 'no_answer' in status_lower
        or 'no-answer' in status_lower
    ):
        return 'Call Not Answered', f'{lead_name}{phone_str} did not pick up the call.'

    if 'busy' in detail_lower or 'busy' in status_lower:
        return 'Line Busy', f'{lead_name}{phone_str} was busy on another call.'

    if 'invalid' in detail_lower or 'invalid_phone' in code_lower:
        return 'Invalid Phone Number', f'The phone number{phone_str} for {lead_name} is invalid.'

    if 'lead_unavailable' in code_lower:
        return 'Lead Removed', f'The lead record for {lead_name} was removed before call initiation.'

    return f'Call Failed for {lead_name}', call.failure_detail or f'The outbound call to {lead_name}{phone_str} could not be completed.'


class CallWebhookNotificationService:
    def notify_completed(self, call: Call) -> None:
        recipient = _post_call_task_owner(call)
        if recipient is None:
            logger.warning(
                'Completed-call notification skipped call_id=%s public_id=%s '
                'reason=no_recipient',
                call.pk,
                call.public_id,
            )
            return
        send_notification(
            recipient=recipient,
            type='generic',
            title=f'Call completed with {call.lead_name or "lead"}',
            message=call.summary or 'The outbound call has completed.',
            data={
                'call_public_id': str(call.public_id),
                'lead_id': call.lead_id,
                'status': call.status,
            },
        )
        logger.info(
            'Completed-call notification sent call_id=%s public_id=%s '
            'recipient_id=%s',
            call.pk,
            call.public_id,
            recipient.pk,
        )

    def notify_failed(self, call: Call) -> None:
        recipient = _post_call_task_owner(call)
        if recipient is None:
            logger.warning(
                'Failed-call notification skipped call_id=%s public_id=%s '
                'reason=no_recipient',
                call.pk,
                call.public_id,
            )
            return
        title, message = get_friendly_failure_notification(call)
        send_notification(
            recipient=recipient,
            type='generic',
            title=title,
            message=message,
            data={
                'call_public_id': str(call.public_id),
                'lead_id': call.lead_id,
                'status': call.status,
                'failure_code': call.failure_code,
            },
        )
        logger.info(
            'Failed-call notification sent call_id=%s public_id=%s '
            'recipient_id=%s failure_code=%s',
            call.pk,
            call.public_id,
            recipient.pk,
            call.failure_code,
        )



class CallWebhookProcessingService:
    def __init__(
        self,
        *,
        status_service: CallStatusTransitionService | None = None,
        transcript_service: CallTranscriptNormalizationService | None = None,
        recording_service: CallRecordingFetchService | None = None,
        analysis_service: PostCallAnalysisService | None = None,
        notification_service: CallWebhookNotificationService | None = None,
    ) -> None:
        self.status_service = status_service or CallStatusTransitionService()
        self.transcript_service = (
            transcript_service or CallTranscriptNormalizationService()
        )
        self.recording_service = recording_service or CallRecordingFetchService()
        self.analysis_service = analysis_service or PostCallAnalysisService()
        self.notification_service = (
            notification_service or CallWebhookNotificationService()
        )

    def process(self, event_id: int) -> CallWebhookEvent:
        logger.info('Webhook processing started event_id=%s', event_id)
        with transaction.atomic():
            webhook_event = (
                CallWebhookEvent.objects.select_for_update(of=('self',))
                .select_related('call')
                .filter(pk=event_id)
                .first()
            )
            if webhook_event is None:
                logger.error(
                    'Webhook processing aborted event_id=%s reason=not_found',
                    event_id,
                )
                raise WebhookProcessingError('Webhook event not found.')
            if webhook_event.process_status in {
                CallWebhookEvent.ProcessStatus.COMPLETED,
                CallWebhookEvent.ProcessStatus.IGNORED,
            }:
                logger.info(
                    'Webhook processing skipped event_id=%s event_type=%s '
                    'process_status=%s reason=already_terminal',
                    webhook_event.pk,
                    webhook_event.event_type,
                    webhook_event.process_status,
                )
                return webhook_event

            webhook_event.process_status = CallWebhookEvent.ProcessStatus.PROCESSING
            webhook_event.save(update_fields=['process_status'])
            logger.info(
                'Webhook processing marked processing event_id=%s event_type=%s '
                'conversation_id=%s call_id=%s',
                webhook_event.pk,
                webhook_event.event_type,
                webhook_event.conversation_id,
                webhook_event.call_id,
            )

        try:
            self._dispatch(webhook_event)
            webhook_event.refresh_from_db()
            if (
                webhook_event.process_status
                == CallWebhookEvent.ProcessStatus.PROCESSING
            ):
                webhook_event.process_status = (
                    CallWebhookEvent.ProcessStatus.COMPLETED
                )
            webhook_event.last_error = ''
            webhook_event.processed_at = timezone.now()
            webhook_event.save(
                update_fields=[
                    'process_status',
                    'last_error',
                    'processed_at',
                ]
            )
            logger.info(
                'Webhook processing finished event_id=%s event_type=%s '
                'process_status=%s conversation_id=%s call_id=%s',
                webhook_event.pk,
                webhook_event.event_type,
                webhook_event.process_status,
                webhook_event.conversation_id,
                webhook_event.call_id,
            )
        except Exception as exc:
            logger.exception(
                'Webhook processing failed event_id=%s event_type=%s '
                'conversation_id=%s error=%s',
                webhook_event.pk,
                webhook_event.event_type,
                webhook_event.conversation_id,
                type(exc).__name__,
            )
            webhook_event.retry_count += 1
            webhook_event.process_status = CallWebhookEvent.ProcessStatus.FAILED
            webhook_event.last_error = str(exc)[:500]
            webhook_event.save(
                update_fields=[
                    'retry_count',
                    'process_status',
                    'last_error',
                ]
            )
            raise
        return webhook_event

    def _dispatch(self, webhook_event: CallWebhookEvent) -> None:
        handlers = {
            CallWebhookEvent.EventType.POST_CALL_TRANSCRIPTION: (
                self._handle_post_call_transcription
            ),
            CallWebhookEvent.EventType.POST_CALL_AUDIO: (
                self._handle_post_call_audio
            ),
            CallWebhookEvent.EventType.CALL_INITIATION_FAILURE: (
                self._handle_call_initiation_failure
            ),
        }
        handler = handlers.get(webhook_event.event_type)
        if handler is None:
            logger.warning(
                'Webhook handler missing event_id=%s event_type=%s; ignoring',
                webhook_event.pk,
                webhook_event.event_type,
            )
            webhook_event.process_status = CallWebhookEvent.ProcessStatus.IGNORED
            webhook_event.save(update_fields=['process_status'])
            return
        logger.info(
            'Webhook dispatch event_id=%s event_type=%s handler=%s',
            webhook_event.pk,
            webhook_event.event_type,
            handler.__name__,
        )
        handler(webhook_event)

    def _resolve_call(self, webhook_event: CallWebhookEvent) -> Call | None:
        if webhook_event.call_id is not None:
            call = (
                Call.objects.select_related(
                    'lead',
                    'lead__created_by',
                    'lead__assigned_to',
                    'context_user',
                    'company',
                )
                .filter(pk=webhook_event.call_id)
                .first()
            )
            logger.info(
                'Webhook resolve_call by call_id event_id=%s call_id=%s '
                'found=%s',
                webhook_event.pk,
                webhook_event.call_id,
                call is not None,
            )
            return call
        if not webhook_event.conversation_id:
            logger.warning(
                'Webhook resolve_call failed event_id=%s reason=no_conversation_id',
                webhook_event.pk,
            )
            return None
        call = (
            Call.objects.select_related(
                'lead',
                'lead__created_by',
                'lead__assigned_to',
                'context_user',
                'company',
            )
            .filter(provider_conversation_id=webhook_event.conversation_id)
            .first()
        )
        if call is not None and webhook_event.call_id is None:
            webhook_event.call = call
            webhook_event.save(update_fields=['call'])
            logger.info(
                'Webhook resolve_call linked event_id=%s conversation_id=%s '
                'call_id=%s public_id=%s',
                webhook_event.pk,
                webhook_event.conversation_id,
                call.pk,
                call.public_id,
            )
        elif call is None:
            logger.warning(
                'Webhook resolve_call failed event_id=%s conversation_id=%s '
                'reason=no_matching_call',
                webhook_event.pk,
                webhook_event.conversation_id,
            )
        return call

    def _handle_post_call_transcription(
        self,
        webhook_event: CallWebhookEvent,
    ) -> None:
        logger.info(
            'Webhook step=post_call_transcription start event_id=%s',
            webhook_event.pk,
        )
        call = self._resolve_call(webhook_event)
        if call is None:
            logger.warning(
                'Webhook step=post_call_transcription ignored event_id=%s '
                'reason=call_not_found',
                webhook_event.pk,
            )
            webhook_event.process_status = CallWebhookEvent.ProcessStatus.IGNORED
            webhook_event.save(update_fields=['process_status'])
            return

        payload = webhook_event.payload
        data = payload.get('data') if isinstance(payload.get('data'), dict) else {}
        transcript = data.get('transcript') or data.get('conversation') or []
        metadata = data.get('metadata') if isinstance(data.get('metadata'), dict) else {}
        analysis = data.get('analysis') if isinstance(data.get('analysis'), dict) else {}
        phone_call = (
            metadata.get('phone_call')
            if isinstance(metadata.get('phone_call'), dict)
            else {}
        )

        logger.info(
            'Webhook step=post_call_transcription payload call_id=%s '
            'public_id=%s transcript_items=%s has_analysis=%s has_metadata=%s',
            call.pk,
            call.public_id,
            len(transcript) if isinstance(transcript, list) else 0,
            bool(analysis),
            bool(metadata),
        )

        identity_updates: list[str] = []
        agent_id = data.get('agent_id')
        if agent_id and not call.provider_agent_id:
            call.provider_agent_id = str(agent_id)
            identity_updates.append('provider_agent_id')
        call_sid = phone_call.get('call_sid') or phone_call.get('CallSid')
        if call_sid and not call.provider_call_id:
            call.provider_call_id = str(call_sid)
            identity_updates.append('provider_call_id')
        phone_number_id = phone_call.get('phone_number_id')
        if phone_number_id and not call.provider_phone_number_id:
            call.provider_phone_number_id = str(phone_number_id)
            identity_updates.append('provider_phone_number_id')
        if identity_updates:
            identity_updates.append('updated_at')
            call.save(update_fields=identity_updates)

        if isinstance(transcript, list):
            self.transcript_service.upsert_turns(call, transcript)
        else:
            logger.warning(
                'Webhook transcript missing or invalid call_id=%s type=%s',
                call.pk,
                type(transcript).__name__,
            )

        if analysis:
            call.provider_analysis = analysis
            call.save(update_fields=['provider_analysis', 'updated_at'])
            logger.info(
                'Webhook provider analysis stored call_id=%s public_id=%s',
                call.pk,
                call.public_id,
            )

        answered_at = _parse_event_timestamp(
            metadata.get('accepted_time_unix_secs')
            or metadata.get('start_time_unix_secs')
        )
        ended_at = _parse_event_timestamp(
            metadata.get('end_time')
            or payload.get('event_timestamp')
            or data.get('ended_at')
        ) or timezone.now()
        duration_seconds = metadata.get('call_duration_secs')
        if duration_seconds is None:
            duration_seconds = metadata.get('duration_seconds')
        if isinstance(duration_seconds, (int, float)):
            duration_seconds = int(duration_seconds)
        else:
            duration_seconds = None

        self.status_service.apply(
            call,
            Call.Status.COMPLETED,
            answered_at=answered_at,
            ended_at=ended_at,
            duration_seconds=duration_seconds,
        )
        call.refresh_from_db()

        analysis_result = self.analysis_service.analyze_call(call)
        if analysis_result:
            self.analysis_service.persist_analysis(call, analysis_result)
            created_tasks = self.analysis_service.materialize_tasks(
                call,
                analysis_result.get('tasks', []),
            )
            logger.info(
                'Webhook analysis step finished call_id=%s tasks_created=%s',
                call.pk,
                created_tasks,
            )
        else:
            logger.warning(
                'Webhook analysis step produced no result call_id=%s public_id=%s',
                call.pk,
                call.public_id,
            )

        self.notification_service.notify_completed(call)
        logger.info(
            'Webhook step=post_call_transcription done event_id=%s call_id=%s '
            'status=%s',
            webhook_event.pk,
            call.pk,
            call.status,
        )

    def _handle_post_call_audio(self, webhook_event: CallWebhookEvent) -> None:
        logger.info(
            'Webhook step=post_call_audio start event_id=%s',
            webhook_event.pk,
        )
        call = self._resolve_call(webhook_event)
        if call is None:
            logger.warning(
                'Webhook step=post_call_audio ignored event_id=%s '
                'reason=call_not_found',
                webhook_event.pk,
            )
            webhook_event.process_status = CallWebhookEvent.ProcessStatus.IGNORED
            webhook_event.save(update_fields=['process_status'])
            return

        if call.recording_available:
            logger.info(
                'Webhook step=post_call_audio done event_id=%s call_id=%s '
                'reason=already_available',
                webhook_event.pk,
                call.pk,
            )
            return

        payload = webhook_event.payload if isinstance(webhook_event.payload, dict) else {}
        data = payload.get('data') if isinstance(payload.get('data'), dict) else {}
        storage_key = str(data.get('recording_storage_key') or '')
        size_bytes = data.get('recording_size_bytes')
        if not storage_key:
            logger.warning(
                'Webhook step=post_call_audio ignored event_id=%s call_id=%s '
                'reason=no_recording_storage_key',
                webhook_event.pk,
                call.pk,
            )
            webhook_event.process_status = CallWebhookEvent.ProcessStatus.IGNORED
            webhook_event.save(update_fields=['process_status'])
            return

        attached = self.recording_service.attach_pending_recording(
            call,
            storage_key=storage_key,
            size_bytes=int(size_bytes) if isinstance(size_bytes, (int, float)) else None,
        )
        logger.info(
            'Webhook step=post_call_audio done event_id=%s call_id=%s '
            'attached=%s storage_key=%s',
            webhook_event.pk,
            call.pk,
            attached,
            storage_key,
        )

    def _handle_call_initiation_failure(
        self,
        webhook_event: CallWebhookEvent,
    ) -> None:
        logger.info(
            'Webhook step=call_initiation_failure start event_id=%s',
            webhook_event.pk,
        )
        call = self._resolve_call(webhook_event)
        if call is None:
            logger.warning(
                'Webhook step=call_initiation_failure ignored event_id=%s '
                'reason=call_not_found',
                webhook_event.pk,
            )
            webhook_event.process_status = CallWebhookEvent.ProcessStatus.IGNORED
            webhook_event.save(update_fields=['process_status'])
            return

        payload = webhook_event.payload
        data = payload.get('data') if isinstance(payload.get('data'), dict) else {}
        failure_reason = str(
            data.get('failure_reason')
            or data.get('reason')
            or data.get('message')
            or 'Call initiation failed.'
        )
        new_status = _initiation_failure_status(failure_reason)
        failure_detail = _initiation_failure_detail(data, failure_reason)
        logger.info(
            'Webhook step=call_initiation_failure applying failure call_id=%s '
            'status=%s reason=%s',
            call.pk,
            new_status,
            failure_detail[:200],
        )
        self.status_service.apply(
            call,
            new_status,
            failure_code='initiation_failure',
            failure_detail=failure_detail,
            ended_at=timezone.now(),
        )
        call.refresh_from_db()
        self.notification_service.notify_failed(call)
        logger.info(
            'Webhook step=call_initiation_failure done event_id=%s call_id=%s '
            'status=%s',
            webhook_event.pk,
            call.pk,
            call.status,
        )
