"""Custom DRF exception handling for billing errors.

DRF's default handler renders only `{"detail": ...}`, so the machine-readable
fields the frontend needs (`code`, `kind`, `limit`, `current`) would be lost.
Billing errors are the ones the client must branch on programmatically, so they
get an explicit body shape here:

    402 {"code": "PAYMENT_REQUIRED", "detail": ..., "status": ..., "allowed": false}
    403 {"code": "PLAN_LIMIT_REACHED", "detail": ..., "kind": ..., "limit": ..., "current": ...}
"""

from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from .exceptions import PaymentRequired, PlanLimitReached

#: Counter kinds are exposed to clients in camelCase, matching the frontend's
#: naming (`UsageCounter.Kind` is snake_case for database friendliness).
_PUBLIC_KIND_NAMES = {
    'users': 'users',
    'team_managers': 'teamManagers',
    'ai_proposals': 'aiProposals',
    'ai_credits': 'aiCredits',
}


def _public_kind(kind):
    return _PUBLIC_KIND_NAMES.get(kind, kind)


def billing_exception_handler(exc, context):
    if isinstance(exc, PlanLimitReached):
        return Response(
            {
                'code': PlanLimitReached.default_code,
                'detail': str(exc.detail),
                'kind': _public_kind(exc.kind),
                'limit': exc.limit,
                'current': exc.current,
            },
            status=exc.status_code,
        )

    if isinstance(exc, PaymentRequired):
        return Response(
            {
                'code': PaymentRequired.default_code,
                'detail': str(exc.detail),
                'status': exc.company_status,
                'allowed': False,
            },
            status=exc.status_code,
        )

    return drf_exception_handler(exc, context)
