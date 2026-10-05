import logging
import stripe
from django.conf import settings
from django.db import IntegrityError, transaction
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from users.models import Company, User

from . import services, stripe_client, webhooks
from .guards import billing_exempt
from .models import ProcessedStripeEvent
from .serializers import (
    CompanyNameTaken,
    EmailAlreadyRegistered,
    RegisterCompanySerializer,
    issue_tokens,
)
from .throttles import BillingCheckoutThrottle, BillingPortalThrottle, RegisterCompanyThrottle

logger = logging.getLogger(__name__)

_TAGS = ['Billing']


def _success_url(session_id_placeholder=True):
    base = settings.FRONTEND_URL.rstrip('/')
    return f'{base}/billing/success?session_id={{CHECKOUT_SESSION_ID}}'


def _cancel_url():
    base = settings.FRONTEND_URL.rstrip('/')
    return f'{base}/billing/cancel'


@billing_exempt
class RegisterCompanyView(APIView):
    """Public, rate-limited self-serve company signup."""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = [RegisterCompanyThrottle]

    def post(self, request):
        serializer = RegisterCompanySerializer(data=request.data)
        try:
            serializer.is_valid(raise_exception=True)
        except EmailAlreadyRegistered:
            return Response(
                {'detail': 'An account with this email already exists.', 'code': 'EMAIL_TAKEN'},
                status=status.HTTP_409_CONFLICT,
            )
        except CompanyNameTaken:
            return Response(
                {'detail': 'A company with this name already exists.', 'code': 'COMPANY_NAME_TAKEN'},
                status=status.HTTP_409_CONFLICT,
            )

        try:
            company, admin_user = serializer.save()
        except IntegrityError:
            # Two concurrent signups raced past the uniqueness pre-check.
            return Response(
                {
                    'detail': 'A company or account with those details already exists.',
                    'code': 'DUPLICATE_SIGNUP',
                },
                status=status.HTTP_409_CONFLICT,
            )

        payload = {
            'company_id': company.id,
            'status': company.status,
            'plan': company.plan,
            'billing_interval': company.billing_interval,
        }
        # Auto-login: hand back the same token pair the login endpoint issues so
        # the new admin never has to sign in twice.
        payload.update(issue_tokens(admin_user))

        if company.plan == Company.Plan.PROFESSIONAL:
            try:
                checkout_url = self._start_checkout(company, admin_user)
            except stripe_client.StripeNotConfigured as exc:
                logger.error('Professional signup without Stripe config: %s', exc)
                return Response(
                    {
                        'detail': 'Billing is not available right now. Please try again later.',
                        'code': 'BILLING_UNAVAILABLE',
                    },
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            payload['checkoutUrl'] = checkout_url

        return Response(payload, status=status.HTTP_201_CREATED)

    @staticmethod
    def _start_checkout(company, admin_user):
        price_id = stripe_client.price_id_for(company.billing_interval)
        customer = stripe_client.create_customer(
            email=admin_user.email,
            name=company.name,
            metadata={'company_id': str(company.pk)},
        )
        session = stripe_client.create_checkout_session(
            customer_id=customer.id,
            price_id=price_id,
            client_reference_id=company.pk,
            success_url=_success_url(),
            cancel_url=_cancel_url(),
            metadata={'company_id': str(company.pk)},
        )
        Company.objects.filter(pk=company.pk).update(stripe_customer_id=customer.id)
        company.stripe_customer_id = customer.id
        services.invalidate(company.pk)
        return session.url


@billing_exempt
class BillingCheckoutView(APIView):
    """Create a fresh Checkout Session for a pending or upgrading company."""

    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [BillingCheckoutThrottle]

    def post(self, request):
        company = request.user.company
        if company is None:
            return Response({'detail': 'No company associated.'}, status=status.HTTP_400_BAD_REQUEST)
        if company.plan == Company.Plan.MANUAL:
            return Response(
                {'detail': 'This company is not on a billed plan.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        interval = request.data.get('interval') or company.billing_interval
        if interval not in dict(Company.BillingInterval.choices):
            interval = Company.BillingInterval.MONTHLY
        if interval != company.billing_interval:
            company.billing_interval = interval
            company.save(update_fields=['billing_interval', 'updated_at'])

        try:
            customer_id = company.stripe_customer_id
            if not customer_id:
                customer = stripe_client.create_customer(
                    email=request.user.email,
                    name=company.name,
                    metadata={'company_id': str(company.pk)},
                )
                customer_id = customer.id
                Company.objects.filter(pk=company.pk).update(stripe_customer_id=customer_id)
                company.stripe_customer_id = customer_id
                services.invalidate(company.pk)

            session = stripe_client.create_checkout_session(
                customer_id=customer_id,
                price_id=stripe_client.price_id_for(interval),
                client_reference_id=company.pk,
                success_url=_success_url(),
                cancel_url=_cancel_url(),
                metadata={'company_id': str(company.pk)},
            )
        except stripe_client.StripeNotConfigured as exc:
            logger.error('Checkout requested without Stripe config: %s', exc)
            return Response(
                {'detail': 'Billing is not available right now.', 'code': 'BILLING_UNAVAILABLE'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response({'checkoutUrl': session.url})


@billing_exempt
class BillingPortalView(APIView):
    """Open the Stripe customer portal for the caller's company."""

    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [BillingPortalThrottle]

    def post(self, request):
        company = request.user.company
        if company is None or not company.stripe_customer_id:
            return Response(
                {'detail': 'No billing account for this company.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        base = settings.FRONTEND_URL.rstrip('/')
        try:
            session = stripe_client.create_portal_session(
                company.stripe_customer_id,
                return_url=f'{base}/subscription',
            )
        except stripe_client.StripeNotConfigured as exc:
            logger.error('Portal requested without Stripe config: %s', exc)
            return Response(
                {'detail': 'Billing is not available right now.', 'code': 'BILLING_UNAVAILABLE'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response({'portalUrl': session.url})


@billing_exempt
class BillingStatusView(APIView):
    """Report billing state plus current usage against plan limits."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        company = request.user.company
        if company is None:
            return Response(
                {
                    'status': Company.Status.ACTIVE,
                    'plan': Company.Plan.MANUAL,
                    'allowed': True,
                    'limits': services.limits_for(Company.Plan.MANUAL),
                    'usage': {},
                }
            )

        state = services.get_company_state(company.pk) or {}
        plan = company.plan
        return Response(
            {
                'status': state.get('status', company.status),
                'plan': plan,
                'billing_interval': company.billing_interval or None,
                'allowed': state.get('allowed', True),
                'grace_until': state.get('grace_until'),
                'current_period_end': state.get('current_period_end'),
                'has_payment_method': bool(company.stripe_customer_id),
                'period_start': services.plan_limits.current_period_start().isoformat(),
                'limits': services.limits_for(plan),
                'usage': services.usage_snapshot(company.pk, plan=plan),
            }
        )


@billing_exempt
class VerifyCheckoutSessionView(APIView):
    """Confirm a Checkout Session belongs to the caller's company and was paid.

    This endpoint deliberately activates nothing: the webhook is the only
    source of truth for whether a company is paid.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        session_id = request.query_params.get('session_id')
        if not session_id:
            return Response(
                {'detail': 'session_id is required.'}, status=status.HTTP_400_BAD_REQUEST
            )

        company = request.user.company
        if company is None:
            return Response({'detail': 'No company associated.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            session = stripe_client.retrieve_checkout_session(session_id)
        except stripe_client.StripeNotConfigured as exc:
            logger.error('Session verification without Stripe config: %s', exc)
            return Response(
                {'detail': 'Billing is not available right now.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except stripe.StripeError:
            return Response(
                {'detail': 'Could not verify the payment session.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        session_obj = session if isinstance(session, dict) else session.to_dict()
        customer_id = session_obj.get('customer')
        customer_id = customer_id.get('id') if isinstance(customer_id, dict) else customer_id
        reference = session_obj.get('client_reference_id')

        belongs = bool(
            (customer_id and customer_id == company.stripe_customer_id)
            or (reference and str(reference) == str(company.pk))
        )
        paid = session_obj.get('payment_status') == 'paid'

        return Response(
            {
                'sessionId': session_id,
                'paid': paid,
                'belongsToCompany': belongs,
                'status': services.get_company_state(company.pk) or {},
            }
        )


@billing_exempt
class StripeWebhookView(APIView):
    """Public Stripe webhook receiver.

    The raw body is read before anything else touches the request, because
    signature verification requires the exact bytes Stripe signed.
    """

    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    parser_classes = []
    throttle_classes = []

    def post(self, request):
        payload = request.body
        signature = request.headers.get('Stripe-Signature')

        secret = getattr(settings, 'STRIPE_WEBHOOK_SECRET', '')
        if not secret:
            logger.error('Stripe webhook received but STRIPE_WEBHOOK_SECRET is unset')
            return Response({'detail': 'Webhook not configured.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            event = stripe.Webhook.construct_event(payload, signature, secret)
        except (ValueError, stripe.SignatureVerificationError):
            logger.warning('Rejected Stripe webhook with an invalid signature')
            return Response(
                {'detail': 'Invalid signature.'}, status=status.HTTP_400_BAD_REQUEST
            )

        event_id = event.get('id')
        event_type = event.get('type', '')

        # Idempotency: claim the event id before handling it, so a redelivery
        # short-circuits instead of re-applying the same state transition.
        try:
            with transaction.atomic():
                ProcessedStripeEvent.objects.create(event_id=event_id, event_type=event_type)
        except IntegrityError:
            return Response({'received': True, 'duplicate': True})

        try:
            webhooks.dispatch(event)
        except Exception:
            logger.exception('Failed to handle Stripe event %s (%s)', event_id, event_type)
            # Release the claim so Stripe's retry can reprocess it.
            ProcessedStripeEvent.objects.filter(event_id=event_id).delete()
            return Response(
                {'detail': 'Webhook handling failed.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response({'received': True})