"""Shared-cache helpers for payment and subscription state."""

import logging

from django.core.cache import caches

logger = logging.getLogger(__name__)

TTL_SECONDS = 60


def _get_cache():
    if 'payment' in caches:
        return caches['payment']
    return caches['default']


def _state_key(company_id):
    return f'company-payment-state:{company_id}'


def get_state(company_id):
    """Return cached company payment state, or None on miss/error."""
    try:
        return _get_cache().get(_state_key(company_id))
    except Exception:
        logger.warning('Payment cache unavailable (get company=%s)', company_id)
        return None


def set_state(company_id, state, ttl=TTL_SECONDS):
    """Store company payment state in cache."""
    try:
        _get_cache().set(_state_key(company_id), state, ttl)
    except Exception:
        logger.warning('Payment cache unavailable (set company=%s)', company_id)


def invalidate(company_id):
    """Drop cached payment state. Called by webhooks and state transitions."""
    try:
        _get_cache().delete(_state_key(company_id))
    except Exception:
        logger.warning('Payment cache unavailable (delete company=%s)', company_id)
