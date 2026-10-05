"""Subscription enforcement for HTTP and WebSocket traffic.

Why middleware *and* a permission class: DRF has no `DEFAULT_GUARDS` setting,
and `REST_FRAMEWORK['DEFAULT_PERMISSION_CLASSES']` is silently bypassed by any
view that declares its own `permission_classes` - which many in this project do.
A middleware runs before the view, so blocking here is the only way to get true
global coverage of existing routes without touching every view. The permission
class in `billing.guards` stays as a second layer for views that rely on the
DRF defaults.

Billing status is always read from the database (through a short-TTL shared
cache), never from a JWT claim, request body or URL parameter.
"""

import logging

from django.conf import settings
from django.http import JsonResponse
from django.urls import Resolver404, resolve
from django.utils.module_loading import import_string
from rest_framework.exceptions import APIException
from rest_framework.request import Request
from rest_framework.views import APIView

from channels.db import database_sync_to_async
from channels.middleware import BaseMiddleware

from users.models import Company, User

from . import services
from .guards import _is_exempt

logger = logging.getLogger(__name__)

# WebSocket close code for "subscription not in good standing".
PAYMENT_REQUIRED_CLOSE_CODE = 4402


def _is_drf_view(request):
    """True when the resolved view for this path is a DRF view.

    `request.resolver_match` is not populated this early in the request cycle,
    so the path is resolved here instead.
    """
    try:
        match = getattr(request, 'resolver_match', None) or resolve(request.path_info)
    except Resolver404:
        return False
    view = match.func
    cls = getattr(view, 'cls', None) or getattr(view, 'view_class', None)
    return isinstance(cls, type) and issubclass(cls, APIView)


def _default_authenticators():
    """Instantiate the project's default authenticators (DRF stores the paths)."""
    return [import_string(path)() for path in
            settings.REST_FRAMEWORK.get('DEFAULT_AUTHENTICATION_CLASSES', [])]


def _resolve_user(request):
    """Authenticate the request through DRF and return its user.

    Reuses the project's configured authenticators so the guard agrees with the
    view about who the caller is.
    """
    drf_request = Request(request, authenticators=_default_authenticators())
    try:
        return drf_request.user
    except APIException:
        # Malformed/expired token: let the view return its usual 401.
        return None


class SubscriptionGuardMiddleware:
    """Return 402 before any view runs for a company that cannot pay."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.process_request(request)
        if response is not None:
            return response
        return self.get_response(request)

    def process_request(self, request):
        if not _is_drf_view(request):
            return None
        if _is_exempt(None, request):
            return None

        user = _resolve_user(request)
        if user is None or not user.is_authenticated:
            return None
        if user.role == User.Role.SUPERADMIN:
            return None

        company = getattr(user, 'company', None)
        if company is None or company.plan == Company.Plan.MANUAL:
            return None

        state = services.get_company_state(company.pk)
        if state is None or state.get('allowed'):
            return None

        return JsonResponse(
            {
                'code': 'PAYMENT_REQUIRED',
                'detail': services.PAYMENT_REQUIRED_MESSAGE,
                'status': state.get('status'),
                'grace_until': state.get('grace_until'),
                'allowed': False,
            },
            status=402,
        )


@database_sync_to_async
def _company_is_allowed(user):
    if user is None or not user.is_authenticated:
        return True
    if user.role == User.Role.SUPERADMIN:
        return True

    company = getattr(user, 'company', None)
    if company is None:
        return True
    if company.plan == Company.Plan.MANUAL:
        return True

    state = services.get_company_state(company.pk)
    if state is None:
        return True
    return bool(state.get('allowed'))


class SubscriptionWebSocketMiddleware(BaseMiddleware):
    """Close WebSocket connections belonging to companies that cannot pay."""

    async def __call__(self, scope, receive, send):
        user = scope.get('user')
        allowed = await _company_is_allowed(user)
        if not allowed:
            logger.info(
                'Closing websocket for user=%s: subscription not in good standing',
                getattr(user, 'pk', None),
            )
            await send({
                'type': 'websocket.close',
                'code': PAYMENT_REQUIRED_CLOSE_CODE,
            })
            return
        return await super().__call__(scope, receive, send)


def JWTSubscriptionStack(inner):
    """JWT auth followed by subscription enforcement.

    Ordering matters: the user must be resolved from the token before the
    company can be looked up.
    """
    from notifications.middleware import JWTAuthMiddleware

    return SubscriptionWebSocketMiddleware(JWTAuthMiddleware(inner))