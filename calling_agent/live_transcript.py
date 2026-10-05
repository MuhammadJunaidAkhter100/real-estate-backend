from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.utils import timezone

from calling_agent.models import Call
from calling_agent.transcript import normalize_speaker

logger = logging.getLogger(__name__)

LIVE_CALL_STATUSES = {
    Call.Status.CLAIMED,
    Call.Status.INITIATING,
    Call.Status.INITIATION_UNKNOWN,
    Call.Status.RINGING,
    Call.Status.IN_PROGRESS,
}
TERMINAL_CALL_STATUSES = {
    Call.Status.COMPLETED,
    Call.Status.FAILED,
    Call.Status.CANCELLED,
    Call.Status.NO_ANSWER,
    Call.Status.BUSY,
}

MONITOR_LOCK_PREFIX = 'calling_agent:live_transcript:'
MONITOR_LOCK_TTL_SECONDS = 2 * 60 * 60
MONITOR_RECV_TIMEOUT_SECONDS = 5.0
MONITOR_RETRY_INITIAL_SECONDS = 1.0
MONITOR_RETRY_MAX_SECONDS = 15.0

_monitor_tasks: dict[str, asyncio.Task[None]] = {}
_monitor_guard = asyncio.Lock()


def call_transcript_group_name(public_id: UUID | str) -> str:
    return f'call_transcript_{public_id}'


def live_transcript_enabled() -> bool:
    return bool(getattr(settings, 'ELEVENLABS_LIVE_TRANSCRIPT_ENABLED', True))


def build_monitor_ws_url(conversation_id: str, api_base_url: str) -> str:
    parsed = urlparse(api_base_url.rstrip('/'))
    scheme = 'wss' if parsed.scheme != 'http' else 'ws'
    host = parsed.netloc or parsed.path
    return (
        f'{scheme}://{host}/v1/convai/conversations/'
        f'{conversation_id}/monitor'
    )


@dataclass(frozen=True)
class LiveTranscriptTurn:
    call_id: str
    event_id: int
    speaker: str
    text: str
    is_final: bool
    sequence: int
    received_at: str
    time_in_call_secs: float | None

    def to_payload(self) -> dict[str, Any]:
        return {
            'call_id': self.call_id,
            'event_id': self.event_id,
            'speaker': self.speaker,
            'text': self.text,
            'is_final': self.is_final,
            'sequence': self.sequence,
            'received_at': self.received_at,
            'time_in_call_secs': self.time_in_call_secs,
        }


def _coerce_event_id(raw: Any, fallback: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return fallback
    return value if value else fallback


class LiveTranscriptSession:
    """In-memory partial-to-final merge. Never writes to PostgreSQL."""

    def __init__(self) -> None:
        self._fallback_event_id = 0
        self._partial_agent_event_id: int | None = None
        self._partial_agent_text = ''
        self._last_agent_event_id: int | None = None
        self._last_agent_text = ''

    def apply_event(
        self,
        event: dict[str, Any],
        *,
        call_public_id: str,
        time_in_call_secs: float | None,
        received_at: datetime | None = None,
    ) -> LiveTranscriptTurn | None:
        if not isinstance(event, dict):
            return None
        stamp = received_at or timezone.now()
        received_at_iso = stamp.isoformat()
        event_type = str(event.get('type') or '')
        if event_type == 'user_transcript':
            return self._apply_user_transcript(
                event,
                call_public_id=call_public_id,
                time_in_call_secs=time_in_call_secs,
                received_at_iso=received_at_iso,
            )
        if event_type == 'agent_chat_response_part':
            return self._apply_agent_chat_response_part(
                event,
                call_public_id=call_public_id,
                time_in_call_secs=time_in_call_secs,
                received_at_iso=received_at_iso,
            )
        if event_type == 'agent_response':
            return self._apply_agent_response(
                event,
                call_public_id=call_public_id,
                time_in_call_secs=time_in_call_secs,
                received_at_iso=received_at_iso,
            )
        if event_type == 'agent_response_correction':
            return self._apply_agent_response_correction(
                event,
                call_public_id=call_public_id,
                time_in_call_secs=time_in_call_secs,
                received_at_iso=received_at_iso,
            )
        return None

    def _next_fallback_id(self) -> int:
        self._fallback_event_id += 1
        return self._fallback_event_id

    def _turn(
        self,
        *,
        call_public_id: str,
        event_id: int,
        speaker: str,
        text: str,
        is_final: bool,
        time_in_call_secs: float | None,
        received_at_iso: str,
    ) -> LiveTranscriptTurn:
        return LiveTranscriptTurn(
            call_id=call_public_id,
            event_id=event_id,
            speaker=speaker,
            text=text,
            is_final=is_final,
            sequence=event_id,
            received_at=received_at_iso,
            time_in_call_secs=time_in_call_secs,
        )

    def _apply_user_transcript(
        self,
        event: dict[str, Any],
        *,
        call_public_id: str,
        time_in_call_secs: float | None,
        received_at_iso: str,
    ) -> LiveTranscriptTurn | None:
        inner = event.get('user_transcription_event')
        if not isinstance(inner, dict):
            return None
        text = str(inner.get('user_transcript') or '').strip()
        if not text:
            return None
        event_id = _coerce_event_id(
            inner.get('event_id'),
            self._next_fallback_id(),
        )
        speaker = normalize_speaker('user')
        return self._turn(
            call_public_id=call_public_id,
            event_id=event_id,
            speaker=speaker,
            text=text,
            is_final=True,
            time_in_call_secs=time_in_call_secs,
            received_at_iso=received_at_iso,
        )

    def _apply_agent_chat_response_part(
        self,
        event: dict[str, Any],
        *,
        call_public_id: str,
        time_in_call_secs: float | None,
        received_at_iso: str,
    ) -> LiveTranscriptTurn | None:
        part = event.get('text_response_part')
        if not isinstance(part, dict):
            return None
        part_type = str(part.get('type') or '')
        event_id = _coerce_event_id(part.get('event_id'), 0)
        chunk = str(part.get('text') or '')
        if part_type == 'start':
            self._partial_agent_event_id = event_id or self._next_fallback_id()
            self._partial_agent_text = ''
            return None
        if part_type == 'delta':
            if self._partial_agent_event_id is None:
                self._partial_agent_event_id = event_id or self._next_fallback_id()
                self._partial_agent_text = ''
            elif event_id and event_id != self._partial_agent_event_id:
                self._partial_agent_event_id = event_id
                self._partial_agent_text = ''
            self._partial_agent_text += chunk
            if not self._partial_agent_text:
                return None
            return self._turn(
                call_public_id=call_public_id,
                event_id=self._partial_agent_event_id,
                speaker=normalize_speaker('agent'),
                text=self._partial_agent_text,
                is_final=False,
                time_in_call_secs=time_in_call_secs,
                received_at_iso=received_at_iso,
            )
        if part_type == 'stop':
            text = self._partial_agent_text.strip()
            emit_id = self._partial_agent_event_id or event_id or self._next_fallback_id()
            self._partial_agent_event_id = None
            self._partial_agent_text = ''
            if not text:
                return None
            self._last_agent_event_id = emit_id
            self._last_agent_text = text
            return self._turn(
                call_public_id=call_public_id,
                event_id=emit_id,
                speaker=normalize_speaker('agent'),
                text=text,
                is_final=True,
                time_in_call_secs=time_in_call_secs,
                received_at_iso=received_at_iso,
            )
        return None

    def _apply_agent_response(
        self,
        event: dict[str, Any],
        *,
        call_public_id: str,
        time_in_call_secs: float | None,
        received_at_iso: str,
    ) -> LiveTranscriptTurn | None:
        inner = event.get('agent_response_event')
        if not isinstance(inner, dict):
            return None
        text = str(inner.get('agent_response') or '').strip()
        if not text:
            return None
        incoming_id = _coerce_event_id(
            inner.get('event_id'),
            self._next_fallback_id(),
        )
        if self._partial_agent_event_id is not None:
            emit_id = self._partial_agent_event_id
            self._partial_agent_event_id = None
            self._partial_agent_text = ''
        else:
            emit_id = incoming_id
        self._last_agent_event_id = emit_id
        self._last_agent_text = text
        return self._turn(
            call_public_id=call_public_id,
            event_id=emit_id,
            speaker=normalize_speaker('agent'),
            text=text,
            is_final=True,
            time_in_call_secs=time_in_call_secs,
            received_at_iso=received_at_iso,
        )

    def _apply_agent_response_correction(
        self,
        event: dict[str, Any],
        *,
        call_public_id: str,
        time_in_call_secs: float | None,
        received_at_iso: str,
    ) -> LiveTranscriptTurn | None:
        inner = event.get('agent_response_correction_event')
        if not isinstance(inner, dict):
            return None
        corrected = str(inner.get('corrected_agent_response') or '').strip()
        if not corrected:
            return None
        incoming_id = _coerce_event_id(
            inner.get('event_id'),
            self._next_fallback_id(),
        )
        if self._partial_agent_event_id is not None:
            emit_id = self._partial_agent_event_id
            self._partial_agent_event_id = None
            self._partial_agent_text = ''
        elif self._last_agent_event_id is not None:
            emit_id = self._last_agent_event_id
        else:
            emit_id = incoming_id
        self._last_agent_event_id = emit_id
        self._last_agent_text = corrected
        return self._turn(
            call_public_id=call_public_id,
            event_id=emit_id,
            speaker=normalize_speaker('agent'),
            text=corrected,
            is_final=True,
            time_in_call_secs=time_in_call_secs,
            received_at_iso=received_at_iso,
        )


def relative_call_time_secs(call: Call, now: datetime | None = None) -> float | None:
    base = call.answered_at or call.initiated_at
    if base is None:
        return None
    current = now or timezone.now()
    elapsed = (current - base).total_seconds()
    return elapsed if elapsed >= 0 else 0.0


async def broadcast_group_event(
    public_id: UUID | str,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    await channel_layer.group_send(
        call_transcript_group_name(public_id),
        {'type': event_type, 'payload': payload},
    )


def broadcast_call_ended(call: Call) -> None:
    """Sync entry point used when a call reaches a terminal status."""
    payload = {
        'call_id': str(call.public_id),
        'status': call.status,
    }
    try:
        channel_layer = get_channel_layer()
        if channel_layer is None:
            return
        async_to_sync(channel_layer.group_send)(
            call_transcript_group_name(call.public_id),
            {'type': 'call.ended', 'payload': payload},
        )
    except Exception:
        logger.exception(
            'Failed to broadcast call.ended public_id=%s',
            call.public_id,
        )
    stop_live_transcript_monitor(str(call.public_id))


def _redis_client():
    try:
        import redis
    except ImportError:
        return None
    try:
        hosts = (
            settings.CHANNEL_LAYERS.get('default', {})
            .get('CONFIG', {})
            .get('hosts')
            or []
        )
        if not hosts:
            return None
        return redis.Redis.from_url(str(hosts[0]), decode_responses=True)
    except Exception:
        logger.debug('Live transcript Redis client unavailable', exc_info=True)
        return None


def _try_acquire_monitor_lock(public_id: str) -> bool:
    client = _redis_client()
    if client is None:
        return True
    try:
        acquired = client.set(
            f'{MONITOR_LOCK_PREFIX}{public_id}',
            '1',
            nx=True,
            ex=MONITOR_LOCK_TTL_SECONDS,
        )
        return bool(acquired)
    except Exception:
        logger.debug(
            'Live transcript monitor lock acquire failed public_id=%s',
            public_id,
            exc_info=True,
        )
        return True


def _release_monitor_lock(public_id: str) -> None:
    client = _redis_client()
    if client is None:
        return
    try:
        client.delete(f'{MONITOR_LOCK_PREFIX}{public_id}')
    except Exception:
        logger.debug(
            'Live transcript monitor lock release failed public_id=%s',
            public_id,
            exc_info=True,
        )


def stop_live_transcript_monitor(public_id: str) -> None:
    task = _monitor_tasks.get(public_id)
    if task is not None and not task.done():
        task.cancel()
    _release_monitor_lock(public_id)


async def ensure_live_transcript_monitor(call_id: int) -> None:
    if not live_transcript_enabled():
        return
    from channels.db import database_sync_to_async

    call = await database_sync_to_async(
        lambda: Call.objects.filter(pk=call_id).first()
    )()
    if call is None:
        return
    public_id = str(call.public_id)
    if call.status in TERMINAL_CALL_STATUSES:
        return

    async with _monitor_guard:
        existing = _monitor_tasks.get(public_id)
        if existing is not None and not existing.done():
            return
        if not _try_acquire_monitor_lock(public_id):
            logger.info(
                'Live transcript monitor already held public_id=%s',
                public_id,
            )
            return
        _monitor_tasks[public_id] = asyncio.create_task(
            _run_monitor(call.pk, public_id),
            name=f'live-transcript-{public_id}',
        )


async def _load_call(call_id: int) -> Call | None:
    from channels.db import database_sync_to_async

    return await database_sync_to_async(
        lambda: Call.objects.filter(pk=call_id).first()
    )()


async def _run_monitor(call_id: int, public_id: str) -> None:
    session = LiveTranscriptSession()
    backoff = MONITOR_RETRY_INITIAL_SECONDS
    try:
        while True:
            call = await _load_call(call_id)
            if call is None or call.status in TERMINAL_CALL_STATUSES:
                if call is not None:
                    await broadcast_group_event(
                        public_id,
                        'call.ended',
                        {'call_id': public_id, 'status': call.status},
                    )
                return
            conversation_id = (call.provider_conversation_id or '').strip()
            if not conversation_id:
                await broadcast_group_event(
                    public_id,
                    'transcript.status',
                    {'call_id': public_id, 'status': 'processing'},
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MONITOR_RETRY_MAX_SECONDS)
                continue
            try:
                closed_cleanly = await _stream_monitor_events(
                    call,
                    session,
                )
                backoff = MONITOR_RETRY_INITIAL_SECONDS
                if closed_cleanly:
                    refreshed = await _load_call(call_id)
                    if refreshed is None or refreshed.status in TERMINAL_CALL_STATUSES:
                        if refreshed is not None:
                            await broadcast_group_event(
                                public_id,
                                'call.ended',
                                {
                                    'call_id': public_id,
                                    'status': refreshed.status,
                                },
                            )
                        return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    'Live transcript monitor stream failed public_id=%s',
                    public_id,
                    exc_info=True,
                )
                await broadcast_group_event(
                    public_id,
                    'transcript.status',
                    {'call_id': public_id, 'status': 'disconnected'},
                )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MONITOR_RETRY_MAX_SECONDS)
    except asyncio.CancelledError:
        logger.info('Live transcript monitor cancelled public_id=%s', public_id)
    finally:
        _monitor_tasks.pop(public_id, None)
        _release_monitor_lock(public_id)


async def _stream_monitor_events(
    call: Call,
    session: LiveTranscriptSession,
) -> bool:
    import websockets

    from calling_agent.elevenlabs import AgentConfigurationResolver

    configuration = AgentConfigurationResolver().resolve(call.agent_config_key)
    uri = build_monitor_ws_url(
        call.provider_conversation_id,
        configuration.api_base_url,
    )
    headers = {'xi-api-key': configuration.api_key}
    public_id = str(call.public_id)
    await broadcast_group_event(
        public_id,
        'transcript.status',
        {'call_id': public_id, 'status': 'processing'},
    )
    connect_kwargs: dict[str, Any] = {'additional_headers': headers}
    try:
        websocket_ctx = websockets.connect(uri, **connect_kwargs)
    except TypeError:
        websocket_ctx = websockets.connect(uri, extra_headers=headers)

    async with websocket_ctx as websocket:
        await broadcast_group_event(
            public_id,
            'transcript.status',
            {'call_id': public_id, 'status': 'connected'},
        )
        logger.info(
            'Live transcript monitor connected public_id=%s conversation_id=%s',
            public_id,
            call.provider_conversation_id,
        )
        while True:
            try:
                raw_message = await asyncio.wait_for(
                    websocket.recv(),
                    timeout=MONITOR_RECV_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                refreshed = await _load_call(call.pk)
                if refreshed is None or refreshed.status in TERMINAL_CALL_STATUSES:
                    return True
                continue
            except asyncio.CancelledError:
                raise
            except Exception:
                return False

            if isinstance(raw_message, bytes):
                raw_message = raw_message.decode('utf-8', errors='replace')
            try:
                event = json.loads(raw_message)
            except json.JSONDecodeError:
                logger.debug(
                    'Live transcript ignored non-JSON monitor frame public_id=%s',
                    public_id,
                )
                continue
            if not isinstance(event, dict):
                continue

            refreshed = await _load_call(call.pk)
            if refreshed is None:
                return True
            turn = session.apply_event(
                event,
                call_public_id=public_id,
                time_in_call_secs=relative_call_time_secs(refreshed),
            )
            if turn is None:
                continue
            await broadcast_group_event(
                public_id,
                'transcript.turn',
                turn.to_payload(),
            )
    return False
