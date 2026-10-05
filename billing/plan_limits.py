"""Single source of truth for plan limits and AI-credit costs.

Both the backend enforcement layer and the frontend (which reads these numbers
from ``GET /billing/status``) derive from this module, so weights and caps are
tuned here and nowhere else.
"""

from datetime import datetime
from datetime import timezone as dt_timezone

from django.utils import timezone

from users.models import Company

# Limits are per billing period except `users` and `team_managers`, which are
# standing caps on the company's roster. `None` means unlimited.
PLAN_LIMITS = {
    Company.Plan.BASIC: {
        'users': 10,
        'team_managers': 2,
        'ai_proposals': 20,
        'ai_credits': 500,
    },
    Company.Plan.PROFESSIONAL: {
        'users': None,
        'team_managers': None,
        'ai_proposals': None,
        'ai_credits': None,
    },
    # Companies created by the super-admin "Add New Company" flow never meter.
    Company.Plan.MANUAL: {
        'users': None,
        'team_managers': None,
        'ai_proposals': None,
        'ai_credits': None,
    },
}

# AI credit cost per metered operation. Metering happens at the service layer
# immediately before the AI call; see billing.services.consume_credits().
CREDIT_COSTS = {
    'chat_turn': 1,
    'ai_proposal': 10,
    'calling_agent_call': 5,
    'transcript_processing': 1,
    'kb_document_extraction': 1,
}

# Kinds that reset every billing period.
PERIODIC_KINDS = ('ai_proposals', 'ai_credits')

# Kinds that count live rows rather than accumulated usage. Users still
# waiting on their first login hold a seat, so `invited` counts; only
# `inactive` (deactivated) users are excluded.
ROSTER_KINDS = ('users', 'team_managers')


def limit_for(plan, kind):
    """Return the cap for ``kind`` on ``plan``, or ``None`` when unlimited."""
    return PLAN_LIMITS.get(plan, PLAN_LIMITS[Company.Plan.MANUAL]).get(kind)


def is_unlimited(plan, kind):
    return limit_for(plan, kind) is None


def current_period_start(now=None):
    """Start of the current UTC calendar month.

    Calendar month (not a signup anniversary) is used so a company's usage
    counter resets on the same predictable boundary as its Stripe invoice.
    """
    now = now or timezone.now()
    return datetime(
        year=now.year,
        month=now.month,
        day=1,
        tzinfo=dt_timezone.utc,
    )