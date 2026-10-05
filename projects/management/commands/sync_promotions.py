from django.core.management.base import BaseCommand
from projects.models import Promotion


class Command(BaseCommand):
    help = 'Sync status and apply or revert unit price discounts for all promotions'

    def handle(self, *args, **options):
        self.stdout.write('Syncing promotions...')
        Promotion.sync_all_promotions()
        self.stdout.write(self.style.SUCCESS('Successfully synced promotions.'))
