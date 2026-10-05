"""Global subscription enforcement.

Enforcement runs in two layers:

1. `SubscriptionGuardMiddleware` (see `billing.middleware`) blocks the request
   before the view executes. This is the layer that gives real global coverage,
   because `REST_FRAMEWORK['DEFAULT_PERMISSION_CLASSES']` is silently bypassed
   by any view declaring its own `permission_classes`.
2. `SubscriptionGuard`, installed right after `IsAuthenticated` in
   `REST_FRAMEWORK['DEFAULT_PERMISSION_CLASSES']`, is a second line of defence
   for views that rely on the DRF defaults.

Billing status is always read from the database (via a short-TTL shared cache),
never from a JWT claim, request body or URL parameter.
"""

import logging

from django.conf import settings
from rest_framework.permissions import BasePermission

from users.models import Company, User

from . import services

logger = logging.getLogger(__name__)

# Paths that must stay reachable while a company cannot pay, so it can pay.
# Matched against the path *inside* the API mount.
EXEMPT_PATH_PREFIXES = (
    'auth/',
    'billing/',
    'webhooks/stripe',
    'me',
    'admin/',
    'swagger',
    'schema',
    'docs/',
)

# Health checks and similar infrastructure probes.
EXEMPT_PATH_NAMES = frozenset({'health', 'healthz', 'ping'})

_EXEMPT_ATTR = 'billing_exempt'


def billing_exempt(view_or_func):
    """Mark a view as exempt from subscription enforcement."""
    setattr(view_or_func, _EXEMPT_ATTR, True)
    return view_or_func


def _resolved_match(request):
    """Return the URL match, resolving the path when it is not already attached.

    Middleware runs before DRF, and `request.resolver_match` is not always set
    at that point, so fall back to resolving the path ourselves.
    """
    match = getattr(request, 'resolver_match', None)
    if match is not None:
        return match
    try:
        from django.urls import Resolver404, resolve

        return resolve(request.path_info)
    except Exception as exc:  # Resolver404, or a path_info this early is missing
        logger.debug('Could not resolve %s for billing guard: %s', request.path_info, exc)
        return None


def _is_exempt(view, request):
    if view is not None and getattr(view, _EXEMPT_ATTR, False):
        return True
    if view is not None and getattr(type(view), _EXEMPT_ATTR, False):
        return True
    if request is None:
        return False

    match = _resolved_match(request)
    if match is not None and match.url_name in EXEMPT_PATH_NAMES:
        return True

    path = getattr(request, 'path_info', '') or ''
    # Strip the API mount prefix so 'billing/status' matches '/api/billing/status'.
    for prefix in getattr(settings, 'API_URL_PREFIXES', ('/api/',)):
        if path.startswith(prefix):
            path = path[len(prefix):]
            break
    path = path.lstrip('/')
    return any(path == p.rstrip('/') or path.startswith(p) for p in EXEMPT_PATH_PREFIXES)


class SubscriptionGuard(BasePermission):
    """Block the API for companies whose billing is not in good standing."""

    message = 'Your subscription requires payment.'

    def has_permission(self, request, view):
        if _is_exempt(view, request):
            return True

        user = getattr(request, 'user', None)
        if user is None or not user.is_authenticated:
            # Unauthenticated requests are handled by IsAuthenticated.
            return True

        # Super admins are not tenants of any paid plan.
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
        if state.get('allowed'):
            return True

        from .exceptions import PaymentRequired

        raise PaymentRequired(company_status=state.get('status'))