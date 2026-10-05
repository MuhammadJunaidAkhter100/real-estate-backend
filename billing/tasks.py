"""Billing maintenance jobs, driven by Celery beat.

`billing_state` is server-side truth derived from Stripe, never from anything a
client sends.
"""

import logging

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from billing import services, stripe_client
from users.models import Company

logger = logging.getLogger(__name__)

# Bound the number of Stripe API calls per run.
CLEANUP_BATCH_SIZE = 50


@shared_task(name='billing.suspend_overdue_companies')
def suspend_overdue_companies() -> dict[str, object]:
    """Suspend companies whose grace window after a failed payment has elapsed."""
    now = timezone.now()
    overdue = list(
        Company.objects.filter(
            status=Company.Status.PAST_DUE,
            grace_until__isnull=False,
            grace_until__lt=now,
        ).only('pk', 'status')
    )

    suspended = 0
    for company in overdue:
        with transaction.atomic():
            locked = Company.objects.select_for_update().filter(pk=company.pk).first()
            if locked is None or locked.status != Company.Status.PAST_DUE:
                continue
            if locked.grace_until is None or locked.grace_until >= now:
                continue
            locked.status = Company.Status.SUSPENDED
            locked.save(update_fields=['status', 'updated_at'])
        services.invalidate(company.pk)
        suspended += 1

    logger.info('suspend_overdue_companies: suspended=%s', suspended)
    return {'suspended': suspended}


def _has_paid_or_live_subscription(customer_id: str) -> bool:
    """True when Stripe shows the customer paid or still holds a subscription.

    Raises on Stripe failure so the caller can skip deletion rather than destroy
    a company whose billing state could not be verified.
    """
    for session in stripe_client.list_checkout_sessions(customer_id):
        if session.get('payment_status') == 'paid' or session.get('status') == 'complete':
            return True

    live = {'active', 'trialing', 'past_due', 'unpaid', 'incomplete'}
    for subscription in stripe_client.list_subscriptions(customer_id):
        if subscription.get('status') in live:
            return True
    return False


@shared_task(name='billing.cleanup_pending_companies')
def cleanup_pending_companies() -> dict[str, object]:
    """Remove abandoned Professional signups that never completed a payment.

    A company is only deleted when it has no subscription id *and* Stripe
    confirms there is no paid checkout session or live subscription for its
    customer. If Stripe cannot be reached, nothing is deleted.
    """
    ttl = timezone.timedelta(hours=settings.BILLING_PENDING_COMPANY_TTL_HOURS)
    cutoff = timezone.now() - ttl

    candidates = list(
        Company.objects.filter(
            status=Company.Status.PENDING_PAYMENT,
            created_at__lt=cutoff,
            stripe_subscription_id='',
        ).only(
            'pk', 'name', 'stripe_customer_id', 'status', 'stripe_subscription_id',
        )[:CLEANUP_BATCH_SIZE]
    )

    deleted, skipped_unverified, skipped_paid = 0, 0, 0
    for company in candidates:
        customer_id = company.stripe_customer_id
        if customer_id:
            try:
                if _has_paid_or_live_subscription(customer_id):
                    logger.info(
                        'cleanup: keeping company=%s, Stripe shows a paid or live subscription',
                        company.pk,
                    )
                    skipped_paid += 1
                    continue
            except Exception:
                logger.exception(
                    'cleanup: could not verify Stripe state for company=%s, skipping',
                    company.pk,
                )
                skipped_unverified += 1
                continue

            try:
                stripe_client.delete_customer(customer_id)
            except Exception:
                logger.exception(
                    'cleanup: could not delete Stripe customer for company=%s',
                    company.pk,
                )

        with transaction.atomic():
            Company.objects.filter(pk=company.pk).delete()
        services.invalidate(company.pk)
        deleted += 1

    logger.info(
        'cleanup_pending_companies: deleted=%s kept_paid=%s kept_unverified=%s',
        deleted, skipped_paid, skipped_unverified,
    )
    return {
        'deleted': deleted,
        'kept_paid': skipped_paid,
        'kept_unverified': skipped_unverified,
    }