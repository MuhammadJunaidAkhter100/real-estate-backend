"""Django middleware for global subscription status enforcement."""

import json
from django.http import JsonResponse
from payment.cache import development_plan_bypass_is_active
from payment.guards import is_exempt
from payment.services import get_company_state


class SubscriptionGuardMiddleware:
    """Blocks requests from companies whose subscription is expired or unpaid."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if is_exempt(request):
            return self.get_response(request)

        user = getattr(request, 'user', None)
        if user and user.is_authenticated:
            # Superadmin or exempt roles pass through
            if getattr(user, 'is_superuser', False) or getattr(user, 'role', '') in ('superadmin', 'axiyon_admin'):
                return self.get_response(request)

            company_id = getattr(user, 'company_id', None)
            if company_id:
                if development_plan_bypass_is_active(company_id):
                    return self.get_response(request)
                state = get_company_state(company_id)
                if state and not state.get('allowed', True):
                    return JsonResponse(
                        {
                            'detail': f"Subscription is {state.get('status')}. Payment required.",
                            'code': 'SUBSCRIPTION_REQUIRED',
                            'status': state.get('status'),
                        },
                        status=402,
                    )

        return self.get_response(request)
