import logging
import stripe
from django.conf import settings
from django.db import IntegrityError, transaction
from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from payment import plan_limits, services, stripe_client, webhooks
from payment.guards import payment_exempt
from payment.models import ProcessedStripeEvent
from payment.serializers import (
    CheckoutRequestSerializer,
    CompanyNameTaken,
    EmailAlreadyRegistered,
    RegisterCompanySerializer,
    VerifySessionSerializer,
    issue_tokens,
)
from users.models import Company

logger = logging.getLogger(__name__)

SWAGGER_TAG = ['Payments & Subscriptions']


def _frontend_url(path=''):
    base = getattr(settings, 'FRONTEND_URL', 'http://localhost:3000').rstrip('/')
    return f"{base}{path}"


@payment_exempt
class PlansListView(APIView):
    """List available subscription plans, features, and pricing for monthly/yearly billing."""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    @swagger_auto_schema(
        tags=SWAGGER_TAG,
        operation_summary="List Subscription Plans",
        operation_description="Returns all available plans (Free, Pro, Plus, Top Tier) with features, monthly and yearly pricing.",
    )
    def get(self, request):
        company_plan = None
        user = getattr(request, 'user', None)
        if user and user.is_authenticated and getattr(user, 'company', None):
            company_plan = plan_limits.normalize_plan(user.company.plan)

        plans_data = []
        for plan_key in [
            plan_limits.PLAN_FREE,
            plan_limits.PLAN_PRO,
            plan_limits.PLAN_PLUS,
            plan_limits.PLAN_TOP_TIER,
        ]:
            info = plan_limits.PLAN_LIMITS[plan_key]
            plans_data.append({
                'id': plan_key,
                'name': info.get('name'),
                'badge': info.get('badge'),
                'monthlyPrice': info.get('monthly_price'),
                'yearlyPrice': info.get('yearly_price'),
                'aiCredits': info.get('ai_credits'),
                'agents': info.get('agents'),
                'teamManagers': info.get('team_managers'),
                'features': info.get('features', {}),
                'isActivePlan': (company_plan == plan_key),
            })

        return Response({
            'plans': plans_data,
            'currentPlan': company_plan or 'free',
        })


@payment_exempt
class CheckoutSessionView(APIView):
    """Create a Stripe Checkout Session for a chosen plan and billing interval."""

    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        tags=SWAGGER_TAG,
        operation_summary="Create Stripe Checkout Session",
        operation_description=(
            "Initiate a Stripe Checkout session for a chosen plan ('pro', 'plus', or 'top_tier') "
            "and billing interval ('monthly' or 'yearly'). Returns a checkoutUrl to redirect the user to."
        ),
        request_body=CheckoutRequestSerializer,
        responses={
            200: openapi.Response(
                description="Checkout session created successfully.",
                schema=openapi.Schema(
                    type=openapi.TYPE_OBJECT,
                    properties={
                        'checkoutUrl': openapi.Schema(type=openapi.TYPE_STRING, description="Stripe checkout URL"),
                        'sessionId': openapi.Schema(type=openapi.TYPE_STRING, description="Stripe session ID"),
                    },
                ),
            ),
            400: "Invalid plan or missing company",
            503: "Payment gateway error",
        },
    )
    def post(self, request):
        company = getattr(request.user, 'company', None)
        if company is None:
            return Response(
                {'detail': 'An associated company is required to start checkout.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = CheckoutRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        plan = plan_limits.normalize_plan(data['plan'])
        interval = plan_limits.normalize_interval(data.get('interval', 'monthly'))

        # Ensure company has a Stripe Customer ID
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

            success_url = data.get('success_url') or _frontend_url('/subscription?success=true&session_id={CHECKOUT_SESSION_ID}')
            cancel_url = data.get('cancel_url') or _frontend_url('/subscription?canceled=true')

            # Dynamic Stripe checkout session - zero config needed in .env!
            session = stripe_client.create_checkout_session(
                customer_id=customer_id,
                plan=plan,
                interval=interval,
                client_reference_id=company.pk,
                success_url=success_url,
                cancel_url=cancel_url,
                metadata={'company_id': str(company.pk), 'plan': plan, 'interval': interval},
            )
        except Exception as exc:
            logger.exception('Stripe checkout session creation failed: %s', exc)
            return Response(
                {'detail': 'Failed to initiate payment session. Please check Stripe configuration.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response({
            'checkoutUrl': session.url,
            'sessionId': session.id,
        })


@payment_exempt
class BillingPortalView(APIView):
    """Generate a link to the Stripe Billing Customer Portal."""

    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        tags=SWAGGER_TAG,
        operation_summary="Open Customer Billing Portal",
        operation_description="Returns a portalUrl to the Stripe Customer Portal for managing saved cards, viewing invoices, or updating subscriptions.",
    )
    def post(self, request):
        company = getattr(request.user, 'company', None)
        if company is None or not company.stripe_customer_id:
            return Response(
                {'detail': 'No Stripe billing account found for this company.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return_url = _frontend_url('/subscription')
        try:
            session = stripe_client.create_portal_session(company.stripe_customer_id, return_url=return_url)
        except Exception as exc:
            logger.exception('Failed to create customer portal session: %s', exc)
            return Response(
                {'detail': 'Could not open billing portal.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response({'portalUrl': session.url})


@payment_exempt
class BillingStatusView(APIView):
    """Retrieve current subscription status, usage counters, and quota limits."""

    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        tags=SWAGGER_TAG,
        operation_summary="Get Subscription & Usage Status",
        operation_description="Returns caller company's current active plan, interval, AI credit balance, user/agent limits, and live usage counters.",
    )
    def get(self, request):
        company = getattr(request.user, 'company', None)
        if company is None:
            return Response({
                'status': 'active',
                'plan': 'manual',
                'allowed': True,
                'limits': services.limits_for('manual'),
                'usage': {},
            })

        state = services.get_company_state(company.pk) or {}
        plan = company.plan or 'free'

        return Response({
            'status': state.get('status', company.status),
            'plan': plan_limits.normalize_plan(plan),
            'billingInterval': company.billing_interval or 'monthly',
            'allowed': state.get('allowed', True),
            'graceUntil': state.get('grace_until'),
            'currentPeriodEnd': state.get('current_period_end'),
            'hasPaymentMethod': bool(company.stripe_customer_id),
            'periodStart': plan_limits.current_period_start().isoformat(),
            'limits': services.limits_for(plan),
            'usage': services.usage_snapshot(company.pk, plan=plan),
        })


@payment_exempt
class VerifyCheckoutSessionView(APIView):
    """Verify if a checkout session was completed successfully."""

    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        tags=SWAGGER_TAG,
        operation_summary="Verify Checkout Session",
        operation_description="Confirms whether a Stripe Checkout Session completed successfully and belongs to caller's company.",
        query_serializer=VerifySessionSerializer,
    )
    def get(self, request):
        session_id = request.query_params.get('session_id')
        if not session_id:
            return Response({'detail': 'session_id query parameter is required.'}, status=status.HTTP_400_BAD_REQUEST)

        company = getattr(request.user, 'company', None)
        if company is None:
            return Response({'detail': 'No company associated.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            session = stripe_client.retrieve_checkout_session(session_id)
        except Exception as exc:
            logger.error('Failed to retrieve checkout session: %s', exc)
            return Response({'detail': 'Could not verify checkout session.'}, status=status.HTTP_400_BAD_REQUEST)

        session_obj = session if isinstance(session, dict) else session.to_dict()
        cust_id = session_obj.get('customer')
        cust_id = cust_id.get('id') if isinstance(cust_id, dict) else cust_id
        ref_id = session_obj.get('client_reference_id')

        belongs = bool(
            (cust_id and cust_id == company.stripe_customer_id)
            or (ref_id and str(ref_id) == str(company.pk))
        )
        paid = (session_obj.get('payment_status') == 'paid')

        return Response({
            'sessionId': session_id,
            'paid': paid,
            'belongsToCompany': belongs,
            'status': services.get_company_state(company.pk) or {},
        })


@payment_exempt
class StripeWebhookView(APIView):
    """Public receiver for Stripe webhook notifications."""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    parser_classes = []
    throttle_classes = []

    def post(self, request):
        payload = request.body
        signature = request.headers.get('Stripe-Signature')

        secret = getattr(settings, 'STRIPE_WEBHOOK_SECRET', '')
        if not secret:
            logger.error('Stripe webhook received but STRIPE_WEBHOOK_SECRET is not configured.')
            return Response({'detail': 'Webhook secret is not configured.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            event = stripe.Webhook.construct_event(payload, signature, secret)
        except (ValueError, stripe.SignatureVerificationError) as exc:
            logger.warning('Invalid Stripe webhook signature: %s', exc)
            return Response({'detail': 'Invalid signature.'}, status=status.HTTP_400_BAD_REQUEST)

        event_id = event.get('id')
        event_type = event.get('type', '')

        # Webhook idempotency ledger
        try:
            with transaction.atomic():
                ProcessedStripeEvent.objects.create(event_id=event_id, event_type=event_type)
        except IntegrityError:
            # Already processed
            return Response({'received': True, 'duplicate': True})

        try:
            webhooks.dispatch(event)
        except Exception:
            logger.exception('Failed to handle Stripe webhook event %s (%s)', event_id, event_type)
            # Release idempotency claim so Stripe retry can be processed
            ProcessedStripeEvent.objects.filter(event_id=event_id).delete()
            return Response({'detail': 'Webhook processing error.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response({'received': True})


@payment_exempt
class RegisterCompanyView(APIView):
    """Public self-serve company signup endpoint."""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    @swagger_auto_schema(
        tags=['Authentication'],
        operation_summary="Register Company",
        operation_description=(
            "Public endpoint to sign up a new company with an initial company admin.\n\n"
            "Features:\n"
            "- Creates Company and Company Admin user account\n"
            "- Sends branded HTML welcome email with workspace access details to the company admin\n"
            "- Sends branded HTML alert email to the super admin\n"
            "- Automatically launches background website scraping if a website is provided\n"
            "- Issues JWT tokens (access + refresh) and optionally generates a dynamic Stripe checkout URL for paid plans."
        ),
        request_body=RegisterCompanySerializer,
        responses={
            201: "Created: returns auth tokens, user info, and optional checkoutUrl",
            400: "Validation error",
            409: "Email already registered or Company name already taken",
        },
    )
    def post(self, request):
        serializer = RegisterCompanySerializer(data=request.data)
        try:
            serializer.is_valid(raise_exception=True)
            company, admin_user = serializer.save()
        except EmailAlreadyRegistered:
            return Response(
                {'detail': 'Email already registered.'},
                status=status.HTTP_409_CONFLICT,
            )
        except CompanyNameTaken:
            return Response(
                {'detail': 'Company name already taken.'},
                status=status.HTTP_409_CONFLICT,
            )

        tokens = issue_tokens(admin_user)
        checkout_url = None

        plan = plan_limits.normalize_plan(company.plan)
        if plan in (plan_limits.PLAN_PRO, plan_limits.PLAN_PLUS, plan_limits.PLAN_TOP_TIER):
            try:
                session = stripe_client.create_checkout_session(
                    company=company,
                    plan=plan,
                    interval=company.billing_interval or 'monthly',
                    success_url=_frontend_url('/subscription?session_id={CHECKOUT_SESSION_ID}&success=true'),
                    cancel_url=_frontend_url('/subscription?canceled=true'),
                )
                checkout_url = session.get('url')
            except Exception as exc:
                logger.warning("Could not create Stripe checkout session on signup: %s", exc)

        response_data = {
            **tokens,
            'checkoutUrl': checkout_url,
        }
        return Response(response_data, status=status.HTTP_201_CREATED)

