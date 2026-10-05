from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from time import monotonic
from typing import Any

import httpx
from django.conf import settings
from elevenlabs import ElevenLabs
from elevenlabs.core.api_error import ApiError
from elevenlabs.types import ConversationInitiationClientDataRequestInput

from calling_agent.exceptions import (
    CallingConfigurationError,
    ElevenLabsRequestError,
    ElevenLabsResponseError,
    ElevenLabsTransientError,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentConfiguration:
    key: str
    api_key: str
    agent_id: str
    phone_number_id: str
    outbound_phone_number: str
    api_base_url: str
    timeout_seconds: int
    recording_enabled: bool


@dataclass(frozen=True)
class OutboundCallResult:
    conversation_id: str
    provider_call_id: str
    message: str


class AgentConfigurationResolver:
    """Resolve provider configuration without coupling callers to settings."""

    def resolve(self, key: str = 'default') -> AgentConfiguration:
        if key != 'default':
            raise CallingConfigurationError(
                f"Unknown ElevenLabs agent configuration key: {key}"
            )

        configuration = AgentConfiguration(
            key=key,
            api_key=settings.ELEVENLABS_API_KEY,
            agent_id=settings.ELEVENLABS_AGENT_ID,
            phone_number_id=settings.ELEVENLABS_AGENT_PHONE_NUMBER_ID,
            outbound_phone_number=settings.ELEVENLABS_OUTBOUND_PHONE_NUMBER,
            api_base_url=settings.ELEVENLABS_API_BASE_URL.rstrip('/'),
            timeout_seconds=settings.ELEVENLABS_REQUEST_TIMEOUT_SECONDS,
            recording_enabled=settings.ELEVENLABS_CALL_RECORDING_ENABLED,
        )
        missing = [
            name
            for name, value in (
                ('ELEVENLABS_API_KEY', configuration.api_key),
                ('ELEVENLABS_AGENT_ID', configuration.agent_id),
                (
                    'ELEVENLABS_AGENT_PHONE_NUMBER_ID',
                    configuration.phone_number_id,
                ),
            )
            if not value
        ]
        if missing:
            raise CallingConfigurationError(
                f"Missing ElevenLabs configuration: {', '.join(missing)}"
            )
        return configuration


class ElevenLabsClient:
    """Small, typed boundary around the ElevenLabs outbound call API."""

    def __init__(
        self,
        configuration: AgentConfiguration,
        client: ElevenLabs | None = None,
    ) -> None:
        self.configuration = configuration
        self.client = client or ElevenLabs(
            api_key=configuration.api_key,
            base_url=configuration.api_base_url,
            timeout=configuration.timeout_seconds,
        )

    def initiate_outbound_call(
        self,
        *,
        to_number: str,
        call_public_id: str,
        lead_id: int,
        lead_name: str,
        company_id: int,
        call_context_token: str,
        extra_dynamic_variables: Mapping[str, str] | None = None,
    ) -> OutboundCallResult:
        started_at = monotonic()
        dynamic_variables: dict[str, Any] = {}
        if extra_dynamic_variables:
            dynamic_variables.update(dict(extra_dynamic_variables))
        # Core auth/identity keys always win over any extras.
        dynamic_variables.update(
            {
                'call_id': call_public_id,
                'lead_id': lead_id,
                'lead_name': lead_name,
                'company_id': company_id,
                'secret__call_context_token': call_context_token,
            }
        )
        initiation_data = ConversationInitiationClientDataRequestInput(
            user_id=f'call:{call_public_id}',
            dynamic_variables=dynamic_variables,
        )

        try:
            response = self.client.conversational_ai.twilio.outbound_call(
                agent_id=self.configuration.agent_id,
                agent_phone_number_id=self.configuration.phone_number_id,
                to_number=to_number,
                conversation_initiation_client_data=initiation_data,
                call_recording_enabled=self.configuration.recording_enabled,
                request_options={
                    'timeout_in_seconds': self.configuration.timeout_seconds,
                    'max_retries': 0,
                },
            )
        except ApiError as exc:
            logger.warning(
                'ElevenLabs rejected outbound call agent_config=%s latency_ms=%d',
                self.configuration.key,
                int((monotonic() - started_at) * 1000),
            )
            raise ElevenLabsRequestError('ElevenLabs rejected the call request.') from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            logger.warning(
                'ElevenLabs outbound call transport failure agent_config=%s '
                'latency_ms=%d',
                self.configuration.key,
                int((monotonic() - started_at) * 1000),
            )
            raise ElevenLabsTransientError(
                'The provider response is unknown after a transport failure.'
            ) from exc
        except Exception as exc:
            logger.exception(
                'Unexpected ElevenLabs outbound call failure agent_config=%s',
                self.configuration.key,
            )
            raise ElevenLabsTransientError(
                'The provider response is unknown after an unexpected failure.'
            ) from exc

        if not response.success:
            raise ElevenLabsRequestError(
                response.message or 'ElevenLabs did not accept the call request.'
            )
        if not response.conversation_id:
            raise ElevenLabsResponseError(
                'ElevenLabs accepted the request without a conversation ID.'
            )

        logger.info(
            'ElevenLabs outbound call accepted agent_config=%s conversation_id=%s '
            'provider_call_id=%s latency_ms=%d',
            self.configuration.key,
            response.conversation_id,
            response.call_sid or '',
            int((monotonic() - started_at) * 1000),
        )
        return OutboundCallResult(
            conversation_id=response.conversation_id,
            provider_call_id=response.call_sid or '',
            message=response.message,
        )

    def fetch_conversation_audio(self, conversation_id: str) -> bytes:
        """Download call audio from the provider API (never from webhook URLs)."""
        started_at = monotonic()
        try:
            chunks = self.client.conversational_ai.conversations.audio.get(
                conversation_id,
                request_options={
                    'timeout_in_seconds': self.configuration.timeout_seconds,
                    'max_retries': 0,
                },
            )
            audio_bytes = b''.join(chunks)
        except ApiError as exc:
            logger.warning(
                'ElevenLabs rejected conversation audio fetch conversation_id=%s '
                'latency_ms=%d',
                conversation_id,
                int((monotonic() - started_at) * 1000),
            )
            raise ElevenLabsRequestError(
                'ElevenLabs rejected the conversation audio request.'
            ) from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            logger.warning(
                'ElevenLabs conversation audio transport failure '
                'conversation_id=%s latency_ms=%d',
                conversation_id,
                int((monotonic() - started_at) * 1000),
            )
            raise ElevenLabsTransientError(
                'The provider audio response is unknown after a transport failure.'
            ) from exc
        except Exception as exc:
            logger.exception(
                'Unexpected ElevenLabs conversation audio failure conversation_id=%s',
                conversation_id,
            )
            raise ElevenLabsTransientError(
                'The provider audio response is unknown after an unexpected failure.'
            ) from exc

        if not audio_bytes:
            raise ElevenLabsResponseError(
                'ElevenLabs returned empty conversation audio.'
            )

        logger.info(
            'ElevenLabs conversation audio fetched conversation_id=%s bytes=%d '
            'latency_ms=%d',
            conversation_id,
            len(audio_bytes),
            int((monotonic() - started_at) * 1000),
        )
        return audio_bytes

    @staticmethod
    def response_to_dict(result: OutboundCallResult) -> dict[str, Any]:
        return {
            'conversation_id': result.conversation_id,
            'provider_call_id': result.provider_call_id,
            'message': result.message,
        }
