"""Celery tasks for the chatbot app."""

from __future__ import annotations
import logging
from celery import shared_task
from chatbot.pinecone_service import PineconeService
from chatbot.data_extraction import (DataExtractor,ExtractionError,UnsupportedFileType)
from chatbot.models import KnowledgeBaseDocument
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
