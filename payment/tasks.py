from datetime import timedelta
import logging
from celery import shared_task
from django.conf import settings
from django.utils import timezone

from payment import services
from users.models import Company

logger = logging.getLogger(__name__)


@shared_task(name='payment.suspend_overdue_companies')
def suspend_overdue_companies():
    """Suspend companies whose payment grace period has expired."""
    now = timezone.now()
    overdue_companies = Company.objects.filter(
        status=Company.Status.PAST_DUE,
        grace_until__lt=now,
    )
    count = 0
    for company in overdue_companies:
        company.status = Company.Status.SUSPENDED
        company.save(update_fields=['status', 'updated_at'])
        services.invalidate(company.pk)
        count += 1
        logger.info('Suspended overdue company %s (grace expired at %s)', company.pk, company.grace_until)

    return f"Suspended {count} overdue companies."


@shared_task(name='payment.cleanup_pending_companies')
def cleanup_pending_companies():
    """Clean up old pending payment registrations."""
    ttl_hours = getattr(settings, 'PAYMENT_PENDING_COMPANY_TTL_HOURS', 48)
    cutoff = timezone.now() - timedelta(hours=ttl_hours)
    pending = Company.objects.filter(
        status=Company.Status.PENDING_PAYMENT,
        created_at__lt=cutoff,
    )
    count = pending.count()
    # Mark as canceled rather than hard-deleting
    pending.update(status=Company.Status.CANCELED)
    return f"Canceled {count} abandoned pending companies."
