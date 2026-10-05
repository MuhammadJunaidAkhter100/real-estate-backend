from rest_framework import serializers

from new_proposal.models import GeneratedProposal
from projects.models import Project, Unit
from users.models import Lead


class GeneratedProposalSerializer(serializers.ModelSerializer):
    project_title = serializers.CharField(source="project.title", read_only=True)
    unit_label = serializers.CharField(source="unit.label", read_only=True)
    lead_name = serializers.CharField(source="lead.name", read_only=True)
    generated_by_name = serializers.SerializerMethodField()

    class Meta:
        model = GeneratedProposal
        fields = [
            "id",
            "project",
            "project_title",
            "lead",
            "lead_name",
            "unit",
            "unit_label",
            "generated_by",
            "generated_by_name",
            "hosted_url",
            "ai_facts",
            "status",
            "error",
            "task_id",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_generated_by_name(self, obj):
        user = obj.generated_by
        if not user:
            return ""
        full = getattr(user, "full_name", "")
        if callable(full):
            try:
                full = full() or ""
            except Exception:  # noqa: BLE001
                full = ""
        if not full:
            full = f"{getattr(user, 'first_name', '')} {getattr(user, 'last_name', '')}".strip()
        return full or getattr(user, "email", "")


class NewProposalSerializer(serializers.Serializer):
    project = serializers.PrimaryKeyRelatedField(
        queryset=Project.objects.all()
    )
    lead = serializers.PrimaryKeyRelatedField(
        queryset=Lead.objects.all()
    )
    unit = serializers.PrimaryKeyRelatedField(
        queryset=Unit.objects.all()
    )

    def validate(self, attrs):
        project = attrs["project"]
        unit = attrs["unit"]
        if unit.project_id != project.id:
            raise serializers.ValidationError(
                {"unit": "This unit does not belong to the given project."}
            )
        return attrs
