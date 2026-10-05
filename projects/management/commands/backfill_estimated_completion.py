"""Backfill Project.estimated_completion from each project's Fact Checks text.

Uses the already-extracted text of the project's ``fact_checks`` document and
OpenAI to derive a short completion date (e.g. "Q4 2026").

Usage:
    python manage.py backfill_estimated_completion         # only blank projects
    python manage.py backfill_estimated_completion --all   # re-extract every project
"""
from django.core.management.base import BaseCommand

from projects.completion_extractor import populate_estimated_completion
from projects.models import Project


class Command(BaseCommand):
    help = "Populate Project.estimated_completion from fact_checks document text."

    def add_arguments(self, parser):
        parser.add_argument(
            '--all',
            action='store_true',
            help='Re-extract for all projects, including those already set.',
        )

    def handle(self, *args, **options):
        qs = Project.objects.all()
        if not options['all']:
            qs = qs.filter(estimated_completion='')

        total = qs.count()
        if not total:
            self.stdout.write(self.style.SUCCESS("No projects to process."))
            return

        self.stdout.write(f"Processing {total} project(s)...")

        updated = 0
        skipped = 0
        for project in qs.iterator():
            value = populate_estimated_completion(project, force=options['all'])
            if value:
                updated += 1
                self.stdout.write(f"  #{project.pk} {project.title!r} -> {value}")
            else:
                skipped += 1

        self.stdout.write(self.style.SUCCESS(
            f"Done. Updated {updated}, unresolved {skipped}."
        ))
