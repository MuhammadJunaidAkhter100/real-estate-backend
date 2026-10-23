"""Shared-cache helpers for payment and subscription state."""

import logging

from django.conf import settings
from django.core.cache import caches

logger = logging.getLogger(__name__)

TTL_SECONDS = 60
DEV_PLAN_BYPASS_KEY = 'development-plan-bypass:{company_id}'


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


def development_plan_bypass_is_active(company_id):
    """Return whether a temporary development-only company bypass is active."""
    if not settings.DEBUG:
        return False
    try:
        return bool(
            caches['billing'].get(
                DEV_PLAN_BYPASS_KEY.format(company_id=company_id)
            )
        )
    except Exception:
        logger.warning(
            'Billing cache unavailable (development bypass get company=%s)',
            company_id,
        )
        return False


def enable_development_plan_bypass(company_id):
    """Enable an indefinite development bypass in the shared billing cache."""
    if not settings.DEBUG:
        return False
    try:
        caches['billing'].set(
            DEV_PLAN_BYPASS_KEY.format(company_id=company_id),
            True,
            timeout=None,
        )
        return True
    except Exception:
        logger.exception(
            'Billing cache unavailable (development bypass set company=%s)',
            company_id,
        )
        return False


def disable_development_plan_bypass(company_id):
    """Disable a development bypass in the shared billing cache."""
    if not settings.DEBUG:
        return False
    try:
        caches['billing'].delete(
            DEV_PLAN_BYPASS_KEY.format(company_id=company_id)
        )
        return True
    except Exception:
        logger.exception(
            'Billing cache unavailable (development bypass delete company=%s)',
            company_id,
        )
        return False
