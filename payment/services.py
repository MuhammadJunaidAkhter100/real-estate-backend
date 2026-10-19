"""Payment, subscription lifecycle, and usage metering services."""

from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
import logging

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from payment import cache, plan_limits, stripe_client
from payment.exceptions import PlanLimitReached
from payment.models import CreditCharge, UsageCounter
from users.models import Company, User

logger = logging.getLogger(__name__)

DEFAULT_GRACE_PERIOD_DAYS = 5


# ── Roster & Counter Snapshot ────────────────────────────────────────────────

def _roster_counts(company_id):
    """Live roster counts for active/invited seats."""
    rows = (
        User.objects.filter(company_id=company_id)
        .exclude(status=User.Status.INACTIVE)
        .aggregate(
            users=Count('id'),
            agents=Count('id', filter=Q(role=User.Role.AGENT)),
            team_managers=Count('id', filter=Q(role=User.Role.TEAM_MANAGER)),
        )
    )
    return {
        'users': rows['users'] or 0,
        'agents': rows['agents'] or 0,
        'team_managers': rows['team_managers'] or 0,
    }


def current_count(company_id, kind, now=None):
    """Current count for a given kind (roster or periodic)."""
    if kind in plan_limits.ROSTER_KINDS:
        return _roster_counts(company_id).get(kind, 0)
    period_start = plan_limits.current_period_start(now)
    return (
        UsageCounter.objects.filter(
            company_id=company_id, kind=kind, period_start=period_start
        ).values_list('used', flat=True).first()
        or 0
    )


def limits_for(plan):
    """Return dictionary of limits for plan."""
    norm_plan = plan_limits.normalize_plan(plan)
    return dict(plan_limits.PLAN_LIMITS.get(norm_plan, plan_limits.PLAN_LIMITS[plan_limits.PLAN_MANUAL]))


def usage_snapshot(company_id, plan=None):
    """Detailed breakdown of current usage vs limit for each metric."""
    if plan is None:
        plan = Company.objects.filter(pk=company_id).values_list('plan', flat=True).first()
    plan = plan_limits.normalize_plan(plan)

    roster = _roster_counts(company_id)
    period_start = plan_limits.current_period_start()

    counters = dict(
        UsageCounter.objects.filter(
            company_id=company_id, period_start=period_start
        ).values_list('kind', 'used')
    )

    kinds = ('users', 'agents', 'team_managers', 'ai_credits', 'ai_proposals')
    snapshot = {}
    for kind in kinds:
        limit = plan_limits.limit_for(plan, kind)
        if kind in roster:
            used = roster[kind]
        else:
            used = counters.get(kind, 0)
        snapshot[kind] = {
            'used': used,
            'limit': limit,
            'unlimited': limit is None,
            'remaining': (limit - used) if limit is not None else None,
        }
    return snapshot


# ── Metering & Credit Charges ────────────────────────────────────────────────

@transaction.atomic
def consume(company_id, kind, cost=1, *, receipt_id=None, now=None):
    """Atomically record `cost` usage of a periodic kind.

    Returns True when usage was recorded now, False when prior receipt already paid.
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
        except Exception:
            # Duplicate receipt - already charged
            return False

    counter, _ = UsageCounter.objects.get_or_create(
        company_id=company_id,
        kind=kind,
        period_start=period_start,
        defaults={'used': 0},
    )

    # Lock row to prevent race conditions
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
    """Return `cost` usage after a failed operation and drop receipt."""
    period_start = plan_limits.current_period_start(now)
    if not transaction.get_connection().in_atomic_block:
        with transaction.atomic():
            return refund(company_id, kind, cost, receipt_id=receipt_id, now=now)

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
    if not receipt_id:
        return False
    return CreditCharge.objects.filter(task_id=receipt_id).exists()


def consume_credits(company_id, operation, *, receipt_id=None):
    """Meter one AI operation."""
    cost = plan_limits.CREDIT_COSTS.get(operation)
    if cost is None:
        raise KeyError(f'Unknown metered operation: {operation!r}')
    return consume(company_id, UsageCounter.Kind.AI_CREDITS, cost, receipt_id=receipt_id)


def refund_credits(company_id, operation, *, receipt_id=None):
    cost = plan_limits.CREDIT_COSTS.get(operation, 0)
    if cost:
        refund(company_id, UsageCounter.Kind.AI_CREDITS, cost, receipt_id=receipt_id)


def assert_can_use_credits(company_id, operation, *, cost=1, now=None):
    """Raise PlanLimitReached (403) if operation would exceed credit cap."""
    plan = Company.objects.only('pk', 'plan').filter(pk=company_id).values_list('plan', flat=True).first()
    limit = plan_limits.limit_for(plan or plan_limits.PLAN_FREE, UsageCounter.Kind.AI_CREDITS)
    if limit is None:
        return

    current = current_count(company_id, UsageCounter.Kind.AI_CREDITS, now=now)
    if current + cost > limit:
        raise PlanLimitReached(kind=UsageCounter.Kind.AI_CREDITS, limit=limit, current=current)


def refund_proposal_generation(company, receipt_id):
    """Refund 1 proposal and its AI credits."""
    if company is None:
        return
    refund(company.pk, UsageCounter.Kind.AI_PROPOSALS, 1, receipt_id=None)
    refund_credits(company.pk, 'ai_proposal', receipt_id=receipt_id)


# ── PlanLimitsService ────────────────────────────────────────────────────────

class PlanLimitsService:
    """Service to enforce roster caps and plan limits."""

    def __init__(self, company_id):
        self.company_id = company_id

    @classmethod
    def for_company(cls, company):
        if isinstance(company, Company):
            return cls(company.pk)
        return cls(company)

    def _locked_plan(self):
        company = Company.objects.select_for_update().only('pk', 'plan').get(pk=self.company_id)
        return plan_limits.normalize_plan(company.plan)

    def _plan(self):
        plan = Company.objects.only('pk', 'plan').filter(pk=self.company_id).values_list('plan', flat=True).first()
        return plan_limits.normalize_plan(plan)

    def limits(self):
        return limits_for(self._plan())

    def current(self, kind, now=None):
        return current_count(self.company_id, kind, now=now)

    def assert_can_add(self, kind, count=1, *, now=None):
        """Raise PlanLimitReached (403) if count more of kind exceeds cap."""
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


def assert_can_add(company_id, kind, count=1, now=None):
    return PlanLimitsService(company_id).assert_can_add(kind, count, now=now)


# ── State Caching & Subscription Lifecycle ───────────────────────────────────

def get_company_state(company_id):
    """Retrieve company payment status, with caching."""
    cached = cache.get_state(company_id)
    if cached is not None:
        return cached

    company = Company.objects.filter(pk=company_id).first()
    if company is None:
        return None

    now = timezone.now()
    allowed = True
    if company.status in (Company.Status.CANCELED, Company.Status.SUSPENDED):
        allowed = False
    elif company.status == Company.Status.PAST_DUE:
        allowed = bool(company.grace_until and company.grace_until > now)

    state = {
        'company_id': company.pk,
        'status': company.status,
        'plan': plan_limits.normalize_plan(company.plan),
        'billing_interval': company.billing_interval or 'monthly',
        'allowed': allowed,
        'grace_until': company.grace_until.isoformat() if company.grace_until else None,
        'current_period_end': company.current_period_end.isoformat() if company.current_period_end else None,
        'has_stripe_customer': bool(company.stripe_customer_id),
        'has_active_subscription': bool(company.stripe_subscription_id and allowed),
    }
    cache.set_state(company_id, state)
    return state


def invalidate(company_id):
    cache.invalidate(company_id)


def activate_company(company, *, plan=None, interval=None, subscription_id='',
                     customer_id='', period_end=None):
    """Mark company active and sync subscription fields. Called by webhooks."""
    fields = ['status', 'updated_at', 'grace_until']
    company.status = Company.Status.ACTIVE
    company.grace_until = None

    if plan:
        company.plan = plan_limits.normalize_plan(plan)
        fields.append('plan')
    if interval:
        company.billing_interval = plan_limits.normalize_interval(interval)
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
    """Flag company as past_due with a grace period."""
    if grace_days is None:
        grace_days = getattr(settings, 'PAYMENT_GRACE_PERIOD_DAYS', DEFAULT_GRACE_PERIOD_DAYS)

    company.status = Company.Status.PAST_DUE
    company.grace_until = timezone.now() + timedelta(days=grace_days)
    company.save(update_fields=['status', 'grace_until', 'updated_at'])
    invalidate(company.pk)
    return company


def sync_subscription(company, subscription):
    """Mirror Stripe subscription state to Company."""
    status_str = subscription.get('status')
    items = (subscription.get('items') or {}).get('data') or [{}]
    price_obj = items[0].get('price') or {}
    price_id = price_obj.get('id') if isinstance(price_obj, dict) else str(price_obj)

    sub_meta = subscription.get('metadata') or {}
    meta_plan = sub_meta.get('plan')
    meta_interval = sub_meta.get('interval')

    if meta_plan:
        plan = plan_limits.normalize_plan(meta_plan)
        interval = plan_limits.normalize_interval(meta_interval or 'monthly')
    else:
        detected_plan, detected_interval = stripe_client.plan_and_interval_for_price(price_id)
        plan = detected_plan or company.plan or plan_limits.PLAN_FREE
        interval = detected_interval or company.billing_interval or 'monthly'

    # Current period end
    period_end_ts = (
        items[0].get('current_period_end')
        or subscription.get('current_period_end')
    )
    period_end = (
        datetime.fromtimestamp(period_end_ts, tz=dt_timezone.utc)
        if period_end_ts else None
    )

    company.stripe_subscription_id = subscription.get('id') or ''
    company.plan = plan
    company.billing_interval = interval
    company.current_period_end = period_end

    grace_days = getattr(settings, 'PAYMENT_GRACE_PERIOD_DAYS', DEFAULT_GRACE_PERIOD_DAYS)

    if status_str in ('active', 'trialing'):
        company.status = Company.Status.ACTIVE
        company.grace_until = None
    elif status_str == 'past_due':
        company.status = Company.Status.PAST_DUE
        if not company.grace_until:
            company.grace_until = timezone.now() + timedelta(days=grace_days)
    elif status_str in ('canceled', 'unpaid', 'incomplete_expired'):
        company.plan = plan_limits.PLAN_FREE
        company.status = Company.Status.ACTIVE
        company.grace_until = None
        company.stripe_subscription_id = ''
    elif status_str == 'incomplete':
        company.status = Company.Status.PENDING_PAYMENT
    else:
        company.status = Company.Status.PENDING_PAYMENT

    company.save(update_fields=[
        'status', 'plan', 'billing_interval', 'stripe_subscription_id',
        'current_period_end', 'grace_until', 'updated_at',
    ])
    invalidate(company.pk)
    return company
