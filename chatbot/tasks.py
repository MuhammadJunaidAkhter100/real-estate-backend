"""Celery tasks for the chatbot app."""

from __future__ import annotations
import logging
from celery import shared_task
from django.core.files.base import ContentFile
from chatbot.pinecone_service import (CHUNK_OVERLAP, CHUNK_SIZE, PineconeService, PineconeServiceError)
from chatbot.data_extraction import (DataExtractor,ExtractionError,UnsupportedFileType)
from chatbot.models import KnowledgeBaseDocument
from chatbot.website_knowledge import (
    MAX_DOCUMENT_CHARS,
    WEBSITE_DOCUMENT_FILE,
    WebsiteImportError,
    crawl_website,
    normalize_website_url,
    prepare_website_document,
)
logger = logging.getLogger(__name__)


@shared_task(bind=True, name="chatbot.extract_knowledge_base_document")
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
        from billing.exceptions import PlanLimitReached
        from billing.services import consume_credits

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
        chunks_stored = PineconeService().upsert_document(
            document_id=doc.pk,
            text=result.text,
            metadata={
                "user_id": doc.user_id,
                "original_filename": doc.original_filename,
                "file_type": result.file_type,
                "associated_country": doc.associated_country,
            },
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
            from billing.services import refund_credits

            refund_credits(
                company.pk, "kb_document_extraction", receipt_id=receipt)
        logger.warning("KB extraction failed for doc %s: %s", document_id, exc)
        return {"success": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        doc.status = KnowledgeBaseDocument.Status.FAILED
        doc.error = str(exc)
        doc.save(update_fields=["status", "error", "updated_at"])
        logger.exception("Unexpected KB extraction failure for doc %s", document_id)
        return {"success": False, "error": str(exc)}


@shared_task(bind=True, name="chatbot.import_company_website_knowledge")
def import_company_website_knowledge(self, company_id: int, website: str = "", requested_by_id: int | None = None):
    """
    Read a company's website and file its text in the knowledge base.

    The text is stored as one document ("Website Data.txt") and indexed for
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

        if doc.file:
            doc.file.delete(save=False)
        doc.file.save(WEBSITE_DOCUMENT_FILE, ContentFile(text.encode("utf-8")), save=False)
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
    except Exception as exc:  # noqa: BLE001
        doc.status = KnowledgeBaseDocument.Status.FAILED
        doc.error = str(exc)
        doc.save(update_fields=["status", "error", "updated_at"])
        logger.exception("Unexpected website import failure for company %s", company_id)
        return {"success": False, "document_id": doc.pk, "error": str(exc)}
