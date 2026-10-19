from rest_framework import serializers


class CheckoutRequestSerializer(serializers.Serializer):
    plan = serializers.ChoiceField(
        choices=['pro', 'plus', 'top_tier'],
        required=True,
        help_text="Plan to subscribe to: 'pro', 'plus', or 'top_tier'",
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
