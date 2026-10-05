"""Backfill cached text for project documents (brochure / floor plan / fact sheet).

Usage:
    python manage.py extract_document_texts          # only docs not yet extracted
    python manage.py extract_document_texts --all    # re-extract every document
    python manage.py extract_document_texts --async  # enqueue via Celery instead
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from projects.models import ProjectDocument
from projects.pdf_extractor import extract_document_text


class Command(BaseCommand):
    help = "Extract and cache text from project document PDFs for chatbot use."

    def add_arguments(self, parser):
        parser.add_argument(
            '--all',
            action='store_true',
            help='Re-extract all documents, including ones already extracted.',
        )
        parser.add_argument(
            '--async',
            dest='use_async',
            action='store_true',
            help='Enqueue extraction via Celery instead of running inline.',
        )

    def handle(self, *args, **options):
        qs = ProjectDocument.objects.all()
        if not options['all']:
            qs = qs.filter(extracted_text='')

        total = qs.count()
        if not total:
            self.stdout.write(self.style.SUCCESS("No documents to extract."))
            return

        self.stdout.write(f"Processing {total} document(s)...")

        if options['use_async']:
            from projects.tasks import extract_document_text_task
            for doc_id in qs.values_list('id', flat=True):
                extract_document_text_task.delay(doc_id)
            self.stdout.write(self.style.SUCCESS(f"Enqueued {total} extraction task(s)."))
            return

        done = 0
        skipped = 0
        for document in qs.iterator():
            if not document.file or not str(document.file.name).lower().endswith('.pdf'):
                skipped += 1
                continue
            text = extract_document_text(document.file)
            document.extracted_text = text
            document.extracted_at = timezone.now()
            document.save(update_fields=['extracted_text', 'extracted_at', 'updated_at'])
            done += 1
            self.stdout.write(f"  [{document.id}] {document.project.title} / {document.label}: {len(text)} chars")

        self.stdout.write(self.style.SUCCESS(f"Done. Extracted {done}, skipped {skipped} (non-PDF/empty)."))
