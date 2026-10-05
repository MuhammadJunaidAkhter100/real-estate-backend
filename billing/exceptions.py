"""Billing-related API errors.

`PaymentRequired` maps to HTTP 402 and `PlanLimitReached` to HTTP 403. Both
carry a machine-readable `code` so the frontend can react without string
matching on messages.
"""

from rest_framework import status
from rest_framework.exceptions import APIException


class PaymentRequired(APIException):
    """Raised when a company may not use the API until its billing is settled."""

    status_code = status.HTTP_402_PAYMENT_REQUIRED
    default_code = 'PAYMENT_REQUIRED'
    default_detail = 'Your subscription requires payment. Please complete billing to continue.'

    def __init__(self, detail=None, code=None, company_status=None):
        super().__init__(detail or self.default_detail, code or self.default_code)
        self.company_status = company_status


class PlanLimitReached(APIException):
    """Raised when an action would push a company past its plan limit."""

    status_code = status.HTTP_403_FORBIDDEN
    default_code = 'PLAN_LIMIT_REACHED'

    def __init__(self, kind, limit, current, detail=None):
        super().__init__(detail or self._detail(kind, limit), self.default_code)
        self.kind = kind
        self.limit = limit
        self.current = current

    @staticmethod
    def _detail(kind, limit):
        labels = {
            'users': 'users',
            'team_managers': 'team managers',
            'ai_proposals': 'AI proposals',
            'ai_credits': 'AI credits',
        }
        label = labels.get(kind, kind.replace('_', ' '))
        return (
            f"Your plan's limit of {limit} {label} has been reached. "
            "Upgrade your plan to continue."
        )