from django.core.files.storage import default_storage
import logging

from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema
from django.core.files.storage import default_storage
from django.db.models import Q
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.filters import OrderingFilter
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser

from api.constants import COUNTRY_CURRENCY_MAP
from api.utils import apply_country_filter
from projects.filters import ProjectFilter, PromotionFilter, UnitFilter
from projects.models import Project, ProjectAgentAssignment, ProjectDocument, Promotion, Unit, project_image_upload_path
from projects.serializers import (
    AssignAgentsSerializer,
    ProjectSerializer,
    ProjectAgentAssignmentSerializer,
    PromotionSerializer,
    UnitSerializer,
)

from users.pagination import CustomPagination
from users.permissions import (
    IsCompanyAdmin,
    IsCompanyAdminOrTeamManager,
    IsCompanyProjectManager,
    IsNotSuperAdmin,
    IsSuperAdmin,
)

logger = logging.getLogger(__name__)


_image_param = openapi.Parameter(
    'image',
    openapi.IN_FORM,
    type=openapi.TYPE_FILE,
    required=True,
    description='Project image. Repeat this field to upload multiple images.',
)

_brochures_param = openapi.Parameter(
    'brochures',
    openapi.IN_FORM,
    type=openapi.TYPE_FILE,
    required=False,
    description='Project brochure file. Repeat this field to upload multiple brochures.',
)

_floor_plans_param = openapi.Parameter(
    'floor_plans',
    openapi.IN_FORM,
    type=openapi.TYPE_FILE,
    required=False,
    description='Project floor plan file. Repeat this field to upload multiple floor plans.',
)

_fact_checks_param = openapi.Parameter(
    'fact_checks',
    openapi.IN_FORM,
    type=openapi.TYPE_FILE,
    required=False,
    description='Project fact check file. Repeat this field to upload multiple fact checks.',
)

_common_form_params = [
    openapi.Parameter('title', openapi.IN_FORM, type=openapi.TYPE_STRING, required=True),
    openapi.Parameter('description', openapi.IN_FORM, type=openapi.TYPE_STRING, required=True),
    openapi.Parameter('location', openapi.IN_FORM, type=openapi.TYPE_STRING, required=True),
    openapi.Parameter('developer', openapi.IN_FORM, type=openapi.TYPE_STRING, required=True),
    openapi.Parameter('status', openapi.IN_FORM, type=openapi.TYPE_STRING, required=True, enum=['in_progress', 'ready', 'planned']),
    openapi.Parameter('starting_price', openapi.IN_FORM, type=openapi.TYPE_NUMBER, required=True),
    openapi.Parameter('yield_percentage', openapi.IN_FORM, type=openapi.TYPE_NUMBER, required=True),
    openapi.Parameter('project_type', openapi.IN_FORM, type=openapi.TYPE_STRING, required=True, enum=['residential', 'commercial', 'hospitality']),
    openapi.Parameter('property_category', openapi.IN_FORM, type=openapi.TYPE_STRING, required=False, description='e.g., Apartment, Villa, Studio'),
    openapi.Parameter('number_of_units', openapi.IN_FORM, type=openapi.TYPE_INTEGER, required=False),
    openapi.Parameter('bed_1', openapi.IN_FORM, type=openapi.TYPE_INTEGER, required=False),
    openapi.Parameter('bed_2', openapi.IN_FORM, type=openapi.TYPE_INTEGER, required=False),
]


class ProjectViewSet(viewsets.ModelViewSet):
    queryset = Project.objects.prefetch_related(
        'agent_assignments__agent',
        'documents',
        'units',
        'promotions',
    ).all()
    serializer_class = ProjectSerializer
    permission_classes = [permissions.IsAuthenticated, IsCompanyProjectManager]
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    http_method_names = ['get', 'post', 'patch', 'delete']

    pagination_class = CustomPagination

    filter_backends = [DjangoFilterBackend, OrderingFilter]
    filterset_class = ProjectFilter
    ordering_fields = ['id', 'created_at', 'updated_at', 'title', 'starting_price', 'status', 'project_status', 'project_type']

    _TAGS = ['Projects']

    def get_queryset(self):
        Promotion.sync_all_promotions()
        user = self.request.user
        if user.is_anonymous:
            return self.queryset.none()

        if user.is_staff and user.is_superuser:
            qs = self.queryset
        elif user.company_id:
            qs = self.queryset.filter(created_by__company_id=user.company_id)
        else:
            qs = self.queryset.none()

        # Country filter applies only to list-style views; retrieve/update/delete
        # must be able to access any project the user has permission to.
        if self.action in ('list', 'by_status', 'recent', 'by_budget'):
            qs = apply_country_filter(qs, user)
            # Filter only live projects for listing views (except by_status which allows explicit status selection)
            if self.action != 'by_status':
                qs = qs.filter(project_status='live')

        return qs.order_by('-created_at')

    def get_permissions(self):
        if self.action == 'by_company':
            return [permissions.IsAuthenticated(), IsSuperAdmin()]

        if self.action in ('assign_agents', 'remove_agent', 'list_assignments'):
            return [permissions.IsAuthenticated(), IsCompanyAdminOrTeamManager()]

        if self.action in ('toggle_visibility',):
            return [permissions.IsAuthenticated(), IsCompanyAdmin()]

        if self.action in ('by_budget',):
            return [permissions.IsAuthenticated(), IsNotSuperAdmin()]

        return super().get_permissions()

    def get_serializer_class(self):
        if self.action in (
            'upload_proposal_images',
            'proposal_images_status',
            'upload_images',
            'upload_brochures',
            'upload_floor_plans',
            'upload_fact_checks',
            'remove_proposal_image',
            'init_project',
        ):
            from rest_framework import serializers as _ser

            class _EmptySerializer(_ser.Serializer):
                pass
            return _EmptySerializer
        return super().get_serializer_class()


    def _create_project_documents(self, project):
        from projects.tasks import extract_document_text_task

        for field_name, label in (
            ('brochures', 'brochure'),
            ('floor_plans', 'floor_plan'),
            ('fact_checks', 'fact_checks'),
        ):
            for file_obj in self.request.FILES.getlist(field_name):
                document = ProjectDocument.objects.create(
                    project=project,
                    label=label,
                    file=file_obj,
                )
                # Extract and cache the PDF text in the background for chatbot use.
                try:
                    extract_document_text_task.delay(document.id)
                except Exception:
                    # Never fail the upload if the broker is unavailable.
                    logger.exception("Failed to enqueue document text extraction")

    def _get_uploaded_images(self):
        uploaded_images = list(self.request.FILES.getlist('image'))
        uploaded_images.extend(self.request.FILES.getlist('images'))
        return uploaded_images

    def _append_project_images(self, project, uploaded_images):
        if not uploaded_images:
            return

        current_images = project.image or []
        if isinstance(current_images, str):
            current_images = [current_images]

        stored_paths = []
        for file_obj in uploaded_images:
            stored_paths.append(
                default_storage.save(project_image_upload_path(project, file_obj.name), file_obj)
            )

        project.image = [*current_images, *stored_paths]
        project.save(update_fields=['image'])

    def perform_create(self, serializer):
        if not self.request.user.company_id:
            raise ValidationError({'detail': 'Your user account is not linked to a company.'})

        user_country = self.request.user.current_country or ''
        save_kwargs = {
            'created_by': self.request.user,
            'currency': COUNTRY_CURRENCY_MAP.get(user_country, ''),
        }
        # Auto-set associated_country from user's current_country (skip 'all')
        if user_country and user_country != 'all' and not serializer.validated_data.get('associated_country'):
            save_kwargs['associated_country'] = user_country
        serializer.save(**save_kwargs)

    @action(detail=False, methods=['get'], url_path=r'by_company/(?P<company_id>[^/.]+)')
    def by_company(self, request, company_id=None):
        projects = self.queryset.filter(
            created_by__company_id=company_id,
        ).order_by('-created_at')
        page = self.paginate_queryset(projects)
        serializer = self.get_serializer(page or projects, many=True)
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)

    @swagger_auto_schema(
        tags=_TAGS,
        manual_parameters=[
            *[p for p in _common_form_params],
            openapi.Parameter('search', openapi.IN_QUERY, type=openapi.TYPE_STRING, description='Search by title or description'),
            openapi.Parameter('ordering', openapi.IN_QUERY, type=openapi.TYPE_STRING, description='Order by: created_at, title, starting_price, status (prefix - for desc)'),
        ],
        consumes=['multipart/form-data'],
        operation_description=(
            "Create a project. Optionally include 'units_to_add' (array of unit objects) to create units alongside the project. "
            "Use the dedicated upload endpoints to attach images, brochures, floor plans, and fact checks after creation."
        ),
    )
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    @swagger_auto_schema(
        tags=_TAGS,
        manual_parameters=[
            openapi.Parameter('title', openapi.IN_FORM, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('description', openapi.IN_FORM, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('location', openapi.IN_FORM, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('developer', openapi.IN_FORM, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('status', openapi.IN_FORM, type=openapi.TYPE_STRING, required=False, enum=['in_progress', 'ready', 'planned']),
            openapi.Parameter('project_status', openapi.IN_FORM, type=openapi.TYPE_STRING, required=False, enum=['draft', 'live'], description='Project visibility status'),
            openapi.Parameter('starting_price', openapi.IN_FORM, type=openapi.TYPE_NUMBER, required=False),
            openapi.Parameter('yield_percentage', openapi.IN_FORM, type=openapi.TYPE_NUMBER, required=False),
            openapi.Parameter('project_type', openapi.IN_FORM, type=openapi.TYPE_STRING, required=False, enum=['residential', 'commercial', 'hospitality']),
            openapi.Parameter('property_category', openapi.IN_FORM, type=openapi.TYPE_STRING, required=False, description='e.g., Apartment, Villa, Studio'),
            openapi.Parameter('number_of_units', openapi.IN_FORM, type=openapi.TYPE_INTEGER, required=False),
            openapi.Parameter('bed_1', openapi.IN_FORM, type=openapi.TYPE_INTEGER, required=False),
            openapi.Parameter('bed_2', openapi.IN_FORM, type=openapi.TYPE_INTEGER, required=False),
        ],
        consumes=['multipart/form-data'],
        operation_description=(
            "Update a project. All fields are optional. "
            "You can also upload additional 'brochures', 'floor_plans', and 'fact_checks' files in the same multipart request. "
            "The project_status field allows explicit control over draft/live status (no automatic conversion)."
        ),
    )
    def partial_update(self, request, *args, **kwargs):
        response = super().partial_update(request, *args, **kwargs)
        response.data = self.get_serializer(self.get_object()).data
        return response

    @swagger_auto_schema(
        tags=_TAGS,
        manual_parameters=[
            openapi.Parameter('search', openapi.IN_QUERY, type=openapi.TYPE_STRING, description='Search by title or description'),
            openapi.Parameter('status', openapi.IN_QUERY, type=openapi.TYPE_STRING, description='in_progress | ready | planned'),
            openapi.Parameter('project_status', openapi.IN_QUERY, type=openapi.TYPE_STRING, description='draft | live'),
            openapi.Parameter('project_type', openapi.IN_QUERY, type=openapi.TYPE_STRING, description='residential | commercial | hospitality'),
            openapi.Parameter('ordering', openapi.IN_QUERY, type=openapi.TYPE_STRING, description='Order by: created_at, title, starting_price, status (prefix - for desc)'),
        ],
        operation_description="List all projects.",
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @swagger_auto_schema(
        tags=_TAGS,
        operation_description="Retrieve a single project.",
    )
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @swagger_auto_schema(
        tags=_TAGS,
        operation_description="Delete a project.",
    )
    def destroy(self, request, *args, **kwargs):
        return super().destroy(request, *args, **kwargs)

    @swagger_auto_schema(
        tags=_TAGS,
        manual_parameters=[
            openapi.Parameter(
                'index',
                openapi.IN_QUERY,
                type=openapi.TYPE_INTEGER,
                required=True,
                description='Zero-based image index to delete from the project image list.',
            ),
        ],
        responses={200: ProjectSerializer, 400: 'Validation error', 404: 'Not found'},
        operation_description='Delete a single project image by its index in the image array.',
    )
    @action(detail=True, methods=['delete'], url_path='remove_image')
    def remove_image(self, request, pk=None):
        project = self.get_object()
        index = request.query_params.get('index')
        if index is None:
            return Response(
                {'detail': 'index query parameter is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            image_index = int(index)
        except (TypeError, ValueError):
            return Response(
                {'detail': 'index must be an integer.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        images = project.image or []
        if image_index < 0 or image_index >= len(images):
            return Response(
                {'detail': 'Image not found for the given index.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        image_path = images.pop(image_index)
        default_storage.delete(image_path)
        project.image = images
        project.save(update_fields=['image'])
        project.refresh_from_db()

        return Response(self.get_serializer(project).data, status=status.HTTP_200_OK)

    @swagger_auto_schema(
        tags=_TAGS,
        responses={200: ProjectSerializer, 404: 'Not found'},
        operation_description='Delete a single brochure, floor plan, or fact check document by its ID.',
    )
    @action(detail=True, methods=['delete'], url_path='remove_document/(?P<document_id>[^/.]+)')
    def remove_document(self, request, pk=None, document_id=None):
        project = self.get_object()
        document = project.documents.filter(pk=document_id).first()
        if not document:
            return Response(
                {'detail': 'Document not found.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        file_path = document.file.name
        document_id = document.id
        document.delete()
        if file_path:
            default_storage.delete(file_path)

        try:
            from chatbot.pinecone_service import PineconeService
            PineconeService().delete_project_document(document_id)
        except Exception:
            logger.exception(
                "Failed to delete Pinecone vectors for project document %s",
                document_id,
            )

        project.refresh_from_db()

        return Response(self.get_serializer(project).data, status=status.HTTP_200_OK)

    # ── Image Upload Endpoint ──────────────────────────────────────────────

    @swagger_auto_schema(
        tags=_TAGS,
        manual_parameters=[
            openapi.Parameter(
                'image',
                openapi.IN_FORM,
                type=openapi.TYPE_FILE,
                required=True,
                description='Project image. Repeat this field to upload multiple images.',
            ),
        ],
        consumes=['multipart/form-data'],
        responses={200: ProjectSerializer, 400: 'Validation error', 404: 'Not found'},
        operation_description='Upload one or more images to an existing project.',
    )
    @action(
        detail=True,
        methods=['post'],
        url_path='upload_images',
        parser_classes=[MultiPartParser, FormParser],
    )
    def upload_images(self, request, pk=None):
        project = self.get_object()
        uploaded_images = self._get_uploaded_images()
        if not uploaded_images:
            return Response(
                {'detail': 'At least one image file is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        self._append_project_images(project, uploaded_images)
        project.refresh_from_db()
        return Response(ProjectSerializer(project, context=self.get_serializer_context()).data, status=status.HTTP_200_OK)

    # ── Document Upload Endpoints ──────────────────────────────────────────

    def _upload_documents_for_label(self, request, label, field_name):
        from projects.tasks import extract_document_text_task

        project = self.get_object()
        files = request.FILES.getlist(field_name)
        if not files:
            return Response(
                {'detail': f'At least one {field_name} file is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        for file_obj in files:
            document = ProjectDocument.objects.create(
                project=project,
                label=label,
                file=file_obj,
            )
            try:
                extract_document_text_task.delay(document.id)
            except Exception:
                logger.exception("Failed to enqueue document text extraction")

        project.refresh_from_db()
        return Response(ProjectSerializer(project, context=self.get_serializer_context()).data, status=status.HTTP_200_OK)

    @swagger_auto_schema(
        tags=_TAGS,
        manual_parameters=[
            openapi.Parameter(
                'brochures',
                openapi.IN_FORM,
                type=openapi.TYPE_FILE,
                required=True,
                description='Project brochure file. Repeat this field to upload multiple brochures.',
            ),
        ],
        consumes=['multipart/form-data'],
        responses={200: ProjectSerializer, 400: 'Validation error', 404: 'Not found'},
        operation_description='Upload one or more brochure files to an existing project.',
    )
    @action(
        detail=True,
        methods=['post'],
        url_path='upload_brochures',
        parser_classes=[MultiPartParser, FormParser],
    )
    def upload_brochures(self, request, pk=None):
        return self._upload_documents_for_label(request, 'brochure', 'brochures')

    @swagger_auto_schema(
        tags=_TAGS,
        manual_parameters=[
            openapi.Parameter(
                'floor_plans',
                openapi.IN_FORM,
                type=openapi.TYPE_FILE,
                required=True,
                description='Project floor plan file. Repeat this field to upload multiple floor plans.',
            ),
        ],
        consumes=['multipart/form-data'],
        responses={200: ProjectSerializer, 400: 'Validation error', 404: 'Not found'},
        operation_description='Upload one or more floor plan files to an existing project.',
    )
    @action(
        detail=True,
        methods=['post'],
        url_path='upload_floor_plans',
        parser_classes=[MultiPartParser, FormParser],
    )
    def upload_floor_plans(self, request, pk=None):
        return self._upload_documents_for_label(request, 'floor_plan', 'floor_plans')

    @swagger_auto_schema(
        tags=_TAGS,
        manual_parameters=[
            openapi.Parameter(
                'fact_checks',
                openapi.IN_FORM,
                type=openapi.TYPE_FILE,
                required=True,
                description='Project fact check file. Repeat this field to upload multiple fact checks.',
            ),
        ],
        consumes=['multipart/form-data'],
        responses={200: ProjectSerializer, 400: 'Validation error', 404: 'Not found'},
        operation_description='Upload one or more fact check files to an existing project.',
    )
    @action(
        detail=True,
        methods=['post'],
        url_path='upload_fact_checks',
        parser_classes=[MultiPartParser, FormParser],
    )
    def upload_fact_checks(self, request, pk=None):
        return self._upload_documents_for_label(request, 'fact_checks', 'fact_checks')

    # ── Init Draft Project ─────────────────────────────────────────────────

    @swagger_auto_schema(
        tags=_TAGS,
        responses={
            201: openapi.Response(
                description='Draft project created',
                schema=openapi.Schema(
                    type=openapi.TYPE_OBJECT,
                    properties={
                        'id': openapi.Schema(type=openapi.TYPE_INTEGER),
                        'project_status': openapi.Schema(type=openapi.TYPE_STRING),
                    },
                ),
            ),
        },
        operation_description=(
            'Create an empty draft project and return its ID. '
            'Use this ID to upload proposal images, then PATCH the project to fill in details '
            '(which will automatically publish it as live).'
        ),
    )
    @action(detail=False, methods=['post'], url_path='init_project')
    def init_project(self, request):
        if not request.user.company_id:
            raise ValidationError({'detail': 'Your user account is not linked to a company.'})

        user_country = request.user.current_country or ''
        # Auto-set associated_country from user's current_country (skip 'all')
        associated_country = user_country if user_country and user_country != 'all' else ''
        project = Project.objects.create(
            title='',
            description='',
            location='',
            developer='',
            starting_price=0,
            yield_percentage=0,
            project_type=Project.ProjectType.RESIDENTIAL,
            status=Project.Status.PLANNED,
            project_status=Project.ProjectStatus.DRAFT,
            created_by=request.user,
            currency=COUNTRY_CURRENCY_MAP.get(user_country, ''),
            associated_country=associated_country,
        )
        return Response(
            {'id': project.id, 'project_status': project.project_status},
            status=status.HTTP_201_CREATED,
        )

    # ── Projects by publish status ─────────────────────────────────────────

    @swagger_auto_schema(
        tags=_TAGS,
        manual_parameters=[
            openapi.Parameter(
                'project_status',
                openapi.IN_QUERY,
                type=openapi.TYPE_STRING,
                required=True,
                description='draft | live',
            ),
            openapi.Parameter(
                'project_type',
                openapi.IN_QUERY,
                type=openapi.TYPE_STRING,
                required=False,
                description='residential | commercial | hospitality',
            ),
            openapi.Parameter(
                'ordering',
                openapi.IN_QUERY,
                type=openapi.TYPE_STRING,
                required=False,
                description='Ordering field, e.g. -id, -created_at, starting_price',
            ),
        ],
        responses={200: ProjectSerializer(many=True)},
        operation_description='List projects filtered by publish status (draft or live), project_type, ordering, etc.',
    )
    @action(detail=False, methods=['get'], url_path='by_status')
    def by_status(self, request):
        project_status = request.query_params.get('project_status', '').strip()
        if project_status not in (Project.ProjectStatus.DRAFT, Project.ProjectStatus.LIVE):
            return Response(
                {'detail': 'project_status must be "draft" or "live".'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        qs = self.get_queryset().filter(project_status=project_status)
        qs = self.filter_queryset(qs)
        page = self.paginate_queryset(qs)
        if page is not None:
            return self.get_paginated_response(ProjectSerializer(page, many=True, context=self.get_serializer_context()).data)
        return Response(ProjectSerializer(qs, many=True, context=self.get_serializer_context()).data)

# ── Recent projects ───────────────────────────────────────────────────

    @swagger_auto_schema(
        tags=_TAGS,
        manual_parameters=[
            openapi.Parameter(
                'limit',
                openapi.IN_QUERY,
                type=openapi.TYPE_INTEGER,
                required=False,
                description='Number of recent projects to return. Defaults to 3.',
            ),
        ],
        responses={200: ProjectSerializer(many=True)},
        operation_description='Return the most recently added live projects (default top 3).',
    )
    @action(detail=False, methods=['get'], url_path='recent')
    def recent(self, request):
        try:
            limit = int(request.query_params.get('limit', 3))
            if limit <= 0:
                limit = 3
        except (TypeError, ValueError):
            limit = 3

        qs = (
            self.get_queryset()
            .filter(project_status=Project.ProjectStatus.LIVE)
            .order_by('-created_at')[:limit]
        )
        return Response(
            ProjectSerializer(qs, many=True, context=self.get_serializer_context()).data
        )

# ── Projects by budget ────────────────────────────────────────────────

    @swagger_auto_schema(
        tags=_TAGS,
        manual_parameters=[
            openapi.Parameter(
                'estimated_budget',
                openapi.IN_QUERY,
                type=openapi.TYPE_NUMBER,
                required=False,
                description='Return projects that have at least one unit with list_price <= estimated_budget.',
            ),
            openapi.Parameter(
                'location',
                openapi.IN_QUERY,
                type=openapi.TYPE_STRING,
                required=False,
                description=(
                    'Comma-separated location keywords (case-insensitive). '
                    'Example: `dubai, uk`. A project matches if its location '
                    'contains any of the given keywords.'
                ),
            ),
            openapi.Parameter(
                'category',
                openapi.IN_QUERY,
                type=openapi.TYPE_STRING,
                required=False,
                description='Match project `property_category` (case-insensitive). Example: `Apartment`.',
            ),
            openapi.Parameter(
                'type',
                openapi.IN_QUERY,
                type=openapi.TYPE_STRING,
                required=False,
                description='Match unit `category` (case-insensitive). Example: `1 Bed`.',
            ),
        ],
        responses={200: ProjectSerializer(many=True)},
        operation_description=(
            'Filter projects by `estimated_budget`, `location`, `category` and '
            '`type`. All are optional. If none are provided, returns all projects '
            'scoped to the current country. Budget matches projects with at least '
            'one unit whose `list_price` is <= the given value. Location accepts '
            'a comma-separated list and matches case-insensitively. `category` '
            'matches the project\'s `property_category`. `type` matches a unit\'s '
            '`category`.'
        ),
    )
    @action(detail=False, methods=['get'], url_path='by_budget')
    def by_budget(self, request):
        budget_str = request.query_params.get('estimated_budget', '').strip()
        location_str = request.query_params.get('location', '').strip()
        category_str = request.query_params.get('category', '').strip()
        type_str = request.query_params.get('type', '').strip()

        qs = self.get_queryset()
        qs = qs.filter(project_status=Project.ProjectStatus.LIVE)

        # Only show projects that have at least one available unit
        qs = qs.filter(units__status='available')

        if budget_str:
            try:
                from decimal import Decimal, InvalidOperation
                budget = Decimal(budget_str)
            except (InvalidOperation, ValueError):
                return Response(
                    {'detail': 'estimated_budget must be a valid number.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            qs = qs.filter(units__list_price__lte=budget)

        if location_str:
            import re
            terms = [t for t in re.split(r'[,\s]+', location_str) if t]
            if terms:
                location_q = Q()
                for term in terms:
                    location_q |= Q(location__icontains=term)
                qs = qs.filter(location_q)

        if category_str:
            qs = qs.filter(property_category__iexact=category_str)

        if type_str:
            qs = qs.filter(units__category__iexact=type_str)

        qs = qs.distinct()
        page = self.paginate_queryset(qs)
        if page is not None:
            return self.get_paginated_response(
                ProjectSerializer(page, many=True, context=self.get_serializer_context()).data
            )
        return Response(ProjectSerializer(qs, many=True, context=self.get_serializer_context()).data)

    # ── Proposal Images Upload ─────────────────────────────────────────────

    @swagger_auto_schema(
        tags=_TAGS,
        manual_parameters=[
            openapi.Parameter(
                'project_id',
                openapi.IN_FORM,
                type=openapi.TYPE_INTEGER,
                required=True,
                description='ID of the existing project to upload images to (use POST /init_project/ to get one).',
            ),
            openapi.Parameter(
                'exterior',
                openapi.IN_FORM,
                type=openapi.TYPE_FILE,
                required=False,
                description='Exterior image(s). Repeat this field to upload multiple files for this label.',
            ),
            openapi.Parameter(
                'master_bedroom',
                openapi.IN_FORM,
                type=openapi.TYPE_FILE,
                required=False,
                description='Master bedroom image(s). Repeat this field to upload multiple files for this label.',
            ),
            openapi.Parameter(
                'kitchen_dining',
                openapi.IN_FORM,
                type=openapi.TYPE_FILE,
                required=False,
                description='Kitchen & dining image(s). Repeat this field to upload multiple files for this label.',
            ),
            openapi.Parameter(
                '1_bed',
                openapi.IN_FORM,
                type=openapi.TYPE_FILE,
                required=False,
                description='1-bed apartment image(s). Repeat this field to upload multiple files for this label.',
            ),
        ],
        consumes=['multipart/form-data'],
        responses={
            202: openapi.Response(
                description='Upload queued',
                schema=openapi.Schema(
                    type=openapi.TYPE_OBJECT,
                    properties={
                        'task_id': openapi.Schema(type=openapi.TYPE_STRING),
                        'project_id': openapi.Schema(type=openapi.TYPE_INTEGER),
                        'status': openapi.Schema(type=openapi.TYPE_STRING),
                        'uploaded_count': openapi.Schema(type=openapi.TYPE_INTEGER),
                        'detail': openapi.Schema(type=openapi.TYPE_STRING),
                    },
                ),
            ),
            400: 'Validation error',
        },
        operation_description=(
            "Upload proposal images to an existing project (use POST /init_project/ first to get the project_id).\n\n"
            "Pass `project_id` as a form field alongside the image files."
        ),
    )
    @action(
        detail=False,
        methods=['post'],
        url_path='upload_proposal_images',
        parser_classes=[MultiPartParser, FormParser],
    )
    def upload_proposal_images(self, request):
        from projects.tasks import upload_proposal_images_task

        # Require an existing project_id.
        project_id = request.data.get('project_id') or request.query_params.get('project_id')
        if not project_id:
            return Response(
                {'detail': 'project_id is required. Create a draft project first via POST /init_project/.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            project = Project.objects.get(pk=int(project_id))
        except (Project.DoesNotExist, ValueError, TypeError):
            return Response(
                {'detail': f'Project with id {project_id} not found.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Collect: {label: [UploadedFile, ...]} from request.FILES — the field name IS the label.
        grouped_files = {}
        for field_name in request.FILES.keys():
            label = (field_name or '').strip()
            if not label:
                continue
            files = request.FILES.getlist(field_name)
            if files:
                grouped_files.setdefault(label, []).extend(files)

        if not grouped_files:
            return Response(
                {'detail': 'At least one image file is required. Use the label as the form field name '
                           '(e.g. `exterior`, `master_bedroom`). Repeat the same field name to upload '
                           'multiple files for that label.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Save files directly to S3 in the view — avoids encoding binary data
        # through Redis. Only lightweight path strings are passed to the task.
        title_slug = project.title.replace(' ', '_').lower()
        saved_files = []
        for label, files in grouped_files.items():
            for file_obj in files:
                label_slug = label.lower().replace(' ', '_')
                storage_path = (
                    f'projects/{project.id}_{title_slug}'
                    f'/proposal_images/{label_slug}/{file_obj.name}'
                )
                saved_path = default_storage.save(storage_path, file_obj)
                saved_files.append({'label': label, 'saved_path': saved_path})

        task = upload_proposal_images_task.delay(project.id, saved_files)

        return Response(
            {
                'task_id': task.id,
                'project_id': project.id,
                'status': 'queued',
                'uploaded_count': len(saved_files),
                'detail': (
                    'Proposal image upload queued. Poll '
                    '`/development_portfolio/proposal_images_status/{task_id}/` '
                    'until status is "completed".'
                ),
            },
            status=status.HTTP_202_ACCEPTED,
        )

    @swagger_auto_schema(
        tags=_TAGS,
        manual_parameters=[
            openapi.Parameter(
                'task_id',
                openapi.IN_PATH,
                type=openapi.TYPE_STRING,
                required=True,
                description='Celery task ID returned from `upload_proposal_images`.',
            ),
        ],
        responses={
            200: openapi.Response(
                description='Task status',
                schema=openapi.Schema(
                    type=openapi.TYPE_OBJECT,
                    properties={
                        'task_id': openapi.Schema(type=openapi.TYPE_STRING),
                        'task_state': openapi.Schema(type=openapi.TYPE_STRING),
                        'task_ready': openapi.Schema(type=openapi.TYPE_BOOLEAN),
                        'task_successful': openapi.Schema(type=openapi.TYPE_BOOLEAN),
                        'task_result': openapi.Schema(type=openapi.TYPE_OBJECT),
                        'project': openapi.Schema(type=openapi.TYPE_OBJECT),
                    },
                ),
            ),
        },
        operation_description=(
            'Check the status of a proposal images upload task. '
            'Returns the Celery task state and, once the project has been saved, '
            'the full Project payload (including hosted `proposal_images` URLs).'
        ),
    )
    @action(
        detail=False,
        methods=['get'],
        url_path='proposal_images_status/(?P<task_id>[^/.]+)',
    )
    def proposal_images_status(self, request, task_id=None):
        from celery.result import AsyncResult

        result = AsyncResult(task_id)

        is_ready = result.ready()
        is_successful = result.successful() if is_ready else None

        task_result = None
        project = None

        if is_ready and not isinstance(result.result, Exception):
            task_result = result.result
            if isinstance(task_result, dict) and task_result.get('project_id'):
                project = Project.objects.filter(pk=task_result['project_id']).first()

        payload = {
            'task_id': task_id,
            'task_state': result.state,
            'task_ready': is_ready,
            'task_successful': is_successful,
            'task_result': task_result,
            'project': (
                ProjectSerializer(project, context=self.get_serializer_context()).data
                if project else None
            ),
        }
        return Response(payload, status=status.HTTP_200_OK)

    @swagger_auto_schema(
        tags=_TAGS,
        manual_parameters=[
            openapi.Parameter(
                'label',
                openapi.IN_QUERY,
                type=openapi.TYPE_STRING,
                required=True,
                description='The proposal image label (e.g. exterior, bedroom, 2_bed).',
            ),
            openapi.Parameter(
                'index',
                openapi.IN_QUERY,
                type=openapi.TYPE_INTEGER,
                required=True,
                description='Zero-based index of the image within that label to delete.',
            ),
        ],
        responses={200: ProjectSerializer, 400: 'Validation error', 404: 'Not found'},
        operation_description=(
            'Delete a single proposal image by label and index.\n\n'
            'Pass the label (e.g. `exterior`) and the zero-based index of the image '
            'within that label list to remove it from S3 and from the project record.'
        ),
    )
    @action(detail=True, methods=['delete'], url_path='remove_proposal_image')
    def remove_proposal_image(self, request, pk=None):
        project = self.get_object()

        label = request.query_params.get('label', '').strip()
        if not label:
            return Response(
                {'detail': 'label query parameter is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        index_str = request.query_params.get('index')
        if index_str is None:
            return Response(
                {'detail': 'index query parameter is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            index = int(index_str)
        except (TypeError, ValueError):
            return Response(
                {'detail': 'index must be an integer.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        proposal_images = project.proposal_images or {}
        images_for_label = proposal_images.get(label)

        if not images_for_label:
            return Response(
                {'detail': f'No proposal images found for label "{label}".'},
                status=status.HTTP_404_NOT_FOUND,
            )

        if index < 0 or index >= len(images_for_label):
            return Response(
                {'detail': f'Image not found at index {index} for label "{label}".'},
                status=status.HTTP_404_NOT_FOUND,
            )

        image_url = images_for_label.pop(index)

        # Delete from S3 — extract the storage key from the URL.
        try:
            from urllib.parse import urlparse
            parsed = urlparse(image_url)
            # S3 key is the path without the leading slash, strip /media/ prefix if present.
            storage_key = parsed.path.lstrip('/')
            # If the bucket stores files under a media/ prefix, remove it.
            if storage_key.startswith('media/'):
                storage_key = storage_key[len('media/'):]
            default_storage.delete(storage_key)
        except Exception:
            logger.exception("Failed to delete proposal image from storage: %s", image_url)

        # Remove the label key entirely if no images remain.
        if images_for_label:
            proposal_images[label] = images_for_label
        else:
            del proposal_images[label]

        project.proposal_images = proposal_images
        project.save(update_fields=['proposal_images'])
        project.refresh_from_db()

        return Response(
            ProjectSerializer(project, context=self.get_serializer_context()).data,
            status=status.HTTP_200_OK,
        )

    # ── Agent Assignments ──────────────────────────────────────────────────

    @swagger_auto_schema(
        tags=_TAGS,
        request_body=AssignAgentsSerializer,
        responses={
            200: ProjectAgentAssignmentSerializer(many=True),
            400: "Validation error",
        },
        operation_description=(
            "Assign agents to this project.\n\n"
            "- Each assignment requires `agent`, `agent_split`, and `company_split`.\n"
            "- `agent_split + company_split` must equal 100.\n"
            "- Re-submitting an existing agent updates their split values."
        ),
    )
    @action(detail=True, methods=['post'], url_path='assign_agents')
    def assign_agents(self, request, pk=None):
        project = self.get_object()
        serializer = AssignAgentsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        assignments = []
        for item in serializer.validated_data['assignments']:
            assignment, _ = ProjectAgentAssignment.objects.update_or_create(
                agent=item['agent'],
                project=project,
                defaults={
                    'agent_split': item['agent_split'],
                    'company_split': item['company_split'],
                },
            )
            assignments.append(assignment)

        return Response(
            ProjectAgentAssignmentSerializer(assignments, many=True).data,
            status=status.HTTP_200_OK,
        )

    @swagger_auto_schema(
        tags=_TAGS,
        manual_parameters=[
            openapi.Parameter('agent_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True),
        ],
        responses={204: "Agent unassigned", 404: "Not found"},
        operation_description="Remove an agent from this project.",
    )
    @action(detail=True, methods=['delete'], url_path='remove_agent')
    def remove_agent(self, request, pk=None):
        project = self.get_object()
        agent_id = request.query_params.get('agent_id')
        if not agent_id:
            return Response(
                {"detail": "agent_id query parameter is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        deleted, _ = ProjectAgentAssignment.objects.filter(
            project=project, agent_id=agent_id,
        ).delete()

        if not deleted:
            return Response(
                {"detail": "Assignment not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(
            {"detail": "Agent removed from project."},
            status=status.HTTP_204_NO_CONTENT,
        )

    @swagger_auto_schema(
        tags=_TAGS,
        responses={200: ProjectAgentAssignmentSerializer(many=True)},
        operation_description="List all agent assignments for this project.",
    )
    @action(detail=True, methods=['get'], url_path='agent_assignments')
    def list_assignments(self, request, pk=None):
        project = self.get_object()
        assignments = project.agent_assignments.select_related('agent').all()
        return Response(
            ProjectAgentAssignmentSerializer(assignments, many=True).data,
        )

    @swagger_auto_schema(
        tags=_TAGS,
        responses={200: ProjectSerializer},
        operation_description=(
            "Toggle project visibility for the current company admin. "
            "Projects are visible by default. Toggling once restricts the project "
            "to the admin's company; toggling again makes it globally visible."
        ),
    )
    @action(detail=True, methods=['post'], url_path='toggle_visibility')
    def toggle_visibility(self, request, pk=None):
        project = self.get_object()
        company = request.user.company

        if project.visible_to_companies.filter(id=company.id).exists():
            project.visible_to_companies.remove(company)
        else:
            project.visible_to_companies.add(company)

        serializer = self.get_serializer(project)
        return Response(serializer.data, status=status.HTTP_200_OK)


class UnitViewSet(viewsets.ModelViewSet):
    queryset = Unit.objects.all()
    serializer_class = UnitSerializer
    permission_classes = [permissions.IsAuthenticated, IsCompanyProjectManager]

    def get_queryset(self):
        user = self.request.user
        if user.is_anonymous:
            return self.queryset.none()
        if user.is_staff and user.is_superuser:
            return self.queryset
        if user.company_id:
            return self.queryset.filter(project__created_by__company_id=user.company_id)
        return self.queryset.none()
    http_method_names = ['get', 'post', 'patch', 'delete']
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    filter_backends = [DjangoFilterBackend, OrderingFilter]
    filterset_class = UnitFilter
    ordering_fields = ['created_at', 'list_price', 'area_m2', 'label']
    ordering = ['label']

    _TAGS = ["Projects -> Units"]

    def get_serializer_class(self):
        if self.action in ('import_from_csv', 'export_csv'):
            from rest_framework import serializers
            class _EmptySerializer(serializers.Serializer):
                pass
            return _EmptySerializer
        return super().get_serializer_class()

    def perform_create(self, serializer):
        serializer.save(
            created_by=self.request.user,
            currency=COUNTRY_CURRENCY_MAP.get(self.request.user.current_country, ''),
        )

    @swagger_auto_schema(tags=_TAGS)
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @swagger_auto_schema(tags=_TAGS)
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @swagger_auto_schema(
        tags=_TAGS,
        consumes=['multipart/form-data'],
        manual_parameters=[
            openapi.Parameter('project', openapi.IN_FORM, type=openapi.TYPE_INTEGER, required=True, description='Project ID'),
            openapi.Parameter('label', openapi.IN_FORM, type=openapi.TYPE_STRING, required=True, description='Unit label e.g. A-101'),
            openapi.Parameter('category', openapi.IN_FORM, type=openapi.TYPE_STRING, required=False, description='e.g. 1 Bed, 2 Bed, Studio'),
            openapi.Parameter('floor', openapi.IN_FORM, type=openapi.TYPE_STRING, required=False, description='e.g. Ground Floor'),
            openapi.Parameter('area_m2', openapi.IN_FORM, type=openapi.TYPE_NUMBER, required=False, description='Area in m²'),
            openapi.Parameter('area_ft2', openapi.IN_FORM, type=openapi.TYPE_NUMBER, required=False, description='Area in ft²'),
            openapi.Parameter('list_price', openapi.IN_FORM, type=openapi.TYPE_NUMBER, required=True, description='List price'),
            openapi.Parameter('discounted_price', openapi.IN_FORM, type=openapi.TYPE_NUMBER, required=False, description='Discounted / selling price'),
            openapi.Parameter('est_market_rent', openapi.IN_FORM, type=openapi.TYPE_NUMBER, required=False, description='Estimated market rent (PCM)'),
            openapi.Parameter('est_yield_gross', openapi.IN_FORM, type=openapi.TYPE_NUMBER, required=False, description='Estimated gross yield in %'),
            openapi.Parameter('status', openapi.IN_FORM, type=openapi.TYPE_STRING, required=True, enum=['available', 'reserved', 'sold']),
            openapi.Parameter('floor_plan_image', openapi.IN_FORM, type=openapi.TYPE_FILE, required=False, description='Floor plan image (PNG/JPG)'),
        ],
    )
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    @swagger_auto_schema(
        tags=_TAGS,
        consumes=['multipart/form-data'],
        manual_parameters=[
            openapi.Parameter('project', openapi.IN_FORM, type=openapi.TYPE_INTEGER, required=False, description='Project ID'),
            openapi.Parameter('label', openapi.IN_FORM, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('category', openapi.IN_FORM, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('floor', openapi.IN_FORM, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('area_m2', openapi.IN_FORM, type=openapi.TYPE_NUMBER, required=False),
            openapi.Parameter('area_ft2', openapi.IN_FORM, type=openapi.TYPE_NUMBER, required=False),
            openapi.Parameter('list_price', openapi.IN_FORM, type=openapi.TYPE_NUMBER, required=False),
            openapi.Parameter('discounted_price', openapi.IN_FORM, type=openapi.TYPE_NUMBER, required=False, description='Discounted / selling price'),
            openapi.Parameter('est_market_rent', openapi.IN_FORM, type=openapi.TYPE_NUMBER, required=False, description='Estimated market rent (PCM)'),
            openapi.Parameter('est_yield_gross', openapi.IN_FORM, type=openapi.TYPE_NUMBER, required=False, description='Estimated gross yield in %'),
            openapi.Parameter('status', openapi.IN_FORM, type=openapi.TYPE_STRING, required=False, enum=['available', 'reserved', 'sold']),
            openapi.Parameter('floor_plan_image', openapi.IN_FORM, type=openapi.TYPE_FILE, required=False, description='Floor plan image (PNG/JPG)'),
        ],
    )
    def partial_update(self, request, *args, **kwargs):
        return super().partial_update(request, *args, **kwargs)

    @swagger_auto_schema(tags=_TAGS)
    def destroy(self, request, *args, **kwargs):
        return super().destroy(request, *args, **kwargs)

    @swagger_auto_schema(
        tags=_TAGS,
        manual_parameters=[
            openapi.Parameter(
                'project_id', openapi.IN_FORM, type=openapi.TYPE_INTEGER, required=True,
                description='Project ID to import units into.',
            ),
            openapi.Parameter(
                'file', openapi.IN_FORM, type=openapi.TYPE_FILE, required=True,
                description=(
                    'CSV, XLSX or XLS file with a header row. Required columns: '
                    '"Unit Label/Name", "Type", "Floor", "Area (sq ft)", "Price", "Status". '
                    'Optional columns: "Discounted Price (Selling Price)", '
                    '"EST. MARKET RENT (PCM)", "EST. YIELD (GROSS)". '
                    'Any extra columns (e.g. "Project", "Notes") are ignored.'
                ),
            ),
        ],
        consumes=['multipart/form-data'],
        operation_description=(
            'Import units into a project from a CSV, XLSX or XLS file. '
            'The file must contain at least the required columns. '
            'No LLM is used — the file is parsed directly.'
        ),
        responses={
            200: openapi.Response(
                description='Import summary',
                schema=openapi.Schema(
                    type=openapi.TYPE_OBJECT,
                    properties={
                        'created': openapi.Schema(type=openapi.TYPE_INTEGER),
                        'failed': openapi.Schema(type=openapi.TYPE_INTEGER),
                        'errors': openapi.Schema(type=openapi.TYPE_ARRAY, items=openapi.Schema(type=openapi.TYPE_STRING)),
                    },
                ),
            ),
            400: 'Validation error',
        },
    )
    @action(detail=False, methods=['post'], url_path='import-from-csv', parser_classes=[MultiPartParser, FormParser])
    def import_from_csv(self, request):
        from projects.utils import import_units

        project_id = request.data.get('project_id')
        csv_file = request.FILES.get('file')

        if not project_id:
            return Response({'detail': 'project_id is required.'}, status=status.HTTP_400_BAD_REQUEST)
        if not csv_file:
            return Response({'detail': 'file is required.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            project = Project.objects.get(pk=project_id)
        except Project.DoesNotExist:
            return Response({'detail': 'Project not found.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            result = import_units(
                csv_file,
                project=project,
                requested_by=request.user,
                filename=getattr(csv_file, 'name', ''),
            )
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(result, status=status.HTTP_200_OK)

    @swagger_auto_schema(
        tags=_TAGS,
        manual_parameters=[
            openapi.Parameter(
                'project_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=False,
                description='Optional. Filter exported units by project ID.',
            ),
        ],
        operation_description=(
            'Export units to a CSV file. Columns: "Unit Label/Name", "Type", '
            '"Floor", "Area (sq ft)", "Price", "Discounted Price (Selling Price)", "Status", '
            '"EST. MARKET RENT (PCM)", "EST. YIELD (GROSS)".'
        ),
        responses={200: 'CSV file download'},
    )
    @action(detail=False, methods=['get'], url_path='export-csv')
    def export_csv(self, request):
        import csv
        from django.http import StreamingHttpResponse

        qs = self.filter_queryset(self.get_queryset())
        project_id = request.query_params.get('project_id')
        if project_id:
            qs = qs.filter(project_id=project_id)

        header = [
            'Unit Label/Name', 'Type', 'Floor', 'Area (sq ft)',
            'Price', 'Discounted Price (Selling Price)', 'Status',
            'EST. MARKET RENT (PCM)', 'EST. YIELD (GROSS)',
        ]

        class _Echo:
            def write(self, value: str) -> str:
                return value

        def _rows():
            yield header
            for u in qs.iterator():
                yield [
                    u.label,
                    u.category or '',
                    u.floor or '',
                    '' if u.area_ft2 is None else u.area_ft2,
                    '' if u.list_price is None else u.list_price,
                    '' if u.discounted_price is None else u.discounted_price,
                    u.status,
                    '' if u.est_market_rent is None else u.est_market_rent,
                    '' if u.est_yield_gross is None else u.est_yield_gross,
                ]

        writer = csv.writer(_Echo())
        response = StreamingHttpResponse(
            (writer.writerow(row) for row in _rows()),
            content_type='text/csv',
        )
        response['Content-Disposition'] = 'attachment; filename="units.csv"'
        return response


class PromotionViewSet(viewsets.ModelViewSet):
    queryset = Promotion.objects.all()
    serializer_class = PromotionSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = CustomPagination
    filter_backends = [DjangoFilterBackend, OrderingFilter]
    filterset_class = PromotionFilter
    ordering_fields = ['id', 'created_at', 'updated_at', 'start_date', 'end_date', 'discount']
    ordering = ['-created_at']

    _TAGS = ['Promotions']

    def get_queryset(self):
        Promotion.sync_all_promotions()
        qs = super().get_queryset()
        project_id = self.request.query_params.get('project_id')
        if project_id:
            qs = qs.filter(project_id=project_id)
        search = self.request.query_params.get('search')
        if search:
            qs = qs.filter(Q(title__icontains=search) | Q(project__title__icontains=search))
        return qs

    def list(self, request, *args, **kwargs):
        Promotion.sync_all_promotions()
        queryset = self.filter_queryset(self.get_queryset())

        total_promotions = queryset.count()
        active_promotions = queryset.filter(status=Promotion.Status.ACTIVE).count()
        upcoming_promotions = queryset.filter(status=Promotion.Status.UPCOMING).count()
        expired_promotions = queryset.filter(status=Promotion.Status.EXPIRED).count()

        from users.models import Lead
        total_leads = 0
        for promo in queryset:
            total_leads += Lead.objects.filter(
                Q(project=promo.project) | Q(projects=promo.project),
                created_at__gte=promo.start_date,
                created_at__lte=promo.end_date,
            ).distinct().count()

        stats = {
            'total_promotions': total_promotions,
            'active_promotions': active_promotions,
            'upcoming_promotions': upcoming_promotions,
            'expired_promotions': expired_promotions,
            'total_leads_generated': total_leads,
        }

        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            response = self.get_paginated_response(serializer.data)
            response.data['stats'] = stats
            return response

        serializer = self.get_serializer(queryset, many=True)
        return Response({
            'stats': stats,
            'results': serializer.data,
        })

    def perform_create(self, serializer):
        promo = serializer.save(created_by=self.request.user if self.request.user.is_authenticated else None)
        promo.sync_status()

    def perform_update(self, serializer):
        instance = self.get_object()
        if instance.status == Promotion.Status.ACTIVE or instance.original_prices:
            instance.deactivate(target_status=Promotion.Status.UPCOMING)
        promo = serializer.save()
        promo.sync_status()

    def perform_destroy(self, instance):
        if instance.status == Promotion.Status.ACTIVE or instance.original_prices:
            instance.deactivate(target_status=Promotion.Status.EXPIRED)
        instance.delete()

