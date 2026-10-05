from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db.models import DecimalField, F, Min
from django.db.models.functions import Coalesce
from django.utils import timezone

from projects.models import Project, Promotion, Unit


def _decimal_to_string(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def compute_budget_fit(
    estimated_budget: Decimal | None,
    lowest_unit_price: Decimal | None,
) -> str:
    if estimated_budget is None or lowest_unit_price is None:
        return 'unknown'
    if lowest_unit_price <= estimated_budget:
        return 'within'
    return 'over'


def lowest_available_unit_price(project_id: int) -> Decimal | None:
    prices = lowest_available_unit_prices_for_projects([project_id])
    return prices.get(project_id)


def lowest_available_unit_prices_for_projects(
    project_ids: list[int],
) -> dict[int, Decimal | None]:
    """Return the minimum available unit price per project in one query."""
    if not project_ids:
        return {}
    effective_price_field = DecimalField(max_digits=12, decimal_places=2)
    rows = (
        Unit.objects.filter(
            project_id__in=project_ids,
            status=Unit.UnitStatus.AVAILABLE,
        )
        .values('project_id')
        .annotate(
            lowest_price=Min(
                Coalesce(
                    F('discounted_price'),
                    F('list_price'),
                    output_field=effective_price_field,
                )
            )
        )
    )
    return {row['project_id']: row['lowest_price'] for row in rows}


def _promotion_end_datetime(promo: Promotion) -> datetime:
    end_dt = promo.end_date
    if (
        end_dt.hour == 0
        and end_dt.minute == 0
        and end_dt.second == 0
    ):
        end_dt = end_dt.replace(hour=23, minute=59, second=59)
    return end_dt


def _caller_facing_promotion_for_project(
    project: Project,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return caller-facing promotion block; expired promotions are never exposed."""
    if now is None:
        now = timezone.now()

    near_expiry_days = getattr(
        settings,
        'CALLING_AGENT_PROMOTION_NEAR_EXPIRY_DAYS',
        7,
    )

    active_promo: Promotion | None = None
    upcoming_promo: Promotion | None = None

    for promo in project.promotions.all().order_by('start_date'):
        computed = promo.compute_status(now)
        if computed == Promotion.Status.EXPIRED:
            continue
        if computed == Promotion.Status.ACTIVE and active_promo is None:
            active_promo = promo
        elif computed == Promotion.Status.UPCOMING and upcoming_promo is None:
            upcoming_promo = promo

    if active_promo is not None:
        end_dt = _promotion_end_datetime(active_promo)
        days_until_end = max(0, (end_dt.date() - now.date()).days)
        is_near_expiring = days_until_end <= near_expiry_days
        status = 'near_expiring' if is_near_expiring else 'active'
        return {
            'status': status,
            'title': active_promo.title,
            'discount_percent': active_promo.discount,
            'start_date': active_promo.start_date.isoformat(),
            'end_date': active_promo.end_date.isoformat(),
            'is_near_expiring': is_near_expiring,
            'days_until_end': days_until_end,
            'discount_source': 'promotion',
        }

    if upcoming_promo is not None:
        return {
            'status': 'upcoming',
            'title': upcoming_promo.title,
            'discount_percent': upcoming_promo.discount,
            'start_date': upcoming_promo.start_date.isoformat(),
            'end_date': upcoming_promo.end_date.isoformat(),
            'is_near_expiring': False,
            'days_until_end': None,
            'discount_source': 'promotion',
        }

    return {
        'status': 'none',
        'title': None,
        'discount_percent': None,
        'start_date': None,
        'end_date': None,
        'is_near_expiring': False,
        'days_until_end': None,
        'discount_source': None,
    }


def unit_pricing_block(
    *,
    list_price: Decimal | None,
    discounted_price: Decimal | None,
    effective_price: Decimal | None,
    has_active_promotion: bool,
) -> dict[str, Any]:
    has_unit_discount = (
        discounted_price is not None
        and list_price is not None
        and discounted_price < list_price
    )
    has_discount = has_unit_discount or (
        discounted_price is not None and list_price is None
    )

    if has_active_promotion and has_unit_discount:
        discount_source = 'both'
    elif has_active_promotion:
        discount_source = 'promotion'
    elif has_discount:
        discount_source = 'unit_price'
    else:
        discount_source = None

    return {
        'list_price': _decimal_to_string(list_price),
        'discounted_price': _decimal_to_string(discounted_price),
        'effective_price': _decimal_to_string(effective_price),
        'has_discount': has_discount,
        'discount_source': discount_source,
    }


def promotion_status_is_active(status: str) -> bool:
    return status in {'active', 'near_expiring'}


def project_has_active_promotion(
    project: Project,
    now: datetime | None = None,
    *,
    promotion_block: dict[str, Any] | None = None,
) -> bool:
    block = promotion_block or _caller_facing_promotion_for_project(project, now=now)
    return promotion_status_is_active(block['status'])


def build_project_promotion_block(project: Project) -> dict[str, Any]:
    return _caller_facing_promotion_for_project(project)


def proposal_summary_snippet(ai_facts: dict[str, Any] | None, max_length: int = 300) -> str:
    if not ai_facts:
        return ''
    parts: list[str] = []
    for key in ('location_label', 'project_title', 'unit_type', 'payment_plan'):
        value = str(ai_facts.get(key) or '').strip()
        if value:
            parts.append(value)
    investment_cases = ai_facts.get('investment_cases') or []
    if isinstance(investment_cases, list) and investment_cases:
        first = investment_cases[0]
        if isinstance(first, dict):
            headline = str(first.get('headline') or first.get('title') or '').strip()
            if headline:
                parts.append(headline)
    text = '; '.join(parts)
    return text[:max_length]
