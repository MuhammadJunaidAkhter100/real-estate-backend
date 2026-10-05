from __future__ import annotations

import logging

from django.conf import settings
from django.core.cache import caches
from redis.exceptions import RedisError
from rest_framework.exceptions import APIException
from rest_framework.request import Request
from rest_framework.throttling import SimpleRateThrottle

from calling_agent.models import Call

logger = logging.getLogger(__name__)


class ToolThrottleUnavailable(APIException):
    status_code = 503
    default_detail = 'Tool service is temporarily unavailable.'
    default_code = 'tool_throttle_unavailable'


class CallToolThrottle(SimpleRateThrottle):
    scope = 'calling_agent_tool'

    def get_rate(self) -> str:
        return settings.ELEVENLABS_TOOL_RATE

    def get_cache_key(self, request: Request, view: object) -> str | None:
        call = request.auth
        if not isinstance(call, Call):
            return None
        return self.cache_format % {
            'scope': self.scope,
            'ident': str(call.public_id),
        }

    def allow_request(self, request: Request, view: object) -> bool:
        self.cache = caches['calling_agent_tools']
        try:
            return super().allow_request(request, view)
        except RedisError as exc:
            logger.exception('Calling tool throttle cache is unavailable')
            raise ToolThrottleUnavailable() from exc


class KnowledgeToolThrottle(CallToolThrottle):
    scope = 'calling_agent_knowledge_tool'

    def get_rate(self) -> str:
        return settings.ELEVENLABS_KNOWLEDGE_TOOL_RATE
