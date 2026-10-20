from datetime import timedelta
import logging
from io import BytesIO

import stripe
from django.conf import settings
from django.core.paginator import EmptyPage, Paginator
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import HttpResponse
from django.utils import timezone
from docx import Document
from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema
from rest_framework import permissions, status, viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from payment import plan_limits, services, stripe_client, webhooks
from payment.guards import payment_exempt
from payment.models import ProcessedStripeEvent, SubscriptionPlan
from payment.serializers import (
    CheckoutRequestSerializer,
    CompanyNameTaken,
    EmailAlreadyRegistered,
    RegisterCompanySerializer,
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


@payment_exempt
class WebsiteScrapeDocxView(APIView):
    """Public endpoint that scrapes a website and returns the text as a .docx file."""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    @staticmethod
    def _build_docx_bytes(website: str) -> bytes:
        from chatbot.website_knowledge import crawl_website, normalize_website_url

        normalized = normalize_website_url(website)
        if not normalized:
            raise ValueError('A valid website URL is required.')

        content = crawl_website(normalized)
        doc = Document()
        doc.add_heading('Website Data', level=1)
        doc.add_paragraph(f'Website: {content.website}')

        for page in content.pages:
            doc.add_heading(page.title or page.url or 'Website Page', level=2)
            if page.description:
                doc.add_paragraph(page.description)
            if page.url:
                doc.add_paragraph(f'URL: {page.url}')
            for line in page.lines:
                if line.strip():
                    doc.add_paragraph(line)

        buffer = BytesIO()
        doc.save(buffer)
        return buffer.getvalue()

    @swagger_auto_schema(
        tags=['Authentication'],
        operation_summary='Scrape Website and Export DOCX',
        operation_description=(
            'Public endpoint that fetches the website content using the same scraper flow as company onboarding, '
            'and returns the results as a Microsoft Word document (.docx).'
        ),
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'website': openapi.Schema(type=openapi.TYPE_STRING, description='Website URL to scrape'),
            },
            required=['website'],
        ),
        responses={
            200: 'Word document file response',
            400: 'Invalid website URL',
            500: 'Scraping failed',
        },
    )
    def post(self, request):
        website = (request.data.get('website') or request.query_params.get('website') or '').strip()
        if not website:
            return Response({'detail': 'Website URL is required.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            docx_bytes = self._build_docx_bytes(website)
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception('Website DOCX export failed for %s: %s', website, exc)
            return Response({'detail': 'Could not scrape the website and generate a Word document.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        response = HttpResponse(docx_bytes, content_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document')
        response['Content-Disposition'] = 'attachment; filename="website_data.docx"'
        return response


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



