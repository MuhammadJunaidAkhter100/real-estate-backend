import logging
import hashlib
from urllib.parse import unquote, urlparse
import httpx
from django.http import HttpResponse
from django.conf import settings
from django.core.cache import cache
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
from rest_framework import permissions, status
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.response import Response
from rest_framework.views import APIView
from whatsapp.services import WhatsAppService
from whatsapp.tasks import sync_chats_and_messages_task
from celery.result import AsyncResult
from whatsapp.models import WhatsAppAccount
from whatsapp.serializers import (
    WhatsAppSendMessageSerializer,
    WhatsAppWebhookSerializer,
    WhatsAppAccountSerializer,
    WhatsAppUpdateProfileNameSerializer,
    WhatsAppUpdateProfilePictureSerializer,
    WhatsAppSaveContactSerializer,
    WhatsAppReactionSerializer,
)

logger = logging.getLogger(__name__)


class WhatsAppQrView(APIView):
    """Create a WhatsApp session and return its QR code for the authenticated user."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        session_name = request.data.get("session_name") or request.query_params.get("session_name")

        service = WhatsAppService()

        try:
            result = service.get_or_create_qr_session(
                user=request.user,
                session_name=session_name,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to create WhatsApp QR session for user %s", request.user.id)
            return Response(
                {
                    "detail": str(exc),
                    "provider": getattr(settings, "WHATSAPP_PROVIDER", "waha"),
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )

        if isinstance(result, dict) and result.get("session_name"):
            service.store_account(
                request.user,
                session_name=result["session_name"],
                session_status=result.get("status", "unknown"),
                provider=result.get("provider", service.provider),
            )

        return Response(result, status=status.HTTP_200_OK)

    def get(self, request):
        return self.post(request)


class WhatsAppStateView(APIView):
    """Get the current state of the WhatsApp session for the authenticated user."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        service = WhatsAppService()
        try:
            result = service.get_session_state(user=request.user)
            if result:
                service.store_account(
                    user=request.user,
                    session_status=result.get("status", "unknown")
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to get WhatsApp session state for user %s", request.user.id)
            return Response(
                {
                    "detail": str(exc),
                    "provider": getattr(settings, "WHATSAPP_PROVIDER", "waha"),
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(result, status=status.HTTP_200_OK)


class WhatsAppSyncView(APIView):
    """Sync WhatsApp chats and messages for the authenticated user."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        # Debounce: check if a sync task is already running for this user
        lock_key = f"whatsapp_sync_lock_{request.user.id}"
        if cache.get(lock_key):
            return Response(
                {
                    "status": "already_syncing",
                    "detail": "WhatsApp sync is already running in the background for this user.",
                },
                status=status.HTTP_200_OK,
            )

        # Enqueue background Celery task to sync chats/messages.
        try:
            async_result = sync_chats_and_messages_task.delay(request.user.id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to enqueue WhatsApp sync task for user %s", request.user.id)
            return Response(
                {
                    "detail": str(exc),
                    "provider": getattr(settings, "WHATSAPP_PROVIDER", "waha"),
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(
            {
                "status": "accepted",
                "task_id": getattr(async_result, "id", None),
                "detail": "WhatsApp sync has been queued and will run in the background.",
            },
            status=status.HTTP_202_ACCEPTED,
        )

    def get(self, request):
        # GET request returns latest sync status without triggering a new Celery task
        is_syncing = bool(cache.get(f"whatsapp_sync_lock_{request.user.id}"))

        return Response(
            {
                "status": "in_progress" if is_syncing else "idle",
            },
            status=status.HTTP_200_OK,
        )


class WhatsAppSyncStatusView(APIView):
    """Return Celery task status and the recorded sync log when available."""

    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        manual_parameters=[
            openapi.Parameter(
                "task_id",
                openapi.IN_QUERY,
                description="Celery task id returned when the sync was queued",
                type=openapi.TYPE_STRING,
                required=True,
            ),
        ],
        responses={200: "Sync status and log"},
    )
    def get(self, request):
        task_id = request.query_params.get("task_id") or request.data.get("task_id")
        if not task_id:
            return Response({"error": "task_id is required as query param or in body."}, status=status.HTTP_400_BAD_REQUEST)

        # Celery AsyncResult
        try:
            async_res = AsyncResult(task_id)
            celery_status = async_res.status
            celery_result = async_res.result if hasattr(async_res, "result") else None
        except Exception:
            celery_status = None
            celery_result = None

        return Response({
            "task_id": task_id,
            "celery_status": celery_status,
            "celery_result": celery_result,
        }, status=status.HTTP_200_OK)


class WhatsAppLogoutView(APIView):
    """Logout the WhatsApp session and delete local account data."""

    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        responses={
            200: 'WhatsApp logout completed successfully',
            404: 'WhatsApp session not found',
            502: 'Provider error',
        },
    )
    def post(self, request):
        service = WhatsAppService()
        try:
            result = service.logout(user=request.user)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to logout WhatsApp session for user %s", request.user.id)
            return Response(
                {
                    "detail": str(exc),
                    "provider": getattr(settings, "WHATSAPP_PROVIDER", "waha"),
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )

        if result.get("status") == "not_found":
            return Response(result, status=status.HTTP_404_NOT_FOUND)

        return Response(result, status=status.HTTP_200_OK)


class WhatsAppSendMessageView(APIView):
    """
    Send WhatsApp messages (text, image, document, video, audio)
    through the configured WhatsApp provider.
    """

    permission_classes = [permissions.IsAuthenticated]


    @swagger_auto_schema(
        consumes=[
            "multipart/form-data",
            "application/json",
        ],
        request_body=WhatsAppSendMessageSerializer,
        responses={
            200: "Message sent successfully",
            400: "Invalid payload",
            404: "WhatsApp session not found",
            502: "Provider error",
        },
    )
    def post(self, request):

        serializer = WhatsAppSendMessageSerializer(data=request.data)

        serializer.is_valid(raise_exception=True)

        payload = serializer.validated_data

        service = WhatsAppService()

        try:

            message_type = payload["type"]

            handlers = {
                "text": service.send_text_message,
                "image": service.send_image_message,
                "document": service.send_document_message,
                "video": service.send_video_message,
                "audio": service.send_audio_message,
            }

            handler = handlers.get(message_type)

            if not handler:
                return Response(
                    {
                        "status": "invalid_payload",
                        "message": f"Unsupported message type '{message_type}'.",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            result = handler(
                user=request.user,
                payload=payload,
            )

        except Exception as exc:
            logger.exception(
                "Failed to send WhatsApp message for user %s",
                request.user.id,
            )

            return Response(
                {
                    "detail": str(exc),
                    "provider": getattr(settings, "WHATSAPP_PROVIDER", "waha"),
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )

        if result.get("status") == "invalid_payload":
            return Response(result, status=status.HTTP_400_BAD_REQUEST)

        if result.get("status") == "not_found":
            return Response(result, status=status.HTTP_404_NOT_FOUND)

        return Response(result, status=status.HTTP_200_OK)
    
class WhatsAppWebhookView(APIView):
    """Receive webhook events from the WhatsApp provider and persist them."""

    permission_classes = [permissions.AllowAny]
    @swagger_auto_schema(
        request_body=WhatsAppWebhookSerializer,
        responses={
            200: 'Webhook processed successfully',
            502: 'Provider error',
        },
    )
    def post(self, request):
        service = WhatsAppService()
        try:
            print(f"Received webhook payload in view: {request.data}")
            result = service.persist_incoming_webhook(request.data)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to process WhatsApp webhook payload")
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        return Response(result, status=status.HTTP_200_OK)


class WhatsAppGetChatsView(APIView):
    """Get all WhatsApp chats for the authenticated user."""

    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        responses={
            200: 'Successfully retrieved all chats',
            502: 'Provider error',
        },
    )
    def get(self, request):
        service = WhatsAppService()
        try:
            result = service.get_chats(user=request.user)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to get WhatsApp chats for user %s", request.user.id)
            return Response(
                {
                    "detail": str(exc),
                    "provider": getattr(settings, "WHATSAPP_PROVIDER", "waha"),
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(result, status=status.HTTP_200_OK)


class WhatsAppGetChatMessagesView(APIView):
    """Get all messages for a specific WhatsApp chat."""

    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        responses={
            200: 'Successfully retrieved chat messages',
            404: 'Chat not found',
            502: 'Provider error',
        },
    )
    def get(self, request, chat_id):
        service = WhatsAppService()
        try:
            result = service.get_chat_messages(user=request.user, chat_id=chat_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to get WhatsApp chat messages for user %s", request.user.id)
            return Response(
                {
                    "detail": str(exc),
                    "provider": getattr(settings, "WHATSAPP_PROVIDER", "waha"),
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )

        if result.get("status") == "chat_not_found":
            return Response(result, status=status.HTTP_404_NOT_FOUND)

        if result.get("status") == "not_found":
            return Response(result, status=status.HTTP_404_NOT_FOUND)

        return Response(result, status=status.HTTP_200_OK)


class WhatsAppMarkChatReadView(APIView):
    """Mark a specific WhatsApp chat as read (unread_count = 0)."""

    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        responses={
            200: 'Successfully marked chat as read',
            404: 'Chat not found',
            502: 'Provider error',
        },
    )
    def post(self, request, chat_id):
        service = WhatsAppService()
        try:
            result = service.mark_chat_as_read(user=request.user, chat_id=chat_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to mark WhatsApp chat as read for user %s", request.user.id)
            return Response(
                {
                    "detail": str(exc),
                    "provider": getattr(settings, "WHATSAPP_PROVIDER", "waha"),
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )

        if result.get("status") in ["chat_not_found", "not_found"]:
            return Response(result, status=status.HTTP_404_NOT_FOUND)

        return Response(result, status=status.HTTP_200_OK)


class WhatsAppDeleteChatView(APIView):
    """Delete a WhatsApp chat and all its messages."""

    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        responses={
            200: 'Chat deleted successfully',
            404: 'Chat not found',
            502: 'Provider error',
        },
    )
    def delete(self, request, chat_id):
        service = WhatsAppService()
        try:
            result = service.delete_chat(user=request.user, chat_id=chat_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to delete WhatsApp chat for user %s", request.user.id)
            return Response(
                {
                    "detail": str(exc),
                    "provider": getattr(settings, "WHATSAPP_PROVIDER", "waha"),
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )

        if result.get("status") == "chat_not_found":
            return Response(result, status=status.HTTP_404_NOT_FOUND)

        if result.get("status") == "not_found":
            return Response(result, status=status.HTTP_404_NOT_FOUND)

        return Response(result, status=status.HTTP_200_OK)


class WhatsAppDeleteMessageView(APIView):
    """Delete a single message from a WhatsApp chat."""

    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        responses={
            200: 'Message deleted successfully',
            404: 'Message or chat not found',
            502: 'Provider error',
        },
    )
    def delete(self, request, chat_id, message_id):
        service = WhatsAppService()
        try:
            result = service.delete_message(user=request.user, chat_id=chat_id, message_id=message_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to delete WhatsApp message for user %s", request.user.id)
            return Response(
                {
                    "detail": str(exc),
                    "provider": getattr(settings, "WHATSAPP_PROVIDER", "waha"),
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )

        if result.get("status") in ("message_not_found", "chat_not_found", "not_found"):
            return Response(result, status=status.HTTP_404_NOT_FOUND)

        return Response(result, status=status.HTTP_200_OK)


class WhatsAppDeleteAllMessagesView(APIView):
    """Delete all messages from a WhatsApp chat."""

    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        responses={
            200: 'All messages deleted successfully',
            404: 'Chat not found',
            502: 'Provider error',
        },
    )
    def delete(self, request, chat_id):
        service = WhatsAppService()
        try:
            result = service.delete_all_messages(user=request.user, chat_id=chat_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to delete all messages from chat for user %s", request.user.id)
            return Response(
                {
                    "detail": str(exc),
                    "provider": getattr(settings, "WHATSAPP_PROVIDER", "waha"),
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )

        if result.get("status") == "chat_not_found":
            return Response(result, status=status.HTTP_404_NOT_FOUND)

        if result.get("status") == "not_found":
            return Response(result, status=status.HTTP_404_NOT_FOUND)

        return Response(result, status=status.HTTP_200_OK)


class WhatsAppProfileView(APIView):
    """Get my WhatsApp profile information."""

    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_description="Get current user WhatsApp profile information from WAHA.",
        responses={200: openapi.Response("Profile data", WhatsAppAccountSerializer)},
    )
    def get(self, request):
        service = WhatsAppService()
        result = service.get_profile(user=request.user)

        if result.get("status") == "error":
            return Response(result, status=status.HTTP_400_BAD_REQUEST)

        return Response(result, status=status.HTTP_200_OK)


class WhatsAppProfileNameView(APIView):
    """Set my WhatsApp profile display name."""

    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        request_body=WhatsAppUpdateProfileNameSerializer,
        responses={200: "Profile name updated successfully"},
    )
    def put(self, request):
        serializer = WhatsAppUpdateProfileNameSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        service = WhatsAppService()
        result = service.set_profile_name(
            user=request.user,
            name=serializer.validated_data["name"],
        )

        if result.get("status") == "error":
            return Response(result, status=status.HTTP_400_BAD_REQUEST)

        return Response(result, status=status.HTTP_200_OK)


class WhatsAppProfilePictureView(APIView):
    """Set or delete my WhatsApp profile picture."""

    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    @swagger_auto_schema(
        consumes=[
            "multipart/form-data",
            "application/json",
        ],
        request_body=WhatsAppUpdateProfilePictureSerializer,
        responses={200: "Profile picture updated successfully"},
    )
    def put(self, request):
        serializer = WhatsAppUpdateProfilePictureSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        file_obj = serializer.validated_data.get("file") or request.FILES.get("file")
        picture_url = serializer.validated_data.get("picture_url")

        service = WhatsAppService()
        result = service.set_profile_picture(
            user=request.user,
            file_obj=file_obj,
            picture_url=picture_url,
        )

        if result.get("status") == "error":
            return Response(result, status=status.HTTP_400_BAD_REQUEST)

        return Response(result, status=status.HTTP_200_OK)

    @swagger_auto_schema(
        responses={200: "Profile picture deleted successfully"},
    )
    def delete(self, request):
        service = WhatsAppService()
        result = service.delete_profile_picture(user=request.user)

        if result.get("status") == "error":
            return Response(result, status=status.HTTP_400_BAD_REQUEST)

        return Response(result, status=status.HTTP_200_OK)


class WhatsAppCheckNumberExistsView(APIView):
    """Check if a phone number exists on WhatsApp."""

    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_description="Check if a phone number is registered on WhatsApp.",
        manual_parameters=[
            openapi.Parameter(
                "phone",
                openapi.IN_QUERY,
                description="Phone number to check (e.g. 923292329152 or +923292329152)",
                type=openapi.TYPE_STRING,
                required=True,
            ),
        ],
        responses={
            200: "Number status response",
            400: "Invalid query parameter",
            502: "Provider error",
        },
    )
    def get(self, request):
        phone = request.query_params.get("phone") or request.query_params.get("number")
        if not phone:
            return Response(
                {"error": "'phone' query parameter is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        service = WhatsAppService()
        try:
            result = service.check_number_exists(user=request.user, phone=phone)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to check WhatsApp number existence for user %s", request.user.id)
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        if result.get("status") == "error":
            return Response(result, status=status.HTTP_400_BAD_REQUEST)

        return Response(result, status=status.HTTP_200_OK)


class WhatsAppSaveContactView(APIView):
    """Save or update a contact on WhatsApp."""

    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_description=(
            "Save or update a WhatsApp contact.\n\n"
            "**Supported Phone Formats:**\n"
            "- With country code (no +): `923001234567`\n"
            "- With country code (with +): `+923001234567`\n"
            "- WhatsApp Contact ID format: `923001234567@c.us`"
        ),
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            required=["phone", "firstName"],
            properties={
                "phone": openapi.Schema(
                    type=openapi.TYPE_STRING,
                    description="Phone number or WhatsApp Contact ID",
                    example="923001234567",
                ),
                "firstName": openapi.Schema(
                    type=openapi.TYPE_STRING,
                    description="First name of the contact",
                    example="John",
                ),
                "lastName": openapi.Schema(
                    type=openapi.TYPE_STRING,
                    description="Last name of the contact",
                    example="Doe",
                ),
            },
        ),
        responses={
            200: "Contact saved successfully",
            400: "Validation error or request error",
            502: "Provider connection error",
        },
    )
    def put(self, request):
        return self._save_contact(request)

    @swagger_auto_schema(
        operation_description=(
            "Save or update a WhatsApp contact.\n\n"
            "**Supported Phone Formats:**\n"
            "- With country code (no +): `923001234567`\n"
            "- With country code (with +): `+923001234567`\n"
            "- WhatsApp Contact ID format: `923001234567@c.us`"
        ),
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            required=["phone", "firstName"],
            properties={
                "phone": openapi.Schema(
                    type=openapi.TYPE_STRING,
                    description="Phone number or WhatsApp Contact ID",
                    example="923001234567",
                ),
                "firstName": openapi.Schema(
                    type=openapi.TYPE_STRING,
                    description="First name of the contact",
                    example="John",
                ),
                "lastName": openapi.Schema(
                    type=openapi.TYPE_STRING,
                    description="Last name of the contact",
                    example="Doe",
                ),
            },
        ),
        responses={
            200: "Contact saved successfully",
            400: "Validation error or request error",
            502: "Provider connection error",
        },
    )
    def post(self, request):
        return self._save_contact(request)

    def _save_contact(self, request):
        serializer = WhatsAppSaveContactSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        validated_data = serializer.validated_data
        service = WhatsAppService()
        try:
            result = service.save_contact(
                user=request.user,
                phone=validated_data["phone"],
                first_name=validated_data["first_name"],
                last_name=validated_data.get("last_name", ""),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to save contact for user %s", request.user.id)
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        if result.get("status") == "error":
            return Response(result, status=status.HTTP_400_BAD_REQUEST)

        return Response(result, status=status.HTTP_200_OK)


class WhatsAppGetAllContactsView(APIView):
    """Fetch all contacts for the authenticated user's WhatsApp session."""

    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_description="Fetch all contacts for the user's active WhatsApp session.",
        manual_parameters=[
            openapi.Parameter(
                "limit",
                openapi.IN_QUERY,
                description="Number of contacts to fetch (optional)",
                type=openapi.TYPE_INTEGER,
                required=False,
            ),
            openapi.Parameter(
                "offset",
                openapi.IN_QUERY,
                description="Offset index (optional)",
                type=openapi.TYPE_INTEGER,
                required=False,
            ),
        ],
        responses={
            200: "List of WhatsApp contacts",
            400: "Session error or request error",
            502: "Provider connection error",
        },
    )
    def get(self, request):
        limit_val = request.query_params.get("limit")
        offset_val = request.query_params.get("offset")

        limit = None
        offset = None

        if limit_val is not None and str(limit_val).strip() != "":
            try:
                limit = int(limit_val)
            except ValueError:
                return Response(
                    {"error": "'limit' query parameter must be a valid integer."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        if offset_val is not None and str(offset_val).strip() != "":
            try:
                offset = int(offset_val)
            except ValueError:
                return Response(
                    {"error": "'offset' query parameter must be a valid integer."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        service = WhatsAppService()
        try:
            result = service.get_all_contacts(user=request.user, limit=limit, offset=offset)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to fetch WhatsApp contacts for user %s", request.user.id)
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        if result.get("status") == "error":
            return Response(result, status=status.HTTP_400_BAD_REQUEST)

        return Response(result, status=status.HTTP_200_OK)


class WhatsAppMediaProxyView(APIView):
    """Backend proxy view for secure access to WAHA media files without exposing API keys to the frontend."""

    permission_classes = [permissions.IsAuthenticated]

    def initial(self, request, *args, **kwargs):
        # Support JWT access token in query params so <img> / <video> tags can load images natively
        if not request.user or not request.user.is_authenticated:
            token = request.query_params.get("token") or request.query_params.get("access_token")
            if token:
                try:
                    from rest_framework_simplejwt.authentication import JWTAuthentication
                    jwt_auth = JWTAuthentication()
                    validated_token = jwt_auth.get_validated_token(token)
                    user = jwt_auth.get_user(validated_token)
                    if user:
                        request.user = user
                except Exception:
                    pass
        super().initial(request, *args, **kwargs)

    @swagger_auto_schema(
        operation_description="Proxy WAHA media file requests securely using session media API key.",
        manual_parameters=[
            openapi.Parameter(
                "url",
                openapi.IN_QUERY,
                description="Original WAHA media URL",
                type=openapi.TYPE_STRING,
                required=True,
            ),
            openapi.Parameter(
                "token",
                openapi.IN_QUERY,
                description="JWT access token (optional, for use directly in HTML img/video src tags)",
                type=openapi.TYPE_STRING,
                required=False,
            ),
        ],
        responses={200: "Binary media file stream"},
    )
    def get(self, request):
        raw_url = request.query_params.get("url") or request.GET.get("url")
        if not raw_url:
            return Response(
                {"detail": "Query parameter 'url' is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        target_url = unquote(raw_url)
        parsed_url = urlparse(target_url)

        # Check server-side cache for instant response (< 5ms)
        cache_key = f"media_proxy_{hashlib.md5(target_url.encode('utf-8')).hexdigest()}"
        cached_media = cache.get(cache_key)
        if cached_media:
            response = HttpResponse(
                cached_media["content"],
                content_type=cached_media["content_type"],
                status=200,
            )
            response["Cache-Control"] = "public, max-age=31536000, immutable"
            return response

        service = WhatsAppService()
        is_external_cdn = any(
            domain in parsed_url.netloc.lower()
            for domain in ["mmg.whatsapp.net", "whatsapp.net", "fbcdn.net", "cdn.whatsapp.net"]
        )

        headers = {
            "accept": "*/*",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }

        # Only send WAHA API key if accessing local WAHA backend, NOT for public WhatsApp CDN
        if not is_external_cdn:
            account = WhatsAppAccount.objects.filter(user=request.user).first()
            if account and account.media_api_key:
                headers["X-Api-Key"] = account.media_api_key
            else:
                headers["X-Api-Key"] = service.api_key

        try:
            waha_response = httpx.get(
                target_url,
                headers=headers,
                timeout=30,
                follow_redirects=True,
            )

            if not is_external_cdn and waha_response.status_code in (401, 403):
                headers["X-Api-Key"] = service.api_key
                waha_response = httpx.get(
                    target_url,
                    headers=headers,
                    timeout=30,
                    follow_redirects=True,
                )

            if waha_response.status_code != 200:
                return Response(
                    {"detail": f"Failed to fetch media from provider (Status {waha_response.status_code})"},
                    status=status.HTTP_502_BAD_GATEWAY,
                )

            content_type = waha_response.headers.get("content-type", "application/octet-stream")
            content_bytes = waha_response.content

            # Cache small/medium media files (< 5MB) in Redis/Django cache for 24 hours
            if len(content_bytes) <= 5 * 1024 * 1024:
                cache.set(
                    cache_key,
                    {"content": content_bytes, "content_type": content_type},
                    timeout=86400,
                )

            response = HttpResponse(
                content_bytes,
                content_type=content_type,
                status=200,
            )
            # Enable browser HTTP cache for 1 year (0ms load time on subsequent renders)
            response["Cache-Control"] = "public, max-age=31536000, immutable"
            return response
        except Exception as exc:  # noqa: BLE001
            logger.exception("Media proxy failed for URL: %s", target_url)
            return Response(
                {"detail": f"Media proxy connection error: {exc}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )


class WhatsAppReactionView(APIView):
    """
    Send or update a reaction to a WhatsApp message.
    """

    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        request_body=WhatsAppReactionSerializer,
        responses={
            200: "Reaction sent successfully",
            400: "Invalid payload",
            502: "Provider error",
        },
    )
    def put(self, request):
        serializer = WhatsAppReactionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        message_id = serializer.validated_data["messageId"]
        reaction = serializer.validated_data["reaction"]
        

        service = WhatsAppService()
        result = service.send_reaction(
            user=request.user,
            message_id=message_id,
            reaction=reaction,
        )

        if result.get("status") == "error":
            status_code = status.HTTP_502_BAD_GATEWAY
            if result.get("status_code") == 400:
                status_code = status.HTTP_400_BAD_REQUEST
            return Response(result, status=status_code)

        return Response(result, status=status.HTTP_200_OK)

