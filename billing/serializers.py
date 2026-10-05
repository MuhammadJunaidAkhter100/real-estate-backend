from django.db import transaction
from rest_framework import serializers
from rest_framework.settings import api_settings
from rest_framework_simplejwt.tokens import RefreshToken

from api.constants import AVAILABLE_COUNTRIES
from users.models import Company, User
from users.serializers import UserDetailSerializer


class EmailAlreadyRegistered(Exception):
    """Raised when a signup email is already in use (409)."""


class CompanyNameTaken(Exception):
    """Raised when a company name is already in use (409)."""


class RegisterCompanySerializer(serializers.Serializer):
    """Public self-serve company signup.

    Strictly whitelisted: any key outside the eight fields below is rejected
    outright, so a tampered body carrying `status`, `role`, `companyId` or a
    Stripe price ID can never reach the ORM.
    """

    company_name = serializers.CharField(max_length=255, trim_whitespace=True)
    countries = serializers.ListField(
        child=serializers.ChoiceField(choices=list(AVAILABLE_COUNTRIES)),
        allow_empty=False,
    )
    first_name = serializers.CharField(max_length=150, trim_whitespace=True)
    last_name = serializers.CharField(max_length=150, trim_whitespace=True, allow_blank=True)
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, min_length=8, trim_whitespace=False)
    plan = serializers.ChoiceField(choices=[Company.Plan.BASIC, Company.Plan.PROFESSIONAL])
    interval = serializers.ChoiceField(
        choices=[Company.BillingInterval.MONTHLY, Company.BillingInterval.ANNUAL],
    )

    #: Names a client is never allowed to send. Called out explicitly because
    #: they are the fields a tamperer would reach for.
    forbidden_field_names = (
        'status', 'role', 'company', 'companyId', 'company_id',
        'priceId', 'price_id', 'stripe_customer_id', 'stripe_subscription_id',
        'current_period_end', 'grace_until',
    )

    def to_internal_value(self, data):
        # A plain Serializer would silently drop unknown keys; reject instead so
        # over-posting is visible rather than ignored. Keyed as a non-field error
        # so DRF renders it under `non_field_errors`.
        if isinstance(data, dict):
            allowed = set(self.fields)
            unexpected = sorted(set(data) - allowed)
            if unexpected:
                forbidden = [f for f in unexpected if f in self.forbidden_field_names]
                message = f'Unexpected field(s): {", ".join(unexpected)}.'
                if forbidden:
                    message += f' Not allowed: {", ".join(forbidden)}.'
                raise serializers.ValidationError(
                    {api_settings.NON_FIELD_ERRORS_KEY: [serializers.ErrorDetail(
                        message, code='unexpected_field')]}
                )
        return super().to_internal_value(data)

    def validate_company_name(self, value):
        value = ' '.join(value.split())
        if not value:
            raise serializers.ValidationError('Company name is required.')
        if Company.objects.filter(name__iexact=value).exists():
            raise CompanyNameTaken()
        return value

    def validate_email(self, value):
        value = value.strip().lower()
        if User.objects.filter(email__iexact=value).exists():
            raise EmailAlreadyRegistered()
        return value

    def validate_password(self, value):
        from django.contrib.auth.password_validation import validate_password

        validate_password(value)
        return value

    def create(self, validated_data):
        plan = validated_data['plan']
        with transaction.atomic():
            company = Company.objects.create(
                name=validated_data['company_name'],
                operating_countries=list(validated_data['countries']),
                plan=plan,
                billing_interval=validated_data['interval'],
                # Basic is usable immediately; Professional waits for the
                # webhook that confirms payment.
                status=(
                    Company.Status.ACTIVE
                    if plan == Company.Plan.BASIC
                    else Company.Status.PENDING_PAYMENT
                ),
            )
            admin_user = User.objects.create_user(
                email=validated_data['email'],
                first_name=validated_data['first_name'],
                last_name=validated_data['last_name'],
                password=validated_data['password'],
                role=User.Role.COMPANY_ADMIN,
                status=User.Status.ACTIVE,
                countries=list(validated_data['countries']),
                company=company,
            )
        return company, admin_user


def issue_tokens(user):
    """Mint the same token pair the normal login endpoint returns."""
    refresh = RefreshToken.for_user(user)
    return {
        'refresh': str(refresh),
        'access': str(refresh.access_token),
        'user': UserDetailSerializer(user).data,
    }