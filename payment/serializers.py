from rest_framework import serializers


class CheckoutRequestSerializer(serializers.Serializer):
    plan = serializers.ChoiceField(
        choices=['pro', 'plus', 'top_tier'],
        required=True,
    )
    interval = serializers.ChoiceField(
        choices=['monthly', 'yearly', 'annual'],
        default='monthly',
    )
    success_url = serializers.URLField(required=False, allow_blank=True)
    cancel_url = serializers.URLField(required=False, allow_blank=True)


class VerifySessionSerializer(serializers.Serializer):
    session_id = serializers.CharField(required=True)
