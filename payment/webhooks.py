"""Stripe webhook event processing."""

from datetime import datetime
from datetime import timezone as dt_timezone
import logging

from django.conf import settings

from payment import services, stripe_client
from payment.plan_limits import PLAN_FREE, normalize_interval, normalize_plan
from users.models import Company

logger = logging.getLogger(__name__)


def _id_of(value):
    if isinstance(value, dict):
        return value.get('id')
    return value


def handle_checkout_session_completed(session):
    """Fired when customer successfully checks out on Stripe Checkout."""
    customer_id = _id_of(session.get('customer'))
    subscription_id = _id_of(session.get('subscription'))
    reference = session.get('client_reference_id') or (session.get('metadata') or {}).get('company_id')

    company = None
    if reference:
        company = Company.objects.filter(pk=reference).first()
    if company is None and customer_id:
        company = Company.objects.filter(stripe_customer_id=customer_id).first()

    if company is None:
        logger.warning('checkout.session.completed received for unknown company/customer: %s', customer_id)
        return

    # Extract price from line items or retrieve subscription details
    price_id = None
    line_items = (session.get('line_items') or {}).get('data') or []
    if line_items:
        price_obj = line_items[0].get('price') or {}
        price_id = price_obj.get('id') if isinstance(price_obj, dict) else str(price_obj)

    if not price_id and subscription_id:
        try:
            sub = stripe_client.retrieve_subscription(subscription_id)
            sub_items = (sub.get('items') or {}).get('data') or []
            if sub_items:
                p = sub_items[0].get('price') or {}
                price_id = p.get('id') if isinstance(p, dict) else str(p)
        except Exception as exc:
            logger.warning('Failed to fetch subscription for price detection: %s', exc)

    detected_plan, detected_interval = stripe_client.plan_and_interval_for_price(price_id)
    plan = detected_plan or company.plan or PLAN_FREE
    interval = detected_interval or company.billing_interval or 'monthly'

    logger.info(
        'Activating company %s on plan=%s, interval=%s, sub=%s',
        company.pk, plan, interval, subscription_id,
    )

    services.activate_company(
        company,
        plan=plan,
        interval=interval,
        subscription_id=subscription_id or '',
        customer_id=customer_id or '',
    )


def handle_invoice_paid(invoice):
    """Fired when an invoice is successfully paid (recurring renewal)."""
    customer_id = _id_of(invoice.get('customer'))
    subscription_id = _id_of(
        invoice.get('parent', {}).get('subscription_details', {}).get('subscription')
        or invoice.get('subscription')
    )

    company = None
    if subscription_id:
        company = Company.objects.filter(stripe_subscription_id=subscription_id).first()
    if company is None and customer_id:
        company = Company.objects.filter(stripe_customer_id=customer_id).first()

    if company is None:
        logger.warning('invoice.paid for unknown customer %s', customer_id)
        return

    # Update period end
    period_end = invoice.get('period_end') or (invoice.get('lines', {}).get('data', [{}])[0].get('period', {}).get('end'))
    period_end_dt = None
    if period_end:
        period_end_dt = datetime.fromtimestamp(period_end, tz=dt_timezone.utc)

    services.activate_company(company, period_end=period_end_dt)
    logger.info('Renewed company %s invoice paid, active until %s', company.pk, period_end_dt)


def handle_invoice_payment_failed(invoice):
    """Fired when payment fails for an invoice (card declined, expired, etc.)."""
    customer_id = _id_of(invoice.get('customer'))
    subscription_id = _id_of(
        invoice.get('parent', {}).get('subscription_details', {}).get('subscription')
        or invoice.get('subscription')
    )

    company = None
    if subscription_id:
        company = Company.objects.filter(stripe_subscription_id=subscription_id).first()
    if company is None and customer_id:
        company = Company.objects.filter(stripe_customer_id=customer_id).first()

    if company is None:
        logger.warning('invoice.payment_failed for unknown customer %s', customer_id)
        return

    logger.warning('Payment failed for company %s, entering grace period', company.pk)
    services.mark_past_due(company)


def handle_customer_subscription_updated(subscription):
    """Fired when plan changes, upgrades, or subscription status updates."""
    customer_id = _id_of(subscription.get('customer'))
    subscription_id = subscription.get('id')

    company = None
    if subscription_id:
        company = Company.objects.filter(stripe_subscription_id=subscription_id).first()
    if company is None and customer_id:
        company = Company.objects.filter(stripe_customer_id=customer_id).first()

    if company is None:
        logger.warning('customer.subscription.updated for unknown customer %s', customer_id)
        return

    services.sync_subscription(company, subscription)


def handle_customer_subscription_deleted(subscription):
    """Fired when a subscription is canceled or expires."""
    customer_id = _id_of(subscription.get('customer'))
    subscription_id = subscription.get('id')

    company = None
    if subscription_id:
        company = Company.objects.filter(stripe_subscription_id=subscription_id).first()
    if company is None and customer_id:
        company = Company.objects.filter(stripe_customer_id=customer_id).first()

    if company is None:
        logger.warning('customer.subscription.deleted for unknown customer %s', customer_id)
        return

    # Revert company to Free plan
    company.plan = PLAN_FREE
    company.status = Company.Status.ACTIVE
    company.stripe_subscription_id = ''
    company.grace_until = None
    company.save(update_fields=['plan', 'status', 'stripe_subscription_id', 'grace_until', 'updated_at'])
    services.invalidate(company.pk)
    logger.info('Subscription deleted: company %s reverted to Free plan', company.pk)


HANDLERS = {
    'checkout.session.completed': handle_checkout_session_completed,
    'invoice.paid': handle_invoice_paid,
    'invoice.payment_failed': handle_invoice_payment_failed,
    'customer.subscription.updated': handle_customer_subscription_updated,
    'customer.subscription.deleted': handle_customer_subscription_deleted,
}


def dispatch(event):
    """Dispatch Stripe event object to the corresponding handler."""
    event_type = event.get('type')
    handler = HANDLERS.get(event_type)
    if handler is None:
        logger.debug('Unhandled Stripe event: %s', event_type)
        return False

    obj = event.get('data', {}).get('object', {}) or {}
    handler(obj)
    return True
