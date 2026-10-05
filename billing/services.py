"""Billing state, plan-limit enforcement and AI-credit accounting.

Every limited action funnels through here rather than through a controller, so
bulk imports, background tasks and agent tools cannot bypass a check.
"""

import logging
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.utils import timezone

from billing import cache as billing_cache
from billing import plan_limits, stripe_client
from billing.exceptions import PaymentRequired, PlanLimitReached
from billing.models import CreditCharge, UsageCounter
from users.models import Company, User

logger = logging.getLogger(__name__)

GRACE_PERIOD = timedelta(days=settings.BILLING_GRACE_PERIOD_DAYS)
PENDING_TTL = timedelta(hours=settings.BILLING_PENDING_COMPANY_TTL_HOURS)

PAYMENT_REQUIRED_MESSAGE = 'Your subscription requires payment.'

# Statuses that grant unrestricted API access. `past_due` is allowed only while
# its grace window is still open.
_ALLOWED_STATUSES = (Company.Status.ACTIVE, Company.Status.PAST_DUE)


# ── Billing state ────────────────────────────────────────────────────────────

def get_company_state(company_id):
    """Read billing state from the database, cached briefly per company.

    This is intentionally never sourced from the JWT, the request body or a URL
    parameter - the database is the only authority on whether a company may use
    the product.
    """
    cached = billing_cache.get_state(company_id)
    if cached is not None:
        return cached

    company = Company.objects.filter(pk=company_id).only(
        'pk', 'status', 'plan', 'grace_until', 'current_period_end',
        'stripe_customer_id', 'stripe_subscription_id', 'billing_interval',
    ).first()
    if company is None:
        return None

    now = timezone.now()
    grace_until = company.grace_until
    state = {
        'status': company.status,
        'plan': company.plan,
        'grace_until': grace_until.isoformat() if grace_until else None,
        'current_period_end': (
            company.current_period_end.isoformat()
            if company.current_period_end
            else None
        ),
        'billing_interval': company.billing_interval,
        'stripe_customer_id': company.stripe_customer_id,
        'stripe_subscription_id': company.stripe_subscription_id,
        # Whether this company may call the API right now.
        'allowed': is_company_allowed(company, now=now),
    }
    billing_cache.set_state(company_id, state)
    return state


def is_company_allowed(company, now=None):
    """True when the company's plan/status permits API access."""
    if company is None:
        return False
    if company.plan == Company.Plan.MANUAL:
        return True
    if company.status in (Company.Status.ACTIVE,):
        return True
    if company.status == Company.Status.PAST_DUE:
        now = now or timezone.now()
        return company.grace_until is not None and company.grace_until >= now
    return False


def assert_company_allowed(company):
    """Raise `PaymentRequired` (402) unless the company may use the API."""
    if company is None:
        return
    if is_company_allowed(company):
        return
    raise PaymentRequired(company_status=company.status)


def invalidate(company_id):
    billing_cache.invalidate(company_id)


# ── Plan limits ───────────────────────────────────────────────────────────────

class PlanLimitsService:
    """Per-company plan-limit checks.

    Every check locks the company row, so two concurrent requests cannot both
    read "9 of 10 used" and insert an eleventh seat. Callers must run
    `assert_can_add()` inside the same transaction as their insert; when called
    outside one, this opens a transaction of its own to keep the lock meaningful.
    """

    def __init__(self, company_id):
        self.company_id = company_id

    @classmethod
    def for_company(cls, company):
        if isinstance(company, Company):
            return cls(company.pk)
        return cls(company)

    def _locked_plan(self):
        # Serialises every limit decision for this company.
        company = Company.objects.select_for_update().only('pk', 'plan').get(
            pk=self.company_id)
        return company.plan

    def limits(self):
        return limits_for(self._plan())

    def _plan(self):
        plan = Company.objects.only('pk', 'plan').filter(pk=self.company_id) \
            .values_list('plan', flat=True).first()
        return plan or Company.Plan.MANUAL

    def current(self, kind, now=None):
        return current_count(self.company_id, kind, now=now)

    def assert_can_add(self, kind, count=1, *, now=None):
        """Raise `PlanLimitReached` (403) if `count` more of `kind` exceeds the cap.

        Unlimited kinds (`limit is None`) always pass.
        """
        if not transaction.get_connection().in_atomic_block:
            with transaction.atomic():
                return self.assert_can_add(kind, count, now=now)

        plan = self._locked_plan()
        limit = plan_limits.limit_for(plan, kind)
        if limit is None:
            return

        current = current_count(self.company_id, kind, now=now)
        if current + count > limit:
            raise PlanLimitReached(kind=kind, limit=limit, current=current)

    def consume(self, kind, count=1, *, receipt_id=None, now=None):
        return consume(self.company_id, kind, count, receipt_id=receipt_id, now=now)

    def consume_credits(self, operation, *, receipt_id=None):
        return consume_credits(self.company_id, operation, receipt_id=receipt_id)

    def refund_credits(self, operation, *, receipt_id=None):
        return refund_credits(self.company_id, operation, receipt_id=receipt_id)


# ── Usage accounting ─────────────────────────────────────────────────────────

def _counter(company_id, kind, period_start):
    counter, _created = UsageCounter.objects.get_or_create(
        company_id=company_id,
        kind=kind,
        period_start=period_start,
    )
    return counter


def usage_snapshot(company_id, plan=None, now=None):
    """Current usage for every limited kind, shaped for ``GET /billing/status``."""
    now = now or timezone.now()
    period_start = plan_limits.current_period_start(now)

    if plan is None:
        plan = Company.objects.filter(pk=company_id).values_list('plan', flat=True).first()
    plan = plan or Company.Plan.MANUAL

    counts = _roster_counts(company_id)
    periodic = dict(
        UsageCounter.objects.filter(
            company_id=company_id,
            period_start=period_start,
            kind__in=plan_limits.PERIODIC_KINDS,
        ).values_list('kind', 'used')
    )

    usage = {}
    for kind in plan_limits.PERIODIC_KINDS:
        usage[kind] = periodic.get(kind, 0)
    usage.update(counts)
    return usage


def _roster_counts(company_id):
    """Live roster counts.

    `users` counts invited *and* active users (an invited user holds a seat
    before its first login); only inactive users are excluded. `team_managers`
    counts users carrying the manager role.
    """
    rows = (
        User.objects.filter(company_id=company_id)
        .exclude(status=User.Status.INACTIVE)
        .aggregate(
            users=Count('id'),
            team_managers=Count('id', filter=Q(role=User.Role.TEAM_MANAGER)),
        )
    )
    return {'users': rows['users'], 'team_managers': rows['team_managers']}


def limits_for(plan):
    return dict(plan_limits.PLAN_LIMITS.get(plan, plan_limits.PLAN_LIMITS[Company.Plan.MANUAL]))


def current_count(company_id, kind, now=None):
    if kind in plan_limits.ROSTER_KINDS:
        return _roster_counts(company_id)[kind]
    period_start = plan_limits.current_period_start(now)
    return (
        UsageCounter.objects.filter(
            company_id=company_id, kind=kind, period_start=period_start
        ).values_list('used', flat=True)
        .first() or 0
    )


def assert_can_add(company_id, kind, count=1, now=None):
    """Raise `PlanLimitReached` (403) if `count` more of `kind` would exceed the cap.

    Thin wrapper around `PlanLimitsService`; prefer instantiating the service
    directly at call sites.
    """
    return PlanLimitsService(company_id).assert_can_add(kind, count, now=now)


@transaction.atomic
def consume(company_id, kind, cost=1, *, receipt_id=None, now=None):
    """Atomically record `cost` usage of a periodic kind.

    Returns True when this call recorded the usage, and False when a matching
    `CreditCharge` receipt shows a previous attempt already paid for it. Callers
    should proceed either way; only refund when the return value is True.

    Unlimited plans still record usage, so analytics stay complete - they simply
    never raise `PlanLimitReached`.
    """
    company = Company.objects.only('pk', 'plan').get(pk=company_id)
    period_start = plan_limits.current_period_start(now)

    if receipt_id:
        try:
            with transaction.atomic():
                CreditCharge.objects.create(
                    task_id=receipt_id,
                    company_id=company_id,
                    kind=kind,
                    cost=cost,
                )
        except IntegrityError:
            logger.info('Credit charge already applied receipt=%s', receipt_id)
            return False

    counter = _counter(company_id, kind, period_start)
    # Row lock on the counter plus the company lock below: the cap cannot be
    # passed by two concurrent callers.
    Company.objects.select_for_update().only('pk').get(pk=company_id)
    locked = UsageCounter.objects.select_for_update().get(pk=counter.pk)

    limit = plan_limits.limit_for(company.plan, kind)
    if limit is not None and locked.used + cost > limit:
        if receipt_id:
            _drop_receipt(receipt_id)
        raise PlanLimitReached(kind=kind, limit=limit, current=locked.used)

    UsageCounter.objects.filter(pk=locked.pk).update(
        used=locked.used + cost,
        updated_at=timezone.now(),
    )
    return True


def refund(company_id, kind, cost=1, *, receipt_id=None, now=None):
    """Return `cost` usage after a failed operation and drop its receipt.

    Deleting the receipt means a Celery retry re-charges cleanly instead of
    refunding the same work twice.
    """
    period_start = plan_limits.current_period_start(now)
    if not transaction.get_connection().in_atomic_block:
        with transaction.atomic():
            return refund(
                company_id, kind, cost, receipt_id=receipt_id, now=now)

    if receipt_id:
        _drop_receipt(receipt_id)

    counter = UsageCounter.objects.filter(
        company_id=company_id, kind=kind, period_start=period_start
    ).first()
    if counter is None:
        return

    locked = UsageCounter.objects.select_for_update().get(pk=counter.pk)
    UsageCounter.objects.filter(pk=locked.pk).update(
        used=max(locked.used - cost, 0),
        updated_at=timezone.now(),
    )


def _drop_receipt(receipt_id):
    CreditCharge.objects.filter(task_id=receipt_id).delete()


def has_receipt(receipt_id):
    """True when this exact piece of work has already been charged.

    Used to keep retried background jobs from also consuming a *second* quota
    unit (a proposal, say) when only the credit charge is idempotent.
    """
    if not receipt_id:
        return False
    return CreditCharge.objects.filter(task_id=receipt_id).exists()


def consume_credits(company_id, operation, *, receipt_id=None):
    """Meter one AI operation.

    Returns True when usage was recorded now, False when a prior receipt already
    covered this exact work. Unlimited plans record usage and return True.
    """
    cost = plan_limits.CREDIT_COSTS.get(operation)
    if cost is None:
        raise KeyError(f'Unknown metered operation: {operation!r}')
    return consume(company_id, UsageCounter.Kind.AI_CREDITS, cost, receipt_id=receipt_id)


def refund_credits(company_id, operation, *, receipt_id=None):
    cost = plan_limits.CREDIT_COSTS.get(operation, 0)
    if cost:
        refund(company_id, UsageCounter.Kind.AI_CREDITS, cost, receipt_id=receipt_id)


def assert_can_use_credits(company_id, operation, *, cost=1, now=None):
    """Raise `PlanLimitReached` (403) if `operation` would exceed the credit cap.

    A read-only preflight for API entry points, so an exhausted allowance is
    reported as a clean 403 before any work is queued. The authoritative check
    still happens in `consume_credits` when the work actually runs.
    """
    if not transaction.get_connection().in_atomic_block:
        with transaction.atomic():
            return assert_can_use_credits(company_id, operation, cost=cost, now=now)

    plan = Company.objects.only('pk', 'plan').filter(pk=company_id) \
        .values_list('plan', flat=True).first()
    limit = plan_limits.limit_for(plan or Company.Plan.MANUAL, UsageCounter.Kind.AI_CREDITS)
    if limit is None:
        return

    current = current_count(company_id, UsageCounter.Kind.AI_CREDITS, now=now)
    if current + cost > limit:
        raise PlanLimitReached(kind=UsageCounter.Kind.AI_CREDITS, limit=limit, current=current)


def refund_proposal_generation(company, receipt_id):
    """Give back one proposal and its credits after a failed generation.

    Dropping the receipt means a Celery retry claims the quota cleanly instead
    of being treated as already-paid work.
    """
    if company is None:
        return
    refund(company.pk, UsageCounter.Kind.AI_PROPOSALS, 1, receipt_id=None)
    refund_credits(company.pk, 'ai_proposal', receipt_id=receipt_id)


# ── Webhook side effects ─────────────────────────────────────────────────────

def _company_for_customer(customer_id):
    return Company.objects.filter(stripe_customer_id=customer_id).first()


def _company_for_subscription(subscription):
    customer_id = subscription.get('customer')
    if isinstance(customer_id, dict):
        customer_id = customer_id.get('id')
    company = _company_for_customer(customer_id) if customer_id else None
    if company is None:
        subscription_id = subscription.get('id')
        company = Company.objects.filter(stripe_subscription_id=subscription_id).first()
    return company


def _period_end(subscription):
    value = (subscription.get('items') or {}).get('data', [{}])[0].get('current_period_end') \
        or subscription.get('current_period_end')
    if not value:
        return None
    return datetime.fromtimestamp(value, tz=dt_timezone.utc)


def activate_company(company, *, plan=None, interval=None, subscription_id='',
                     customer_id='', period_end=None):
    """Mark a company paid and active. Only webhook handlers call this."""
    fields = ['status', 'updated_at', 'grace_until']
    company.status = Company.Status.ACTIVE
    company.grace_until = None
    if plan:
        company.plan = plan
        fields.append('plan')
    if interval:
        company.billing_interval = interval
        fields.append('billing_interval')
    if subscription_id:
        company.stripe_subscription_id = subscription_id
        fields.append('stripe_subscription_id')
    if customer_id:
        company.stripe_customer_id = customer_id
        fields.append('stripe_customer_id')
    if period_end is not None:
        company.current_period_end = period_end
        fields.append('current_period_end')
    company.save(update_fields=list(set(fields)))
    invalidate(company.pk)
    return company


def mark_past_due(company, *, grace_days=None):
    grace_days = settings.BILLING_GRACE_PERIOD_DAYS if grace_days is None else grace_days
    company.status = Company.Status.PAST_DUE
    company.grace_until = timezone.now() + timedelta(days=grace_days)
    company.save(update_fields=['status', 'grace_until', 'updated_at'])
    invalidate(company.pk)
    return company


def sync_subscription(company, subscription):
    """Mirror a Stripe subscription's plan/status onto the company."""
    status_str = subscription.get('status')
    items = (subscription.get('items') or {}).get('data') or [{}]
    price_id = items[0].get('price') or {}
    if isinstance(price_id, str):
        price_id = {'id': price_id}

    plan = Company.Plan.PROFESSIONAL if stripe_client.is_professional_price(
        price_id.get('id')
    ) else Company.Plan.BASIC

    company.stripe_subscription_id = subscription.get('id') or ''
    company.plan = plan
    company.current_period_end = _period_end(subscription)

    if status_str in ('active', 'trialing'):
        company.status = Company.Status.ACTIVE
        company.grace_until = None
    elif status_str == 'past_due':
        company.status = Company.Status.PAST_DUE
        company.grace_until = timezone.now() + GRACE_PERIOD
    elif status_str in ('canceled', 'unpaid', 'incomplete_expired'):
        # Cancelling drops the company back to the free Basic plan.
        company.status = Company.Status.ACTIVE if plan == Company.Plan.BASIC else Company.Status.CANCELED
        company.plan = Company.Plan.BASIC
        company.grace_until = None
    elif status_str == 'incomplete':
        company.status = Company.Status.PENDING_PAYMENT
    else:
        company.status = Company.Status.PENDING_PAYMENT

    company.save(update_fields=[
        'status', 'plan', 'stripe_subscription_id', 'current_period_end',
        'grace_until', 'updated_at',
    ])
    invalidate(company.pk)
    return company