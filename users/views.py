import csv
import logging
import random
import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.http import HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.db.models import Count, Q
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import OrderingFilter
from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework_simplejwt.views import TokenObtainPairView

from api.constants import COUNTRY_CURRENCY_MAP
from api.utils import apply_country_filter
from users.filters import LeadFilter, TaskFilter, UserFilter
from users.models import Company, Lead, PasswordResetOTP, Task, Team
from users.pagination import CustomPagination
from users.permissions import IsSuperAdminOrCompanyAdminOrTeamManager, IsCompanyAdminOrTeamManager, IsSuperAdmin, IsNotSuperAdmin
from projects.models import Project, Unit
from projects.serializers import UserProjectDetailSerializer, UserProjectSerializer
from users.serializers import (
    ChangePasswordSerializer,
    CompanyAutocompleteSerializer,
    CompanyCreateSerializer,
    CompanyDetailSerializer,
    CompanyUpdateSerializer,
    CustomTokenObtainPairSerializer,
    ForgotPasswordSerializer,
    LeadSerializer,
    MeUpdateSerializer,
    ResetPasswordSerializer,
    TaskSerializer,
    TeamAutocompleteSerializer,
    UserDetailSerializer,
    UserLeadDetailSerializer,
    UserLeadSerializer,
    UserManagementSerializer,
    VerifyOTPSerializer,
)
from users.tasks import import_leads_csv, import_users_csv, send_otp_email, send_superadmin_created_account_email
from users.utils import generate_password

User = get_user_model()

logger = logging.getLogger(__name__)


class DashboardSummaryView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        tags=['Dashboard'],
        responses={
            200: openapi.Response(
                description='Dashboard summary counts scoped to the caller.',
                examples={
                    'application/json': {
                        'total_leads': 128,
                        'total_tasks': 54,
                        'total_active_projects': 12,
                        'total_proposals': 3,
                        'total_calls': 20,
                    }
                },
            )
        },
        operation_description=(
            'Return dashboard counts for leads, tasks, projects, proposals, and calls.\n\n'
            '- superadmin: counts across all companies.\n'
            '- axiyon_admin / company_admin: counts scoped to their company.\n'
            '- team_manager: leads, tasks, proposals, and calls assigned to or created by team members.\n'
            '- agent: only leads, tasks, proposals, and calls assigned to or created by them.\n'
            '- project counts include all visible projects regardless of status.'
        ),
    )
    def get(self, request):
        from calling_agent.services import calls_visible_to_user
        from new_proposal.models import GeneratedProposal

        user = request.user

        if getattr(user, 'role', None) == User.Role.SUPERADMIN:
            leads_qs = Lead.objects.all()
            tasks_qs = Task.objects.all()
            proposals_qs = GeneratedProposal.objects.all()

        elif user.role in (User.Role.AXIYON_ADMIN, User.Role.COMPANY_ADMIN, 'axiyon_admin', 'company_admin'):
            if user.company_id:
                leads_qs = Lead.objects.filter(created_by__company=user.company)
                tasks_qs = Task.objects.filter(created_by__company=user.company)
                proposals_qs = GeneratedProposal.objects.filter(generated_by__company=user.company)
            else:
                leads_qs = Lead.objects.filter(created_by=user)
                tasks_qs = Task.objects.filter(created_by=user)
                proposals_qs = GeneratedProposal.objects.filter(generated_by=user)

        elif user.role == User.Role.TEAM_MANAGER:
            managed_team = getattr(user, 'managed_team', None)
            if managed_team:
                leads_qs = Lead.objects.filter(
                    Q(assigned_to__team=managed_team) |
                    Q(created_by__team=managed_team) |
                    Q(assigned_to=user) |
                    Q(created_by=user)
                ).distinct()
                tasks_qs = Task.objects.filter(
                    Q(created_by=user) | Q(created_by__team=managed_team)
                ).distinct()
                proposals_qs = GeneratedProposal.objects.filter(
                    Q(generated_by=user) | Q(generated_by__team=managed_team)
                )
            else:
                leads_qs = Lead.objects.filter(
                    Q(assigned_to=user) | Q(created_by=user)
                ).distinct()
                tasks_qs = Task.objects.filter(created_by=user)
                proposals_qs = GeneratedProposal.objects.filter(generated_by=user)

        else:
            leads_qs = Lead.objects.filter(
                Q(assigned_to=user) | Q(created_by=user)
            ).distinct()

            tasks_qs = Task.objects.filter(created_by=user)
            proposals_qs = GeneratedProposal.objects.filter(generated_by=user)

        # Same for all roles
        projects_qs = Project.objects.all()
        calls_qs = calls_visible_to_user(user)

        leads_qs = apply_country_filter(leads_qs, user)
        tasks_qs = apply_country_filter(tasks_qs, user)
        projects_qs = apply_country_filter(projects_qs, user).filter(
            project_status=Project.ProjectStatus.LIVE
        )

        # Proposals filter by linked project's associated_country
        country = getattr(user, 'current_country', None)
        if country and country != 'all':
            proposals_qs = proposals_qs.filter(project__associated_country=country)

        # Calculate pipeline_value across all live projects
        pipeline_value = 0.0
        for project in projects_qs.prefetch_related('units'):
            avail_units = [
                u for u in project.units.all()
                if u.status == Unit.UnitStatus.AVAILABLE
            ]
            count = len(avail_units)
            if count > 0:
                total_avail_price = sum(
                    float(u.list_price if (u.list_price and u.list_price > 0) else (u.list_price or 0))
                    for u in avail_units
                )
                pipeline_value += total_avail_price / count

        return Response(
            {
                'total_leads': leads_qs.count(),
                'total_tasks': tasks_qs.count(),
                'total_active_projects': projects_qs.count(),
                'total_proposals': proposals_qs.count(),
                'total_calls': calls_qs.count(),
                'pipeline_value': round(pipeline_value, 2),
            },
            status=status.HTTP_200_OK,
        )


class CommissionAnalyticsView(APIView):
    """
    Role-based Commission and Sales Analytics API.
    Provides periodic chart breakdown (1D, 1W, 1M, 3M, 1Y) and summary totals.
    - Agent: sees only their own won deals and agent commission.
    - Company Admin / Axiyon Admin: sees their company's sales and commissions.
    - Team Manager: sees their team's sales and commissions.
    - Super Admin: sees all companies' sales and commissions, with company breakdown.
    """
    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        tags=['Commissions'],
        manual_parameters=[
            openapi.Parameter('timeframe', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False, description='Timeframe: 1D | 1W | 1M | 3M | 1Y (default: 1D)'),
            openapi.Parameter('company_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=False, description='Filter by Company ID (superadmin)'),
            openapi.Parameter('agent_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=False, description='Filter by Agent ID'),
            openapi.Parameter('project_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=False, description='Filter by Project ID'),
            openapi.Parameter('start_date', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False, description='YYYY-MM-DD start date filter'),
            openapi.Parameter('end_date', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False, description='YYYY-MM-DD end date filter'),
            openapi.Parameter('year', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=False, description='Target year (e.g. 2025 or 2026)'),
            openapi.Parameter('currency', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False, description='Target currency code (e.g. GBP, USD, AED)'),
        ],
        operation_description=(
            "Return role-scoped commission and sales volume metrics along with periodic time-series chart data.\n\n"
            "- **agent**: only sales and commissions from leads assigned to them.\n"
            "- **company_admin / axiyon_admin**: sales and commissions for their company.\n"
            "- **team_manager**: sales and commissions for their team members.\n"
            "- **superadmin**: aggregate sales and commissions across all companies, plus `companies_breakdown` list."
        ),
    )
    def get(self, request):
        from users.commission_service import CommissionAnalyticsService

        timeframe = request.query_params.get('timeframe', '1D')
        company_id = request.query_params.get('company_id')
        agent_id = request.query_params.get('agent_id')
        project_id = request.query_params.get('project_id')
        start_date = request.query_params.get('start_date')
        end_date = request.query_params.get('end_date')
        year = request.query_params.get('year')
        target_currency = request.query_params.get('currency')

        company_id_int = int(company_id) if company_id and str(company_id).isdigit() else None
        agent_id_int = int(agent_id) if agent_id and str(agent_id).isdigit() else None
        project_id_int = int(project_id) if project_id and str(project_id).isdigit() else None
        year_int = int(year) if year and str(year).isdigit() else None

        service = CommissionAnalyticsService(user=request.user, target_currency=target_currency)
        data = service.get_analytics(
            timeframe=timeframe,
            company_id=company_id_int,
            agent_id=agent_id_int,
            project_id=project_id_int,
            start_date_str=start_date,
            end_date_str=end_date,
            year=year_int,
        )
        return Response(data, status=status.HTTP_200_OK)


class LatestCommissionsView(APIView):
    """
    Return the latest commissions for the dashboard 'Commission Intelligence' / 'Earnings Outlook' widget.
    Defaults to the latest 3 commissions, scoped by user role:
    - Agent: sees only their own won deals and agent commission.
    - Team Manager: sees their own won deals and won deals of their team members.
    - Company Admin / Axiyon Admin: sees company won deals across team managers and agents.
    - Super Admin: sees won deals across all companies.
    """
    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        tags=['Commissions'],
        manual_parameters=[
            openapi.Parameter('limit', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=False, description='Number of items to return (default: 3)'),
            openapi.Parameter('company_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=False, description='Filter by Company ID (superadmin)'),
            openapi.Parameter('agent_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=False, description='Filter by Agent ID'),
            openapi.Parameter('project_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=False, description='Filter by Project ID'),
            openapi.Parameter('currency', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False, description='Target currency code (e.g. GBP, USD, AED)'),
        ],
        operation_description=(
            "Return the latest commissions for the 'Commission Intelligence' dashboard widget.\n\n"
            "- **agent**: only their own latest commissions.\n"
            "- **team_manager**: latest commissions for their managed team members and self.\n"
            "- **company_admin / axiyon_admin**: latest commissions across their company's team managers and agents.\n"
            "- **superadmin**: latest commissions across all companies."
        ),
    )
    def get(self, request):
        from users.commission_service import CommissionAnalyticsService

        limit = request.query_params.get('limit', '3')
        company_id = request.query_params.get('company_id')
        agent_id = request.query_params.get('agent_id')
        project_id = request.query_params.get('project_id')
        target_currency = request.query_params.get('currency')

        limit_int = int(limit) if limit and str(limit).isdigit() else 3
        company_id_int = int(company_id) if company_id and str(company_id).isdigit() else None
        agent_id_int = int(agent_id) if agent_id and str(agent_id).isdigit() else None
        project_id_int = int(project_id) if project_id and str(project_id).isdigit() else None

        service = CommissionAnalyticsService(user=request.user, target_currency=target_currency)
        data = service.get_latest_commissions(
            limit=limit_int,
            company_id=company_id_int,
            agent_id=agent_id_int,
            project_id=project_id_int,
        )
        return Response(data, status=status.HTTP_200_OK)


class StreamActivityView(APIView):
    """
    Return recent stream activity items (latest lead assigned, latest call logged, latest proposal out)
    for a user (specified via query param user_id, path param user_id, or defaulting to authenticated user).
    """

    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        tags=['Dashboard'],
        manual_parameters=[
            openapi.Parameter('user_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, description='Target user ID (defaults to current user)'),
        ],
        responses={
            200: openapi.Response(
                description='Latest 3 activity items for the stream (Lead Assigned, Call Logged, Proposal Out).',
            )
        },
        operation_description=(
            "Return latest activity stream items for a user including latest lead assigned, "
            "latest call logged, and latest proposal generated."
        ),
    )
    def get(self, request, user_id=None):
        from calling_agent.models import Call
        from new_proposal.models import GeneratedProposal

        target_user_id = user_id or request.query_params.get('user_id', '').strip()
        if target_user_id and str(target_user_id).isdigit():
            target_user = get_object_or_404(User, pk=int(target_user_id))
        else:
            target_user = request.user

        activities = []

        # 1. Latest Lead Assigned
        latest_lead = (
            Lead.objects.filter(Q(assigned_to=target_user) | Q(created_by=target_user))
            .select_related('project')
            .order_by('-updated_at', '-created_at')
            .first()
        )
        if latest_lead:
            try:
                budget_val = float(latest_lead.estimated_budget) if latest_lead.estimated_budget else None
                budget_str = f" (${budget_val:,.0f} Asset)" if budget_val else ""
            except (ValueError, TypeError):
                budget_str = ""

            source_str = f" via {latest_lead.source}" if latest_lead.source else " via CRM routing"
            lead_time = latest_lead.updated_at or latest_lead.created_at
            activities.append({
                'type': 'LEAD_ASSIGNED',
                'category': 'lead_assigned',
                'title': 'LEAD ASSIGNED',
                'description': f"{latest_lead.name}{budget_str} assigned{source_str}.",
                'time': lead_time.strftime('%H:%M') if lead_time else '',
                'timestamp': lead_time.isoformat() if lead_time else None,
                'details': {
                    'lead_id': latest_lead.id,
                    'lead_name': latest_lead.name,
                    'phone_no': latest_lead.phone_no,
                    'country': latest_lead.country,
                    'status': latest_lead.get_status_display(),
                    'estimated_budget': str(latest_lead.estimated_budget) if latest_lead.estimated_budget else None,
                    'source': latest_lead.source,
                }
            })

        # 2. Latest Call Logged
        call_filter = Q(context_user=target_user)
        if getattr(target_user, 'company_id', None):
            call_filter = call_filter | Q(company=target_user.company, lead__assigned_to=target_user)

        latest_call = (
            Call.objects.filter(call_filter)
            .select_related('lead')
            .order_by('-created_at')
            .first()
        )
        if latest_call:
            call_lead_name = (latest_call.lead.name if latest_call.lead and latest_call.lead.name else latest_call.lead_name) or 'Client'
            outcome_display = latest_call.get_status_display()
            activities.append({
                'type': 'CALL_LOGGED',
                'category': 'call_logged',
                'title': 'CALL LOGGED',
                'description': f"Follow-up session with {call_lead_name}. Status: {outcome_display}.",
                'time': latest_call.created_at.strftime('%H:%M') if latest_call.created_at else '',
                'timestamp': latest_call.created_at.isoformat() if latest_call.created_at else None,
                'details': {
                    'call_id': latest_call.id,
                    'public_id': str(latest_call.public_id),
                    'lead_name': call_lead_name,
                    'phone_number': latest_call.phone_number,
                    'status': outcome_display,
                    'duration': latest_call.duration,
                }
            })

        # 3. Latest Proposal Generated
        latest_proposal = (
            GeneratedProposal.objects.filter(Q(generated_by=target_user) | Q(lead__assigned_to=target_user))
            .select_related('project', 'lead', 'unit')
            .order_by('-created_at')
            .first()
        )
        if latest_proposal:
            proj_name = latest_proposal.project.title if latest_proposal.project else 'Project'
            prop_lead_name = latest_proposal.lead.name if latest_proposal.lead else ''
            lead_suffix = f" for {prop_lead_name}" if prop_lead_name else ""

            file_url = None
            try:
                if latest_proposal.file and hasattr(latest_proposal.file, 'url'):
                    file_url = latest_proposal.file.url
            except Exception:
                file_url = latest_proposal.hosted_url or None

            if not file_url:
                file_url = latest_proposal.hosted_url or None

            activities.append({
                'type': 'PROPOSAL_OUT',
                'category': 'proposal_out',
                'title': 'PROPOSAL OUT',
                'description': f"{proj_name} portfolio breakdown sent{lead_suffix}.",
                'time': latest_proposal.created_at.strftime('%H:%M') if latest_proposal.created_at else '',
                'timestamp': latest_proposal.created_at.isoformat() if latest_proposal.created_at else None,
                'details': {
                    'proposal_id': latest_proposal.id,
                    'project_id': latest_proposal.project_id,
                    'project_name': proj_name,
                    'lead_name': prop_lead_name,
                    'status': latest_proposal.get_status_display(),
                    'file_url': file_url,
                }
            })

        # Sort activities by timestamp descending
        activities.sort(key=lambda x: x['timestamp'] or '', reverse=True)

        lead_detail = next((a['details'] for a in activities if a['type'] == 'LEAD_ASSIGNED'), None)
        call_detail = next((a['details'] for a in activities if a['type'] == 'CALL_LOGGED'), None)
        proposal_detail = next((a['details'] for a in activities if a['type'] == 'PROPOSAL_OUT'), None)

        return Response({
            'user_id': target_user.id,
            'user_name': target_user.full_name,
            'activities': activities,
            'latest_lead': lead_detail,
            'latest_call': call_detail,
            'latest_proposal': proposal_detail,
        }, status=status.HTTP_200_OK)


# ── Auth views ────────────────────────────────────────────────────────────────


class CustomTokenObtainPairView(TokenObtainPairView):
    permission_classes = [permissions.AllowAny]
    serializer_class = CustomTokenObtainPairSerializer

    @swagger_auto_schema(
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'email': openapi.Schema(type=openapi.TYPE_STRING),
                'password': openapi.Schema(type=openapi.TYPE_STRING),
            },
            required=['email', 'password']
        ),
        responses={
            200: openapi.Schema(
                type=openapi.TYPE_OBJECT,
                properties={
                    'access': openapi.Schema(type=openapi.TYPE_STRING),
                    'refresh': openapi.Schema(type=openapi.TYPE_STRING),
                    'user': openapi.Schema(
                        type=openapi.TYPE_OBJECT,
                        properties={
                            'id': openapi.Schema(type=openapi.TYPE_INTEGER),
                            'email': openapi.Schema(type=openapi.TYPE_STRING),
                            'first_name': openapi.Schema(type=openapi.TYPE_STRING),
                            'last_name': openapi.Schema(type=openapi.TYPE_STRING),
                            'role': openapi.Schema(type=openapi.TYPE_STRING),
                            'status': openapi.Schema(type=openapi.TYPE_STRING),
                            'countries': openapi.Schema(type=openapi.TYPE_ARRAY, items=openapi.Schema(type=openapi.TYPE_STRING)),
                        }
                    ),
                }
            )
        }
    )
    def post(self, request, *args, **kwargs):
        return super().post(request, *args, **kwargs)


class ForgotPasswordView(APIView):
    permission_classes = [permissions.AllowAny]

    @swagger_auto_schema(
        request_body=ForgotPasswordSerializer,
        responses={200: "OTP sent", 429: "Cooldown active", 400: "Invalid email"},
        operation_description=(
            "Request a password reset OTP.\n\n"
            "- OTP valid for **5 minutes**.\n"
            "- Max one request every **60 seconds**."
        ),
    )
    def post(self, request):
        serializer = ForgotPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = User.objects.filter(
            email=serializer.validated_data["email"],
            status=User.Status.ACTIVE,
        ).first()
        if not user:
            return Response({"error": "Invalid email."}, status=status.HTTP_400_BAD_REQUEST)

        otp_entry = PasswordResetOTP.objects.filter(user=user, is_verified=False).last()
        if otp_entry and not otp_entry.can_resend():
            wait_seconds = int((otp_entry.resend_allowed_at - timezone.now()).total_seconds())
            return Response(
                {"error": f"Please wait {wait_seconds} seconds before requesting another OTP."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        PasswordResetOTP.objects.filter(user=user, is_verified=False).delete()
        otp = str(random.randint(100000, 999999))
        PasswordResetOTP.objects.create(
            user=user,
            otp=otp,
            resend_allowed_at=timezone.now() + timedelta(seconds=60),
        )

        send_otp_email(user, otp)
        return Response({"message": "OTP sent to email."}, status=status.HTTP_200_OK)


class VerifyOTPView(APIView):
    permission_classes = [permissions.AllowAny]

    @swagger_auto_schema(
        request_body=VerifyOTPSerializer,
        responses={200: "OTP verified — reset_token returned", 400: "Invalid or expired OTP"},
        operation_description=(
            "Verify password reset OTP.\n\n"
            "- OTP valid for **5 minutes**.\n"
            "- Returns `reset_token` for use in `/api/auth/reset_password/`."
        ),
    )
    def post(self, request):
        serializer = VerifyOTPSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = serializer.validated_data["email"]
        otp = serializer.validated_data["otp"]
        invalid_response = Response(
            {"error": "Invalid or expired OTP."},
            status=status.HTTP_400_BAD_REQUEST,
        )

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            return invalid_response

        otp_entry = PasswordResetOTP.objects.filter(user=user, otp=otp, is_verified=False).last()
        if not otp_entry or otp_entry.is_expired():
            return invalid_response

        otp_entry.is_verified = True
        otp_entry.save(update_fields=["is_verified"])

        return Response(
            {"message": "OTP verified successfully.", "reset_token": str(otp_entry.reset_uuid)},
            status=status.HTTP_200_OK,
        )


class ResetPasswordView(APIView):
    permission_classes = [permissions.AllowAny]

    @swagger_auto_schema(
        request_body=ResetPasswordSerializer,
        responses={200: "Password reset successful", 400: "Invalid or expired token"},
        operation_description=(
            "Reset password using the token from `/api/auth/verify_otp/`.\n\n"
            "- Requires `email`, `reset_token`, `new_password`, `confirm_password`.\n"
            "- Token is deleted after use."
        ),
    )
    def post(self, request):
        serializer = ResetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = serializer.validated_data["email"]
        reset_uuid = serializer.validated_data["reset_uuid"]
        new_password = serializer.validated_data["new_password"]

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            return Response({"error": "Invalid email."}, status=status.HTTP_400_BAD_REQUEST)

        otp_entry = PasswordResetOTP.objects.filter(
            user=user, reset_uuid=reset_uuid, is_verified=True
        ).last()

        if not otp_entry:
            return Response({"error": "Invalid or unverified reset token."}, status=status.HTTP_400_BAD_REQUEST)

        if otp_entry.is_expired():
            return Response({"error": "Reset token expired."}, status=status.HTTP_400_BAD_REQUEST)

        user.password = make_password(new_password)
        user.save(update_fields=["password"])
        otp_entry.delete()

        return Response({"message": "Password reset successful."}, status=status.HTTP_200_OK)


class ChangePasswordView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        request_body=ChangePasswordSerializer,
        responses={200: "Password changed successfully", 400: "Invalid old password or validation error"},
        operation_description=(
            "Change password for the authenticated user.\n\n"
            "- Requires `old_password`, `new_password`, `confirm_password`.\n"
            "- Old password must be correct.\n"
            "- New password must match confirm password."
        ),
    )
    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = request.user
        old_password = serializer.validated_data["old_password"]
        new_password = serializer.validated_data["new_password"]

        # Verify old password
        if not user.check_password(old_password):
            return Response(
                {"error": "Old password is incorrect."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Set new password
        user.set_password(new_password)
        user.save(update_fields=["password"])

        return Response(
            {"message": "Password changed successfully."},
            status=status.HTTP_200_OK
        )


# ── Superadmin CRUD views ─────────────────────────────────────────────────────

class MeView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    @swagger_auto_schema(
        responses={200: UserDetailSerializer},
        operation_description="Returns the currently authenticated user's profile.",
    )
    def get(self, request):
        serializer = UserDetailSerializer(request.user)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @swagger_auto_schema(
        request_body=MeUpdateSerializer,
        consumes=['multipart/form-data', 'application/json'],
        responses={200: UserDetailSerializer, 400: "Validation error"},
        operation_description=(
            "Update the authenticated user's own profile.\n\n"
            "- Editable fields: `first_name`, `last_name`, `profile_image`.\n"
            "- Send as `multipart/form-data` to upload `profile_image`.\n"
            "- `email`, `role`, `status`, `company`, etc. are read-only."
        ),
    )
    def patch(self, request):
        serializer = MeUpdateSerializer(
            request.user, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(UserDetailSerializer(request.user).data, status=status.HTTP_200_OK)


class AdminCompanyViewSet(viewsets.ModelViewSet):
    permission_classes = [permissions.IsAuthenticated, IsSuperAdmin]
    ordering = ['-created_at'] 
    http_method_names = ['get', 'post', 'patch', 'delete']

    def get_serializer_class(self):
        if self.action == 'create':
            return CompanyCreateSerializer
        if self.action in ['partial_update', 'update']:
            return CompanyUpdateSerializer
        return CompanyDetailSerializer

    def get_queryset(self):
        current_user = self.request.user
        if current_user.is_anonymous:
            return Company.objects.none()

        if current_user.role == 'superadmin':
            qs = Company.objects.order_by('-created_at')
        else:
            qs = Company.objects.filter(users=current_user).order_by('-created_at')

        return apply_country_filter(qs, current_user)
    

    @swagger_auto_schema(
        responses={200: CompanyDetailSerializer(many=True)},
        operation_description="List all companies with their admins. Superadmin only.",
        tags=['Companies (managed by Super Admin)'],
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @swagger_auto_schema(
        responses={200: CompanyDetailSerializer},
        operation_description="Retrieve a company by ID. Superadmin only.",
        tags=['Companies (managed by Super Admin)'],
    )
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @swagger_auto_schema(
        request_body=CompanyCreateSerializer,
        responses={
            201: CompanyDetailSerializer,
            400: "Validation error",
        },
        operation_description=(
            "Create a company and its first admin.\n\n"
            "- `admin.email`, `admin.first_name`, `admin.last_name` are required.\n"
            "- Admin is created with role `company_admin`, `status=active`.\n"
            "- A secure password is auto-generated and emailed to the admin.\n"
            "- Admin's countries default to the company's `operating_countries`."
        ),
        tags=['Companies (managed by Super Admin)'],
    )
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        company = serializer.save()

        # Email credentials to the newly created admin
        send_superadmin_created_account_email(
            company._admin_user,
            company._admin_plain_password,
            self.request.user
        )

        return Response(
            CompanyDetailSerializer(company).data,
            status=status.HTTP_201_CREATED,
        )

    @swagger_auto_schema(
        request_body=CompanyUpdateSerializer,
        responses={200: CompanyDetailSerializer, 400: "Validation error"},
        operation_description="Update company name or operating countries. Superadmin only.",
        tags=['Companies (managed by Super Admin)'],
    )
    def partial_update(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(CompanyDetailSerializer(instance).data, status=status.HTTP_200_OK)

    @swagger_auto_schema(
        responses={204: "Company deleted", 404: "Not found"},
        operation_description="Delete a company. All associated users will have company set to null.",
        tags=['Companies (managed by Super Admin)'],
    )
    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        # if any of instance.users is a superadmin, return error

        if instance.users.filter(role=User.Role.SUPERADMIN).exists():
            return Response(
                {"detail": "Cannot delete company with associated superuser."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        instance.delete()
        return Response({"message": "Company deleted successfully."}, status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=['get'], url_path='stats')
    def stats(self, request):
        qs = self.get_queryset()

        counts = qs.aggregate(
            added_last_month=Count('id', filter=Q(created_at__gte=timezone.now() - timedelta(days=30))),
            added_last_week=Count('id', filter=Q(created_at__gte=timezone.now() - timedelta(days=7))),
        )
        counts['total'] = qs.count()  # ← separate count, not sum of the above

        return Response(counts)

class UsersViewSet(viewsets.ModelViewSet):
    http_method_names = ['get', 'post', 'patch', 'delete']
    serializer_class = UserManagementSerializer
    pagination_class = CustomPagination

    filter_backends = [DjangoFilterBackend, OrderingFilter]
    filterset_class = UserFilter
    ordering_fields = ['created_at', 'updated_at', 'email', 'first_name', 'last_name']
    ordering = ['-updated_at']

    BASE_QUERYSET = (
        User.objects
            .exclude(role='superadmin')
            .select_related('company', 'team')
    )

    def get_permissions(self):
        if self.action in ['create', 'update', 'partial_update', 'destroy']:
            return [permissions.IsAuthenticated(), IsCompanyAdminOrTeamManager()]

        if self.action in ['import_csv', 'import_csv_result']:
            return [permissions.IsAuthenticated(), IsCompanyAdminOrTeamManager()]

        return [permissions.IsAuthenticated(), IsSuperAdminOrCompanyAdminOrTeamManager()]

    def get_queryset(self):
        user = self.request.user
        if user.is_anonymous:
            return Company.objects.none()

        if user.role == 'superadmin':
            # Superadmin sees everyone (including unverified / superadmins)
            qs = User.objects.select_related('company', 'team').order_by('-created_at')
        elif user.role in ('axiyon_admin', 'company_admin'):
            qs = self.BASE_QUERYSET.filter(company=user.company).order_by('-created_at')
        elif user.role == 'team_manager':
            qs = self.BASE_QUERYSET.filter(company=user.company, role=User.Role.AGENT).order_by('-created_at')
        else:
            return User.objects.none()

        # Apply country filter for list and stats actions
        if self.action in ('list', 'stats'):
            qs = apply_country_filter(qs, user)

        return qs

    def perform_create(self, serializer):
        """Auto-generate password and send welcome email."""
        from django.db import transaction

        plain_password = generate_password()
        # The INSERT and the password UPDATE commit together, so a failure
        # cannot leave an invited user with an unusable password.
        with transaction.atomic():
            user = serializer.save()
            user.set_password(plain_password)
            user.save(update_fields=['password'])
        send_superadmin_created_account_email(user, plain_password, self.request.user)

    # ------------------------------------------------------------------ #
    #  Swagger docs                                                         #
    # ------------------------------------------------------------------ #
    _TAGS = ['User Management']

    @swagger_auto_schema(tags=_TAGS, operation_description="List users. Scoped by caller role and current country.")
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @swagger_auto_schema(tags=_TAGS, operation_description="Retrieve a single user.")
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @swagger_auto_schema(
        tags=_TAGS,
        request_body=UserManagementSerializer,
        responses={201: UserManagementSerializer, 400: "Validation error"},
        operation_description=(
            "Create a user.\n\n"
            "- **company_admin**: user is auto-assigned to their company; "
            "cannot set role above `team_manager`.\n"
            "- **team_manager**: user is auto-assigned to their team with role `agent`.\n"
            "- A secure password is auto-generated and emailed to the new user."
        ),
    )
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    @swagger_auto_schema(
        tags=_TAGS,
        responses={200: UserManagementSerializer, 400: "Validation error"},
        operation_description="Partial update. Same role/company/team restrictions as create.",
    )
    def partial_update(self, request, *args, **kwargs):
        return super().partial_update(request, *args, **kwargs)

    @swagger_auto_schema(
        tags=_TAGS,
        responses={204: "Deleted", 404: "Not found"},
        operation_description="Delete a user.",
    )
    def destroy(self, request, *args, **kwargs):
        return super().destroy(request, *args, **kwargs)

    def perform_destroy(self, instance):
        if instance.pk == self.request.user.pk:
            raise ValidationError({"detail": "You cannot delete your own account."})
        instance.delete()


    # ------------------------------------------------------------------ #
    #  Autocomplete — Companies                                            #
    # ------------------------------------------------------------------ #

    @swagger_auto_schema(
        tags=_TAGS,
        manual_parameters=[
            openapi.Parameter('q', openapi.IN_QUERY, type=openapi.TYPE_STRING, description='Search by name'),
        ],
        responses={200: CompanyAutocompleteSerializer(many=True)},
        operation_description="Superadmin only. Search companies by name for use in create-user form.",
    )
    @action(detail=False, methods=['get'], url_path='autocomplete_companies')
    def autocomplete_companies(self, request):
        user = request.user
        q = request.query_params.get('q', '').strip()

        if user.role == 'superadmin':
            qs = Company.objects.all()
            if q:
                qs = qs.filter(name__icontains=q)
            qs = qs.order_by('name')[:20]
        else:
            # company_admin, team_manager — return only their own company
            qs = Company.objects.filter(pk=user.company_id)

        return Response(CompanyAutocompleteSerializer(qs, many=True).data)

    # ------------------------------------------------------------------ #
    #  Autocomplete — Teams                                                #
    # ------------------------------------------------------------------ #

    @swagger_auto_schema(
        tags=_TAGS,
        manual_parameters=[
            openapi.Parameter('q', openapi.IN_QUERY, type=openapi.TYPE_STRING, description='Search by name'),
            openapi.Parameter('company', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, description='Filter by company ID (superadmin only)'),
        ],
        responses={200: TeamAutocompleteSerializer(many=True)},
        operation_description=(
            "Search teams for use in create-user form.\n\n"
            "- **superadmin**: sees all teams; optionally filter by `?company=<id>`.\n"
            "- **company_admin**: sees only their own company's teams.\n"
            "- **team_manager**: sees only their own team.\n"
        ),
    )
    @action(detail=False, methods=['get'], url_path='autocomplete_teams')
    def autocomplete_teams(self, request):
        user = request.user
        q = request.query_params.get('q', '').strip()

        if user.role == 'superadmin':
            qs = Team.objects.select_related('company').all()
            company_id = request.query_params.get('company')
            if company_id:
                qs = qs.filter(company_id=company_id)

        elif user.role in ('axiyon_admin', 'company_admin'):
            qs = Team.objects.select_related('company').filter(company=user.company)

        elif user.role == 'team_manager':
            managed_team = getattr(user, 'managed_team', None)
            qs = Team.objects.filter(pk=managed_team.pk) if managed_team else Team.objects.none()

        else:
            return Response(
                {"detail": "You do not have permission to access this endpoint."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if q:
            qs = qs.filter(name__icontains=q)

        qs = qs.order_by('name')[:20]
        return Response(TeamAutocompleteSerializer(qs, many=True).data)

    # ------------------------------------------------------------------ #
    #  Stats                                                               #
    # ------------------------------------------------------------------ #

    @swagger_auto_schema(
        tags=_TAGS,
        responses={
            200: openapi.Response(
                description="User status counts scoped to the caller's access.",
                examples={
                    "application/json": {
                        "total": 42,
                        "active": 30,
                        "invited": 2,
                    }
                },
            )
        },
        operation_description=(
            "Returns user counts by status, scoped to the same queryset the caller can see.\n\n"
            "- **superadmin**: all users.\n"
            "- **company_admin**: users in their company.\n"
            "- **team_manager**: users in their team."
        ),
    )
    @action(detail=False, methods=['get'], url_path='stats')
    def stats(self, request):
        qs = self.get_queryset()  # already scoped + country-filtered by role

        counts = qs.aggregate(
            active=Count('id', filter=Q(status=User.Status.ACTIVE)),
            invited=Count('id', filter=Q(status=User.Status.INVITED)),
        )
        counts['total'] = sum(counts.values())

        return Response(counts)

    @swagger_auto_schema(
        tags=_TAGS,
        operation_description=(
            "Export users as CSV. Scoped to the same queryset as the caller's role.\n\n"
            "- **superadmin**: all users.\n"
            "- **company_admin**: users in their company.\n"
            "- **team_manager**: agents in their company."
        ),
        responses={200: "CSV file download"},
    )
    @action(detail=False, methods=['get'], url_path='export_csv')
    def export_csv(self, request):
        qs = self.get_queryset()

        class _Echo:
            def write(self, value: str) -> str:
                return value

        def _generate_rows():
            yield ['ID', 'First Name', 'Last Name', 'Email', 'Role', 'Status', 'Countries', 'Company']
            for user in qs.values(
                'id', 'first_name', 'last_name', 'email', 'role', 'status', 'countries', 'company__name'
            ).iterator():
                yield [
                    user['id'],
                    user['first_name'],
                    user['last_name'],
                    user['email'],
                    user['role'],
                    user['status'],
                    ', '.join(user['countries'] or []),
                    user['company__name'] or '',
                ]

        writer = csv.writer(_Echo())
        response = StreamingHttpResponse(
            (writer.writerow(row) for row in _generate_rows()),
            content_type='text/csv',
        )
        response['Content-Disposition'] = 'attachment; filename="users.csv"'
        return response

    @swagger_auto_schema(
        tags=_TAGS,
        operation_description=(
            "Import users from a CSV file.\n\n"
            "Accepted columns: `email`, `first_name`, `last_name`, `role`, `countries` (comma-separated), "
            "`team` (optional ID), `company` (required for superadmin only).\n\n"
            "- Existing users (matched by email) will be updated.\n"
            "- New users will be created with `status=invited` and emailed their credentials.\n"
            "- Same role/company/countries validations apply as the POST endpoint.\n\n"
            "Returns a task ID to poll for results."
        ),
        responses={
            202: openapi.Response(
                description="Task accepted",
                examples={"application/json": {"task_id": "abc-123"}}
            ),
            400: "Validation error",
        },
    )
    @action(detail=False, methods=['post'], url_path='import_csv', parser_classes=[MultiPartParser])
    def import_csv(self, request):
        file = request.FILES.get('file')

        if not file:
            return Response(
                {"detail": "No file provided."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not file.name.endswith('.csv'):
            return Response(
                {"detail": "Only CSV files are accepted."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Save to S3 and pass only the key — avoids loading the full CSV into
        # memory and serialising binary data through Redis.
        from django.core.files.storage import default_storage
        temp_key = f'imports/csv/temp/{uuid.uuid4()}/{file.name}'
        default_storage.save(temp_key, file)

        task = import_users_csv.delay(temp_key, request.user.pk)

        return Response(
            {"task_id": task.id},
            status=status.HTTP_202_ACCEPTED,
        )


    @swagger_auto_schema(
        tags=_TAGS,
        operation_description="Poll the result of a CSV import task by task ID.",
        responses={
            200: openapi.Response(
                description="Task result",
                examples={
                    "application/json": {
                        "state": "SUCCESS",
                        "result": {
                            "created": 5,
                            "updated": 2,
                            "errors": []
                        }
                    }
                }
            )
        },
    )
    @action(detail=False, methods=['get'], url_path='import_csv_result/(?P<task_id>[^/.]+)')
    def import_csv_result(self, request, task_id=None):
        from celery.result import AsyncResult
        task = AsyncResult(task_id)

        if task.state == 'PENDING':
            return Response({"state": "PENDING", "result": None})
        elif task.state == 'SUCCESS':
            return Response({"state": "SUCCESS", "result": task.result})
        elif task.state == 'FAILURE':
            return Response({"state": "FAILURE", "result": str(task.result)})

        return Response({"state": task.state, "result": None})

    # ── User Projects ─────────────────────────────────────────────────────

    @swagger_auto_schema(
        tags=_TAGS,
        operation_description=(
            "List all projects with assignment status for a specific user.\n\n"
            "- **company_admin**: target user must be in their company.\n"
            "- **team_manager**: target user must be an agent in their company.\n"
            "- Returns each project with `is_assigned` and `assignment` fields."
        ),
        manual_parameters=[
            openapi.Parameter('search', openapi.IN_QUERY, type=openapi.TYPE_STRING,
                              description='Search by project title or description'),
            openapi.Parameter('status', openapi.IN_QUERY, type=openapi.TYPE_STRING,
                              description='in_progress | ready | planned'),
            openapi.Parameter('project_type', openapi.IN_QUERY, type=openapi.TYPE_STRING,
                              description='residential | commercial | hospitality'),
        ],
        responses={200: UserProjectSerializer(many=True)},
    )
    @action(detail=True, methods=['get'], url_path='projects')
    def user_projects(self, request, pk=None):
        user = self._get_target_user(pk)

        qs = Project.objects.all()
        qs = self._filter_projects(qs, request)
        qs = apply_country_filter(qs, request.user)
        qs = qs.order_by('-created_at')

        page = self.paginate_queryset(qs)
        if page is not None:
            serializer = UserProjectSerializer(
                page, many=True, context={'target_user_id': user.id}
            )
            return self.get_paginated_response(serializer.data)

        serializer = UserProjectSerializer(
            qs, many=True, context={'target_user_id': user.id}
        )
        return Response(serializer.data)

    @swagger_auto_schema(
        tags=_TAGS,
        operation_description=(
            "Retrieve a single project with assignment status for a specific user.\n\n"
            "- **company_admin**: target user must be in their company.\n"
            "- **team_manager**: target user must be an agent in their company."
        ),
        responses={200: UserProjectDetailSerializer},
    )
    @action(detail=True, methods=['get'], url_path='projects/(?P<project_id>[^/.]+)')
    def user_project_detail(self, request, pk=None, project_id=None):
        user = self._get_target_user(pk)

        project = Project.objects.filter(pk=project_id).first()
        if not project:
            return Response(
                {"detail": "Project not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = UserProjectDetailSerializer(
            project, context={'target_user_id': user.id}
        )
        return Response(serializer.data)

    def _get_target_user(self, user_pk):
        """Resolve the target user, enforcing scope rules for the caller."""
        caller = self.request.user
        try:
            target = User.objects.get(pk=user_pk)
        except User.DoesNotExist:
            raise NotFound("User not found.")

        if caller.role in ('axiyon_admin', 'company_admin'):
            if target.company_id != caller.company_id:
                raise PermissionDenied("You can only view projects for users in your company.")

        elif caller.role == 'team_manager':
            if target.company_id != caller.company_id or target.role != User.Role.AGENT:
                raise PermissionDenied("You can only view projects for agents in your company.")

        return target

    def _filter_projects(self, qs, request):
        search = request.query_params.get('search', '').strip()
        status_filter = request.query_params.get('status', '').strip()
        project_type = request.query_params.get('project_type', '').strip()

        if search:
            from django.db.models import Q
            qs = qs.filter(
                Q(title__icontains=search) | Q(description__icontains=search)
            )
        if status_filter:
            qs = qs.filter(status=status_filter)
        if project_type:
            qs = qs.filter(project_type=project_type)

        return qs

    # ── User Leads ──────────────────────────────────────────────────────

    @swagger_auto_schema(
        tags=_TAGS,
        operation_description=(
            "List all leads with assignment status for a specific user.\n\n"
            "- **company_admin**: target user must be in their company.\n"
            "- **team_manager**: target user must be an agent in their company.\n"
            "- Returns each lead with `is_assigned` field."
        ),
        manual_parameters=[
            openapi.Parameter('search', openapi.IN_QUERY, type=openapi.TYPE_STRING,
                              description='Search by lead name, email, phone_no, or country'),
            openapi.Parameter('status', openapi.IN_QUERY, type=openapi.TYPE_STRING,
                              description='Lead.Status code (staged CRM taxonomy)'),
            openapi.Parameter('source', openapi.IN_QUERY, type=openapi.TYPE_STRING,
                              description='Filter by lead source (icontains)'),
            openapi.Parameter('country', openapi.IN_QUERY, type=openapi.TYPE_STRING,
                              description='Filter by lead country (icontains)'),
        ],
        responses={200: UserLeadSerializer(many=True)},
    )
    @action(detail=True, methods=['get'], url_path='leads')
    def user_leads(self, request, pk=None):
        target_user = User.objects.filter(pk=pk).first()
        target_user_id = target_user.id if target_user else None

        qs = Lead.objects.select_related('created_by', 'assigned_to').filter(
            created_by=request.user,
            is_assign=False,
        )

        qs = self._filter_leads(qs, request)
        qs = apply_country_filter(qs, request.user)
        qs = qs.order_by('-created_at')

        page = self.paginate_queryset(qs)
        if page is not None:
            serializer = UserLeadSerializer(
                page, many=True, context={'target_user_id': target_user_id}
            )
            return self.get_paginated_response(serializer.data)

        serializer = UserLeadSerializer(
            qs, many=True, context={'target_user_id': target_user_id}
        )
        return Response(serializer.data)

    @swagger_auto_schema(
        tags=_TAGS,
        operation_description=(
            "Retrieve a single lead with assignment status for a specific user.\n\n"
            "- **company_admin**: target user must be in their company.\n"
            "- **team_manager**: target user must be an agent in their company."
        ),
        responses={200: UserLeadDetailSerializer},
    )
    @action(detail=True, methods=['get'], url_path='leads/(?P<lead_id>[^/.]+)')
    def user_lead_detail(self, request, pk=None, lead_id=None):
        user = self._get_target_user(pk)

        lead = Lead.objects.filter(pk=lead_id).first()
        if not lead:
            return Response(
                {"detail": "Lead not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = UserLeadDetailSerializer(
            lead, context={'target_user_id': user.id}
        )
        return Response(serializer.data)

    def _filter_leads(self, qs, request):
        search = request.query_params.get('search', '').strip()
        status_filter = request.query_params.get('status', '').strip()
        source = request.query_params.get('source', '').strip()
        country = request.query_params.get('country', '').strip()

        if search:
            qs = qs.filter(
                Q(name__icontains=search) |
                Q(email__icontains=search) |
                Q(phone_no__icontains=search) |
                Q(country__icontains=search)
            )
        if status_filter:
            qs = qs.filter(status=status_filter)
        if source:
            qs = qs.filter(source__icontains=source)
        if country:
            qs = qs.filter(country__icontains=country)

        return qs

    @swagger_auto_schema(
        tags=_TAGS,
        operation_description=(
            "Assign a lead to a specific user (team manager or agent).\n\n"
            "- Target user must have role `team_manager` or `agent`.\n"
            "- **company_admin**: target user must be in their company.\n"
            "- **team_manager**: target user must be in their company."
        ),
        responses={200: UserLeadDetailSerializer, 400: "Validation error"},
    )
    @action(detail=True, methods=['post'], url_path='leads/(?P<lead_id>[^/.]+)/assign')
    def lead_assign(self, request, pk=None, lead_id=None):
        user = self._get_target_agent(pk)

        lead = Lead.objects.filter(pk=lead_id).first()
        if not lead:
            return Response(
                {"detail": "Lead not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        lead.assigned_to = user
        lead.save(update_fields=['assigned_to'])

        try:
            from notifications.services import notify_lead_missing_commission
            notify_lead_missing_commission(lead, extra_recipients=[request.user])
        except Exception:  
            logger.exception("Failed to dispatch commission-missing notification")

        try:
            from notifications.services import notify_lead_project_missing_images
            notify_lead_project_missing_images(lead, assigned_by=request.user)
        except Exception:
            logger.exception("Failed to dispatch lead-project missing-images notification")

        serializer = UserLeadDetailSerializer(
            lead, context={'target_user_id': user.id}
        )
        return Response(serializer.data)

    @swagger_auto_schema(
        tags=_TAGS,
        operation_description=(
            "Remove a lead assignment from a specific user (team manager or agent).\n\n"
            "- Target user must have role `team_manager` or `agent`.\n"
            "- The lead must currently be assigned to this user.\n"
            "- **company_admin**: target user must be in their company.\n"
            "- **team_manager**: target user must be in their company."
        ),
        responses={200: UserLeadDetailSerializer, 400: "Validation error"},
    )
    @action(detail=True, methods=['post'], url_path='leads/(?P<lead_id>[^/.]+)/remove')
    def lead_remove(self, request, pk=None, lead_id=None):
        user = self._get_target_agent(pk)

        lead = Lead.objects.filter(pk=lead_id).first()
        if not lead:
            return Response(
                {"detail": "Lead not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if lead.assigned_to_id != user.id:
            return Response(
                {"detail": "This lead is not assigned to this agent."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        lead.assigned_to = None
        lead.save(update_fields=['assigned_to'])

        serializer = UserLeadDetailSerializer(
            lead, context={'target_user_id': user.id}
        )
        return Response(serializer.data)

    def _get_target_agent(self, user_pk):
        """Resolve the target user, enforcing they are a team manager or agent for lead assignment."""
        caller = self.request.user
        try:
            target = User.objects.get(pk=user_pk)
        except User.DoesNotExist:
            raise NotFound("User not found.")

        # Allow both team managers and agents to be assigned leads
        if target.role not in [User.Role.TEAM_MANAGER, User.Role.AGENT]:
            raise ValidationError({"detail": "Leads can only be assigned to team managers or agents."})

        if caller.role in ('axiyon_admin', 'company_admin'):
            if target.company_id != caller.company_id:
                raise PermissionDenied("You can only assign leads to users in your company.")

        elif caller.role == 'team_manager':
            if target.company_id != caller.company_id:
                raise PermissionDenied("You can only assign leads to users in your company.")

        return target


# ── Tasks views ───────────────────────────────────────────────────────────────

class TaskViewSet(viewsets.ModelViewSet):
    """
    Tasks API.

    Any authenticated user (superadmin, company_admin, team_manager, agent) can
    create tasks. Each user can list / retrieve / update / delete only the
    tasks they created. Superadmins can see and manage all tasks.
    """
    serializer_class = TaskSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = CustomPagination

    filter_backends = [DjangoFilterBackend, OrderingFilter]
    filterset_class = TaskFilter
    ordering_fields = ['created_at', 'updated_at', 'due_date', 'priority', 'status', 'name']
    ordering = ['-created_at']

    http_method_names = ['get', 'post', 'patch', 'delete']

    _TAGS = ['Tasks']

    def get_queryset(self):
        user = self.request.user
        if user.is_anonymous:
            return Task.objects.none()

        qs = Task.objects.select_related(
            'created_by', 'related_lead', 'related_lead__assigned_to', 'related_lead__created_by'
        )

        role = getattr(user, 'role', None)
        if role == User.Role.SUPERADMIN:
            pass  # qs already has all tasks
        elif role in (User.Role.AXIYON_ADMIN, User.Role.COMPANY_ADMIN):
            qs = qs.filter(created_by__company=user.company)
        elif role == User.Role.TEAM_MANAGER:
            managed_team = getattr(user, 'managed_team', None)
            if managed_team:
                qs = qs.filter(
                    Q(created_by=user) | Q(created_by__team=managed_team)
                ).distinct()
            else:
                qs = qs.filter(created_by=user)
        else:
            qs = qs.filter(created_by=user)

        # Country filter applies only to list views; retrieve/update/delete must
        # be able to access any task the user has permission to.
        if self.action == 'list':
            qs = apply_country_filter(qs, user)

        return qs

    def perform_create(self, serializer):
        user = self.request.user
        # Always auto-set associated_country from user's current_country (backend-only field)
        current_country = getattr(user, 'current_country', '') or ''
        country = current_country if (current_country and current_country != 'all') else ''
        serializer.save(created_by=user, associated_country=country)

    @swagger_auto_schema(
        tags=_TAGS,
        manual_parameters=[
            openapi.Parameter('month', openapi.IN_QUERY, type=openapi.TYPE_STRING,
                              description='Filter by due_date month. Format: YYYY-MM (e.g. 2026-05)'),
            openapi.Parameter('year', openapi.IN_QUERY, type=openapi.TYPE_INTEGER,
                              description='Filter by due_date year (e.g. 2026)'),
            openapi.Parameter('priority', openapi.IN_QUERY, type=openapi.TYPE_STRING,
                              description='high | medium | low'),
            openapi.Parameter('status', openapi.IN_QUERY, type=openapi.TYPE_STRING,
                              description='pending | in_progress | completed'),
            openapi.Parameter('related_lead', openapi.IN_QUERY, type=openapi.TYPE_INTEGER,
                              description='Filter tasks by related Lead ID'),
            openapi.Parameter('user_role', openapi.IN_QUERY, type=openapi.TYPE_STRING,
                              description='Filter tasks by related lead user role: all, agent, team_manager, company_admin, axiyon_admin, superadmin'),
            openapi.Parameter('search', openapi.IN_QUERY, type=openapi.TYPE_STRING,
                              description='Search by task name'),
            openapi.Parameter('ordering', openapi.IN_QUERY, type=openapi.TYPE_STRING,
                              description='Order by: created_at, due_date, priority, status, name (prefix - for desc)'),
        ],
        operation_description="List tasks. Scoped to the current user (superadmin sees all).",
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @swagger_auto_schema(tags=_TAGS, operation_description="Retrieve a single task by ID.")
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @swagger_auto_schema(
        tags=_TAGS,
        request_body=TaskSerializer,
        responses={201: TaskSerializer, 400: 'Validation error'},
        operation_description=(
            "Create a task. The `created_by` field is automatically set to the "
            "authenticated user."
        ),
    )
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    @swagger_auto_schema(
        tags=_TAGS,
        request_body=TaskSerializer,
        responses={200: TaskSerializer, 400: 'Validation error'},
        operation_description="Partial update of a task. Only the owner (or superadmin) can update.",
    )
    def partial_update(self, request, *args, **kwargs):
        return super().partial_update(request, *args, **kwargs)

    @swagger_auto_schema(
        tags=_TAGS,
        responses={204: 'Deleted', 404: 'Not found'},
        operation_description="Delete a task. Only the owner (or superadmin) can delete.",
    )
    def destroy(self, request, *args, **kwargs):
        return super().destroy(request, *args, **kwargs)

    @swagger_auto_schema(
        tags=_TAGS,
        responses={
            200: openapi.Response(
                description="Task counts scoped to the caller.",
                examples={
                    "application/json": {
                        "total": 12,
                        "pending": 5,
                        "in_progress": 4,
                        "completed": 3,
                    }
                },
            )
        },
        operation_description=(
            "Returns task counts by status, scoped to the same queryset as the caller "
            "(superadmin sees all, others see only their own tasks)."
        ),
    )
    @action(detail=False, methods=['get'], url_path='stats')
    def stats(self, request):
        qs = self.get_queryset()
        # Apply country filter based on user's current_country
        qs = apply_country_filter(qs, request.user)

        counts = qs.aggregate(
            pending=Count('id', filter=Q(status=Task.Status.PENDING)),
            in_progress=Count('id', filter=Q(status=Task.Status.IN_PROGRESS)),
            completed=Count('id', filter=Q(status=Task.Status.COMPLETED)),
            expired=Count('id', filter=Q(status=Task.Status.EXPIRED)),
        )
        counts['total'] = qs.count()
        return Response(counts)

# ── Leads views ───────────────────────────────────────────────────────────────

class LeadViewSet(viewsets.ModelViewSet):
    """
    Leads API.

    Any authenticated user (superadmin, company_admin, team_manager, agent) can
    create leads. Lead visibility is filtered by role:
    - **superadmin**: sees all leads
    - **company_admin**: sees all leads in their company
    - **team_manager**: sees leads assigned to or created by team members
    - **agent**: sees only leads assigned to them or created by them
    """
    serializer_class = LeadSerializer
    pagination_class = CustomPagination

    filter_backends = [DjangoFilterBackend, OrderingFilter]
    filterset_class = LeadFilter
    ordering_fields = ['created_at', 'updated_at', 'scheduled_at', 'status', 'name']
    ordering = ['-created_at']

    http_method_names = ['get', 'post', 'patch', 'delete']

    _TAGS = ['Leads']

    def get_serializer_class(self):
        if self.action in ('import_csv', 'import_csv_result', 'export_csv'):
            from rest_framework import serializers as _ser
            class _EmptySerializer(_ser.Serializer):
                pass
            return _EmptySerializer
        return super().get_serializer_class()

    def get_permissions(self):
        if self.action in ['import_csv', 'import_csv_result', 'create',
                           'update', 'partial_update', 'destroy']:
            return [permissions.IsAuthenticated(), IsNotSuperAdmin()]

        return [permissions.IsAuthenticated()]

    def get_queryset(self):
        user = self.request.user
        if user.is_anonymous:
            return Lead.objects.none()

        qs = Lead.objects.select_related('project', 'created_by', 'assigned_to').prefetch_related('projects')

        if getattr(user, 'role', None) == 'superadmin':
            pass  # sees all leads
        elif user.role in ('axiyon_admin', 'company_admin'):
            qs = qs.filter(created_by__company=user.company)
        elif user.role == 'team_manager':
            managed_team = getattr(user, 'managed_team', None)
            if managed_team:
                qs = qs.filter(
                    Q(assigned_to__team=managed_team) |
                    Q(created_by__team=managed_team) |
                    Q(assigned_to=user)
                ).distinct()
            else:
                qs = qs.filter(
                    Q(assigned_to=user) | Q(created_by=user)
                ).distinct()
        else:
            qs = qs.filter(
                Q(assigned_to=user) | Q(created_by=user)
            ).distinct()

        # Country filter applies only to list views; retrieve/update/delete must
        # be able to access any lead the user has permission to.
        if self.action == 'list':
            qs = apply_country_filter(qs, user)

        return qs.order_by('-created_at')

    def _filter_leads(self, qs, request):
        search = request.query_params.get('search', '').strip()
        status_filter = request.query_params.get('status', '').strip()
        source = request.query_params.get('source', '').strip()
        country = request.query_params.get('country', '').strip()
        is_assign = request.query_params.get('is_assign', '').strip()

        if search:
            qs = qs.filter(
                Q(name__icontains=search) |
                Q(email__icontains=search) |
                Q(phone_no__icontains=search) |
                Q(country__icontains=search)
            )
        if status_filter:
            qs = qs.filter(status=status_filter)
        if source:
            qs = qs.filter(source__icontains=source)
        if country:
            qs = qs.filter(country__icontains=country)
        if is_assign.lower() in ('true', 'false'):
            qs = qs.filter(is_assign=(is_assign.lower() == 'true'))

        return qs

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    @swagger_auto_schema(
        tags=_TAGS,
        manual_parameters=[
            openapi.Parameter('month', openapi.IN_QUERY, type=openapi.TYPE_STRING,
                              description='Filter by scheduled_at month. Format: YYYY-MM (e.g. 2026-05)'),
            openapi.Parameter('year', openapi.IN_QUERY, type=openapi.TYPE_INTEGER,
                              description='Filter by scheduled_at year (e.g. 2026)'),
            openapi.Parameter('status', openapi.IN_QUERY, type=openapi.TYPE_STRING,
                              description='Lead.Status code (staged CRM taxonomy)'),
            openapi.Parameter('source', openapi.IN_QUERY, type=openapi.TYPE_STRING,
                              description='Filter by lead source (icontains)'),
            openapi.Parameter('country', openapi.IN_QUERY, type=openapi.TYPE_STRING,
                              description='Filter by lead country (icontains)'),
            openapi.Parameter('desired_country', openapi.IN_QUERY, type=openapi.TYPE_STRING,
                              description='Filter by desired country (icontains)'),
            openapi.Parameter('category', openapi.IN_QUERY, type=openapi.TYPE_STRING,
                              description='Filter by category (icontains)'),
            openapi.Parameter('type', openapi.IN_QUERY, type=openapi.TYPE_STRING,
                              description='Filter by type (icontains)'),
            openapi.Parameter('project', openapi.IN_QUERY, type=openapi.TYPE_INTEGER,
                              description='Filter by project ID'),
            openapi.Parameter('user_role', openapi.IN_QUERY, type=openapi.TYPE_STRING,
                              description='Filter leads by assigned or created-by user role: all, agent, team_manager, company_admin, axiyon_admin, superadmin'),
            openapi.Parameter('is_assign', openapi.IN_QUERY, type=openapi.TYPE_BOOLEAN,
                              description='Filter by assignment status: true or false'),
            openapi.Parameter('search', openapi.IN_QUERY, type=openapi.TYPE_STRING,
                              description='Search by name, email, phone_no, or country'),
            openapi.Parameter('ordering', openapi.IN_QUERY, type=openapi.TYPE_STRING,
                              description='Order by: created_at, scheduled_at, status, name (prefix - for desc)'),
        ],
        operation_description="List leads. Scoped to the current user (super_admin and axiyon_admin sees all).",
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @swagger_auto_schema(tags=_TAGS, operation_description="Retrieve a single lead by ID.")
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @swagger_auto_schema(
        tags=_TAGS,
        operation_description=(
            "List leads available for proposal generation for the current logged-in user. "
            "Returns only leads assigned to or created by the current user."
        ),
        manual_parameters=[
            openapi.Parameter('search', openapi.IN_QUERY, type=openapi.TYPE_STRING,
                              description='Search by lead name, email, phone_no, or country'),
            openapi.Parameter('status', openapi.IN_QUERY, type=openapi.TYPE_STRING,
                              description='Lead.Status code (staged CRM taxonomy)'),
            openapi.Parameter('source', openapi.IN_QUERY, type=openapi.TYPE_STRING,
                              description='Filter by lead source (icontains)'),
            openapi.Parameter('country', openapi.IN_QUERY, type=openapi.TYPE_STRING,
                              description='Filter by lead country (icontains)'),
        ],
        responses={200: LeadSerializer(many=True)},
    )
    @action(detail=False, methods=['get'], url_path='proposal_leads')
    def proposal_leads(self, request):
        user = request.user
        if user.is_anonymous:
            return Response(
                {'detail': 'Authentication credentials were not provided.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        qs = Lead.objects.select_related('project', 'created_by', 'assigned_to').prefetch_related('projects')
        qs = qs.filter(Q(assigned_to=user) | Q(created_by=user)).distinct()
        qs = self._filter_leads(qs, request)
        qs = apply_country_filter(qs, request.user)
        qs = qs.order_by('-created_at')

        page = self.paginate_queryset(qs)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = self.get_serializer(qs, many=True)
        return Response(serializer.data)

    @swagger_auto_schema(
        tags=_TAGS,
        operation_description=(
            "Export leads as CSV. Scoped to the same queryset as the caller's role.\n\n"
            "- **superadmin**: all leads.\n"
            "- **company_admin**: leads in their company.\n"
            "- **team_manager**: leads in their company.\n"
            "- **agent**: leads in their company."
        ),
        responses={200: "CSV file download"},
    )
    @action(detail=False, methods=['get'], url_path='export_csv')
    def export_csv(self, request):
        qs = self.get_queryset().prefetch_related('projects')
        
        # Filter leads by matching current user's current_country with lead's desired_country
        current_user_country = request.user.current_country
        if current_user_country:
            qs = qs.filter(desired_country=current_user_country)

        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="leads.csv"'

        writer = csv.writer(response)
        writer.writerow([
            'Name',
            'Phone No',
            'Email',
            'Status',
            'Source',
            'Country',
            'Desired Country',
            'Desired Location',
            'Estimated Budget',
            'Category',
            'Type',
            'Other Type',
            'Scheduled At',
            'Project(s)',
        ])

        for lead in qs:
            project_titles = [project.title for project in lead.projects.all()]
            if lead.project and lead.project.title not in project_titles:
                project_titles.insert(0, lead.project.title)

            writer.writerow([
                lead.name,
                lead.phone_no,
                lead.email,
                lead.status,
                lead.source,
                lead.country,
                lead.desired_country,
                lead.desired_location,
                lead.estimated_budget or '',
                lead.category,
                lead.type,
                lead.other_type,
                lead.scheduled_at.isoformat() if lead.scheduled_at else '',
                ', '.join(project_titles),
            ])

        return response

    @swagger_auto_schema(
        tags=_TAGS,
        manual_parameters=[
            openapi.Parameter(
                'file',
                openapi.IN_FORM,
                type=openapi.TYPE_FILE,
                required=True,
                description='CSV file to import leads from. Required columns: name, phone_no, estimated_budget.',
            ),
        ],
        consumes=['multipart/form-data'],
        operation_description=(
            "Import leads from a CSV file.\n\n"
            "**Required columns**: `name`, `phone_no`, `estimated_budget`\n\n"
            "**Optional columns**: `email`, `status`, `source`, `country`, "
            "`desired_country`, `desired_location`, `category`, `type`, `other_type`, "
            "`scheduled_at`, `project`, `assigned_to`.\n\n"
            "Returns a task ID to poll for results via `GET /import_csv_result/{task_id}/`."
        ),
        responses={
            202: openapi.Response(
                description="Task accepted",
                examples={"application/json": {"task_id": "abc-123"}}
            ),
            400: "Validation error",
        },
    )
    @action(detail=False, methods=['post'], url_path='import_csv', parser_classes=[MultiPartParser])
    def import_csv(self, request):
        file = request.FILES.get('file')

        if not file:
            return Response(
                {"detail": "No file provided."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not file.name.endswith('.csv'):
            return Response(
                {"detail": "Only CSV files are accepted."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        file_content = file.read().decode('utf-8')

        task = import_leads_csv.delay(file_content, request.user.pk)

        return Response(
            {"task_id": task.id},
            status=status.HTTP_202_ACCEPTED,
        )

    @swagger_auto_schema(
        tags=_TAGS,
        operation_description="Poll the result of a lead CSV import task by task ID.",
        responses={
            200: openapi.Response(
                description="Task result",
                examples={
                    "application/json": {
                        "state": "SUCCESS",
                        "result": {
                            "created": 5,
                            "errors": []
                        }
                    }
                }
            )
        },
    )
    @action(detail=False, methods=['get'], url_path='import_csv_result/(?P<task_id>[^/.]+)')
    def import_csv_result(self, request, task_id=None):
        from celery.result import AsyncResult
        task = AsyncResult(task_id)

        if task.state == 'PENDING':
            return Response({"state": "PENDING", "result": None})
        elif task.state == 'SUCCESS':
            return Response({"state": "SUCCESS", "result": task.result})
        elif task.state == 'FAILURE':
            return Response({"state": "FAILURE", "result": str(task.result)})

        return Response({"state": task.state, "result": None})

    @swagger_auto_schema(
        tags=_TAGS,
        request_body=LeadSerializer,
        responses={201: LeadSerializer, 400: 'Validation error'},
        operation_description=(
            "Create a lead. The `created_by` field is automatically set to the "
            "authenticated user."
        ),
    )
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    @swagger_auto_schema(
        tags=_TAGS,
        request_body=LeadSerializer,
        responses={200: LeadSerializer, 400: 'Validation error'},
        operation_description="Partial update of a lead. Only the owner (or superadmin) can update.",
    )
    def partial_update(self, request, *args, **kwargs):
        return super().partial_update(request, *args, **kwargs)

    @swagger_auto_schema(
        tags=_TAGS,
        responses={204: 'Deleted', 404: 'Not found'},
        operation_description="Delete a lead. Only the owner (or superadmin) can delete.",
    )
    def destroy(self, request, *args, **kwargs):
        return super().destroy(request, *args, **kwargs)

    @swagger_auto_schema(
        tags=_TAGS,
        responses={
            200: openapi.Response(
                description="Lead counts scoped to the caller.",
                examples={
                    "application/json": {
                        "total": 10,
                        "new": 4,
                        "contacted": 2,
                        "interested": 3,
                        "negotiation_ongoing": 1,
                        "by_status": {"new": 4, "contacted": 2},
                        "by_stage": {
                            "new": 4,
                            "contact_attempt": 2,
                            "qualification": 3,
                            "nurturing": 0,
                            "sales_process": 1,
                            "closed": 0,
                        },
                    }
                },
            )
        },
        operation_description=(
            "Returns lead counts by status and stage, scoped to the same "
            "queryset as the caller (superadmin sees all, others see only "
            "their own leads)."
        ),
    )
    @action(detail=False, methods=['get'], url_path='stats')
    def stats(self, request):
        qs = self.get_queryset()
        # Apply country filter based on user's current_country
        qs = apply_country_filter(qs, request.user)

        status_filters = {
            status.value: Count('id', filter=Q(status=status.value))
            for status in Lead.Status
        }
        counts = qs.aggregate(**status_filters)
        counts['total'] = qs.count()

        by_status = {
            status.value: counts.pop(status.value, 0)
            for status in Lead.Status
        }
        by_stage = {stage.value: 0 for stage in Lead.Stage}
        for status_code, count in by_status.items():
            stage = Lead.STATUS_TO_STAGE.get(status_code, Lead.Stage.NEW)
            by_stage[stage] = by_stage.get(stage, 0) + count

        counts['by_status'] = by_status
        counts['by_stage'] = by_stage
        # Convenience top-level keys for common dashboard cards.
        counts['new'] = by_status.get(Lead.Status.NEW, 0)
        counts['contacted'] = by_status.get(Lead.Status.CONTACTED, 0)
        counts['interested'] = by_status.get(Lead.Status.INTERESTED, 0)
        counts['negotiation_ongoing'] = by_status.get(
            Lead.Status.NEGOTIATION_ONGOING,
            0,
        )
        return Response(counts)

    @swagger_auto_schema(
        tags=_TAGS,
        filter_inspectors=[],
        manual_parameters=[
            openapi.Parameter('tab', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False, description='Tab filter: all | my_commission | managers | agents'),
            openapi.Parameter('agent_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=False, description='Filter by Agent ID'),
            openapi.Parameter('project_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=False, description='Filter by Project ID'),
            openapi.Parameter('company_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=False, description='Filter by Company ID'),
            openapi.Parameter('start_date', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False, description='YYYY-MM-DD start date filter'),
            openapi.Parameter('end_date', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False, description='YYYY-MM-DD end date filter'),
            openapi.Parameter('export', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False, description='Set export=true to download CSV report'),
        ],
        operation_description="Get role-based commission breakdown, top performer metrics, and transactions with tab filtering.",
    )
    @action(detail=False, methods=['get'], url_path='commissions')
    def commissions(self, request):
        from projects.models import ProjectAgentAssignment, Unit
        from decimal import Decimal
        from django.db.models import Q
        from django.http import HttpResponse
        import csv

        user = request.user
        role = getattr(user, 'role', User.Role.AGENT)
        tab = str(request.query_params.get('tab', '') or '').strip().lower()

        # Available tabs per role
        if role == User.Role.SUPERADMIN:
            available_tabs = ['all']
            default_tab = 'all'
        elif role in (User.Role.AXIYON_ADMIN, User.Role.COMPANY_ADMIN):
            available_tabs = ['all', 'my_commission', 'managers', 'agents']
            default_tab = 'all'
        elif role == User.Role.TEAM_MANAGER:
            available_tabs = ['all', 'my_commission', 'agents']
            default_tab = 'all'
        else:  # Agent
            available_tabs = ['my_commission']
            default_tab = 'my_commission'

        if not tab or tab not in available_tabs:
            tab = default_tab

        qs = self.get_queryset().filter(status=Lead.Status.CONVERTED_WON)

        # 1. Base Role Scoping
        if role == User.Role.SUPERADMIN:
            company_id = request.query_params.get('company_id')
            if company_id:
                qs = qs.filter(created_by__company_id=company_id)
        elif role in (User.Role.AXIYON_ADMIN, User.Role.COMPANY_ADMIN):
            qs = qs.filter(created_by__company=user.company)
        elif role == User.Role.TEAM_MANAGER:
            managed_team = getattr(user, 'managed_team', None)
            if managed_team:
                qs = qs.filter(Q(assigned_to=user) | Q(assigned_to__team=managed_team))
            else:
                qs = qs.filter(assigned_to=user)
        else:  # Agent
            qs = qs.filter(assigned_to=user)

        # 2. Tab Filtering
        if tab == 'my_commission':
            qs = qs.filter(assigned_to=user)
        elif tab == 'managers':
            qs = qs.filter(assigned_to__role=User.Role.TEAM_MANAGER)
        elif tab == 'agents':
            qs = qs.filter(assigned_to__role=User.Role.AGENT)

        # 3. Additional Filters (agent_id, project_id, start_date, end_date)
        agent_id = request.query_params.get('agent_id')
        project_id = request.query_params.get('project_id')
        start_date = request.query_params.get('start_date')
        end_date = request.query_params.get('end_date')

        if agent_id:
            qs = qs.filter(assigned_to_id=agent_id)
        if project_id:
            qs = qs.filter(project_id=project_id)
        if start_date:
            qs = qs.filter(updated_at__date__gte=start_date)
        if end_date:
            qs = qs.filter(updated_at__date__lte=end_date)

        # Pre-fetch ProjectAgentAssignment map for quick lookup: (agent_id, project_id) -> assignment
        assignments = {
            (a.agent_id, a.project_id): a
            for a in ProjectAgentAssignment.objects.all()
        }

        from users.utils import get_exchange_rate

        user_country = getattr(request.user, 'current_country', '') or ''
        if not user_country and getattr(request.user, 'company', None) and request.user.company.operating_countries:
            user_country = request.user.company.operating_countries[0]
        if not user_country and getattr(request.user, 'countries', None) and len(request.user.countries) > 0:
            user_country = request.user.countries[0]

        user_currency = COUNTRY_CURRENCY_MAP.get(user_country, '') or 'USD'

        transactions_data = []
        total_won_leads = 0
        total_deal_volume = Decimal('0.00')
        total_agent_commissions = Decimal('0.00')
        total_company_commissions = Decimal('0.00')

        # Project and Performer aggregations for Metric Cards
        project_totals = {}
        performer_totals = {}

        for lead in qs:
            total_won_leads += 1
            linked_units = list(lead.units.all())
            if not linked_units and lead.unit:
                linked_units = [lead.unit]

            if not linked_units:
                units_to_process = [(None, lead.project)]
            else:
                units_to_process = [(u, u.project or lead.project) for u in linked_units]

            for matched_unit, proj in units_to_process:
                unit_price = Decimal('0.00')
                if matched_unit:
                    if (
                        matched_unit.discounted_price is not None
                        and Decimal(str(matched_unit.discounted_price)) > Decimal('0.00')
                    ):
                        unit_price = Decimal(str(matched_unit.discounted_price))
                    elif matched_unit.list_price is not None:
                        unit_price = Decimal(str(matched_unit.list_price))

                if unit_price == Decimal('0.00'):
                    if lead.estimated_budget:
                        unit_price = Decimal(str(lead.estimated_budget))
                    elif proj and proj.starting_price:
                        unit_price = Decimal(str(proj.starting_price))

                list_price = unit_price

                # Convert list_price to user_currency if project currency differs
                stored_currency = (proj.currency if (proj and proj.currency) else 'USD') or 'USD'
                if user_currency and stored_currency and stored_currency != user_currency:
                    try:
                        rate = get_exchange_rate(stored_currency, user_currency)
                        list_price = round(list_price * Decimal(str(rate)), 2)
                    except Exception:
                        pass

                total_deal_volume += list_price

                proj_id = proj.id if proj else lead.project_id
                proj_title = proj.title if proj else (lead.project.title if lead.project else 'N/A')
                assignment = assignments.get((lead.assigned_to_id, proj_id))
                is_commission_assigned = bool(assignment is not None)

                if assignment:
                    agent_split = assignment.agent_split
                    company_split = assignment.company_split
                    agent_commission = round(list_price * (agent_split / Decimal('100.00')), 2)
                    company_commission = round(list_price * (company_split / Decimal('100.00')), 2)
                else:
                    agent_split = Decimal('0.00')
                    company_split = Decimal('0.00')
                    agent_commission = Decimal('0.00')
                    company_commission = Decimal('0.00')

                total_agent_commissions += agent_commission
                total_company_commissions += company_commission

                # Aggregate by project
                project_totals[proj_title] = project_totals.get(proj_title, Decimal('0.00')) + list_price

                # Aggregate by performer
                agent = lead.assigned_to
                if agent:
                    agent_name = agent.full_name or agent.email
                    performer_totals[agent_name] = performer_totals.get(agent_name, Decimal('0.00')) + agent_commission

                # Persist commission record in database if assignment exists
                if is_commission_assigned and lead.assigned_to and proj:
                    from users.models import AgentCommission
                    AgentCommission.objects.update_or_create(
                        lead=lead,
                        project=proj,
                        unit=matched_unit,
                        defaults={
                            'agent': lead.assigned_to,
                            'unit_details': {
                                'id': matched_unit.id if matched_unit else None,
                                'label': matched_unit.label if matched_unit else None,
                                'floor': matched_unit.floor if matched_unit else None,
                                'category': matched_unit.category if matched_unit else None,
                            } if matched_unit else {},
                            'list_price': list_price,
                            'agent_split': agent_split,
                            'company_split': company_split,
                            'agent_commission': agent_commission,
                            'company_commission': company_commission,
                        }
                    )

                message = (
                    "Commission split configured."
                    if is_commission_assigned
                    else "Commission is not set for this project for the assigned agent."
                )

                # Initials helper
                agent_initials = ""
                if agent and agent.first_name:
                    agent_initials += agent.first_name[0].upper()
                if agent and agent.last_name:
                    agent_initials += agent.last_name[0].upper()
                if not agent_initials and agent:
                    agent_initials = agent.email[:2].upper()

                company_name = ""
                if agent and getattr(agent, 'company', None):
                    company_name = agent.company.name
                elif lead.created_by and getattr(lead.created_by, 'company', None):
                    company_name = lead.created_by.company.name

                transactions_data.append({
                    'lead_id': lead.id,
                    'lead_name': lead.name,
                    'company_name': company_name,
                    'project_id': proj_id,
                    'project_title': proj_title,
                    'assigned_to_id': lead.assigned_to_id,
                    'assigned_to_name': agent.full_name if agent else None,
                    'assigned_to_initials': agent_initials,
                    'assigned_to_role': agent.role if agent else None,
                    'unit_id': matched_unit.id if matched_unit else None,
                    'unit_label': matched_unit.label if matched_unit else None,
                    'unit_status': matched_unit.status if matched_unit else None,
                    'date': lead.updated_at.strftime('%d %b %Y'),
                    'is_commission_assigned': is_commission_assigned,
                    'message': message,
                    'list_price': float(list_price),
                    'agent_split_percentage': float(agent_split),
                    'company_split_percentage': float(company_split),
                    'agent_commission': float(agent_commission),
                    'company_commission': float(company_commission),
                    'display_commission': float(company_commission if role == User.Role.SUPERADMIN else agent_commission),
                    'closed_at': lead.updated_at.isoformat(),
                })

        # Calculate Featured Project & Top Performer
        featured_project = max(project_totals, key=project_totals.get) if project_totals else "N/A"
        top_performer_name = max(performer_totals, key=performer_totals.get) if performer_totals else "N/A"
        top_performer_earned = float(performer_totals.get(top_performer_name, Decimal('0.00')))
        
        top_performer_initials = "".join([part[0].upper() for part in top_performer_name.split() if part])[:2] if top_performer_name != "N/A" else "N/A"

        # Check export query param
        if request.query_params.get('export') == 'true':
            response = HttpResponse(content_type='text/csv')
            response['Content-Disposition'] = 'attachment; filename="commission_report.csv"'
            writer = csv.writer(response)
            writer.writerow([
                'Lead ID', 'Lead Name', 'Company', 'Project', 'Agent', 'Role',
                'Unit', 'Date', 'Price', 'Agent Split %', 'Company Split %',
                'Agent Commission', 'Company Commission', 'Status Message'
            ])
            for t in transactions_data:
                writer.writerow([
                    t['lead_id'], t['lead_name'], t['company_name'], t['project_title'],
                    t['assigned_to_name'], t['assigned_to_role'], t['unit_label'], t['date'],
                    t['list_price'], t['agent_split_percentage'], t['company_split_percentage'],
                    t['agent_commission'], t['company_commission'], t['message']
                ])
            return response

        total_earned = float(total_company_commissions if role == User.Role.SUPERADMIN else total_agent_commissions)

        return Response({
            'role': role,
            'active_tab': tab,
            'available_tabs': available_tabs,
            'total_won_leads': total_won_leads,
            'total_deal_volume': float(total_deal_volume),
            'total_agent_commissions': float(total_agent_commissions),
            'total_company_commissions': float(total_company_commissions),
            'metrics': {
                'total_earned': total_earned,
                'featured_project': featured_project,
                'top_performer': {
                    'name': top_performer_name,
                    'initials': top_performer_initials,
                    'generated_commission': top_performer_earned,
                    'badge': 'TOP PERFORMER',
                },
                'total_won_leads': total_won_leads,
                'total_deal_volume': float(total_deal_volume),
                'total_agent_commissions': float(total_agent_commissions),
                'total_company_commissions': float(total_company_commissions),
            },
            'currency': user_currency,
            'recent_transactions': transactions_data,
            'leads': transactions_data,
        })