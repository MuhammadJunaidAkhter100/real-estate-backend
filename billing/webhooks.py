"""Stripe webhook event handling.

This module owns the only code path that may activate a company. It is never
reachable from a request body, a URL parameter or a success page.
"""

import logging

from django.conf import settings

from billing import services, stripe_client
from users.models import Company

logger = logging.getLogger(__name__)


def _id_of(value):
    if isinstance(value, dict):
        return value.get('id')
    return value


def _interval_for_price(price_id):
    from users.models import Company as _Company

    if price_id == getattr(settings, 'STRIPE_PRICE_PRO_ANNUAL', ''):
        return _Company.BillingInterval.ANNUAL
    return _Company.BillingInterval.MONTHLY


def handle_checkout_session_completed(session):
    """A successful checkout activates the company and records Stripe ids."""
    company = services._company_for_customer(_id_of(session.get('customer')))
    if company is None:
        reference = session.get('client_reference_id')
        if reference:
            company = Company.objects.filter(pk=reference).first()
    if company is None:
        logger.warning('checkout.session.completed for unknown customer')
        return

    line_items = (session.get('line_items') or {}).get('data') or [{}]
    price = line_items[0].get('price') or {}
    price_id = price.get('id') or price

    plan = (
        Company.Plan.PROFESSIONAL
        if stripe_client.is_professional_price(price_id)
        else Company.Plan.BASIC
    )
    subscription_id = _id_of(session.get('subscription'))

    services.activate_company(
        company,
        plan=plan,
        interval=_interval_for_price(price_id),
        subscription_id=subscription_id or '',
        customer_id=_id_of(session.get('customer')) or '',
    )


def handle_invoice_paid(invoice):
    company = services._company_for_customer(_id_of(invoice.get('customer')))
    if company is None:
        subscription_id = _id_of(
            invoice.get('parent', {}).get('subscription_details', {}).get('subscription')
            or invoice.get('subscription')
        )
        if subscription_id:
            company = Company.objects.filter(stripe_subscription_id=subscription_id).first()
    if company is None:
        logger.warning('invoice.paid for unknown customer')
        return

    period_end = invoice.get('period_end') or invoice.get('lines', {}).get('data', [{}])[0].get(
        'period', {}
    ).get('end')
    if period_end:
        from datetime import timezone as dt_timezone

        from django.utils import timezone as dj_timezone

        company.current_period_end = dj_timezone.datetime.fromtimestamp(
            period_end, tz=dt_timezone.utc
        )
        company.save(update_fields=['current_period_end', 'updated_at'])

    services.activate_company(company)


def handle_invoice_payment_failed(invoice):
    company = services._company_for_customer(_id_of(invoice.get('customer')))
    if company is None:
        subscription_id = _id_of(
            invoice.get('parent', {}).get('subscription_details', {}).get('subscription')
            or invoice.get('subscription')
        )
        if subscription_id:
            company = Company.objects.filter(stripe_subscription_id=subscription_id).first()
    if company is None:
        logger.warning('invoice.payment_failed for unknown customer')
        return
    services.mark_past_due(company)


def handle_customer_subscription_updated(subscription):
    company = services._company_for_subscription(subscription)
    if company is None:
        logger.warning('customer.subscription.updated for unknown customer')
        return
    services.sync_subscription(company, subscription)


def handle_customer_subscription_deleted(subscription):
    company = services._company_for_subscription(subscription)
    if company is None:
        logger.warning('customer.subscription.deleted for unknown customer')
        return
    # A deleted subscription drops the company back to free Basic rather than
    # leaving it on an unlimited paid plan.
    company.plan = Company.Plan.BASIC
    company.status = Company.Status.ACTIVE
    company.stripe_subscription_id = ''
    company.grace_until = None
    company.save(update_fields=[
        'plan', 'status', 'stripe_subscription_id', 'grace_until', 'updated_at',
    ])
    services.invalidate(company.pk)


HANDLERS = {
    'checkout.session.completed': handle_checkout_session_completed,
    'invoice.paid': handle_invoice_paid,
    'invoice.payment_failed': handle_invoice_payment_failed,
    'customer.subscription.updated': handle_customer_subscription_updated,
    'customer.subscription.deleted': handle_customer_subscription_deleted,
}


def dispatch(event):
    event_type = event.get('type')
    handler = HANDLERS.get(event_type)
    if handler is None:
        logger.info('Ignoring unhandled Stripe event %s', event_type)
        return False
    handler(event.get('data', {}).get('object', {}) or {})
    return True