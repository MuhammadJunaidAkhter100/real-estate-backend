"""Access control guards for active subscriptions."""

import logging
from rest_framework.permissions import BasePermission

from payment.cache import development_plan_bypass_is_active
from payment.exceptions import CompanyNotActive
from payment.services import get_company_state

logger = logging.getLogger(__name__)

EXEMPT_PATHS = (
    '/api/auth/',
    '/api/payment/',
    '/api/billing/',
    '/api/countries/',
    '/api/health/',
    '/swagger/',
    '/redoc/',
)


def payment_exempt(view_cls_or_fn):
    """Mark a DRF view or ViewSet as exempt from subscription checks."""
    view_cls_or_fn._payment_exempt = True
    view_cls_or_fn._billing_exempt = True
    return view_cls_or_fn


# Backward compatibility alias
billing_exempt = payment_exempt


def is_exempt(request, view=None):
    """Check if request or view is exempt from subscription check."""
    if view is not None and (
        getattr(view, '_payment_exempt', False)
        or getattr(view, '_billing_exempt', False)
    ):
        return True

    path = request.path
    for exempt_prefix in EXEMPT_PATHS:
        if path.startswith(exempt_prefix):
            return True

    return False


class SubscriptionGuard(BasePermission):
    """DRF permission: blocks requests if company subscription is suspended/unpaid."""

    message = 'An active subscription is required to perform this action.'

    def has_permission(self, request, view):
        if is_exempt(request, view):
            return True

        user = getattr(request, 'user', None)
        if not user or not user.is_authenticated:
            return True

        # Admins are exempt
        if getattr(user, 'is_superuser', False) or getattr(user, 'role', '') in ('superadmin', 'axiyon_admin'):
            return True

        company_id = getattr(user, 'company_id', None)
        if not company_id:
            return True
        if development_plan_bypass_is_active(company_id):
            return True

        state = get_company_state(company_id)
        if state is None:
            return True

        if not state.get('allowed', True):
            raise CompanyNotActive(
                reason=state.get('status'),
                detail=f"Subscription status is '{state.get('status')}'. Please update your payment plan to continue.",
            )

        return True
