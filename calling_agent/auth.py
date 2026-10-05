from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.core import signing
from django.utils.crypto import constant_time_compare
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.request import Request

from calling_agent.models import Call

logger = logging.getLogger(__name__)

CALL_CONTEXT_SALT = 'calling_agent.tool_context.v1'
TOOL_SECRET_HEADER = 'X-ElevenLabs-Tool-Secret'
CALL_CONTEXT_HEADER = 'X-ElevenLabs-Call-Context'


@dataclass(frozen=True)
class ElevenLabsToolPrincipal:
    call_public_id: uuid.UUID

    @property
    def is_authenticated(self) -> bool:
        return True


class CallContextTokenService:
    @staticmethod
    def issue(call: Call) -> str:
        if call.pk is None:
            raise ValueError('A call must be saved before issuing tool context.')
        payload = {
            'version': 1,
            'call_public_id': str(call.public_id),
            'company_id': call.company_id,
            'initiation_key': str(call.initiation_key),
        }
        return signing.dumps(payload, salt=CALL_CONTEXT_SALT, compress=True)

    @staticmethod
    def load(token: str) -> dict[str, Any]:
        payload = signing.loads(
            token,
            salt=CALL_CONTEXT_SALT,
            max_age=settings.ELEVENLABS_TOOL_CONTEXT_TTL_SECONDS,
        )
        if not isinstance(payload, dict) or payload.get('version') != 1:
            raise signing.BadSignature('Unsupported call context token.')
        return payload


class ElevenLabsToolAuthentication(BaseAuthentication):
    """Authenticate one active call using static and signed credentials."""

    def authenticate(
        self,
        request: Request,
    ) -> tuple[ElevenLabsToolPrincipal, Call]:
        configured_secret = settings.ELEVENLABS_TOOL_AUTH_SECRET
        supplied_secret = request.headers.get(TOOL_SECRET_HEADER, '')
        context_token = request.headers.get(CALL_CONTEXT_HEADER, '')

        if (
            not configured_secret
            or not supplied_secret
            or not constant_time_compare(supplied_secret, configured_secret)
            or not context_token
        ):
            raise AuthenticationFailed('Invalid tool credentials.')

        try:
            payload = CallContextTokenService.load(context_token)
            public_id = uuid.UUID(str(payload['call_public_id']))
            initiation_key = uuid.UUID(str(payload['initiation_key']))
            company_id = int(payload['company_id'])
        except (
            KeyError,
            TypeError,
            ValueError,
            signing.BadSignature,
            signing.SignatureExpired,
        ) as exc:
            raise AuthenticationFailed('Invalid tool credentials.') from exc

        call = (
            Call.objects.select_related(
                'company',
                'lead',
                'lead__created_by',
                'lead__assigned_to',
                'context_user',
            )
            .prefetch_related('lead__projects')
            .filter(
                public_id=public_id,
                initiation_key=initiation_key,
                company_id=company_id,
            )
            .first()
        )
        if call is None or call.status not in {
            Call.Status.RINGING,
            Call.Status.IN_PROGRESS,
        }:
            logger.warning(
                'Rejected ElevenLabs tool context call_id=%s',
                public_id,
            )
            raise AuthenticationFailed('Invalid tool credentials.')

        principal = ElevenLabsToolPrincipal(call_public_id=call.public_id)
        return principal, call

    def authenticate_header(self, request: Request) -> str:
        return 'ElevenLabs-Tool'
