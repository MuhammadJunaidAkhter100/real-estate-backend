from rest_framework import status
from rest_framework.exceptions import APIException


class PlanLimitReached(APIException):
    """Raised when a company reaches its plan cap for users, team managers, or credits."""

    status_code = status.HTTP_403_FORBIDDEN
    default_code = 'PLAN_LIMIT_REACHED'
    default_detail = 'You have reached the limit for your subscription plan.'

    def __init__(self, kind=None, limit=None, current=None, detail=None, code=None):
        self.kind = kind
        self.limit = limit
        self.current = current

        if detail is None and kind is not None:
            human_kind = str(kind).replace('_', ' ').capitalize()
            detail = f'{human_kind} limit reached ({current}/{limit}) for your current plan.'

        super().__init__(detail=detail, code=code or self.default_code)


class CompanyNotActive(APIException):
    """Raised when an operation is blocked because the company's subscription is not active."""

    status_code = status.HTTP_402_PAYMENT_REQUIRED
    default_code = 'SUBSCRIPTION_REQUIRED'
    default_detail = 'A valid and active subscription is required to perform this action.'

    def __init__(self, reason=None, detail=None, code=None):
        self.reason = reason
        super().__init__(detail=detail or self.default_detail, code=code or self.default_code)


class StripeNotConfigured(APIException):
    """Raised when a billing action is attempted without Stripe credentials or price IDs."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_code = 'STRIPE_NOT_CONFIGURED'
    default_detail = 'Payment service is currently unavailable. Please try again later.'
