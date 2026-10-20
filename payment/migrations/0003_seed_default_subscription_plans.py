from django.db import migrations


def seed_subscription_plans(apps, schema_editor):
    SubscriptionPlan = apps.get_model('payment', 'SubscriptionPlan')

    plans = [
        {
            'name': 'Free',
            'slug': 'free',
            'description': 'For individuals and small teams getting started.',
            'icon': 'sparkles',
            'badge': '',
            'monthly_price': 0,
            'yearly_price': 0,
            'currency': 'USD',
            'ai_credits': 100,
            'is_credits_unlimited': False,
            'agents': 5,
            'is_agents_unlimited': False,
            'team_managers': 1,
            'is_team_managers_unlimited': False,
            'features': {
                'lead_management': True,
                'task_management': True,
                'task_automation': False,
                'commission_intelligence': True,
                'team_management': True,
                'ai_proposal_generation': False,
                'ai_chatbot': True,
                'whatsapp_integration': True,
                'ai_calling_agent': True,
                'call_transcript_ai_summary': False,
            },
            'custom_features': [],
            'is_active': True,
            'order': 1,
        },
        {
            'name': 'Pro',
            'slug': 'pro',
            'description': 'For professional agencies looking to scale sales & marketing.',
            'icon': 'zap',
            'badge': '',
            'monthly_price': 49,
            'yearly_price': 39,
            'currency': 'USD',
            'ai_credits': 250,
            'is_credits_unlimited': False,
            'agents': 10,
            'is_agents_unlimited': False,
            'team_managers': 2,
            'is_team_managers_unlimited': False,
            'features': {
                'lead_management': True,
                'task_management': True,
                'task_automation': True,
                'commission_intelligence': True,
                'team_management': True,
                'ai_proposal_generation': True,
                'ai_chatbot': True,
                'whatsapp_integration': True,
                'ai_calling_agent': True,
                'call_transcript_ai_summary': True,
            },
            'custom_features': ['Priority email support'],
            'is_active': True,
            'order': 2,
        },
        {
            'name': 'Plus',
            'slug': 'plus',
            'description': 'For growing teams demanding high AI volume and fast turnaround.',
            'icon': 'star',
            'badge': 'Most Popular',
            'monthly_price': 99,
            'yearly_price': 79,
            'currency': 'USD',
            'ai_credits': 500,
            'is_credits_unlimited': False,
            'agents': 20,
            'is_agents_unlimited': False,
            'team_managers': 5,
            'is_team_managers_unlimited': False,
            'features': {
                'lead_management': True,
                'task_management': True,
                'task_automation': True,
                'commission_intelligence': True,
                'team_management': True,
                'ai_proposal_generation': True,
                'ai_chatbot': True,
                'whatsapp_integration': True,
                'ai_calling_agent': True,
                'call_transcript_ai_summary': True,
            },
            'custom_features': ['Dedicated account manager', 'Custom onboarding session'],
            'is_active': True,
            'order': 3,
        },
        {
            'name': 'Top Tier',
            'slug': 'top_tier',
            'description': 'Unlimited agent roster and enterprise capability for premier brokerages.',
            'icon': 'crown',
            'badge': 'Premium',
            'monthly_price': 199,
            'yearly_price': 159,
            'currency': 'USD',
            'ai_credits': 1000,
            'is_credits_unlimited': False,
            'agents': None,
            'is_agents_unlimited': True,
            'team_managers': None,
            'is_team_managers_unlimited': True,
            'features': {
                'lead_management': True,
                'task_management': True,
                'task_automation': True,
                'commission_intelligence': True,
                'team_management': True,
                'ai_proposal_generation': True,
                'ai_chatbot': True,
                'whatsapp_integration': True,
                'ai_calling_agent': True,
                'call_transcript_ai_summary': True,
            },
            'custom_features': ['Unlimited agent seats', 'Dedicated SLA & 24/7 assistance'],
            'is_active': True,
            'order': 4,
        },
    ]

    for plan_data in plans:
        SubscriptionPlan.objects.update_or_create(
            slug=plan_data['slug'],
            defaults=plan_data,
        )


def unseed_subscription_plans(apps, schema_editor):
    SubscriptionPlan = apps.get_model('payment', 'SubscriptionPlan')
    SubscriptionPlan.objects.filter(slug__in=['free', 'pro', 'plus', 'top_tier']).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('payment', '0002_subscriptionplan'),
    ]

    operations = [
        migrations.RunPython(seed_subscription_plans, unseed_subscription_plans),
    ]
