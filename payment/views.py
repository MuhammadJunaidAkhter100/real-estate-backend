from datetime import timedelta
import logging

import stripe
from django.conf import settings
from django.core.paginator import EmptyPage, Paginator
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.utils import timezone
from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema
from rest_framework import mixins, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from payment import plan_limits, services, stripe_client, webhooks
from payment.guards import payment_exempt
from payment.models import ProcessedStripeEvent, ReferralInvitation, SubscriptionPlan
from payment.serializers import (
    CheckoutRequestSerializer,
    CompanyNameTaken,
    EmailAlreadyRegistered,
    PlanChangeRequestSerializer,
    RegisterCompanySerializer,
    ReferralInvitationSerializer,
    SubscriptionPlanSerializer,
    VerifySessionSerializer,
    issue_tokens,
)
from users.models import Company, User

logger = logging.getLogger(__name__)

SWAGGER_TAG = ['Payments & Subscriptions']
ADMIN_SWAGGER_TAG = ['Admin Subscription Plans']


class CanViewOrManageSubscriptionPlans(permissions.BasePermission):
    """
    - Read (GET): Public, so plans can be shown on the public signup pages.
    - Write (POST, PATCH, DELETE): Allowed only for Superadmin and Axiyon Admin.
    """
    message = "Only administrators can create, modify, or delete subscription plans."

    def has_permission(self, request, view):
        if request.method in permissions.SAFE_METHODS:
            return True

        user = getattr(request, 'user', None)
        if not (user and user.is_authenticated):
            return False

        is_admin = bool(
            user.is_superuser
            or user.is_staff
            or getattr(user, 'role', '') in ['superadmin', 'axiyon_admin']
        )
        return is_admin


def _frontend_url(path=''):
    base = getattr(settings, 'FRONTEND_URL', 'http://localhost:3000').rstrip('/')
    return f"{base}{path}"


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
class ChangePlanView(APIView):
    """Upgrade or downgrade the authenticated company billing plan."""

    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        tags=SWAGGER_TAG,
        operation_summary='Change Subscription Plan',
        operation_description=(
            'Change the authenticated company subscription. Paid plan changes apply through Stripe '
            'with proration; downgrading to Free schedules cancellation at the current period end. '
            'Companies without an active Stripe subscription receive a checkout URL.'
        ),
        request_body=PlanChangeRequestSerializer,
        responses={200: 'Plan updated or cancellation scheduled', 400: 'Invalid plan', 403: 'Company admin required'},
    )
    def post(self, request):
        if request.user.role != User.Role.COMPANY_ADMIN or not request.user.company_id:
            return Response(
                {'detail': 'Only a company admin can change the company plan.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        company = request.user.company
        serializer = PlanChangeRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        requested_plan = plan_limits.normalize_plan(serializer.validated_data['plan'])
        interval = plan_limits.normalize_interval(
            serializer.validated_data.get('interval') or company.billing_interval or 'monthly'
        )

        if requested_plan == plan_limits.PLAN_MANUAL:
            return Response(
                {'detail': 'The manual plan cannot be selected through billing.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        is_known_plan = requested_plan in plan_limits.PLAN_LIMITS or SubscriptionPlan.objects.filter(
            slug=requested_plan,
            is_active=True,
        ).exists()
        if not is_known_plan:
            return Response({'plan': ['Select a valid active plan.']}, status=status.HTTP_400_BAD_REQUEST)

        current_plan = plan_limits.normalize_plan(company.plan)
        current_interval = plan_limits.normalize_interval(company.billing_interval or 'monthly')
        if requested_plan == current_plan and interval == current_interval:
            return Response({
                'plan': current_plan,
                'interval': current_interval,
                'detail': 'This company is already on the requested plan.',
            })

        if requested_plan == plan_limits.PLAN_FREE:
            if company.stripe_subscription_id:
                try:
                    stripe_client.cancel_subscription(company.stripe_subscription_id)
                except Exception:
                    logger.exception('Failed to schedule Free plan downgrade for company %s', company.pk)
                    return Response(
                        {'detail': 'Could not schedule the plan downgrade.'},
                        status=status.HTTP_503_SERVICE_UNAVAILABLE,
                    )
                return Response({
                    'plan': current_plan,
                    'downgrade_to': plan_limits.PLAN_FREE,
                    'effective': 'current_period_end',
                    'detail': 'Downgrade to Free is scheduled for the end of the current billing period.',
                })

            company.plan = plan_limits.PLAN_FREE
            company.billing_interval = interval
            company.save(update_fields=['plan', 'billing_interval', 'updated_at'])
            services.invalidate(company.pk)
            return Response({'plan': company.plan, 'interval': company.billing_interval})

        if not SubscriptionPlan.objects.filter(slug=requested_plan, is_active=True).exists() and requested_plan not in (
            plan_limits.PLAN_PRO,
            plan_limits.PLAN_PLUS,
            plan_limits.PLAN_TOP_TIER,
        ):
            return Response({'plan': ['Select an active paid plan.']}, status=status.HTTP_400_BAD_REQUEST)

        try:
            if not company.stripe_subscription_id:
                customer_id = company.stripe_customer_id
                if not customer_id:
                    customer = stripe_client.create_customer(
                        email=request.user.email,
                        name=company.name,
                        metadata={'company_id': str(company.pk)},
                    )
                    customer_id = customer.id
                    company.stripe_customer_id = customer_id
                    company.save(update_fields=['stripe_customer_id', 'updated_at'])
                    services.invalidate(company.pk)

                session = stripe_client.create_checkout_session(
                    customer_id=customer_id,
                    plan=requested_plan,
                    interval=interval,
                    client_reference_id=company.pk,
                    success_url=_frontend_url('/subscription?success=true&session_id={CHECKOUT_SESSION_ID}'),
                    cancel_url=_frontend_url('/subscription?canceled=true'),
                    metadata={'company_id': str(company.pk), 'plan': requested_plan, 'interval': interval},
                )
                session_data = session if isinstance(session, dict) else session.to_dict()
                return Response({
                    'checkoutUrl': session_data.get('url'),
                    'plan': requested_plan,
                    'interval': interval,
                    'checkoutRequired': True,
                })

            subscription = stripe_client.retrieve_subscription(company.stripe_subscription_id)
            subscription_data = (
                subscription.to_dict_recursive()
                if hasattr(subscription, 'to_dict_recursive')
                else subscription if isinstance(subscription, dict) else subscription.to_dict()
            )
            customer_id = subscription_data.get('customer')
            if isinstance(customer_id, dict):
                customer_id = customer_id.get('id')
            if company.stripe_customer_id and customer_id != company.stripe_customer_id:
                return Response(
                    {'detail': 'The Stripe subscription does not belong to this company.'},
                    status=status.HTTP_403_FORBIDDEN,
                )

            items = (subscription_data.get('items') or {}).get('data') or []
            if not items or not items[0].get('id'):
                return Response(
                    {'detail': 'The Stripe subscription has no billable plan item.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            price_id = stripe_client.price_id_for_plan_change(requested_plan, interval)
            updated_subscription = stripe_client.update_subscription_plan(
                company.stripe_subscription_id,
                items[0]['id'],
                price_id,
                plan=requested_plan,
                interval=interval,
                company_id=company.pk,
            )
            updated_data = (
                updated_subscription.to_dict_recursive()
                if hasattr(updated_subscription, 'to_dict_recursive')
                else updated_subscription if isinstance(updated_subscription, dict)
                else updated_subscription.to_dict()
            )
            services.sync_subscription(company, updated_data)
        except Exception as exc:
            logger.exception('Failed to change plan for company %s', company.pk)
            return Response(
                {'detail': 'Could not change the plan. Check the billing account and try again.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response({
            'plan': plan_limits.normalize_plan(company.plan),
            'interval': company.billing_interval,
            'subscriptionId': company.stripe_subscription_id,
            'detail': 'Subscription plan updated.',
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
            'limits': services.limits_for(plan, company_id=company.pk),
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


class IsCompanyAdmin(permissions.BasePermission):
    def has_permission(self, request, view):
        user = getattr(request, 'user', None)
        return bool(
            user
            and user.is_authenticated
            and user.role == User.Role.COMPANY_ADMIN
            and user.company_id
        )


class ReferralInvitationViewSet(
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = ReferralInvitationSerializer
    permission_classes = [permissions.IsAuthenticated, IsCompanyAdmin]

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return ReferralInvitation.objects.none()

        user = getattr(self.request, 'user', None)
        company_id = getattr(user, 'company_id', None)
        if not company_id:
            return ReferralInvitation.objects.none()

        return ReferralInvitation.objects.filter(
            inviter_company_id=company_id
        ).select_related('inviter_user', 'joined_company')

    def list(self, request, *args, **kwargs):
        invitations = self.get_queryset()
        totals = invitations.aggregate(credits_earned=Sum('reward_credits'))
        return Response({
            'summary': {
                'people_invited': invitations.count(),
                'joined': invitations.filter(status=ReferralInvitation.Status.JOINED).count(),
                'pending': invitations.filter(status=ReferralInvitation.Status.PENDING).count(),
                'credits_earned': totals['credits_earned'] or 0,
            },
            'invitations': self.get_serializer(invitations, many=True).data,
        })

    def create(self, request, *args, **kwargs):
        if not request.data.get('invited_email'):
            return Response(
                {'invited_email': ['This field is required for email invitations.']},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return super().create(request, *args, **kwargs)

    @action(detail=False, methods=['post'], url_path='generate-url')
    def generate_url(self, request):
        invitation = ReferralInvitation.objects.create(
            inviter_company=request.user.company,
            inviter_user=request.user,
        )
        serializer = self.get_serializer(invitation)
        return Response(
            {'referral_url': serializer.data['referral_url']},
            status=status.HTTP_201_CREATED,
        )

    def perform_create(self, serializer):
        invitation = serializer.save(
            inviter_company=self.request.user.company,
            inviter_user=self.request.user,
        )
        if invitation.invited_email:
            from django.conf import settings
            from users.tasks import send_referral_invitation_email

            frontend = getattr(settings, 'FRONTEND_URL', 'http://localhost:3000').rstrip('/')
            signup_url = f'{frontend}/signup?referral_code={invitation.referral_code}'
            transaction.on_commit(lambda: send_referral_invitation_email.delay(
                invitation.invited_email,
                self.request.user.full_name,
                invitation.inviter_company.name,
                signup_url,
            ))

    def perform_destroy(self, instance):
        if instance.status != ReferralInvitation.Status.PENDING:
            raise ValidationError('Joined referrals cannot be deleted.')
        instance.delete()


@payment_exempt
class WebsiteScrapeDocxView(APIView):
    """Queue website import into the company's knowledge base and projects."""

    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        tags=['Authentication'],
        operation_summary='Import Website into Knowledge Base and Create Projects',
        operation_description=(
            'Queues the same background website import used during company signup. Creates or resets the '
            'company website knowledge-base document, stores the scraped content as DOCX, and extracts projects.'
        ),
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'website': openapi.Schema(type=openapi.TYPE_STRING, description='Website URL to scrape'),
                'company_id': openapi.Schema(type=openapi.TYPE_INTEGER, description='Optional target company ID (superadmin only)'),
            },
            required=['website'],
        ),
        responses={
            202: 'Website import queued',
            400: 'Invalid website URL',
            401: 'Authentication required',
            403: 'Not allowed to scrape for this company',
            404: 'Company not found',
            503: 'Could not queue website import',
        },
    )
    def post(self, request):
        website = (request.data.get('website') or request.query_params.get('website') or '').strip()
        if not website:
            return Response({'detail': 'Website URL is required.'}, status=status.HTTP_400_BAD_REQUEST)





        from chatbot.website_knowledge import normalize_website_url, prepare_website_document

        website = normalize_website_url(website)
        if not website:
            return Response({'detail': 'A valid website URL is required.'}, status=status.HTTP_400_BAD_REQUEST)

        company = None
        company_id = request.data.get('company_id') or request.query_params.get('company_id')
        user = request.user
        is_superadmin = user.is_superuser or user.role == User.Role.SUPERADMIN
        if company_id and is_superadmin:
            try:
                company = Company.objects.filter(pk=int(company_id)).first()
            except (TypeError, ValueError):
                company = None
            if company is None:
                return Response({'detail': 'Company not found.'}, status=status.HTTP_404_NOT_FOUND)
        else:
            company = getattr(user, 'company', None)
            if company_id and (company is None or str(company.pk) != str(company_id)):
                return Response(
                    {'detail': 'You can only scrape a website for your own company.'},
                    status=status.HTTP_403_FORBIDDEN,
                )

        if company is None:
            return Response(
                {'detail': 'Your user account is not linked to a company.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        company.website = website
        company.website_scrape_status = 'processing'
        company.website_data = {
            'status': 'processing',
            'progress': 5,
            'stage': 'Scraping initiated...',
            'total_projects': 0,
            'projects_created_or_updated': 0,
        }
        company.save(update_fields=['website', 'website_scrape_status', 'website_data'])

        owner = user if user.company_id == company.pk else None
        document = prepare_website_document(company, website, owner=owner)
        if document is None:
            return Response(
                {'detail': 'The company has no user to own its website knowledge-base document.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            from users.tasks import scrape_company_website_task

            task = scrape_company_website_task.delay(company.pk, website)
        except Exception:
            document.status = document.Status.FAILED
            document.error = 'Could not queue website import.'
            document.save(update_fields=['status', 'error', 'updated_at'])
            company.website_scrape_status = 'failed'
            company.website_data.update({'status': 'failed', 'error': document.error})
            company.save(update_fields=['website_scrape_status', 'website_data'])
            logger.exception('Could not queue website scrape for company %s', company.pk)
            return Response(
                {'detail': 'Could not queue website import. Please try again.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response({
            'detail': 'Website import and project extraction queued.',
            'status': 'processing',
            'website': website,
            'company_id': company.pk,
            'document_id': document.pk,
            'document_status': document.status,
            'task_id': task.id,
        }, status=status.HTTP_202_ACCEPTED)


@payment_exempt
class AdminSubscriptionPlanViewSet(viewsets.ModelViewSet):
    """Admin CRUD ViewSet for managing subscription plans.
    
    - List / Retrieve: Superadmin, Axiyon Admin, and Company Admin
    - Create / Patch / Delete: Superadmin and Axiyon Admin only
    """

    http_method_names = ['get', 'post', 'patch', 'delete', 'head', 'options']
    serializer_class = SubscriptionPlanSerializer
    permission_classes = [CanViewOrManageSubscriptionPlans]

    def get_queryset(self):
        user = getattr(self.request, 'user', None)
        is_admin = bool(
            user
            and user.is_authenticated
            and (
                user.is_superuser
                or user.is_staff
                or getattr(user, 'role', '') in ['superadmin', 'axiyon_admin']
            )
        )
        qs = SubscriptionPlan.objects.all().order_by('order', 'id')
        if not is_admin:
            # Company admin only sees active plans
            return qs.filter(is_active=True)
        return qs

    @swagger_auto_schema(
        tags=ADMIN_SWAGGER_TAG,
        operation_summary="List All Subscription Plans (Admin & Company Admin)",
        operation_description="Returns subscription plans. Company admins see active plans; superadmins see all plans.",
    )
    def list(self, request, *args, **kwargs):
        is_active = request.query_params.get('is_active')
        qs = self.get_queryset()
        if is_active is not None:
            active_bool = is_active.lower() in ('true', '1', 'yes')
            qs = qs.filter(is_active=active_bool)
        serializer = self.get_serializer(qs, many=True)
        return Response(serializer.data)

    @swagger_auto_schema(
        tags=ADMIN_SWAGGER_TAG,
        operation_summary="Create Subscription Plan (Admin)",
        operation_description="Creates a new subscription plan with pricing, limits, and software feature toggles.",
    )
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        plan = serializer.save()
        return Response(
            self.get_serializer(plan).data,
            status=status.HTTP_201_CREATED,
        )

    @swagger_auto_schema(
        tags=ADMIN_SWAGGER_TAG,
        operation_summary="Get Subscription Plan Details (Admin)",
    )
    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance)
        return Response(serializer.data)

    @swagger_auto_schema(
        tags=ADMIN_SWAGGER_TAG,
        operation_summary="Partial Update Subscription Plan (Admin)",
    )
    def partial_update(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        plan = serializer.save()
        return Response(self.get_serializer(plan).data)

    @swagger_auto_schema(
        tags=ADMIN_SWAGGER_TAG,
        operation_summary="Delete Subscription Plan (Admin)",
        operation_description="Deletes a subscription plan. If companies are actively assigned to it, it is deactivated (soft-deleted) to prevent data corruption.",
    )
    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        # Check if any company is using this plan
        is_in_use = Company.objects.filter(plan__iexact=instance.slug).exists()
        if is_in_use:
            instance.is_active = False
            instance.save(update_fields=['is_active', 'updated_at'])
            return Response(
                {
                    'detail': f"Plan '{instance.name}' is currently in use by companies. It has been deactivated instead of deleted.",
                    'deactivated': True,
                },
                status=status.HTTP_200_OK,
            )
        instance.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@payment_exempt
class AdminSubscriptionsOverviewView(APIView):
    """Admin dashboard endpoint for listing all organizations/subscriptions with live stats & filters."""

    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        tags=['Admin Subscriptions'],
        operation_summary="Get Subscriptions Overview & Stats (Admin)",
        operation_description=(
            "Returns aggregated subscription statistics (Active, Expiring Soon, In Grace Period) "
            "and a paginated list of all companies/organizations with their plan, billing cycle, "
            "activation date, next expiry, and live status. Supports filtering by tier, status, cycle, and search."
        ),
        manual_parameters=[
            openapi.Parameter('search', openapi.IN_QUERY, type=openapi.TYPE_STRING, description="Search by company name or admin email/name"),
            openapi.Parameter('tier', openapi.IN_QUERY, type=openapi.TYPE_STRING, description="Filter by plan tier (e.g. 'all', 'free', 'pro', 'plus', 'top_tier')"),
            openapi.Parameter('status', openapi.IN_QUERY, type=openapi.TYPE_STRING, description="Filter by status ('all', 'active', 'expiring_soon', 'in_grace_period', 'past_due', 'canceled')"),
            openapi.Parameter('cycle', openapi.IN_QUERY, type=openapi.TYPE_STRING, description="Filter by billing cycle ('all', 'monthly', 'annual', 'yearly')"),
            openapi.Parameter('page', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, description="Page number (default 1)"),
            openapi.Parameter('page_size', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, description="Page size (default 10)"),
        ],
    )
    def get(self, request):
        user = request.user
        is_admin = bool(
            user.is_superuser
            or user.is_staff
            or getattr(user, 'role', '') in ['superadmin', 'axiyon_admin']
        )
        if not is_admin:
            return Response(
                {'detail': 'Only administrators can access subscriptions overview.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        now = timezone.now()
        thirty_days_later = now + timedelta(days=30)
        start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        # ── Global Top Stats ──────────────────────────────────────────────────
        active_companies = Company.objects.filter(status=Company.Status.ACTIVE)
        active_count = active_companies.count()
        new_this_month = active_companies.filter(created_at__gte=start_of_month).count()

        expiring_soon_count = Company.objects.filter(
            status=Company.Status.ACTIVE,
            current_period_end__gte=now,
            current_period_end__lte=thirty_days_later,
        ).count()

        in_grace_period_count = Company.objects.filter(
            Q(status=Company.Status.PAST_DUE) | Q(grace_until__gt=now)
        ).count()

        stats_data = {
            'active_subscriptions': active_count,
            'active_this_month': new_this_month,
            'active_this_month_badge': f"+{new_this_month} this month",
            'expiring_soon': expiring_soon_count,
            'expiring_soon_badge': "Requires renewal",
            'in_grace_period': in_grace_period_count,
            'in_grace_period_badge': "Action needed",
        }

        # ── Base Queryset ─────────────────────────────────────────────────────
        qs = Company.objects.all().prefetch_related('users').order_by('-created_at', 'id')

        # 1. Search filter
        search = request.query_params.get('search', '').strip()
        if search:
            qs = qs.filter(
                Q(name__icontains=search)
                | Q(users__email__icontains=search)
                | Q(users__first_name__icontains=search)
                | Q(users__last_name__icontains=search)
            ).distinct()

        # 2. Tier / Plan filter
        tier = request.query_params.get('tier') or request.query_params.get('plan')
        if tier and tier.lower() != 'all':
            qs = qs.filter(plan__iexact=tier.strip())

        # 3. Cycle / Interval filter
        cycle = request.query_params.get('cycle') or request.query_params.get('interval')
        if cycle and cycle.lower() != 'all':
            cycle_val = cycle.lower().strip()
            if cycle_val in ('annual', 'yearly', 'year'):
                qs = qs.filter(billing_interval__in=['yearly', 'annual'])
            elif cycle_val == 'monthly':
                qs = qs.filter(billing_interval='monthly')

        # 4. Status filter
        status_filter = request.query_params.get('status', '').lower().strip()
        if status_filter and status_filter != 'all':
            if status_filter == 'expiring_soon':
                qs = qs.filter(
                    status=Company.Status.ACTIVE,
                    current_period_end__gte=now,
                    current_period_end__lte=thirty_days_later,
                )
            elif status_filter == 'in_grace_period':
                qs = qs.filter(
                    Q(status=Company.Status.PAST_DUE) | Q(grace_until__gt=now)
                )
            elif status_filter == 'active':
                qs = qs.filter(status=Company.Status.ACTIVE)
            elif status_filter == 'past_due':
                qs = qs.filter(status=Company.Status.PAST_DUE)
            elif status_filter in ('canceled', 'cancelled'):
                qs = qs.filter(status=Company.Status.CANCELED)
            elif status_filter == 'suspended':
                qs = qs.filter(status=Company.Status.SUSPENDED)

        # ── Pagination ────────────────────────────────────────────────────────
        page = request.query_params.get('page', 1)
        page_size = request.query_params.get('page_size', 10)
        try:
            page = int(page)
            if page < 1:
                page = 1
        except (TypeError, ValueError):
            page = 1

        try:
            page_size = int(page_size)
            if page_size < 1 or page_size > 100:
                page_size = 10
        except (TypeError, ValueError):
            page_size = 10

        paginator = Paginator(qs, page_size)
        try:
            paginated_page = paginator.page(page)
        except EmptyPage:
            paginated_page = []

        # ── Serialization ─────────────────────────────────────────────────────
        organizations = []
        for company in paginated_page:
            admin_user = (
                company.users.filter(role=User.Role.COMPANY_ADMIN).first()
                or company.users.first()
            )

            # Determine live status
            if (company.grace_until and company.grace_until > now) or company.status == Company.Status.PAST_DUE:
                computed_status = 'in_grace_period'
            elif company.status == Company.Status.ACTIVE:
                if company.current_period_end and now <= company.current_period_end <= thirty_days_later:
                    computed_status = 'expiring_soon'
                else:
                    computed_status = 'active'
            elif company.status == Company.Status.CANCELED:
                computed_status = 'canceled'
            elif company.status == Company.Status.SUSPENDED:
                computed_status = 'suspended'
            else:
                computed_status = company.status

            # Billing cycle
            b_interval = (company.billing_interval or 'monthly').lower()
            billing_cycle = 'annual' if b_interval in ('yearly', 'annual', 'year') else 'monthly'

            act_date = company.created_at
            exp_date = company.current_period_end
            is_expiring_soon = bool(
                exp_date and now <= exp_date <= thirty_days_later
            )

            org_data = {
                'id': company.pk,
                'company_id': company.pk,
                'company_name': company.name,
                'admin_email': admin_user.email if admin_user else '',
                'admin_name': admin_user.full_name if admin_user else '',
                'plan': company.plan,
                'billing_cycle': billing_cycle,
                'activated_at': act_date.isoformat() if act_date else None,
                'next_expiry': exp_date.isoformat() if exp_date else None,
                'is_expiring_soon': is_expiring_soon,
                'status': computed_status,
            }
            organizations.append(org_data)

        return Response({
            'stats': stats_data,
            'pagination': {
                'total': paginator.count,
                'page': page,
                'page_size': page_size,
                'total_pages': paginator.num_pages,
                'has_next': paginated_page.has_next() if hasattr(paginated_page, 'has_next') else False,
                'has_prev': paginated_page.has_previous() if hasattr(paginated_page, 'has_previous') else False,
            },
            'organizations': organizations,
        })



