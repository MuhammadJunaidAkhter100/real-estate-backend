"""Rate limits for the public website chatbot (visitors who are not logged in).

Rates live on the classes, so no DEFAULT_THROTTLE_RATES entry is needed. Each
class has its own scope, so it never shares a counter with another throttle.
"""

import ipaddress

from rest_framework.throttling import AnonRateThrottle, SimpleRateThrottle


def _is_loopback(ident) -> bool:
    try:
        return ipaddress.ip_address(ident).is_loopback
    except ValueError:
        return False


class _PublicChatThrottle(AnonRateThrottle):
    """Limit per visitor address."""

    def get_ident(self, request):
        # nginx appends the real client address as the last X-Forwarded-For
        # entry; earlier entries come from the client and can be made up.
        forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
        return forwarded.split(',')[-1].strip() or request.META.get('REMOTE_ADDR')

    def allow_request(self, request, view):
        # With no visitor address (the proxy forwarded none) every visitor
        # would share one counter and lock each other out. Leave those to the
        # global limit.
        ident = self.get_ident(request)
        if not ident or _is_loopback(ident):
            return True
        return super().allow_request(request, view)


class PublicChatBurstThrottle(_PublicChatThrottle):
    scope = 'public_chat_burst'
    rate = '10/min'


class PublicChatDailyThrottle(_PublicChatThrottle):
    scope = 'public_chat_daily'
    rate = '150/day'


class PublicChatGlobalThrottle(SimpleRateThrottle):
    """One limit for all visitors together, to cap the OpenAI bill. Checked by
    the view only for messages that will really reach OpenAI, so nobody can
    use it up with rejected requests."""

    scope = 'public_chat_global'
    rate = '3000/day'

    def get_cache_key(self, request, view):
        return self.cache_format % {'scope': self.scope, 'ident': 'all'}
