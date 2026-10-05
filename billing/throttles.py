"""Rate limits for public and self-serve billing endpoints.

DRF's `SimpleRateThrottle` uses the class-level `rate` when it is set, so these
work without adding entries to `DEFAULT_THROTTLE_RATES`.
"""

from django.conf import settings
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle


class RegisterCompanyThrottle(AnonRateThrottle):
    """Throttle self-serve signups per client IP."""

    rate = getattr(settings, 'REGISTER_COMPANY_THROTTLE_RATE', '5/hour')


class BillingCheckoutThrottle(UserRateThrottle):
    """Throttle Checkout Session creation per signed-in user."""

    rate = getattr(settings, 'BILLING_CHECKOUT_THROTTLE_RATE', '20/hour')


class BillingPortalThrottle(UserRateThrottle):
    """Throttle customer-portal session creation per signed-in user."""

    rate = getattr(settings, 'BILLING_PORTAL_THROTTLE_RATE', '20/hour')