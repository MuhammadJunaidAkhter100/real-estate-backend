import json
import logging
import os
import uuid
from datetime import timedelta

from django.http import StreamingHttpResponse
from django.utils import timezone
from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema
from rest_framework import exceptions, status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from chatbot.data_extraction import DataExtractor
from chatbot.public_chat import PUBLIC_MESSAGE_MAX_CHARS, astream_reply, clean_history
from chatbot.models import ChatSession, KnowledgeBaseDocument, PublicKnowledgeBaseDocument
from chatbot.graph import delete_history, get_history, stream_response
from chatbot.throttles import (
    PublicChatBurstThrottle,
    PublicChatDailyThrottle,
    PublicChatGlobalThrottle,
)
from chatbot.tasks import (
    extract_knowledge_base_document,
    extract_public_knowledge_base_document,
    fail_stalled_documents,
    fail_stalled_public_documents,
    import_company_website_knowledge,
)
from chatbot.website_knowledge import (
    normalize_website_url,
    prepare_website_document,
    website_document,
)
from payment.services import consume_credits, refund_credits
from users.permissions import IsCompanyAdmin
from chatbot.audio_service import create_realtime_transcription_session
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

def _delete_history_quietly(thread_id: str) -> None:
    try:
        delete_history(thread_id)
    except Exception:  # noqa: BLE001
        logger.exception("Could not delete chat history for thread %s", thread_id)


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
    # Logged-in users only. Visitors use PublicChatStreamView.
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        tags=['Chatbot'],
        operation_description=(
            "Stream a chatbot response via SSE. Login required.\n\n"
            "- Answers come from the user's own company data only (projects, units, "
            "documents, knowledge base, leads, tasks, calls, users). The superadmin "
            "gets all companies. Visitors who are not logged in use "
            "`POST /api/chatbot/public/stream/` instead.\n"
            "- A conversation (thread_id) can only be continued by the user who "
            "started it; anyone else gets 403.\n"
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
        try:
            thread_id = str(uuid.UUID(thread_id))
        except ValueError:
            return Response({'detail': 'thread_id must be a UUID.'}, status=status.HTTP_400_BAD_REQUEST)

        user = request.user

        # A conversation is keyed on its thread_id and belongs to the user who
        # started it. Nobody else may continue it: its saved history holds that
        # user's company data.
        session = ChatSession.objects.filter(thread_id=thread_id).first()
        if session is None:
            # History saved under this thread_id without a session belongs to
            # a deleted chat; never hand it to the new owner.
            _delete_history_quietly(thread_id)
            session = ChatSession.objects.create(thread_id=thread_id, user=user)
        elif session.user_id != user.pk:
            return Response(
                {'detail': 'This conversation belongs to someone else. Please start a new chat.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Use first message as session title if not set yet
        if not session.title:
            session.title = message[:80]
            session.save(update_fields=['title'])

        # Use the thread_id exactly as provided (or the new session's UUID)
        thread_id_str = str(session.thread_id)

        current_country = getattr(user, 'current_country', '') or ''
        user_id = user.id
        user_role = getattr(user, 'role', '') or ''

        # Billing: one credit per chat turn, charged before the stream opens so
        # an exhausted allowance is a clean 403 instead of a broken stream.
        charged_here = charge_chat_turn(user)

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


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


async def _public_event_stream(message, history):
    try:
        async for text in astream_reply(message, history):
            yield _sse({"type": "token", "content": text})
        yield _sse({"type": "done"})
    except Exception:  # noqa: BLE001
        logger.exception("Public chat reply failed")
        yield _sse({"type": "error", "detail": "Sorry, something went wrong. Please try again."})


class PublicChatStreamView(APIView):
    """
    POST /api/chatbot/public/stream/

    The website chatbot for visitors who are not logged in. Answers questions
    about Nexusproperty (features, services, pricing, onboarding, contact) from the
    public website only. No login, no tools, no data access, nothing saved on
    the server: the browser keeps the conversation and sends the latest turns.
    """
    # No authentication at all: a stale token in the browser must not turn a
    # visitor's question into a 401.
    authentication_classes = []
    permission_classes = [AllowAny]
    parser_classes = [JSONParser]
    throttle_classes = [PublicChatBurstThrottle, PublicChatDailyThrottle]

    def check_throttles(self, request):
        # Stop at the first limit hit, so a refused request is not counted by
        # the other limits.
        for throttle in self.get_throttles():
            if not throttle.allow_request(request, self):
                self.throttled(request, throttle.wait())

    def throttled(self, request, wait):
        raise exceptions.Throttled(
            wait=wait,
            detail='You are sending messages too quickly. Please wait a moment and try again.',
        )

    @swagger_auto_schema(
        tags=['Chatbot'],
        operation_description=(
            "Website chatbot for visitors who are not logged in. Streams the reply as "
            "Server-Sent Events, in the same format as `/api/chatbot/stream/`.\n\n"
            "- No login (send no Authorization header).\n"
            "- Answers only from the public knowledge base. It has no access to any company's data and "
            "cannot create proposals, leads or tasks.\n"
            "- Nothing is saved on the server. Send the conversation so far in `history`.\n"
            "- Rate limited per visitor (10 a minute, 150 a day); 429 when exceeded."
        ),
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            required=['message'],
            properties={
                'message': openapi.Schema(type=openapi.TYPE_STRING, description='The visitor\'s question'),
                'history': openapi.Schema(
                    type=openapi.TYPE_ARRAY,
                    description='Earlier turns, oldest first (optional)',
                    items=openapi.Schema(
                        type=openapi.TYPE_OBJECT,
                        properties={
                            'role': openapi.Schema(type=openapi.TYPE_STRING, enum=['user', 'assistant']),
                            'content': openapi.Schema(type=openapi.TYPE_STRING),
                        },
                    ),
                ),
            },
        ),
        responses={
            200: openapi.Response(description=(
                'SSE stream.\n'
                'data: {"type": "token", "content": "chunk..."}\n'
                'data: {"type": "done"}\n'
                'data: {"type": "error", "detail": "..."}'
            )),
            400: 'Missing message',
            429: 'Too many messages',
        },
    )
    def post(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        message = data.get('message')
        message = message.strip() if isinstance(message, str) else ''
        if not message:
            return Response({'detail': 'message is required.'}, status=status.HTTP_400_BAD_REQUEST)
        if PUBLIC_MESSAGE_MAX_CHARS and len(message) > PUBLIC_MESSAGE_MAX_CHARS:
            return Response(
                {'detail': f'Please keep your message under {PUBLIC_MESSAGE_MAX_CHARS} characters.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # The cap for all visitors together counts only messages that will
        # really reach OpenAI.
        if not PublicChatGlobalThrottle().allow_request(request, self):
            return Response(
                {'detail': 'The website assistant is busy right now. Please try again later, '
                           'or email hello@nexusproperty.io.'},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        response = StreamingHttpResponse(
            _public_event_stream(message, clean_history(data.get('history'))),
            content_type='text/event-stream',
        )
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
        thread_id = str(session.thread_id)
        session.delete()
        _delete_history_quietly(thread_id)
        return Response(status=status.HTTP_204_NO_CONTENT)


# ── Knowledge base ──────────────────────────────────────────────────────────────

# What the API returns for a knowledge-base document.
KB_DOCUMENT_FIELDS = (
    'id', 'original_filename', 'file_type', 'associated_country',
    'hosted_url', 'status', 'error', 'task_id', 'source', 'source_url',
    'created_at', 'updated_at',
)

# A website import left in pending/processing for longer than this (a worker
# that died mid-run) no longer blocks starting a new one.
WEBSITE_IMPORT_STALE_AFTER = timedelta(minutes=15)


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
        documents = KnowledgeBaseDocument.objects.filter(user=request.user)
        fail_stalled_documents(documents)
        return Response(list(documents.values(*KB_DOCUMENT_FIELDS)))

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
                from payment.exceptions import PlanLimitReached
                from payment.services import assert_can_use_credits

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

def _website_payload(company, doc):
    """The company's website and where its knowledge-base import stands."""
    return {
        'website': company.website,
        'status': doc.status if doc else 'not_started',
        'error': doc.error if doc else '',
        'document': {name: getattr(doc, name) for name in KB_DOCUMENT_FIELDS} if doc else None,
    }


class KnowledgeBaseWebsiteView(APIView):
    """
    The company's own website as a knowledge-base document ("Website Data.txt").

    GET  /api/chatbot/knowledge-base/website/  → the website and its import status
    POST /api/chatbot/knowledge-base/website/  → read the website again

    The website is read automatically when a company signs up with one. POST
    lets a company admin add or change the address later, or retry a failed
    import. The import runs in the background and replaces the previous
    document.
    """
    permission_classes = [IsAuthenticated, IsCompanyAdmin]
    parser_classes = [JSONParser, FormParser, MultiPartParser]

    _response_schema = openapi.Schema(
        type=openapi.TYPE_OBJECT,
        properties={
            'website': openapi.Schema(type=openapi.TYPE_STRING),
            'status': openapi.Schema(
                type=openapi.TYPE_STRING,
                enum=['not_started', 'pending', 'processing', 'completed', 'failed'],
            ),
            'error': openapi.Schema(type=openapi.TYPE_STRING),
            'document': openapi.Schema(
                type=openapi.TYPE_OBJECT,
                description='Same shape as an item of GET /chatbot/knowledge-base/, or null.',
            ),
        },
    )

    @swagger_auto_schema(
        tags=['Chatbot'],
        operation_description=(
            'Company admins only. Returns the company website and the status of '
            'its knowledge-base import: not_started, pending, processing, '
            'completed or failed.'
        ),
        responses={200: _response_schema},
    )
    def get(self, request):
        company = getattr(request.user, 'company', None)
        if company is None:
            return Response(
                {'detail': 'Your user account is not linked to a company.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        doc = website_document(company)
        if doc is not None and fail_stalled_documents(
                KnowledgeBaseDocument.objects.filter(pk=doc.pk)):
            doc.refresh_from_db()
        return Response(_website_payload(company, doc))

    @swagger_auto_schema(
        tags=['Chatbot'],
        operation_description=(
            'Company admins only. Reads the company website and stores its text '
            'in the knowledge base as "Website Data.txt", replacing the previous '
            'import. Send `website` to add or change the address; leave it out '
            'to read the saved one again. Runs in the background: poll GET until '
            'the status is completed or failed. Answers 409 while an import is '
            'already running.'
        ),
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                'website': openapi.Schema(
                    type=openapi.TYPE_STRING,
                    description='Optional. For example https://example.com',
                ),
            },
        ),
        responses={
            202: _response_schema,
            400: 'Invalid or missing website',
            409: 'Import already running',
        },
    )
    def post(self, request):
        company = getattr(request.user, 'company', None)
        if company is None:
            return Response(
                {'detail': 'Your user account is not linked to a company.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        raw = request.data.get('website') if hasattr(request.data, 'get') else None
        if raw in (None, ''):
            website = normalize_website_url(company.website)
            if not website:
                return Response(
                    {'website': ['Add your website address first.']},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        else:
            website = normalize_website_url(str(raw))
            if not website or len(website) > 255:
                return Response(
                    {'website': ['Enter a valid website address.']},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        doc = website_document(company)
        running = (
            KnowledgeBaseDocument.Status.PENDING,
            KnowledgeBaseDocument.Status.PROCESSING,
        )
        if (
            doc is not None
            and doc.status in running
            and doc.updated_at > timezone.now() - WEBSITE_IMPORT_STALE_AFTER
        ):
            return Response(
                {
                    'detail': 'The website is already being imported.',
                    **_website_payload(company, doc),
                },
                status=status.HTTP_409_CONFLICT,
            )

        if website != company.website:
            company.website = website
            company.save(update_fields=['website', 'updated_at'])

        doc = prepare_website_document(company, website, owner=request.user)
        async_result = import_company_website_knowledge.delay(
            company.pk, website, request.user.pk)
        KnowledgeBaseDocument.objects.filter(pk=doc.pk).update(
            task_id=async_result.id or '')
        doc.refresh_from_db()

        return Response(_website_payload(company, doc), status=status.HTTP_202_ACCEPTED)


# ── Admin Public Knowledge Base ───────────────────────────────────────────────

PUBLIC_KB_DOCUMENT_FIELDS = (
    'id', 'original_filename', 'file_type', 'hosted_url',
    'status', 'error', 'task_id', 'chunk_count',
    'created_at', 'updated_at',
)


class IsAdminOrSuperAdmin(BasePermission):
    """Allow access only to platform administrators/superadmins."""
    message = "Only platform administrators can manage public knowledge base documents."

    def has_permission(self, request, view):
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and (
                user.is_superuser
                or user.is_staff
                or getattr(user, 'role', '') in ['superadmin', 'axiyon_admin']
            )
        )


class AdminPublicKnowledgeBaseView(APIView):
    """
    GET  /api/chatbot/admin/public-kb/  → List all public knowledge-base documents.
    POST /api/chatbot/admin/public-kb/  → Upload documents (PDF, TXT, CSV, XLSX, XLS, DOCX)
                                          to be embedded in Pinecone for the public chatbot.
    """
    permission_classes = [IsAuthenticated, IsAdminOrSuperAdmin]
    parser_classes = [MultiPartParser, FormParser]

    @swagger_auto_schema(
        tags=['Admin Public KB'],
        operation_description='List all public knowledge-base documents used by the public chatbot.',
        responses={200: 'List of public documents'},
    )
    def get(self, request):
        documents = PublicKnowledgeBaseDocument.objects.all()
        fail_stalled_public_documents(documents)
        return Response(list(documents.values(*PUBLIC_KB_DOCUMENT_FIELDS)))

    @swagger_auto_schema(
        tags=['Admin Public KB'],
        operation_description=(
            "Upload public knowledge-base documents (PDF, TXT, CSV, XLSX, XLS, DOCX). "
            "Text extraction and Pinecone vectorization run asynchronously."
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

            doc = PublicKnowledgeBaseDocument.objects.create(
                uploaded_by=request.user,
                file=upload,
                original_filename=upload.name,
                file_type=os.path.splitext(upload.name)[1].lower().lstrip('.'),
            )
            try:
                doc.hosted_url = doc.file.url
            except Exception:  # noqa: BLE001
                doc.hosted_url = ''

            async_result = extract_public_knowledge_base_document.delay(doc.pk)
            doc.task_id = async_result.id or ''
            doc.save(update_fields=['hosted_url', 'task_id', 'updated_at'])

            created.append({
                'id': doc.pk,
                'original_filename': doc.original_filename,
                'file_type': doc.file_type,
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


class AdminPublicKnowledgeBaseDeleteView(APIView):
    """
    DELETE /api/chatbot/admin/public-kb/<document_id>/

    Delete a public knowledge-base document, its stored file, and its Pinecone vectors.
    """
    permission_classes = [IsAuthenticated, IsAdminOrSuperAdmin]

    @swagger_auto_schema(
        tags=['Admin Public KB'],
        operation_description='Delete a public knowledge-base document and its Pinecone vectors.',
        responses={204: 'Deleted', 404: 'Not found'},
    )
    def delete(self, request, document_id):
        try:
            doc = PublicKnowledgeBaseDocument.objects.get(pk=document_id)
        except PublicKnowledgeBaseDocument.DoesNotExist:
            return Response({'detail': 'Document not found.'}, status=status.HTTP_404_NOT_FOUND)

        try:
            from chatbot.pinecone_service import PineconeService
            PineconeService().delete_public_document(doc.pk, chunk_count=doc.chunk_count)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to delete Pinecone vectors for public doc %s", doc.pk)

        if doc.file:
            doc.file.delete(save=False)
        doc.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AdminPublicKnowledgeBaseStatusView(APIView):
    """
    GET /api/chatbot/admin/public-kb/<document_id>/status/

    Check the extraction and indexing status of a public knowledge-base document.
    """
    permission_classes = [IsAuthenticated, IsAdminOrSuperAdmin]

    @swagger_auto_schema(
        tags=['Admin Public KB'],
        operation_description='Check status of a single public knowledge-base document.',
        responses={200: 'Document status', 404: 'Not found'},
    )
    def get(self, request, document_id):
        try:
            doc = PublicKnowledgeBaseDocument.objects.get(pk=document_id)
        except PublicKnowledgeBaseDocument.DoesNotExist:
            return Response({'detail': 'Document not found.'}, status=status.HTTP_404_NOT_FOUND)

        return Response({name: getattr(doc, name) for name in PUBLIC_KB_DOCUMENT_FIELDS})


class RealtimeTranscriptionSessionView(APIView):
    """Create a short-lived client secret for realtime audio transcription."""
    permission_classes = [AllowAny]

    @swagger_auto_schema(
        tags=['Chatbot'],
        operation_description=(
            "Create a short-lived OpenAI client secret for realtime transcription. "
            "Use the returned secret from the client to connect to the Realtime API."
        ),
        responses={
            200: openapi.Response(
                description='Realtime transcription session created.',
                schema=openapi.Schema(
                    type=openapi.TYPE_OBJECT,
                    properties={
                        'client_secret': openapi.Schema(type=openapi.TYPE_STRING),
                        'expires_at': openapi.Schema(type=openapi.TYPE_INTEGER),
                        'session': openapi.Schema(type=openapi.TYPE_OBJECT),
                    },
                ),
            ),
            502: 'Could not create a realtime transcription session.',
            503: 'Speech transcription is not configured.',
        },
    )
    def post(self, request):
        return Response(
            create_realtime_transcription_session(),
            status=status.HTTP_200_OK,
        )
