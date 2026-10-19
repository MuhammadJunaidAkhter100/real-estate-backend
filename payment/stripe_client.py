"""Stripe client integration for payment and subscriptions."""

import logging
import stripe
from django.conf import settings

from payment.exceptions import StripeNotConfigured
from payment.plan_limits import (
    PLAN_PRO,
    PLAN_PLUS,
    PLAN_TOP_TIER,
    normalize_plan,
    normalize_interval,
)

logger = logging.getLogger(__name__)


def is_configured():
    return bool(getattr(settings, 'STRIPE_SECRET_KEY', ''))


def _client():
    secret_key = getattr(settings, 'STRIPE_SECRET_KEY', '')
    if not secret_key:
        raise StripeNotConfigured('STRIPE_SECRET_KEY is not configured.')
    return stripe.StripeClient(secret_key)


def get_price_env_map():
    """Mapping of (plan, interval) to environment variable names."""
    return {
        (PLAN_PRO, 'monthly'): 'STRIPE_PRICE_PRO_MONTHLY',
        (PLAN_PRO, 'yearly'): 'STRIPE_PRICE_PRO_YEARLY',
        (PLAN_PLUS, 'monthly'): 'STRIPE_PRICE_PLUS_MONTHLY',
        (PLAN_PLUS, 'yearly'): 'STRIPE_PRICE_PLUS_YEARLY',
        (PLAN_TOP_TIER, 'monthly'): 'STRIPE_PRICE_TOP_TIER_MONTHLY',
        (PLAN_TOP_TIER, 'yearly'): 'STRIPE_PRICE_TOP_TIER_YEARLY',
    }


def price_id_for(plan, interval='monthly'):
    """Resolve the server-side price ID for a plan and billing interval."""
    plan = normalize_plan(plan)
    interval = normalize_interval(interval)

    # Free and manual plans do not have Stripe price IDs
    if plan in ('free', 'manual'):
        return None

    env_map = get_price_env_map()
    env_name = env_map.get((plan, interval))

    # Backward compatibility fallback for PRO annual
    if not env_name and plan == PLAN_PRO and interval == 'yearly':
        env_name = 'STRIPE_PRICE_PRO_ANNUAL'

    if not env_name:
        raise ValueError(f'Unsupported plan/interval combination: plan={plan}, interval={interval}')

    price_id = getattr(settings, env_name, '')
    if not price_id and plan == PLAN_PRO and interval == 'yearly':
        price_id = getattr(settings, 'STRIPE_PRICE_PRO_ANNUAL', '')

    if not price_id:
        raise StripeNotConfigured(f'Stripe price setting {env_name} is not configured.')

    return price_id


def plan_and_interval_for_price(price_id):
    """Reverse lookup: return (plan, interval) for a given Stripe price ID."""
    if not price_id:
        return ('free', 'monthly')

    env_map = get_price_env_map()
    for (plan, interval), env_name in env_map.items():
        val = getattr(settings, env_name, '')
        if val and val == price_id:
            return (plan, interval)

    # Fallback check for STRIPE_PRICE_PRO_ANNUAL
    if price_id == getattr(settings, 'STRIPE_PRICE_PRO_ANNUAL', ''):
        return (PLAN_PRO, 'yearly')

    return (None, None)


def create_customer(*, email, name, metadata=None):
    """Create a new Stripe customer."""
    client = _client()
    return client.customers.create({
        'email': email,
        'name': name,
        'metadata': metadata or {},
    })


def create_checkout_session(
    *,
    customer_id,
    price_id,
    client_reference_id,
    success_url,
    cancel_url,
    metadata=None,
):
    """Create a Stripe checkout session for subscription."""
    client = _client()
    return client.checkout.sessions.create({
        'mode': 'subscription',
        'customer': customer_id,
        'line_items': [{'price': price_id, 'quantity': 1}],
        'client_reference_id': str(client_reference_id),
        'success_url': success_url,
        'cancel_url': cancel_url,
        'metadata': metadata or {},
    })


def create_portal_session(customer_id, return_url):
    """Create a Stripe Billing Customer Portal session."""
    client = _client()
    return client.billing_portal.sessions.create({
        'customer': customer_id,
        'return_url': return_url,
    })


def retrieve_checkout_session(session_id):
    """Retrieve details for a checkout session."""
    client = _client()
    return client.checkout.sessions.retrieve(session_id)


def retrieve_subscription(subscription_id):
    """Retrieve a subscription by ID."""
    client = _client()
    return client.subscriptions.retrieve(subscription_id)


def cancel_subscription(subscription_id):
    """Cancel a subscription at period end."""
    client = _client()
    return client.subscriptions.update(
        subscription_id,
        {'cancel_at_period_end': True},
    )
