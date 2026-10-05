from __future__ import annotations

import csv
import logging
from time import monotonic
from typing import Any, Callable

from django.db import transaction
from django.http import FileResponse, HttpResponse
from django.shortcuts import get_object_or_404
from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema
from rest_framework import permissions, status
from rest_framework.exceptions import (
    APIException,
    AuthenticationFailed,
    NotAuthenticated,
    Throttled,
)
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import Serializer
from rest_framework.views import APIView

from calling_agent.action_services import (
    LeadUpdateActionService,
    TaskActionService,
)
from calling_agent.auth import ElevenLabsToolAuthentication
from calling_agent.exceptions import (
    CallAuthorizationError,
    CallConflictError,
    CallingConfigurationError,
    ElevenLabsRequestError,
    ElevenLabsResponseError,
    ElevenLabsTransientError,
    InvalidPhoneNumberError,
    LeadNotCallableError,
    ToolActionRejectedError,
    ToolContextUnavailableError,
    ToolServiceUnavailableError,
)
from calling_agent.models import Call
from calling_agent.serializers import (
    CallAnalyticsSerializer,
    CallEndSerializer,
    CallSerializer,
    CallSerializerDetails,
    CreateTaskToolSerializer,
    EmptyToolSerializer,
    KnowledgeSearchToolSerializer,
    ManualCallInitiationSerializer,
    ProjectSearchToolSerializer,
    RequestProposalToolSerializer,
    ResolveTransferToolSerializer,
    UnitSearchToolSerializer,
    UpdateLeadToolSerializer,
)
from calling_agent.services import (
    CallAnalyticsService,
    CallTerminationService,
    ManualCallInitiationService,
    calls_visible_to_user,
    is_call_owned_by_user,
)
from calling_agent.storage import get_call_recording_storage
from calling_agent.tasks import process_webhook_event_task
from calling_agent.throttles import (
    CallToolThrottle,
    KnowledgeToolThrottle,
)
from calling_agent.tool_services import (
    KnowledgeSearchToolService,
    LeadContextToolService,
    ProjectContextToolService,
    ProjectSearchToolService,
    RequestProposalToolService,
    UnitSearchToolService,
)
from calling_agent.transfer import TransferResolutionService
from calling_agent.webhook_services import (
    CallWebhookIngestionService,
    WebhookVerificationError,
)
from users.models import Task
from users.pagination import CustomPagination

logger = logging.getLogger(__name__)


class CallListView(APIView):
    """List calls visible to the authenticated user's role and company."""

    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_description="List all call history records visible to the authenticated user's role.",
        manual_parameters=[
            openapi.Parameter('status', openapi.IN_QUERY, type=openapi.TYPE_STRING, description='Filter by call status'),
            openapi.Parameter('lead_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, description='Filter by lead ID'),
            openapi.Parameter('user_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, description='Filter by context user ID'),
            openapi.Parameter('ordering', openapi.IN_QUERY, type=openapi.TYPE_STRING, description='Order by field, e.g. -id, -initiated_at'),
        ],
        responses={200: CallSerializer(many=True)},
        tags=["Calling Agent"],
    )
    def get(self, request, *args, **kwargs):
        calls = calls_visible_to_user(request.user).select_related('lead')
        call_status = request.query_params.get('status', '').strip()
        lead_id = request.query_params.get('lead_id', '').strip()
        user_id = request.query_params.get('user_id', '').strip()
        ordering = request.query_params.get('ordering', '').strip()

        if call_status:
            calls = calls.filter(status=call_status)
        if lead_id.isdigit():
            calls = calls.filter(lead_id=int(lead_id))
        if user_id.isdigit():
            calls = calls.filter(context_user_id=int(user_id))

        if ordering:
            calls = calls.order_by(ordering)
        else:
            calls = calls.order_by('-id')

        paginator = CustomPagination()
        page = paginator.paginate_queryset(calls, request, view=self)
        serializer = CallSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)


class CallExportCSVView(APIView):
    """Export call records as a CSV file visible to the authenticated user's role and company."""

    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_description="Export call history records as CSV format with optional filters.",
        manual_parameters=[
            openapi.Parameter('status', openapi.IN_QUERY, type=openapi.TYPE_STRING, description='Filter by call status'),
            openapi.Parameter('lead_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, description='Filter by lead ID'),
            openapi.Parameter('user_id', openapi.IN_QUERY, type=openapi.TYPE_INTEGER, description='Filter by context user ID'),
            openapi.Parameter('ordering', openapi.IN_QUERY, type=openapi.TYPE_STRING, description='Order by field, e.g. -id, -initiated_at'),
        ],
        responses={200: "CSV file download"},
        tags=["Calling Agent"],
    )
    def get(self, request, *args, **kwargs):
        calls = calls_visible_to_user(request.user).select_related('lead')
        call_status = request.query_params.get('status', '').strip()
        lead_id = request.query_params.get('lead_id', '').strip()
        user_id = request.query_params.get('user_id', '').strip()
        ordering = request.query_params.get('ordering', '').strip()

        if call_status:
            calls = calls.filter(status=call_status)
        if lead_id.isdigit():
            calls = calls.filter(lead_id=int(lead_id))
        if user_id.isdigit():
            calls = calls.filter(context_user_id=int(user_id))

        if ordering:
            calls = calls.order_by(ordering)
        else:
            calls = calls.order_by('-created_at')

        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="calls.csv"'

        writer = csv.writer(response)
        writer.writerow([
            'Date & Time',
            'Lead Name',
            'Phone Number',
            'Duration',
            'Country',
            'Call Outcome',
            'Lead Status',
        ])

        for call in calls:
            date_str = call.created_at.strftime('%Y-%m-%d %H:%M:%S') if call.created_at else ''
            lead_name = (call.lead.name if call.lead and call.lead.name else call.lead_name) or ''
            phone = call.phone_number or (call.lead.phone_no if call.lead else '') or ''
            duration = call.duration or ''
            country = (call.lead.country if call.lead and call.lead.country else '')
            call_outcome = call.get_status_display()
            lead_status = (call.lead.get_status_display() if call.lead else '')

            writer.writerow([
                date_str,
                lead_name,
                phone,
                duration,
                country,
                call_outcome,
                lead_status,
            ])

        return response


class CallDetailView(APIView):
    """Retrieve or delete a tenant-scoped call record by public ID."""

    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_description="Retrieve a stored call record by public call ID.",
        responses={200: CallSerializerDetails()},
        tags=["Calling Agent"],
    )
    def get(self, request, public_id, *args, **kwargs):
        call_obj = get_object_or_404(
            calls_visible_to_user(request.user),
            public_id=public_id,
        )
        serializer = CallSerializerDetails(call_obj)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @swagger_auto_schema(
        operation_description="Delete a call record owned by the authenticated user.",
        responses={
            200: "Call deleted successfully",
            403: "Permission denied (user can only delete their own calls)",
            404: "Call not found",
        },
        tags=["Calling Agent"],
    )
    def delete(self, request, public_id, *args, **kwargs):
        call_obj = get_object_or_404(
            calls_visible_to_user(request.user),
            public_id=public_id,
        )
        if not is_call_owned_by_user(call_obj, request.user):
            return Response(
                {
                    'error': 'permission_denied',
                    'detail': 'You can only delete your own calls.',
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        if call_obj.recording_storage_key:
            try:
                storage = get_call_recording_storage()
                if storage.exists(call_obj.recording_storage_key):
                    storage.delete(call_obj.recording_storage_key)
            except Exception as exc:
                logger.warning(
                    "Could not remove recording for call %s: %s",
                    public_id,
                    exc,
                )

        call_obj.delete()
        return Response(
            {'message': 'Call deleted successfully.'},
            status=status.HTTP_200_OK,
        )


class ManualCallInitiationView(APIView):
    """Initiate an immediate outbound call for an authorized lead."""

    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_description="Initiate an outbound ElevenLabs call.",
        request_body=ManualCallInitiationSerializer,
        responses={
            201: CallSerializerDetails(),
            200: CallSerializerDetails(),
            400: "Invalid request",
            404: "Lead not found",
            409: "Idempotency conflict",
            502: "Provider rejected or returned an invalid response",
            503: "Provider response unknown or configuration unavailable",
        },
        tags=["Calling Agent"],
    )
    def post(self, request, *args, **kwargs):
        serializer = ManualCallInitiationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            call, created = ManualCallInitiationService().initiate(
                user=request.user,
                lead_id=serializer.validated_data['lead_id'],
                idempotency_key=serializer.validated_data['idempotency_key'],
                agent_config_key=serializer.validated_data['agent_config_key'],
            )
        except CallAuthorizationError:
            return Response(
                {'error': 'lead_not_found', 'detail': 'Lead not found.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        except InvalidPhoneNumberError as exc:
            return Response(
                {'error': 'invalid_phone_number', 'detail': str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except LeadNotCallableError as exc:
            return Response(
                {'error': 'lead_not_callable', 'detail': str(exc)},
                status=status.HTTP_409_CONFLICT,
            )
        except CallConflictError as exc:
            return Response(
                {'error': 'idempotency_conflict', 'detail': str(exc)},
                status=status.HTTP_409_CONFLICT,
            )
        except CallingConfigurationError:
            logger.exception('ElevenLabs configuration is unavailable')
            return Response(
                {
                    'error': 'calling_unavailable',
                    'detail': 'Outbound calling is not configured.',
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except (ElevenLabsRequestError, ElevenLabsResponseError):
            return Response(
                {
                    'error': 'provider_rejected',
                    'detail': 'The call provider did not accept the request.',
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except ElevenLabsTransientError:
            return Response(
                {
                    'error': 'provider_response_unknown',
                    'detail': (
                        'The provider response is unknown. The call will not be '
                        'retried automatically.'
                    ),
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        response_status = (
            status.HTTP_201_CREATED if created else status.HTTP_200_OK
        )
        return Response(
            CallSerializerDetails(call).data,
            status=response_status,
        )


class CallEndView(APIView):
    """Terminate a live outbound call for an authorized user."""

    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_description=(
            "End an in-progress call by hanging up the Twilio call leg."
        ),
        request_body=CallEndSerializer,
        responses={
            200: CallSerializerDetails(),
            404: "Call not found",
            409: "Call is not active",
            502: "Provider rejected or returned an invalid response",
            503: "Provider response unknown or configuration unavailable",
        },
        tags=["Calling Agent"],
    )
    def post(self, request, public_id, *args, **kwargs):
        serializer = CallEndSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            call = CallTerminationService().terminate(
                user=request.user,
                public_id=public_id,
                reason=serializer.validated_data.get('reason', ''),
            )
        except CallAuthorizationError:
            return Response(
                {'error': 'call_not_found', 'detail': 'Call not found.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        except CallConflictError as exc:
            return Response(
                {'error': 'call_not_active', 'detail': str(exc)},
                status=status.HTTP_409_CONFLICT,
            )
        except CallingConfigurationError as exc:
            logger.exception('Call hangup configuration is unavailable')
            return Response(
                {
                    'error': 'calling_unavailable',
                    'detail': str(exc) or 'Outbound calling is not configured.',
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except (ElevenLabsRequestError, ElevenLabsResponseError):
            return Response(
                {
                    'error': 'provider_rejected',
                    'detail': 'The call provider did not accept the request.',
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except ElevenLabsTransientError:
            return Response(
                {
                    'error': 'provider_response_unknown',
                    'detail': (
                        'The provider response is unknown. The call was not '
                        'marked completed.'
                    ),
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response(
            CallSerializerDetails(call).data,
            status=status.HTTP_200_OK,
        )


class CallRecordingView(APIView):
    """Stream a private recording after tenant authorization."""

    permission_classes = [permissions.IsAuthenticated]

    def initial(self, request, *args, **kwargs):
        # Support JWT in query params so <audio src> can stream natively
        # without an Authorization header (same pattern as WhatsApp media).
        if not getattr(request.user, 'is_authenticated', False):
            token = (
                request.query_params.get('access_token')
                or request.query_params.get('token')
            )
            if token:
                try:
                    from rest_framework_simplejwt.authentication import (
                        JWTAuthentication,
                    )

                    jwt_auth = JWTAuthentication()
                    validated_token = jwt_auth.get_validated_token(token)
                    user = jwt_auth.get_user(validated_token)
                    if user is not None:
                        request.user = user
                except Exception:
                    pass
        super().initial(request, *args, **kwargs)

    @swagger_auto_schema(
        operation_description="Stream the private recording for a call.",
        responses={200: "Audio stream", 404: "Recording not found"},
        tags=["Calling Agent"],
    )
    def get(self, request, public_id, *args, **kwargs):
        call_obj = get_object_or_404(
            calls_visible_to_user(request.user),
            public_id=public_id,
        )
        if not call_obj.recording_available or not call_obj.recording_storage_key:
            return Response(
                {'error': 'recording_not_found', 'detail': 'Recording not found.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        storage = get_call_recording_storage()
        if not storage.exists(call_obj.recording_storage_key):
            logger.warning(
                'Call recording is marked available but missing call_id=%s',
                call_obj.public_id,
            )
            return Response(
                {'error': 'recording_not_found', 'detail': 'Recording not found.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        recording = storage.open(call_obj.recording_storage_key, 'rb')
        response = FileResponse(
            recording,
            content_type=call_obj.recording_content_type or 'audio/mpeg',
            as_attachment=False,
            filename=f'call-{call_obj.public_id}.mp3',
        )
        response['Accept-Ranges'] = 'bytes'
        response['Cache-Control'] = 'private, max-age=60'
        return response


class CallAnalyticsView(APIView):
    """Return aggregate call metrics for the authenticated tenant scope."""

    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_description='Return aggregate outbound call analytics.',
        responses={200: CallAnalyticsSerializer()},
        tags=['Calling Agent'],
    )
    def get(self, request, *args, **kwargs):
        calls = calls_visible_to_user(request.user)
        call_status = request.query_params.get('status', '').strip()
        lead_id = request.query_params.get('lead_id', '').strip()

        if call_status:
            calls = calls.filter(status=call_status)
        if lead_id.isdigit():
            calls = calls.filter(lead_id=int(lead_id))

        summary = CallAnalyticsService().build_summary(calls)
        serializer = CallAnalyticsSerializer(summary)
        return Response(serializer.data, status=status.HTTP_200_OK)


class ElevenLabsWebhookView(APIView):
    """Accept signed ElevenLabs post-call webhook events."""

    authentication_classes = []
    permission_classes = []

    @swagger_auto_schema(
        auto_schema=None,
    )
    def post(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        started_at = monotonic()
        raw_body = request.body.decode('utf-8')
        signature_header = (
            request.headers.get('ElevenLabs-Signature')
            or request.headers.get('elevenlabs-signature')
            or ''
        )
        logger.info(
            'ElevenLabs webhook received body_bytes=%d has_signature=%s '
            'content_type=%s',
            len(raw_body.encode('utf-8')),
            bool(signature_header),
            request.content_type or '',
        )
        try:
            webhook_event, created = CallWebhookIngestionService().ingest(
                raw_body=raw_body,
                signature_header=signature_header,
            )
        except WebhookVerificationError as exc:
            logger.warning(
                'Rejected ElevenLabs webhook with invalid signature '
                'reason=%s latency_ms=%d',
                str(exc),
                int((monotonic() - started_at) * 1000),
            )
            return Response(
                {'error': 'invalid_signature'},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        except Exception:
            logger.exception(
                'Unexpected ElevenLabs webhook ingestion failure latency_ms=%d',
                int((monotonic() - started_at) * 1000),
            )
            return Response(
                {'error': 'webhook_ingestion_failed'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        logger.info(
            'ElevenLabs webhook ingested event_id=%s event_key=%s '
            'event_type=%s conversation_id=%s call_id=%s created=%s '
            'latency_ms=%d',
            webhook_event.pk,
            webhook_event.event_key,
            webhook_event.event_type,
            webhook_event.conversation_id,
            webhook_event.call_id,
            created,
            int((monotonic() - started_at) * 1000),
        )

        if created:
            event_pk = webhook_event.pk

            def _enqueue_processing() -> None:
                logger.info(
                    'Enqueueing webhook processing event_id=%s queue=calling',
                    event_pk,
                )
                process_webhook_event_task.apply_async(
                    args=[event_pk],
                    queue='calling',
                )

            transaction.on_commit(_enqueue_processing)
        else:
            logger.info(
                'Skipping webhook enqueue for duplicate event_id=%s '
                'event_key=%s process_status=%s',
                webhook_event.pk,
                webhook_event.event_key,
                webhook_event.process_status,
            )
        return Response({'status': 'received'}, status=status.HTTP_200_OK)


class ElevenLabsToolView(APIView):
    authentication_classes = [ElevenLabsToolAuthentication]
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [CallToolThrottle]
    tool_name = 'unknown'

    @staticmethod
    def _success(data: dict[str, Any]) -> Response:
        return Response(
            {
                'success': True,
                'data': data,
                'error': None,
            },
            status=status.HTTP_200_OK,
        )

    @staticmethod
    def _error(
        *,
        code: str,
        message: str,
        response_status: int,
        fields: dict[str, Any] | None = None,
    ) -> Response:
        error: dict[str, Any] = {
            'code': code,
            'message': message,
        }
        if fields:
            error['fields'] = fields
        return Response(
            {
                'success': False,
                'data': None,
                'error': error,
            },
            status=response_status,
        )

    def handle_exception(self, exc: Exception) -> Response:
        if isinstance(exc, (AuthenticationFailed, NotAuthenticated)):
            return self._error(
                code='authentication_failed',
                message='Invalid tool credentials.',
                response_status=status.HTTP_401_UNAUTHORIZED,
            )
        if isinstance(exc, Throttled):
            return self._error(
                code='rate_limited',
                message='Tool request rate limit exceeded.',
                response_status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        if isinstance(exc, APIException):
            return self._error(
                code=str(exc.get_codes()),
                message='Tool service is temporarily unavailable.',
                response_status=exc.status_code,
            )
        return super().handle_exception(exc)

    def execute(
        self,
        request: Request,
        serializer: Serializer,
        operation: Callable[[Call, dict[str, Any]], dict[str, Any]],
    ) -> Response:
        started_at = monotonic()
        call = request.auth
        if not isinstance(call, Call):
            return self._error(
                code='authentication_failed',
                message='Invalid tool credentials.',
                response_status=status.HTTP_401_UNAUTHORIZED,
            )
        if not serializer.is_valid():
            self._log_outcome(call, started_at, 'invalid_request')
            return self._error(
                code='invalid_request',
                message='The tool request payload is invalid.',
                fields=serializer.errors,
                response_status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            data = operation(call, serializer.validated_data)
        except ToolContextUnavailableError:
            self._log_outcome(call, started_at, 'context_unavailable')
            return self._error(
                code='context_unavailable',
                message='The call context is no longer available.',
                response_status=status.HTTP_409_CONFLICT,
            )
        except CallConflictError:
            self._log_outcome(call, started_at, 'idempotency_conflict')
            return self._error(
                code='idempotency_conflict',
                message='The idempotency key conflicts with another action.',
                response_status=status.HTTP_409_CONFLICT,
            )
        except ToolActionRejectedError:
            self._log_outcome(call, started_at, 'action_rejected')
            return self._error(
                code='action_rejected',
                message='The requested action violates the lead policy.',
                response_status=status.HTTP_409_CONFLICT,
            )
        except ToolServiceUnavailableError:
            self._log_outcome(call, started_at, 'service_unavailable')
            return self._error(
                code='service_unavailable',
                message='The tool service is temporarily unavailable.',
                response_status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except Exception:
            logger.exception(
                'Unexpected ElevenLabs tool failure tool=%s call_id=%s',
                self.tool_name,
                call.public_id,
            )
            self._log_outcome(call, started_at, 'internal_error')
            return self._error(
                code='internal_error',
                message='The tool request could not be completed.',
                response_status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        self._log_outcome(call, started_at, 'success')
        return self._success(data)

    def _log_outcome(
        self,
        call: Call,
        started_at: float,
        outcome: str,
    ) -> None:
        logger.info(
            'ElevenLabs tool request tool=%s call_id=%s conversation_id=%s '
            'outcome=%s latency_ms=%d',
            self.tool_name,
            call.public_id,
            call.provider_conversation_id,
            outcome,
            int((monotonic() - started_at) * 1000),
        )


class LeadContextToolView(ElevenLabsToolView):
    tool_name = 'lead_context'

    @swagger_auto_schema(
        request_body=EmptyToolSerializer,
        responses={200: 'Lead context'},
        tags=['Calling Agent Tools'],
    )
    def post(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        serializer = EmptyToolSerializer(data=request.data)
        return self.execute(
            request,
            serializer,
            lambda call, data: LeadContextToolService().get_context(call),
        )


class ProjectContextToolView(ElevenLabsToolView):
    tool_name = 'project_context'

    @swagger_auto_schema(
        request_body=EmptyToolSerializer,
        responses={200: 'Project context'},
        tags=['Calling Agent Tools'],
    )
    def post(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        serializer = EmptyToolSerializer(data=request.data)
        return self.execute(
            request,
            serializer,
            lambda call, data: ProjectContextToolService().get_context(call),
        )


class ProjectSearchToolView(ElevenLabsToolView):
    tool_name = 'project_search'

    @swagger_auto_schema(
        request_body=ProjectSearchToolSerializer,
        responses={200: 'Matching eligible projects'},
        tags=['Calling Agent Tools'],
    )
    def post(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        serializer = ProjectSearchToolSerializer(data=request.data)
        return self.execute(
            request,
            serializer,
            lambda call, data: ProjectSearchToolService().search(call, data),
        )


class UnitSearchToolView(ElevenLabsToolView):
    tool_name = 'unit_search'

    @swagger_auto_schema(
        request_body=UnitSearchToolSerializer,
        responses={200: 'Matching units'},
        tags=['Calling Agent Tools'],
    )
    def post(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        serializer = UnitSearchToolSerializer(data=request.data)
        return self.execute(
            request,
            serializer,
            lambda call, data: UnitSearchToolService().search(call, data),
        )


class KnowledgeSearchToolView(ElevenLabsToolView):
    tool_name = 'knowledge_search'
    throttle_classes = [CallToolThrottle, KnowledgeToolThrottle]

    @swagger_auto_schema(
        request_body=KnowledgeSearchToolSerializer,
        responses={200: 'Authorized knowledge passages'},
        tags=['Calling Agent Tools'],
    )
    def post(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        serializer = KnowledgeSearchToolSerializer(data=request.data)
        return self.execute(
            request,
            serializer,
            lambda call, data: KnowledgeSearchToolService().search(
                call,
                query=data['query'],
                top_k=data['top_k'],
                project_id=data.get('project_id'),
            ),
        )


class RequestProposalToolView(ElevenLabsToolView):
    tool_name = 'request_proposal'

    @swagger_auto_schema(
        request_body=RequestProposalToolSerializer,
        responses={200: 'Proposal request response'},
        tags=['Calling Agent Tools'],
    )
    def post(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        serializer = RequestProposalToolSerializer(data=request.data)
        return self.execute(
            request,
            serializer,
            lambda call, data: RequestProposalToolService().request_proposal(
                call,
                project_id=data.get('project_id'),
                unit_id=data.get('unit_id'),
                reason=data.get('reason', ''),
            ),
        )


class CreateTaskToolView(ElevenLabsToolView):
    tool_name = 'create_task'

    @swagger_auto_schema(
        request_body=CreateTaskToolSerializer,
        responses={200: 'Created or existing task'},
        tags=['Calling Agent Tools'],
    )
    def post(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        serializer = CreateTaskToolSerializer(data=request.data)
        return self.execute(request, serializer, self._create_task)

    @staticmethod
    def _create_task(call: Call, data: dict[str, Any]) -> dict[str, Any]:
        action, task, created = TaskActionService().create_task(
            call=call,
            title=data['title'],
            priority=data['priority'],
            scheduled_at=data.get('scheduled_at'),
            due_date=data.get('due_date'),
            task_type=data.get('type') or Task.Type.CALLBACK,
            reason=data['reason'],
            open_ended=bool(data.get('open_ended')),
        )
        return {
            'created': created,
            'action_id': action.pk,
            'task': {
                'id': task.pk,
                'title': task.name,
                'priority': task.priority,
                'type': task.type,
                'open_ended': task.open_ended,
                'due_date': (
                    task.due_date.isoformat()
                    if task.due_date is not None
                    else None
                ),
                'scheduled_at': (
                    task.scheduled_at.isoformat()
                    if task.scheduled_at is not None
                    else None
                ),
                'status': task.status,
            },
        }


class UpdateLeadToolView(ElevenLabsToolView):
    tool_name = 'update_lead'

    @swagger_auto_schema(
        request_body=UpdateLeadToolSerializer,
        responses={200: 'Updated lead fields'},
        tags=['Calling Agent Tools'],
    )
    def post(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        serializer = UpdateLeadToolSerializer(data=request.data)
        return self.execute(request, serializer, self._update_lead)

    @staticmethod
    def _update_lead(call: Call, data: dict[str, Any]) -> dict[str, Any]:
        from calling_agent.tool_services import eligible_projects_for_call

        field_map = {
            'property_type': 'type',
            'other_property_type': 'other_type',
        }
        assigned_project_id = data.get('assigned_project_id')
        assigned_project = None
        if assigned_project_id is not None:
            assigned_project = (
                eligible_projects_for_call(call)
                .filter(pk=assigned_project_id)
                .first()
            )
            if assigned_project is None:
                raise ToolActionRejectedError(
                    'The assigned project is not eligible for this call.'
                )

        changes = {
            field_map.get(field, field): value
            for field, value in data.items()
            if field not in {'reason', 'assigned_project_id'}
        }
        action, _lead, updated = LeadUpdateActionService().update(
            call=call,
            changes=changes,
            reason=data['reason'],
            assigned_project=assigned_project,
        )
        changed_fields = sorted(changes)
        if assigned_project is not None:
            changed_fields = sorted([*changed_fields, 'assigned_project_id'])
        return {
            'updated': updated,
            'action_id': action.pk,
            'changed_fields': changed_fields,
        }


class ResolveTransferToolView(ElevenLabsToolView):
    tool_name = 'resolve_transfer'

    @swagger_auto_schema(
        request_body=ResolveTransferToolSerializer,
        responses={200: 'Transfer resolution'},
        tags=['Calling Agent Tools'],
    )
    def post(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        serializer = ResolveTransferToolSerializer(data=request.data)
        return self.execute(request, serializer, self._resolve_transfer)

    @staticmethod
    def _resolve_transfer(
        call: Call,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        resolution, task, fallback_created = (
            TransferResolutionService().resolve(
                call=call,
                reason=data['reason'],
            )
        )
        return {
            'available': resolution.available,
            'destination': resolution.destination,
            'transfer_mode': resolution.transfer_mode,
            'fallback_action': resolution.fallback_action,
            'public_message': resolution.public_message,
            'fallback_created': fallback_created,
            'fallback_task_id': task.pk if task is not None else None,
        }
