"""Shared-cache helpers for billing state.

`CACHES['billing']` is Redis so a webhook invalidation in one worker is visible
to every other worker (the project default cache is LocMemCache, which is
per-process and useless here). Every operation degrades to a no-op if the cache
is unavailable, so callers can always fall back to reading the database.
"""

import logging

from django.core.cache import caches
from django.conf import settings

logger = logging.getLogger(__name__)

CACHE_ALIAS = 'billing'
TTL_SECONDS = 30


def _state_key(company_id):
    return f'company-state:{company_id}'


def get_state(company_id):
    """Return cached billing state, or ``None`` on a miss/outage."""
    try:
        return caches[CACHE_ALIAS].get(_state_key(company_id))
    except Exception:  # noqa: BLE001 - cache must never break the request
        logger.warning('Billing cache unavailable (get company=%s)', company_id)
        return None


def set_state(company_id, state, ttl=TTL_SECONDS):
    try:
        caches[CACHE_ALIAS].set(_state_key(company_id), state, ttl)
    except Exception:  # noqa: BLE001
        logger.warning('Billing cache unavailable (set company=%s)', company_id)


def invalidate(company_id):
    """Drop cached billing state. Called by the webhook and by cron jobs."""
    try:
        caches[CACHE_ALIAS].delete(_state_key(company_id))
    except Exception:  # noqa: BLE001
        logger.warning('Billing cache unavailable (delete company=%s)', company_id)