"""Single source of truth for subscription plans, limits, and AI-credit costs."""

from datetime import datetime
from datetime import timezone as dt_timezone

from django.utils import timezone

# Plans definitions
PLAN_FREE = 'free'
PLAN_PRO = 'pro'
PLAN_PLUS = 'plus'
PLAN_TOP_TIER = 'top_tier'
PLAN_MANUAL = 'manual'

# Backward compatibility aliases
PLAN_BASIC = 'basic'
PLAN_PROFESSIONAL = 'professional'

PLAN_LIMITS = {
    PLAN_FREE: {
        'name': 'Free',
        'monthly_price': 0,
        'yearly_price': 0,
        'users': 6,          # 1 manager + 5 agents
        'agents': 5,
        'team_managers': 1,
        'ai_credits': 100,
        'ai_proposals': 0,   # Not included in Free
        'features': {
            'lead_management': True,
            'task_management': True,
            'task_automation': False,
            'commission_intelligence': True,
            'team_management': True,
            'ai_proposal_generation': False,
            'whatsapp_integration': True,
            'ai_calling_agent': True,
            'call_transcript_ai_summary': False,
        },
    },
    PLAN_PRO: {
        'name': 'Pro',
        'monthly_price': 49,
        'yearly_price': 39,  # Billed yearly $468/yr ($39/mo)
        'users': 12,         # 2 managers + 10 agents
        'agents': 10,
        'team_managers': 2,
        'ai_credits': 250,
        'ai_proposals': None,
        'features': {
            'lead_management': True,
            'task_management': True,
            'task_automation': True,
            'commission_intelligence': True,
            'team_management': True,
            'ai_proposal_generation': True,
            'whatsapp_integration': True,
            'ai_calling_agent': True,
            'call_transcript_ai_summary': True,
        },
    },
    PLAN_PLUS: {
        'name': 'Plus',
        'badge': 'Most Popular',
        'monthly_price': 99,
        'yearly_price': 79,  # Billed yearly $948/yr ($79/mo)
        'users': 25,         # 5 managers + 20 agents
        'agents': 20,
        'team_managers': 5,
        'ai_credits': 500,
        'ai_proposals': None,
        'features': {
            'lead_management': True,
            'task_management': True,
            'task_automation': True,
            'commission_intelligence': True,
            'team_management': True,
            'ai_proposal_generation': True,
            'whatsapp_integration': True,
            'ai_calling_agent': True,
            'call_transcript_ai_summary': True,
        },
    },
    PLAN_TOP_TIER: {
        'name': 'Top Tier',
        'badge': 'Premium',
        'monthly_price': 199,
        'yearly_price': 159,  # Billed yearly $1908/yr ($159/mo)
        'users': None,        # Unlimited
        'agents': None,       # Unlimited
        'team_managers': None,# Unlimited
        'ai_credits': 1000,
        'ai_proposals': None,
        'features': {
            'lead_management': True,
            'task_management': True,
            'task_automation': True,
            'commission_intelligence': True,
            'team_management': True,
            'ai_proposal_generation': True,
            'whatsapp_integration': True,
            'ai_calling_agent': True,
            'call_transcript_ai_summary': True,
        },
    },
    PLAN_MANUAL: {
        'name': 'Manual / Superadmin',
        'monthly_price': 0,
        'yearly_price': 0,
        'users': None,
        'agents': None,
        'team_managers': None,
        'ai_credits': None,
        'ai_proposals': None,
        'features': {
            'lead_management': True,
            'task_management': True,
            'task_automation': True,
            'commission_intelligence': True,
            'team_management': True,
            'ai_proposal_generation': True,
            'whatsapp_integration': True,
            'ai_calling_agent': True,
            'call_transcript_ai_summary': True,
        },
    },
}

# Alias mapping
PLAN_ALIASES = {
    PLAN_BASIC: PLAN_FREE,
    PLAN_PROFESSIONAL: PLAN_PRO,
}

CREDIT_COSTS = {
    'chat_turn': 1,
    'ai_proposal': 10,
    'calling_agent_call': 5,
    'transcript_processing': 1,
    'kb_document_extraction': 1,
}

PERIODIC_KINDS = ('ai_proposals', 'ai_credits')
ROSTER_KINDS = ('users', 'agents', 'team_managers')


def normalize_plan(plan):
    """Normalize plan names and aliases."""
    if not plan:
        return PLAN_FREE
    plan = str(plan).lower()
    return PLAN_ALIASES.get(plan, plan)


def normalize_interval(interval):
    """Normalize monthly/yearly/annual intervals."""
    if not interval:
        return 'monthly'
    interval = str(interval).lower()
    if interval in ('annual', 'yearly', 'year'):
        return 'yearly'
    return 'monthly'


def limit_for(plan, kind):
    """Return the cap for ``kind`` on ``plan``, or ``None`` when unlimited."""
    norm_plan = normalize_plan(plan)
    config = PLAN_LIMITS.get(norm_plan, PLAN_LIMITS[PLAN_MANUAL])
    return config.get(kind)


def is_unlimited(plan, kind):
    return limit_for(plan, kind) is None


def current_period_start(now=None):
    """Start of the current UTC calendar month."""
    now = now or timezone.now()
    return datetime(
        year=now.year,
        month=now.month,
        day=1,
        tzinfo=dt_timezone.utc,
    )
