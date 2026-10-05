from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

from django.utils import timezone
from rest_framework import serializers

from calling_agent.models import Call, CallGeneratedAction, CallTranscriptTurn
from projects.models import Project, Unit
from users.models import Lead, Task as UserTask

# LLM often sends lead-status or CRM language as Task.type. Coerce these to
# callback; anything else still 400s.
_CREATE_TASK_TYPE_ALIASES = frozenset({
    UserTask.Type.CALLBACK,
    'follow_up',
    'followup',
    'viewing',
    'site_visit',
    'whatsapp',
    'whatsapp_follow_up',
})


def _slugify_tool_choice(raw: str) -> str:
    return '_'.join(raw.lower().replace('-', ' ').replace('_', ' ').split())


class UserTaskSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserTask
        ref_name = 'CallingAgentUserTask'
        fields = [
            'id',
            'name',
            'description',
            'status',
            'priority',
            'type',
            'due_date',
            'scheduled_at',
            'associated_country',
            'created_at',
            'updated_at',
        ]


class CallGeneratedActionSerializer(serializers.ModelSerializer):
    class Meta:
        model = CallGeneratedAction
        ref_name = 'CallingAgentGeneratedAction'
        fields = [
            'id',
            'action_type',
            'title',
            'payload',
            'status',
            'task',
            'created_at',
            'updated_at',
        ]
        read_only_fields = fields


class CallTranscriptTurnSerializer(serializers.ModelSerializer):
    time_in_call_secs = serializers.SerializerMethodField()

    class Meta:
        model = CallTranscriptTurn
        ref_name = 'CallingAgentTranscriptTurn'
        fields = [
            'turn_index',
            'speaker',
            'message',
            'started_at',
            'time_in_call_secs',
        ]
        read_only_fields = fields

    def get_time_in_call_secs(self, obj: CallTranscriptTurn) -> int | float | None:
        if isinstance(obj.raw, dict):
            val = obj.raw.get('time_in_call_secs')
            if isinstance(val, (int, float)):
                return val
        return None



class CallSerializer(serializers.ModelSerializer):
    duration = serializers.CharField(read_only=True)
    lead_status = serializers.SerializerMethodField()

    class Meta:
        model = Call
        ref_name = 'CallingAgentCall'
        fields = [
            'public_id',
            'lead',
            'lead_name',
            'lead_status',
            'phone_number',
            'outbound_number',
            'direction',
            'trigger',
            'attempt_number',
            'duration',
            'duration_seconds',
            'status',
            'scheduled_for',
            'initiated_at',
            'answered_at',
            'ended_at',
            'created_at',
            'updated_at',
        ]
        read_only_fields = fields

    def get_lead_status(self, obj: Call) -> str | None:
        lead = getattr(obj, 'lead', None)
        if lead is None:
            return None
        return lead.status


class CallSerializerDetails(serializers.ModelSerializer):
    tasks = UserTaskSerializer(many=True, read_only=True)
    generated_actions = CallGeneratedActionSerializer(many=True, read_only=True)
    transcript_turns = CallTranscriptTurnSerializer(many=True, read_only=True)
    prior_calls = serializers.SerializerMethodField()
    duration = serializers.CharField(read_only=True)
    lead_status = serializers.SerializerMethodField()

    class Meta:
        model = Call
        ref_name = 'CallingAgentCallDetails'
        fields = [
            'public_id',
            'lead',
            'lead_name',
            'lead_status',
            'phone_number',
            'outbound_number',
            'direction',
            'trigger',
            'attempt_number',
            'duration',
            'duration_seconds',
            'status',
            'provider_conversation_id',
            'provider_call_id',
            'scheduled_for',
            'initiated_at',
            'answered_at',
            'ended_at',
            'recording_available',
            'transcript_data',
            'transcript_turns',
            'summary',
            'key_sentiments',
            'detected_intents',
            'call_insights',
            'tasks',
            'generated_actions',
            'prior_calls',
            'created_at',
            'updated_at',
        ]
        read_only_fields = fields

    def get_prior_calls(self, obj: Call) -> list[dict[str, Any]]:
        if obj.lead_id is None:
            return []
        prior_calls = (
            Call.objects.filter(lead_id=obj.lead_id)
            .exclude(pk=obj.pk)
            .order_by('-created_at')[:5]
        )
        return CallSerializer(prior_calls, many=True).data

    def get_lead_status(self, obj: Call) -> str | None:
        lead = getattr(obj, 'lead', None)
        if lead is None:
            return None
        return lead.status


class CallAnalyticsSerializer(serializers.Serializer):
    total_calls = serializers.IntegerField()
    completed_calls = serializers.IntegerField()
    failed_calls = serializers.IntegerField()
    no_answer_calls = serializers.IntegerField()
    busy_calls = serializers.IntegerField()
    in_progress_calls = serializers.IntegerField()
    answer_rate = serializers.FloatField()
    failure_rate = serializers.FloatField()
    average_duration_seconds = serializers.FloatField(allow_null=True)
    sentiment_breakdown = serializers.DictField(child=serializers.IntegerField())
    task_conversion_count = serializers.IntegerField()


class ManualCallInitiationSerializer(serializers.Serializer):
    lead_id = serializers.IntegerField(min_value=1)
    idempotency_key = serializers.UUIDField()
    agent_config_key = serializers.CharField(
        max_length=64,
        default='default',
        required=False,
    )


class CallEndSerializer(serializers.Serializer):
    reason = serializers.CharField(
        max_length=500,
        required=False,
        allow_blank=True,
        default='',
        trim_whitespace=True,
    )


class StrictToolSerializer(serializers.Serializer):
    def to_internal_value(self, data: Any) -> dict[str, Any]:
        if isinstance(data, Mapping):
            unknown_fields = set(data) - set(self.get_fields())
            if unknown_fields:
                raise serializers.ValidationError(
                    {
                        field: ['Unknown field.']
                        for field in sorted(unknown_fields)
                    }
                )
        return super().to_internal_value(data)


class EmptyToolSerializer(StrictToolSerializer):
    """Optional reason field satisfies ElevenLabs webhook schema (min 1 property)."""

    reason = serializers.CharField(
        max_length=500,
        required=False,
        allow_blank=True,
        default='',
        trim_whitespace=True,
    )


class KnowledgeSearchToolSerializer(StrictToolSerializer):
    query = serializers.CharField(
        max_length=500,
        min_length=2,
        trim_whitespace=True,
    )
    project_id = serializers.IntegerField(
        min_value=1,
        required=False,
    )
    top_k = serializers.IntegerField(
        min_value=1,
        max_value=5,
        default=3,
        required=False,
    )


class ProjectSearchToolSerializer(StrictToolSerializer):
    country = serializers.CharField(
        max_length=100,
        required=False,
        trim_whitespace=True,
        allow_blank=False,
    )
    location = serializers.CharField(
        max_length=255,
        required=False,
        trim_whitespace=True,
        allow_blank=False,
    )
    project_name = serializers.CharField(
        max_length=255,
        required=False,
        trim_whitespace=True,
        allow_blank=False,
    )
    project_type = serializers.ChoiceField(
        choices=Project.ProjectType.choices,
        required=False,
    )
    property_category = serializers.CharField(
        max_length=100,
        required=False,
        trim_whitespace=True,
        allow_blank=False,
    )
    bedrooms = serializers.IntegerField(
        min_value=0,
        max_value=3,
        required=False,
    )
    min_budget = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=0,
        required=False,
    )
    max_budget = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=0,
        required=False,
    )
    limit = serializers.IntegerField(
        min_value=1,
        max_value=10,
        default=5,
        required=False,
    )

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        min_budget = attrs.get('min_budget')
        max_budget = attrs.get('max_budget')
        if (
            min_budget is not None
            and max_budget is not None
            and min_budget > max_budget
        ):
            raise serializers.ValidationError(
                {'max_budget': ['Must be greater than or equal to min_budget.']}
            )
        return attrs


class UnitSearchToolSerializer(StrictToolSerializer):
    project_id = serializers.IntegerField(min_value=1, required=False)
    unit_id = serializers.IntegerField(min_value=1, required=False)
    unit_label = serializers.CharField(
        max_length=100,
        required=False,
        trim_whitespace=True,
        allow_blank=True,
    )
    label = serializers.CharField(
        max_length=100,
        required=False,
        trim_whitespace=True,
        allow_blank=True,
    )
    unit_name = serializers.CharField(
        max_length=100,
        required=False,
        trim_whitespace=True,
        allow_blank=True,
    )
    search_query = serializers.CharField(
        max_length=200,
        required=False,
        trim_whitespace=True,
        allow_blank=True,
    )
    status = serializers.ChoiceField(
        choices=Unit.UnitStatus.choices,
        required=False,
    )
    category = serializers.CharField(
        max_length=100,
        required=False,
        trim_whitespace=True,
    )
    min_budget = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=0,
        required=False,
    )
    max_budget = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=0,
        required=False,
    )
    min_area_m2 = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        min_value=0,
        required=False,
    )
    max_area_m2 = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        min_value=0,
        required=False,
    )
    limit = serializers.IntegerField(
        min_value=1,
        max_value=10,
        default=5,
        required=False,
    )


    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        min_budget = attrs.get('min_budget')
        max_budget = attrs.get('max_budget')
        if (
            min_budget is not None
            and max_budget is not None
            and min_budget > max_budget
        ):
            raise serializers.ValidationError(
                {'max_budget': ['Must be greater than or equal to min_budget.']}
            )

        min_area = attrs.get('min_area_m2')
        max_area = attrs.get('max_area_m2')
        if (
            min_area is not None
            and max_area is not None
            and min_area > max_area
        ):
            raise serializers.ValidationError(
                {'max_area_m2': ['Must be greater than or equal to min_area_m2.']}
            )
        return attrs


class RequestProposalToolSerializer(StrictToolSerializer):
    project_id = serializers.IntegerField(min_value=1, required=False)
    unit_id = serializers.IntegerField(min_value=1, required=False)
    reason = serializers.CharField(
        max_length=500,
        required=False,
        allow_blank=True,
        default='Lead requested proposal on call.',
        trim_whitespace=True,
    )



class CreateTaskToolSerializer(StrictToolSerializer):
    title = serializers.CharField(
        max_length=255,
        min_length=2,
        trim_whitespace=True,
    )
    priority = serializers.ChoiceField(
        choices=UserTask.Priority.choices,
        default=UserTask.Priority.MEDIUM,
        required=False,
    )
    type = serializers.CharField(
        max_length=64,
        default=UserTask.Type.CALLBACK,
        required=False,
        allow_null=True,
        allow_blank=True,
        trim_whitespace=True,
    )
    scheduled_at = serializers.DateTimeField(
        allow_null=True,
        required=False,
    )
    due_date = serializers.DateField(
        allow_null=True,
        required=False,
    )
    open_ended = serializers.BooleanField(required=False, default=False)
    reason = serializers.CharField(
        max_length=500,
        min_length=2,
        trim_whitespace=True,
    )

    def validate_type(self, value: str | None) -> str:
        if value is None or value == '':
            return UserTask.Type.CALLBACK
        slug = _slugify_tool_choice(value)
        if slug in _CREATE_TASK_TYPE_ALIASES:
            return UserTask.Type.CALLBACK
        raise serializers.ValidationError(f'"{value}" is not a valid choice.')

    def validate_scheduled_at(self, value: datetime | None) -> datetime | None:
        if value is not None and value <= timezone.now():
            raise serializers.ValidationError(
                'Scheduled time cannot be in the past.'
            )
        return value

    def validate_due_date(self, value: date | None) -> date | None:
        if value is not None and value < timezone.localdate():
            raise serializers.ValidationError(
                'Due date cannot be in the past.'
            )
        return value

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        attrs = super().validate(attrs)
        if not attrs.get('type'):
            attrs['type'] = UserTask.Type.CALLBACK
        if attrs.get('open_ended'):
            attrs['scheduled_at'] = None
            attrs['due_date'] = None
            return attrs
        if attrs.get('due_date') is None and attrs.get('scheduled_at') is not None:
            attrs['due_date'] = timezone.localtime(attrs['scheduled_at']).date()
        return attrs


class UpdateLeadToolSerializer(StrictToolSerializer):
    status = serializers.CharField(
        max_length=64,
        required=False,
        trim_whitespace=True,
    )
    do_not_contact = serializers.BooleanField(required=False)
    assigned_project_id = serializers.IntegerField(
        min_value=1,
        required=False,
    )
    desired_country = serializers.CharField(
        allow_blank=True,
        max_length=100,
        required=False,
        trim_whitespace=True,
    )
    desired_location = serializers.CharField(
        allow_blank=True,
        max_length=255,
        required=False,
        trim_whitespace=True,
    )
    estimated_budget = serializers.DecimalField(
        allow_null=True,
        max_digits=15,
        decimal_places=2,
        min_value=0,
        required=False,
    )
    category = serializers.CharField(
        allow_blank=True,
        max_length=255,
        required=False,
        trim_whitespace=True,
    )
    property_type = serializers.CharField(
        allow_blank=True,
        max_length=255,
        required=False,
        trim_whitespace=True,
    )
    other_property_type = serializers.CharField(
        allow_blank=True,
        max_length=255,
        required=False,
        trim_whitespace=True,
    )
    reason = serializers.CharField(
        max_length=500,
        min_length=2,
        trim_whitespace=True,
    )

    def validate_status(self, value: str) -> str:
        from calling_agent.webhook_services import normalize_post_call_lead_status

        status, _dnc = normalize_post_call_lead_status(value)
        if status is None or status not in Lead.AI_SETTABLE_STATUSES:
            raise serializers.ValidationError(
                f'"{value}" is not a valid choice.'
            )
        return status

    def validate_do_not_contact(self, value: bool) -> bool:
        if value is False:
            raise serializers.ValidationError(
                'do_not_contact cannot be cleared by the calling agent.'
            )
        return value

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        attrs = super().validate(attrs)
        action_fields = set(attrs) - {'reason'}
        if not action_fields:
            raise serializers.ValidationError(
                'At least one lead field must be provided.'
            )
        return attrs


class ResolveTransferToolSerializer(StrictToolSerializer):
    reason = serializers.CharField(
        max_length=500,
        min_length=2,
        trim_whitespace=True,
    )
