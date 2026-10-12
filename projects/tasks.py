"""
Celery tasks for projects app
"""
import hashlib
import logging
import mimetypes
from pathlib import PurePosixPath
from urllib.parse import urlparse

import requests
from celery import shared_task
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from projects.models import Project, ProjectDocument
from projects.pdf_extractor import extract_document_text

logger = logging.getLogger(__name__)

User = get_user_model()


def _merge_saved_proposal_images(project_id: int, saved_files: list):
    try:
        project = Project.objects.get(pk=project_id)
    except Project.DoesNotExist:
        return {'success': False, 'error': 'Project not found', 'project_id': project_id}

    if not saved_files:
        return {'success': False, 'error': 'No files to process', 'project_id': project_id}

    project.refresh_from_db()
    proposal_images = project.proposal_images or {}
    if not isinstance(proposal_images, dict):
        proposal_images = {}

    for item in saved_files:
        label = (item.get('label') or '').strip()
        saved_path = item.get('saved_path') or ''
        if not label or not saved_path:
            continue
        label_paths = proposal_images.setdefault(label, [])
        if saved_path not in label_paths:
            label_paths.append(saved_path)

    project.proposal_images = proposal_images
    project.save(update_fields=['proposal_images', 'updated_at'])

    return {
        'success': True,
        'project_id': project.id,
        'uploaded_count': len(saved_files),
        'proposal_images': proposal_images,
    }


@shared_task(bind=True, name='projects.upload_proposal_images')
def upload_proposal_images_task(self, project_id: int, saved_files: list):
    """Merge already-uploaded S3 paths into project.proposal_images.

    saved_files: [{'label': str, 'saved_path': str}, ...]
    Files are uploaded to S3 by the view before this task is queued,
    so this task only handles the DB write.
    """
    return _merge_saved_proposal_images(project_id, saved_files)


@shared_task(bind=True, name='projects.import_scraped_proposal_image')
def import_scraped_proposal_image_task(self, project_id: int, image_url: str):
    """Download a scraped project image into the exterior proposal-image slot."""
    try:
        project = Project.objects.get(pk=project_id)
    except Project.DoesNotExist:
        return {'success': False, 'error': 'Project not found', 'project_id': project_id}

    parsed_url = urlparse(image_url or '')
    if parsed_url.scheme not in ('http', 'https') or not parsed_url.netloc:
        return {'success': False, 'error': 'Invalid image URL', 'project_id': project_id}

    label = 'exterior'
    image_hash = hashlib.sha256(image_url.encode('utf-8')).hexdigest()
    proposal_images = project.proposal_images or {}
    existing_paths = proposal_images.get(label, []) if isinstance(proposal_images, dict) else []
    if any(PurePosixPath(path).name.startswith(image_hash) for path in existing_paths):
        return {
            'success': True,
            'project_id': project_id,
            'uploaded_count': 0,
            'proposal_images': proposal_images,
        }

    try:
        response = requests.get(
            image_url,
            headers={'User-Agent': 'Mozilla/5.0'},
            timeout=30,
        )
        response.raise_for_status()
        content_type = response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
        if not content_type.startswith('image/'):
            return {'success': False, 'error': 'URL did not return an image', 'project_id': project_id}

        extension = PurePosixPath(parsed_url.path).suffix.lower()
        if extension not in {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.avif', '.svg', '.bmp', '.tif', '.tiff'}:
            extension = mimetypes.guess_extension(content_type) or '.jpg'

        filename = f'{image_hash}{extension}'
        title_slug = project.title.replace(' ', '_').lower()
        storage_path = (
            f'projects/{project.id}_{title_slug}'
            f'/proposal_images/{label}/{filename}'
        )

        if default_storage.exists(storage_path):
            saved_path = storage_path
        else:
            saved_path = default_storage.save(
                storage_path,
                ContentFile(response.content, name=filename),
            )

        result = _merge_saved_proposal_images(
            project_id,
            [{'label': label, 'saved_path': saved_path}],
        )
        if not result.get('success'):
            return result

        project.refresh_from_db()
        project_images = project.image if isinstance(project.image, list) else [project.image]
        if image_url in project_images:
            project.image = [path for path in project_images if path != image_url]
            project.save(update_fields=['image', 'updated_at'])

        return result
    except requests.RequestException as exc:
        logger.warning('Could not download scraped project image %s: %s', image_url, exc)
        return {'success': False, 'error': str(exc), 'project_id': project_id}


@shared_task(bind=True, name='projects.extract_document_text')
def extract_document_text_task(self, document_id: int):
    """Extract and cache the text of a ProjectDocument's PDF for chatbot use.

    Also indexes the extracted text into Pinecone so the chatbot can retrieve
    it via `search_knowledge_base`. Project documents are indexed under a
    dedicated id prefix (``project-doc-<id>-*``) with metadata that distinguishes
    them from user knowledge-base uploads.
    """
    from django.utils import timezone

    try:
        document = ProjectDocument.objects.get(pk=document_id)
    except ProjectDocument.DoesNotExist:
        logger.warning(f"extract_document_text_task: document {document_id} not found")
        return {"success": False, "error": "Document not found"}

    if not document.file:
        return {"success": False, "error": "Document has no file"}

    # Only attempt PDFs; other formats aren't supported by the extractor.
    if not str(document.file.name).lower().endswith('.pdf'):
        logger.info(f"extract_document_text_task: skipping non-PDF document {document_id}")
        return {"success": False, "error": "Not a PDF"}

    text = extract_document_text(document.file)
    document.extracted_text = text
    document.extracted_at = timezone.now()
    document.save(update_fields=['extracted_text', 'extracted_at', 'updated_at'])

    chunks_stored = 0
    if text.strip():
        try:
            from chatbot.pinecone_service import PineconeService
            chunks_stored = PineconeService().upsert_project_document(
                document_id=document.id,
                text=text,
                metadata={
                    "source": "project_document",
                    "project_document_id": document.id,
                    "project_id": document.project_id,
                    "project_title": document.project.title,
                    "associated_country": document.project.associated_country,
                    "label": document.label,
                    "original_filename": document.file.name.rsplit('/', 1)[-1],
                },
            )
            logger.info(
                "Pinecone: indexed %d chunks for project document %s",
                chunks_stored, document_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "Failed to index project document %s in Pinecone: %s",
                document_id, exc,
            )

    # For fact-check documents, derive the project's estimated completion date
    # from the extracted text (only when the project field is still blank).
    if document.label == "fact_checks" and text.strip():
        try:
            from projects.completion_extractor import populate_estimated_completion
            populate_estimated_completion(document.project)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "Failed to populate estimated_completion for project %s: %s",
                document.project_id, exc,
            )

    return {
        "success": True,
        "document_id": document_id,
        "chars": len(text),
        "chunks_stored": chunks_stored,
    }


@shared_task(bind=True, name='projects.sync_promotions')
def sync_promotions_task(self):
    """Periodic task to sync status and apply/revert unit price discounts for promotions."""
    from projects.models import Promotion
    try:
        Promotion.sync_all_promotions()
        return {"success": True}
    except Exception as exc:
        logger.exception("Failed to sync promotions: %s", exc)
        return {"success": False, "error": str(exc)}


