import json
import logging
import os

from django.http import StreamingHttpResponse
from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from chatbot.data_extraction import DataExtractor
from chatbot.models import ChatSession, KnowledgeBaseDocument
from chatbot.graph import stream_response, get_history
from chatbot.tasks import extract_knowledge_base_document
from billing.services import consume_credits, refund_credits

logger = logging.getLogger(__name__)


def charge_chat_turn(user) -> bool:
    """Bill 1 AI credit for one chat turn.

    Returns True when this turn was charged, so a turn that never produced a
    reply can be refunded. Raises `PlanLimitReached` (403) when the company's
    credit allowance is exhausted.
    """
    company = getattr(user, 'company', None)
    if company is None:
        return False
    return consume_credits(company.pk, 'chat_turn')


def refund_chat_turn(user) -> None:
    company = getattr(user, 'company', None)
    if company is None:
        return
    # No receipt: chat turns are not individually idempotent, so the credit is
    # simply returned.
    refund_credits(company.pk, 'chat_turn')


# ── Stream chat ────────────────────────────────────────────────────────────────

class ChatStreamView(APIView):
    """
    POST /api/chatbot/stream/

    Streams the assistant reply as Server-Sent Events (SSE).

    Request body:
        {
            "message": "What are the best investment opportunities in Dubai?",
            "session_id": 42          // optional — omit to start a new session
        }

    SSE events:
        data: {"type": "token",  "content": "chunk..."}
        data: {"type": "done",   "session_id": 42}
        data: {"type": "error",  "detail": "..."}

    Frontend consumption example (fetch + ReadableStream):
        const res = await fetch('/api/chatbot/stream/', { method:'POST', ... });
        const reader = res.body.getReader();
        // read chunks and parse SSE lines
    """
    permission_classes = [AllowAny]

    @swagger_auto_schema(
        tags=['Chatbot'],
        operation_description=(
            "Stream a chatbot response via SSE.\n\n"
            "- **thread_id** (optional): pass an existing thread_id to continue a conversation. "
            "Omit (or pass null) to start a new conversation — a new thread_id will be returned in the `done` event.\n"
            "- Greeting messages (hi, hello, who are you …) return instantly without hitting the LLM.\n"
            "- Real-estate queries are answered by GPT-4o-mini with full persistent memory per thread."
        ),
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            required=['message', 'thread_id'],
            properties={
                'message': openapi.Schema(type=openapi.TYPE_STRING, description='User message'),
                'thread_id': openapi.Schema(type=openapi.TYPE_STRING, description='Thread UUID — use any UUID to start a new conversation or pass an existing one to continue it'),
            },
        ),
        responses={200: openapi.Response(description=(
            'SSE stream.\n'
            'data: {"type": "token",  "content": "chunk..."}\n'
            'data: {"type": "done",   "thread_id": "uuid", "session_id": 1}\n'
            'data: {"type": "error",  "detail": "..."}'
        ))},
    )
    def post(self, request):
        message = request.data.get('message', '').strip()
        thread_id = request.data.get('thread_id', '').strip()

        if not message:
            return Response({'detail': 'message is required.'}, status=status.HTTP_400_BAD_REQUEST)
        if not thread_id:
            return Response({'detail': 'thread_id is required.'}, status=status.HTTP_400_BAD_REQUEST)

        user = request.user if request.user.is_authenticated else None

        # Reuse the same thread_id when it already exists, even for anonymous/public requests.
        # A conversation is keyed on its thread_id; if the user is authenticated, attach it to the session.
        session = ChatSession.objects.filter(thread_id=thread_id).first()
        if session is None:
            session = ChatSession.objects.create(thread_id=thread_id, user=user)
        elif user is not None and session.user_id != user.pk:
            session.user = user
            session.save(update_fields=['user'])

        # Use first message as session title if not set yet
        if not session.title:
            session.title = message[:80]
            session.save(update_fields=['title'])

        # Use the thread_id exactly as provided (or the new session's UUID)
        thread_id_str = str(session.thread_id)

        if user is not None:
            current_country = getattr(user, 'current_country', '') or ''
            user_id = user.id
            user_role = getattr(user, 'role', '') or ''
        else:
            current_country = ''
            user_id = None
            user_role = 'public'

        # Billing: one credit per chat turn, charged before the stream opens so
        # an exhausted allowance is a clean 403 instead of a broken stream.
        # Public chatbot requests bypass the billing gate.
        charged_here = charge_chat_turn(user) if user is not None else False

        def _event_stream():
            yielded = False
            try:
                for chunk in stream_response(
                    thread_id_str, message, current_country, user_id, user_role
                ):
                    yielded = True
                    payload = json.dumps({"type": "token", "content": chunk})
                    yield f"data: {payload}\n\n"
                done_payload = json.dumps({
                    "type": "done",
                    "thread_id": thread_id_str,
                    "session_id": session.pk,
                })
                yield f"data: {done_payload}\n\n"
            except Exception as exc:
                # A turn that produced no answer at all is not billed.
                if charged_here and not yielded:
                    refund_chat_turn(user)
                error_payload = json.dumps({"type": "error", "detail": str(exc)})
                yield f"data: {error_payload}\n\n"

        response = StreamingHttpResponse(_event_stream(), content_type='text/event-stream')
        response['Cache-Control'] = 'no-cache'
        response['X-Accel-Buffering'] = 'no'   # disable nginx buffering
        return response


# ── Conversation history ───────────────────────────────────────────────────────

class ChatHistoryView(APIView):
    """
    GET /api/chatbot/history/<session_id>/

    Returns full conversation history for a session.
    """
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        tags=['Chatbot'],
        operation_description="Get full conversation history for a chat session.",
        responses={
            200: openapi.Schema(
                type=openapi.TYPE_OBJECT,
                properties={
                    'session_id': openapi.Schema(type=openapi.TYPE_INTEGER),
                    'messages': openapi.Schema(
                        type=openapi.TYPE_ARRAY,
                        items=openapi.Schema(
                            type=openapi.TYPE_OBJECT,
                            properties={
                                'role': openapi.Schema(type=openapi.TYPE_STRING, description='user | assistant'),
                                'content': openapi.Schema(type=openapi.TYPE_STRING),
                            },
                        ),
                    ),
                },
            ),
        },
    )
    def get(self, request, session_id):
        try:
            session = ChatSession.objects.get(pk=session_id, user=request.user)
        except ChatSession.DoesNotExist:
            return Response({'detail': 'Session not found.'}, status=status.HTTP_404_NOT_FOUND)

        messages = get_history(str(session.thread_id))
        return Response({'session_id': session.pk, 'thread_id': str(session.thread_id), 'messages': messages})


# ── Session list ───────────────────────────────────────────────────────────────

class ChatSessionListView(APIView):
    """
    GET  /api/chatbot/sessions/    → list user's sessions
    DELETE /api/chatbot/sessions/<id>/  → delete a session
    """
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        tags=['Chatbot'],
        operation_description="List all chat sessions for the authenticated user.",
        responses={
            200: openapi.Schema(
                type=openapi.TYPE_ARRAY,
                items=openapi.Schema(
                    type=openapi.TYPE_OBJECT,
                    properties={
                        'id': openapi.Schema(type=openapi.TYPE_INTEGER),
                        'created_at': openapi.Schema(type=openapi.TYPE_STRING),
                        'updated_at': openapi.Schema(type=openapi.TYPE_STRING),
                    },
                ),
            )
        },
    )
    def get(self, request):
        sessions = ChatSession.objects.filter(user=request.user).values('id', 'thread_id', 'title', 'created_at', 'updated_at')
        return Response(list(sessions))


class ChatSessionDeleteView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        tags=['Chatbot'],
        operation_description="Delete a chat session and its history.",
        responses={204: 'Deleted', 404: 'Not found'},
    )
    def delete(self, request, session_id):
        try:
            session = ChatSession.objects.get(pk=session_id, user=request.user)
        except ChatSession.DoesNotExist:
            return Response({'detail': 'Session not found.'}, status=status.HTTP_404_NOT_FOUND)
        session.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ── Knowledge base ──────────────────────────────────────────────────────────────

class KnowledgeBaseView(APIView):
    """
    Knowledge-base documents for the authenticated user.

    POST   /api/chatbot/knowledge-base/         → upload one or more documents
    GET    /api/chatbot/knowledge-base/         → list documents
    DELETE /api/chatbot/knowledge-base/<id>/    → delete a document (and its file)

    Uploaded files (PDF, TXT, CSV, XLSX, XLS, DOCX) are stored, then their text
    is extracted asynchronously in a Celery task so the request returns
    immediately.
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    @swagger_auto_schema(
        tags=['Chatbot'],
        operation_description='List the knowledge-base documents for the authenticated user.',
        responses={200: 'List of documents'},
    )
    def get(self, request):
        docs = KnowledgeBaseDocument.objects.filter(user=request.user).values(
            'id', 'original_filename', 'file_type', 'associated_country',
            'hosted_url', 'status', 'error', 'task_id', 'created_at', 'updated_at',
        )
        return Response(list(docs))

    @swagger_auto_schema(
        tags=['Chatbot'],
        operation_description=(
            "Upload knowledge-base documents (PDF, TXT, CSV, XLSX, XLS, DOCX). "
            "Text extraction runs asynchronously; poll the document status to "
            "know when it is completed."
        ),
        manual_parameters=[
            openapi.Parameter(
                'files', openapi.IN_FORM,
                description='One or more documents (PDF, TXT, CSV, XLSX, XLS, DOCX).',
                type=openapi.TYPE_FILE, required=True,
            ),
        ],
        responses={202: openapi.Response(description='Documents queued for extraction.')},
    )
    def post(self, request):
        files = request.FILES.getlist('files') or request.FILES.getlist('file')
        if not files:
            return Response(
                {'detail': 'No files provided. Use the "files" form field.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        extractor = DataExtractor()
        created = []
        rejected = []

        for upload in files:
            if not extractor.is_supported(upload.name):
                rejected.append({
                    'original_filename': upload.name,
                    'detail': 'Unsupported file type. Allowed: PDF, TXT, CSV, XLSX, XLS, DOCX.',
                })
                continue

            # Immediate guard so an exhausted allowance is a clean 403 rather
            # than a document that silently fails in the background worker.
            if request.user.company_id:
                from billing.exceptions import PlanLimitReached
                from billing.services import assert_can_use_credits

                try:
                    assert_can_use_credits(
                        request.user.company_id, 'kb_document_extraction')
                except PlanLimitReached as exc:
                    rejected.append({
                        'original_filename': upload.name,
                        'detail': exc.detail,
                        'code': PlanLimitReached.default_code,
                    })
                    continue

            doc = KnowledgeBaseDocument.objects.create(
                user=request.user,
                file=upload,
                original_filename=upload.name,
                file_type=os.path.splitext(upload.name)[1].lower().lstrip('.'),
                associated_country=getattr(request.user, 'current_country', '') or '',
            )
            try:
                doc.hosted_url = doc.file.url
            except Exception:  # noqa: BLE001
                doc.hosted_url = ''

            async_result = extract_knowledge_base_document.delay(doc.pk)
            doc.task_id = async_result.id or ''
            doc.save(update_fields=['hosted_url', 'task_id', 'updated_at'])

            created.append({
                'id': doc.pk,
                'original_filename': doc.original_filename,
                'hosted_url': doc.hosted_url,
                'status': doc.status,
                'task_id': doc.task_id,
            })

        if not created:
            return Response(
                {'detail': 'No valid files to process.', 'rejected': rejected},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            {'documents': created, 'rejected': rejected},
            status=status.HTTP_202_ACCEPTED,
        )


class KnowledgeBaseDeleteView(APIView):
    """
    DELETE /api/chatbot/knowledge-base/<document_id>/

    Delete a knowledge-base document and its stored file.
    """
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        tags=['Chatbot'],
        operation_description='Delete a knowledge-base document and its stored file.',
        responses={204: 'Deleted', 404: 'Not found'},
    )
    def delete(self, request, document_id):
        try:
            doc = KnowledgeBaseDocument.objects.get(pk=document_id, user=request.user)
        except KnowledgeBaseDocument.DoesNotExist:
            return Response({'detail': 'Document not found.'}, status=status.HTTP_404_NOT_FOUND)

        # Remove the document's vectors from Pinecone (best-effort).
        try:
            from chatbot.pinecone_service import PineconeService
            PineconeService().delete_document(doc.pk)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to delete Pinecone vectors for doc %s", doc.pk)

        if doc.file:
            doc.file.delete(save=False)
        doc.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)