"""Custom DRF exception handling for payment and plan limit errors."""

from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from payment.exceptions import CompanyNotActive, PlanLimitReached

_PUBLIC_KIND_NAMES = {
    'users': 'users',
    'agents': 'agents',
    'team_managers': 'teamManagers',
    'ai_proposals': 'aiProposals',
    'ai_credits': 'aiCredits',
}


def _public_kind(kind):
    return _PUBLIC_KIND_NAMES.get(kind, str(kind))


def payment_exception_handler(exc, context):
    """Format payment and plan limit errors with machine-readable fields."""
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

    if isinstance(exc, CompanyNotActive):
        return Response(
            {
                'code': CompanyNotActive.default_code,
                'detail': str(exc.detail),
                'status': exc.reason,
                'allowed': False,
            },
            status=exc.status_code,
        )

    return drf_exception_handler(exc, context)


# Backward compatibility alias
billing_exception_handler = payment_exception_handler
