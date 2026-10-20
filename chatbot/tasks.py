"""Celery tasks for the chatbot app."""

from __future__ import annotations
import logging
from datetime import timedelta
from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from django.core.files.base import ContentFile
from django.db.models import Q
from django.utils import timezone
from chatbot.pinecone_service import (CHUNK_OVERLAP, CHUNK_SIZE, PineconeService, PineconeServiceError)
from chatbot.data_extraction import (DataExtractor, ExtractionError, UnsupportedFileType)
from chatbot.models import KnowledgeBaseDocument, PublicKnowledgeBaseDocument
from chatbot.website_knowledge import (
    MAX_DOCUMENT_CHARS,
    WEBSITE_DOCUMENT_FILE,
    WebsiteImportError,
    crawl_website,
    normalize_website_url,
    prepare_website_document,
    save_website_document_versions,
)
logger = logging.getLogger(__name__)

# One stuck call (OpenAI, Pinecone, a slow website) must not hold the only
# worker slot for ever. The soft limit raises inside the task, which marks the
# document failed; the hard limit kills the process a minute later.
KB_TASK_SOFT_TIME_LIMIT = 10 * 60
KB_TASK_TIME_LIMIT = KB_TASK_SOFT_TIME_LIMIT + 60

# A worker killed mid-task (out of memory, a restart) never marks its document
# failed, so it would show "processing" for ever. Past these ages the worker is
# gone. Processing must stay above KB_TASK_TIME_LIMIT.
KB_PROCESSING_STALLED_AFTER = timedelta(minutes=15)
KB_PENDING_STALLED_AFTER = timedelta(hours=1)

STALLED_ERROR = "Processing stopped before it finished. Please try again."
TIMED_OUT_ERROR = "Processing took too long. Please try again."


def fail_stalled_documents(documents) -> int:
    """Mark the documents in `documents` whose worker is gone as failed.

    Returns how many were marked.
    """
    now = timezone.now()
    stalled = documents.filter(
        Q(status=KnowledgeBaseDocument.Status.PROCESSING,
          updated_at__lt=now - KB_PROCESSING_STALLED_AFTER)
        | Q(status=KnowledgeBaseDocument.Status.PENDING,
            updated_at__lt=now - KB_PENDING_STALLED_AFTER)
    )
    # Read first: on SQLite even an UPDATE that matches nothing takes the
    # database write lock, and this runs on every knowledge-base list.
    if not stalled.exists():
        return 0
    return stalled.update(
        status=KnowledgeBaseDocument.Status.FAILED,
        error=STALLED_ERROR,
        updated_at=now,
    )


def fail_stalled_public_documents(documents) -> int:
    """Mark public KB documents whose worker is gone as failed."""
    now = timezone.now()
    stalled = documents.filter(
        Q(status=PublicKnowledgeBaseDocument.Status.PROCESSING,
          updated_at__lt=now - KB_PROCESSING_STALLED_AFTER)
        | Q(status=PublicKnowledgeBaseDocument.Status.PENDING,
            updated_at__lt=now - KB_PENDING_STALLED_AFTER)
    )
    if not stalled.exists():
        return 0
    return stalled.update(
        status=PublicKnowledgeBaseDocument.Status.FAILED,
        error=STALLED_ERROR,
        updated_at=now,
    )



@shared_task(
    bind=True,
    name="chatbot.extract_knowledge_base_document",
    soft_time_limit=KB_TASK_SOFT_TIME_LIMIT,
    time_limit=KB_TASK_TIME_LIMIT,
)
def extract_knowledge_base_document(self, document_id: int):
    """
    Extract text from an uploaded knowledge-base document.

    Reads the stored file, runs the format-specific extractor, and persists
    the extracted text + metadata back onto the `KnowledgeBaseDocument` row.
    """
    try:
        doc = KnowledgeBaseDocument.objects.get(pk=document_id)
    except KnowledgeBaseDocument.DoesNotExist:
        logger.warning("extract_knowledge_base_document: doc %s not found", document_id)
        return {"success": False, "error": "Document not found"}

    doc.status = KnowledgeBaseDocument.Status.PROCESSING
    doc.error = ""
    doc.task_id = self.request.id or ""
    doc.save(update_fields=["status", "error", "task_id", "updated_at"])

    # AI extraction costs 1 credit, charged once per document.
    company = getattr(doc.user, "company", None)
    receipt = f"kb-extraction:{doc.pk}"
    charged_here = False
    if company is not None:
        from payment.exceptions import PlanLimitReached
        from payment.services import consume_credits

        try:
            charged_here = consume_credits(
                company.pk, "kb_document_extraction", receipt_id=receipt)
        except PlanLimitReached:
            doc.status = KnowledgeBaseDocument.Status.FAILED
            doc.error = "AI credit limit reached."
            doc.save(update_fields=["status", "error", "updated_at"])
            logger.warning("KB extraction skipped for doc %s: credit limit reached", document_id)
            return {
                "success": False,
                "error": "AI credit limit reached.",
                "code": "PLAN_LIMIT_REACHED",
                "document_id": doc.pk,
            }

    try:
        doc.file.open("rb")
        try:
            data = doc.file.read()
        finally:
            doc.file.close()

        result = DataExtractor().extract(data, doc.original_filename)   
        logger.info("Extracted %d chars from document %s (%s)", result.char_count, document_id, result.file_type)     
        meta = {
            "user_id": doc.user_id,
            "original_filename": doc.original_filename,
            "file_type": result.file_type,
            "associated_country": doc.associated_country,
        }
        if doc.user and getattr(doc.user, "company_id", None):
            meta["company_id"] = doc.user.company_id

        chunks_stored = PineconeService().upsert_document(
            document_id=doc.pk,
            text=result.text,
            metadata=meta,
        )
        
        logger.info("Chunks stored for document %s: %d", document_id, chunks_stored)

        doc.file_type = result.file_type
        doc.status = KnowledgeBaseDocument.Status.COMPLETED
        doc.save(update_fields=["file_type", "status", "updated_at"])

        return {
            "success": True,
            "document_id": doc.pk,
            "char_count": result.char_count,
            "file_type": result.file_type,
            "chunks_stored": chunks_stored,
        }

    except (UnsupportedFileType, ExtractionError) as exc:
        doc.status = KnowledgeBaseDocument.Status.FAILED
        doc.error = str(exc)
        doc.save(update_fields=["status", "error", "updated_at"])
        if charged_here:
            from payment.services import refund_credits

            refund_credits(
                company.pk, "kb_document_extraction", receipt_id=receipt)
        logger.warning("KB extraction failed for doc %s: %s", document_id, exc)
        return {"success": False, "error": str(exc)}
    except SoftTimeLimitExceeded:
        doc.status = KnowledgeBaseDocument.Status.FAILED
        doc.error = TIMED_OUT_ERROR
        doc.save(update_fields=["status", "error", "updated_at"])
        logger.warning("KB extraction timed out for doc %s", document_id)
        return {"success": False, "error": TIMED_OUT_ERROR}
    except Exception as exc:  # noqa: BLE001
        doc.status = KnowledgeBaseDocument.Status.FAILED
        doc.error = str(exc)
        doc.save(update_fields=["status", "error", "updated_at"])
        logger.exception("Unexpected KB extraction failure for doc %s", document_id)
        return {"success": False, "error": str(exc)}


@shared_task(
    bind=True,
    name="chatbot.import_company_website_knowledge",
    soft_time_limit=KB_TASK_SOFT_TIME_LIMIT,
    time_limit=KB_TASK_TIME_LIMIT,
)
def import_company_website_knowledge(self, company_id: int, website: str = "", requested_by_id: int | None = None):
    """
    Read a company's website and file its text in the knowledge base.

    The text is stored as one document ("Website Data.docx") and indexed for
    search. Running it again replaces the previous import, so a company only
    ever has one website document. Not charged: it is part of onboarding.
    """
    from users.models import Company

    company = Company.objects.filter(pk=company_id).first()
    if company is None:
        logger.warning("import_company_website_knowledge: company %s not found", company_id)
        return {"success": False, "error": "Company not found"}

    website = normalize_website_url(website or company.website)
    if not website:
        logger.info("import_company_website_knowledge: company %s has no website", company_id)
        return {"success": False, "error": "No website to import"}

    owner = company.users.filter(pk=requested_by_id).first() if requested_by_id else None
    doc = prepare_website_document(company, website, owner=owner)
    if doc is None:
        logger.warning("import_company_website_knowledge: company %s has no user", company_id)
        return {"success": False, "error": "Company has no user"}

    doc.status = KnowledgeBaseDocument.Status.PROCESSING
    doc.task_id = self.request.id or ""
    doc.save(update_fields=["status", "task_id", "updated_at"])

    try:
        content = crawl_website(website)
        text = content.as_text()

        save_website_document_versions(doc, content, text)
        try:
            doc.hosted_url = doc.file.url
        except Exception:  # noqa: BLE001
            doc.hosted_url = ""
        doc.source_url = content.website
        doc.save(update_fields=["file", "hosted_url", "source_url", "updated_at"])

        service = PineconeService()
        chunks_stored = service.upsert_document(
            document_id=doc.pk,
            text=text,
            metadata={
                "user_id": doc.user_id,
                "company_id": company.pk,
                "original_filename": doc.original_filename,
                "file_type": doc.file_type,
                "associated_country": doc.associated_country,
                "source": KnowledgeBaseDocument.Source.WEBSITE.value,
                "source_url": content.website,
            },
        )
        # A shorter site than last time leaves old chunks behind; clear them.
        max_chunks = MAX_DOCUMENT_CHARS // (CHUNK_SIZE - CHUNK_OVERLAP) + 1
        try:
            service.delete_document_chunks(doc.pk, chunks_stored, max_chunks)
        except PineconeServiceError:
            logger.warning("Could not clear old website chunks for doc %s", doc.pk)

        doc.status = KnowledgeBaseDocument.Status.COMPLETED
        doc.save(update_fields=["status", "updated_at"])
        logger.info(
            "Website import done for company %s: %d pages, %d chunks",
            company_id, len(content.pages), chunks_stored,
        )
        return {
            "success": True,
            "document_id": doc.pk,
            "pages": len(content.pages),
            "char_count": len(text),
            "chunks_stored": chunks_stored,
        }

    except WebsiteImportError as exc:
        doc.status = KnowledgeBaseDocument.Status.FAILED
        doc.error = str(exc)
        doc.save(update_fields=["status", "error", "updated_at"])
        logger.warning("Website import failed for company %s: %s", company_id, exc)
        return {"success": False, "document_id": doc.pk, "error": str(exc)}
    except SoftTimeLimitExceeded:
        doc.status = KnowledgeBaseDocument.Status.FAILED
        doc.error = TIMED_OUT_ERROR
        doc.save(update_fields=["status", "error", "updated_at"])
        logger.warning("Website import timed out for company %s", company_id)
        return {"success": False, "document_id": doc.pk, "error": TIMED_OUT_ERROR}
    except Exception as exc:  # noqa: BLE001
        doc.status = KnowledgeBaseDocument.Status.FAILED
        doc.error = str(exc)
        doc.save(update_fields=["status", "error", "updated_at"])
        logger.exception("Unexpected website import failure for company %s", company_id)
        return {"success": False, "document_id": doc.pk, "error": str(exc)}


@shared_task(
    bind=True,
    name="chatbot.extract_public_knowledge_base_document",
    soft_time_limit=KB_TASK_SOFT_TIME_LIMIT,
    time_limit=KB_TASK_TIME_LIMIT,
)
def extract_public_knowledge_base_document(self, document_id: int):
    """
    Extract text from an uploaded public knowledge-base document and index it in Pinecone.
    """
    try:
        doc = PublicKnowledgeBaseDocument.objects.get(pk=document_id)
    except PublicKnowledgeBaseDocument.DoesNotExist:
        logger.warning("extract_public_knowledge_base_document: doc %s not found", document_id)
        return {"success": False, "error": "Document not found"}

    doc.status = PublicKnowledgeBaseDocument.Status.PROCESSING
    doc.error = ""
    doc.task_id = self.request.id or ""
    doc.save(update_fields=["status", "error", "task_id", "updated_at"])

    try:
        doc.file.open("rb")
        try:
            data = doc.file.read()
        finally:
            doc.file.close()

        result = DataExtractor().extract(data, doc.original_filename)
        logger.info(
            "Extracted %d chars from public document %s (%s)",
            result.char_count, document_id, result.file_type,
        )

        meta = {
            "original_filename": doc.original_filename,
            "file_type": result.file_type,
        }

        chunks_stored = PineconeService().upsert_public_document(
            document_id=doc.pk,
            text=result.text,
            metadata=meta,
        )

        logger.info("Chunks stored for public document %s: %d", document_id, chunks_stored)

        doc.file_type = result.file_type
        doc.chunk_count = chunks_stored
        doc.status = PublicKnowledgeBaseDocument.Status.COMPLETED
        doc.save(update_fields=["file_type", "chunk_count", "status", "updated_at"])

        return {
            "success": True,
            "document_id": doc.pk,
            "char_count": result.char_count,
            "file_type": result.file_type,
            "chunks_stored": chunks_stored,
        }

    except (UnsupportedFileType, ExtractionError) as exc:
        doc.status = PublicKnowledgeBaseDocument.Status.FAILED
        doc.error = str(exc)
        doc.save(update_fields=["status", "error", "updated_at"])
        logger.warning("Public KB extraction failed for doc %s: %s", document_id, exc)
        return {"success": False, "error": str(exc)}
    except SoftTimeLimitExceeded:
        doc.status = PublicKnowledgeBaseDocument.Status.FAILED
        doc.error = TIMED_OUT_ERROR
        doc.save(update_fields=["status", "error", "updated_at"])
        logger.warning("Public KB extraction timed out for doc %s", document_id)
        return {"success": False, "error": TIMED_OUT_ERROR}
    except Exception as exc:  # noqa: BLE001
        doc.status = PublicKnowledgeBaseDocument.Status.FAILED
        doc.error = str(exc)
        doc.save(update_fields=["status", "error", "updated_at"])
        logger.exception("Unexpected Public KB extraction failure for doc %s", document_id)
        return {"success": False, "error": str(exc)}

