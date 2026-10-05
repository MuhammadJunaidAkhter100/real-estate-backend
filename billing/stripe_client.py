"""Thin, lazily-initialised Stripe boundary.

Price IDs are resolved from settings (never from request data) so a client can
never choose what it is charged. The module imports cleanly without the
``stripe`` package or credentials configured; only calling into it raises.
"""

import logging

import stripe
from django.conf import settings

from users.models import Company

logger = logging.getLogger(__name__)

_PRICE_ENV_BY_INTERVAL = {
    Company.BillingInterval.MONTHLY: 'STRIPE_PRICE_PRO_MONTHLY',
    Company.BillingInterval.ANNUAL: 'STRIPE_PRICE_PRO_ANNUAL',
}


class StripeNotConfigured(Exception):
    """Raised when a billing call is attempted without Stripe credentials."""


def is_configured():
    return bool(getattr(settings, 'STRIPE_SECRET_KEY', ''))


def _client():
    secret_key = getattr(settings, 'STRIPE_SECRET_KEY', '')
    if not secret_key:
        raise StripeNotConfigured('STRIPE_SECRET_KEY is not configured.')
    return stripe.StripeClient(secret_key)


def price_id_for(interval):
    """Resolve the server-side price ID for a Professional subscription."""
    env_name = _PRICE_ENV_BY_INTERVAL.get(interval)
    if env_name is None:
        raise ValueError(f'Unsupported billing interval: {interval!r}')
    price_id = getattr(settings, env_name, '')
    if not price_id:
        raise StripeNotConfigured(f'{env_name} is not configured.')
    return price_id


def is_professional_price(price_id):
    """True when ``price_id`` is one of our paid Professional prices."""
    if not price_id:
        return False
    return price_id in {
        getattr(settings, 'STRIPE_PRICE_PRO_MONTHLY', ''),
        getattr(settings, 'STRIPE_PRICE_PRO_ANNUAL', ''),
    }


def create_customer(*, email, name, metadata=None):
    client = _client()
    params = {'email': email, 'name': name, 'metadata': metadata or {}}
    return client.customers.create(params)


def create_checkout_session(
    *,
    customer_id,
    price_id,
    client_reference_id,
    success_url,
    cancel_url,
    metadata=None,
):
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
    client = _client()
    return client.billing_portal.sessions.create({
        'customer': customer_id,
        'return_url': return_url,
    })


def retrieve_checkout_session(session_id):
    client = _client()
    return client.checkout.sessions.retrieve(session_id)


def retrieve_subscription(subscription_id):
    client = _client()
    return client.subscriptions.retrieve(subscription_id)


def list_checkout_sessions(customer_id, limit=100):
    client = _client()
    return list(
        client.checkout.sessions.list({'customer': customer_id, 'limit': limit})
        .auto_paging_iter()
    )


def list_subscriptions(customer_id, limit=100):
    client = _client()
    return list(
        client.subscriptions.list({
            'customer': customer_id,
            'limit': limit,
            'status': 'all',
        }).auto_paging_iter()
    )


def delete_customer(customer_id):
    client = _client()
    return client.customers.delete(customer_id)