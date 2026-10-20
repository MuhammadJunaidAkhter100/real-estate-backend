from rest_framework import serializers
from rest_framework.settings import api_settings


class EmailAlreadyRegistered(Exception):
    """Raised when a signup email is already in use (409)."""


class CompanyNameTaken(Exception):
    """Raised when a company name is already in use (409)."""


class CheckoutRequestSerializer(serializers.Serializer):
    plan = serializers.CharField(
        required=True,
        help_text="Plan to subscribe to (e.g. 'pro', 'plus', 'top_tier', or any active plan slug)",
    )
    interval = serializers.ChoiceField(
        choices=['monthly', 'yearly', 'annual'],
        default='monthly',
        required=False,
        help_text="Billing cycle: 'monthly' or 'yearly'",
    )
    success_url = serializers.URLField(
        required=False,
        allow_blank=True,
        help_text="Optional frontend URL to redirect upon payment success",
    )
    cancel_url = serializers.URLField(
        required=False,
        allow_blank=True,
        help_text="Optional frontend URL to redirect if customer cancels",
    )


class VerifySessionSerializer(serializers.Serializer):
    session_id = serializers.CharField(
        required=True,
        help_text="Stripe checkout session ID (starts with cs_...)",
    )


class RegisterCompanySerializer(serializers.Serializer):
    """Public self-serve company signup serializer."""

    company_name = serializers.CharField(max_length=255, trim_whitespace=True)
    phone = serializers.CharField(max_length=32, required=False, allow_blank=True, default='')
    website = serializers.URLField(max_length=255, required=False, allow_blank=True, default='')
    countries = serializers.ListField(
        child=serializers.CharField(),
        allow_empty=False,
    )
    first_name = serializers.CharField(max_length=150, trim_whitespace=True)
    last_name = serializers.CharField(max_length=150, trim_whitespace=True, required=False, allow_blank=True, default='')
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, min_length=8, trim_whitespace=False)
    plan = serializers.CharField(required=False, default='free')
    interval = serializers.CharField(required=False, default='monthly')

    forbidden_field_names = (
        'status', 'role', 'company', 'companyId', 'company_id',
        'priceId', 'price_id', 'stripe_customer_id', 'stripe_subscription_id',
        'current_period_end', 'grace_until',
    )

    def to_internal_value(self, data):
        if isinstance(data, dict):
            allowed = set(self.fields)
            unexpected = sorted(set(data) - allowed)
            if unexpected:
                forbidden = [f for f in unexpected if f in self.forbidden_field_names]
                if forbidden:
                    raise serializers.ValidationError(
                        {api_settings.NON_FIELD_ERRORS_KEY: [serializers.ErrorDetail(
                            f'Not allowed: {", ".join(forbidden)}.', code='unexpected_field')]}
                    )
        return super().to_internal_value(data)

    def validate_company_name(self, value):
        from users.models import Company

        value = ' '.join(value.split())
        if not value:
            raise serializers.ValidationError('Company name is required.')
        if Company.objects.filter(name__iexact=value).exists():
            raise CompanyNameTaken()
        return value

    def validate_email(self, value):
        from users.models import User

        value = value.strip().lower()
        if User.objects.filter(email__iexact=value).exists():
            raise EmailAlreadyRegistered()
        return value

    def validate_countries(self, value):
        from api.constants import AVAILABLE_COUNTRIES

        if not value:
            raise serializers.ValidationError('At least one country is required.')
        invalid = [c for c in value if c not in AVAILABLE_COUNTRIES]
        if invalid:
            raise serializers.ValidationError(f"Invalid country: {', '.join(invalid)}")
        return value

    def validate_password(self, value):
        from django.contrib.auth.password_validation import validate_password

        validate_password(value)
        return value

    def create(self, validated_data):
        from django.db import transaction
        from payment import plan_limits
        from users.models import Company, User

        raw_plan = validated_data.get('plan', 'free')
        normalized_plan = plan_limits.normalize_plan(raw_plan)
        interval = validated_data.get('interval', 'monthly')
        if interval == 'annual':
            interval = 'yearly'

        is_paid = normalized_plan in (
            plan_limits.PLAN_PRO,
            plan_limits.PLAN_PLUS,
            plan_limits.PLAN_TOP_TIER,
        )
        status = Company.Status.PENDING_PAYMENT if is_paid else Company.Status.ACTIVE

        with transaction.atomic():
            company = Company.objects.create(
                name=validated_data['company_name'],
                phone=validated_data.get('phone', ''),
                website=validated_data.get('website', ''),
                operating_countries=list(validated_data['countries']),
                plan=raw_plan,
                billing_interval=interval,
                status=status,
            )
            admin_user = User.objects.create_user(
                email=validated_data['email'],
                first_name=validated_data['first_name'],
                last_name=validated_data.get('last_name', ''),
                password=validated_data['password'],
                role=User.Role.COMPANY_ADMIN,
                status=User.Status.ACTIVE,
                countries=list(validated_data['countries']),
                company=company,
            )

            # 1. Super Admin notification (HTML template with plain text fallback)
            from users.tasks import _person, notify_admins
            notify_admins("New company signed up", [
                f"Company: {company.name}",
                f"Admin: {_person(admin_user)}",
                f"Plan: {company.get_plan_display()} ({company.billing_interval})",
                f"Status: {company.get_status_display()}",
                f"Countries: {', '.join(company.operating_countries or []) or '-'}",
                f"Website: {company.website or '-'}",
                f"Phone: {company.phone or '-'}",
            ])

            # # 2. Company Admin welcome email (Professional HTML template)
            # from users.tasks import send_company_admin_welcome_email
            # transaction.on_commit(
            #     lambda: send_company_admin_welcome_email(admin_user, company)
            # )

            # # 3. WhatsApp welcome message (if configured)
            # from whatsapp.tasks import queue_company_admin_welcome
            # queue_company_admin_welcome(
            #     company.pk,
            #     admin_user.pk,
            #     None,
            # )

            # 4. Scraping task (if website provided)
            if company.website:
                from users.tasks import scrape_company_website_task
                transaction.on_commit(
                    lambda: scrape_company_website_task.delay(
                        company.pk,
                        company.website,
                    )
                )

        return company, admin_user


def issue_tokens(user):
    """Mint the same token pair the normal login endpoint returns."""
    from rest_framework_simplejwt.tokens import RefreshToken
    from users.serializers import UserDetailSerializer

    refresh = RefreshToken.for_user(user)
    return {
        'refresh': str(refresh),
        'access': str(refresh.access_token),
        'user': UserDetailSerializer(user).data,
    }


DEFAULT_PLAN_FEATURES = {
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
}


class SubscriptionPlanSerializer(serializers.ModelSerializer):
    """Serializer for admin subscription plan CRUD and public presentation."""

    users = serializers.IntegerField(source='users_limit', read_only=True)
    monthly_price = serializers.DecimalField(max_digits=10, decimal_places=2, coerce_to_string=False, required=False)
    yearly_price = serializers.DecimalField(max_digits=10, decimal_places=2, coerce_to_string=False, required=False)

    class Meta:
        from payment.models import SubscriptionPlan

        model = SubscriptionPlan
        fields = [
            'id',
            'name',
            'slug',
            'description',
            'icon',
            'badge',
            'monthly_price',
            'yearly_price',
            'currency',
            'ai_credits',
            'is_credits_unlimited',
            'agents',
            'is_agents_unlimited',
            'team_managers',
            'is_team_managers_unlimited',
            'users',
            'features',
            'custom_features',
            'is_active',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'slug', 'users', 'created_at', 'updated_at']

    def to_internal_value(self, data):
        if isinstance(data, dict):
            data = data.copy()
            if 'short_description' in data and 'description' not in data:
                data['description'] = data['short_description']
            if 'monthlyPrice' in data and 'monthly_price' not in data:
                data['monthly_price'] = data['monthlyPrice']
            if 'yearlyPrice' in data and 'yearly_price' not in data:
                data['yearly_price'] = data['yearlyPrice']
            if 'credits' in data and 'ai_credits' not in data:
                data['ai_credits'] = data['credits']
            if 'aiCredits' in data and 'ai_credits' not in data:
                data['ai_credits'] = data['aiCredits']
            if 'isCreditsUnlimited' in data and 'is_credits_unlimited' not in data:
                data['is_credits_unlimited'] = data['isCreditsUnlimited']
            if 'isAgentsUnlimited' in data and 'is_agents_unlimited' not in data:
                data['is_agents_unlimited'] = data['isAgentsUnlimited']
            if 'teamManagers' in data and 'team_managers' not in data:
                data['team_managers'] = data['teamManagers']
            if 'isTeamManagersUnlimited' in data and 'is_team_managers_unlimited' not in data:
                data['is_team_managers_unlimited'] = data['isTeamManagersUnlimited']
            if 'customFeatures' in data and 'custom_features' not in data:
                data['custom_features'] = data['customFeatures']
            if 'isActive' in data and 'is_active' not in data:
                data['is_active'] = data['isActive']
            elif 'status' in data and 'is_active' not in data:
                val = str(data['status']).lower()
                data['is_active'] = (val in ('active', 'true', '1'))

            # Merge features with defaults if features dict provided
            feat = data.get('features')
            if isinstance(feat, dict):
                merged = dict(DEFAULT_PLAN_FEATURES)
                merged.update(feat)
                data['features'] = merged
            elif 'features' not in data and self.instance is None:
                data['features'] = dict(DEFAULT_PLAN_FEATURES)

        return super().to_internal_value(data)


