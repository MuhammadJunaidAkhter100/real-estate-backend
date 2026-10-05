from decimal import Decimal
from urllib.parse import urlparse

from django.contrib.auth import get_user_model
from django.core.files.storage import default_storage
from rest_framework import serializers
import json
import re

from api.constants import COUNTRY_CURRENCY_MAP
from projects.models import Project, ProjectAgentAssignment, ProjectDocument, Promotion, Unit
from users.utils import get_exchange_rate

User = get_user_model()


def _build_image_urls(image_paths, request=None):
    urls = []
    for image_path in image_paths or []:
        if not image_path:
            continue

        parsed = urlparse(str(image_path))
        if parsed.scheme and parsed.netloc:
            urls.append(str(image_path))
            continue

        try:
            url = default_storage.url(str(image_path))
        except Exception:
            url = str(image_path)

        # In local dev, default_storage.url returns a relative path like /media/...
        # Use the request to build an absolute URL.
        if request is not None and url.startswith('/'):
            url = request.build_absolute_uri(url)

        urls.append(url)

    return urls


class UnitSerializer(serializers.ModelSerializer):
    created_by_name = serializers.CharField(source='created_by.full_name', read_only=True)
    created_by_email = serializers.EmailField(source='created_by.email', read_only=True)

    class Meta:
        model = Unit
        fields = [
            'id', 'project', 'label', 'category', 'floor',
            'area_m2', 'area_ft2', 'list_price', 'discounted_price',
            'est_market_rent', 'est_yield_gross',
            'currency', 'status',
            'floor_plan_image',
            'created_by', 'created_by_name', 'created_by_email',
            'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'currency', 'created_by', 'created_by_name',
            'created_by_email', 'created_at', 'updated_at',
        ]
        extra_kwargs = {
            'project': {'required': True},
            'label': {'required': True},
            'list_price': {'required': True},
            'status': {'required': True},
            'discounted_price': {'required': False, 'allow_null': True},
            'est_market_rent': {'required': False, 'allow_null': True},
            'est_yield_gross': {'required': False, 'allow_null': True},
            'floor_plan_image': {'required': False},
        }

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            user_currency = COUNTRY_CURRENCY_MAP.get(request.user.current_country, '')
            stored_currency = instance.currency
            if user_currency and stored_currency and stored_currency != user_currency:
                try:
                    rate = get_exchange_rate(stored_currency, user_currency)
                    data['list_price'] = round(Decimal(str(instance.list_price)) * Decimal(str(rate)), 2)
                    if instance.discounted_price is not None:
                        data['discounted_price'] = round(
                            Decimal(str(instance.discounted_price)) * Decimal(str(rate)), 2
                        )
                    data['currency'] = user_currency
                except Exception:
                    pass
        return data


class UnitWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Unit
        fields = [
            'label', 'category', 'floor', 'area_m2', 'area_ft2',
            'list_price', 'discounted_price',
            'est_market_rent', 'est_yield_gross', 'currency',
            'created_by', 'status', 'floor_plan_image',
        ]


def _parse_json(data):
    """Parse a JSON string, stripping MIME line-folding artifacts first."""
    if not isinstance(data, str):
        return data
    cleaned = re.sub(r'\r?\n\s*', '', data)
    return json.loads(cleaned)


class JSONListField(serializers.ListField):
    """Accepts a list, a single dict, or a JSON string of either — plus lists of JSON strings."""

    def to_internal_value(self, data):
        if isinstance(data, str):
            try:
                data = _parse_json(data)
            except json.JSONDecodeError:
                raise serializers.ValidationError("Invalid JSON string.")
        if isinstance(data, dict):
            data = [data]
        if isinstance(data, list):
            parsed = []
            for item in data:
                if isinstance(item, str):
                    try:
                        item = _parse_json(item)
                    except json.JSONDecodeError:
                        raise serializers.ValidationError(f"Invalid JSON string: {item}")
                if isinstance(item, list):
                    parsed.extend(item)
                else:
                    parsed.append(item)
            data = parsed
        if not isinstance(data, list):
            raise serializers.ValidationError("Expected a list, object, or JSON array/object string.")
        return super().to_internal_value(data)


class ProjectAgentAssignmentSerializer(serializers.ModelSerializer):
    agent_name = serializers.CharField(source='agent.full_name', read_only=True)
    agent_email = serializers.EmailField(source='agent.email', read_only=True)

    class Meta:
        model = ProjectAgentAssignment
        fields = [
            'id', 'agent', 'agent_name', 'agent_email',
            'project', 'agent_split', 'company_split',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'agent_name', 'agent_email', 'created_at', 'updated_at']

    def validate(self, attrs):
        agent_split = attrs.get('agent_split')
        company_split = attrs.get('company_split')
        if agent_split is not None and company_split is not None:
            if agent_split + company_split != 100:
                raise serializers.ValidationError(
                    "agent_split + company_split must equal 100."
                )
        return attrs


class ProjectDocumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectDocument
        fields = ['id', 'label', 'file', 'created_at', 'updated_at']
        read_only_fields = fields


def _build_proposal_images_urls(proposal_images, request=None):
    """Convert stored S3 paths in the proposal_images JSON tree to absolute URLs.

    Expected structure: {label: [path, path, ...], ...}
    """
    if not proposal_images or not isinstance(proposal_images, dict):
        return {}

    result = {}
    for label, paths in proposal_images.items():
        if isinstance(paths, dict):
            # Legacy nested structure: {category: {label: [paths]}}. Flatten it.
            for sub_label, sub_paths in paths.items():
                result[sub_label] = _build_image_urls(
                    sub_paths if isinstance(sub_paths, list) else [sub_paths], request,
                )
            continue
        result[label] = _build_image_urls(
            paths if isinstance(paths, list) else [paths], request,
        )
    return result


class ProjectSerializer(serializers.ModelSerializer):
    image = serializers.ListField(child=serializers.CharField(), read_only=True)
    proposal_images = serializers.JSONField(read_only=True)
    units = serializers.PrimaryKeyRelatedField(many=True, read_only=True)
    units_to_add = JSONListField(
        child=serializers.DictField(),
        write_only=True,
        required=False,
    )
    units_count = serializers.IntegerField(source='units.count', read_only=True)
    created_by_name = serializers.CharField(source='created_by.full_name', read_only=True)
    created_by_email = serializers.EmailField(source='created_by.email', read_only=True)
    documents = ProjectDocumentSerializer(many=True, read_only=True)
    units = UnitSerializer(many=True, read_only=True)
    agent_assignments = ProjectAgentAssignmentSerializer(many=True, read_only=True)
    agents_count = serializers.IntegerField(source='agent_assignments.count', read_only=True)
    is_visible_to_company = serializers.SerializerMethodField()

    class Meta:
        model = Project
        fields = [
            'id', 'title', 'associated_country', 'description', 'location', 'developer',
            'estimated_completion',
            'status', 'project_status', 'units', 'units_count', 'units_to_add',
            'starting_price', 'currency', 'yield_percentage',
            'has_active_promotion', 'active_promotion_discount', 'promotion_title', 'promotion_status',
            'image', 'proposal_images', 'project_type', 'property_category',
            'number_of_units', 'bed_1', 'bed_2', 'bed_3', 'studio',
            'documents', 'is_visible_to_company',
            'created_by', 'created_by_name', 'created_by_email',
            'agent_assignments', 'agents_count',
            'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'units', 'units_count', 'currency',
            'has_active_promotion', 'active_promotion_discount', 'promotion_title', 'promotion_status',
            'image', 'proposal_images',
            'number_of_units', 'bed_1', 'bed_2', 'bed_3', 'studio',
            'documents', 'is_visible_to_company',
            'created_by', 'created_by_name', 'created_by_email',
            'agent_assignments', 'agents_count',
            'created_at', 'updated_at',
        ]
        extra_kwargs = {
            'title': {'required': True},
            'description': {'required': True},
            'location': {'required': True},
            'developer': {'required': True},
            'status': {'required': True},
            'starting_price': {'required': True},
            'yield_percentage': {'required': True},
            'project_type': {'required': True},
        }

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get('request')
        data['image'] = _build_image_urls(instance.image, request)
        data['proposal_images'] = _build_proposal_images_urls(instance.proposal_images, request)
        if request and request.user.is_authenticated:
            user_currency = COUNTRY_CURRENCY_MAP.get(request.user.current_country, '')
            stored_currency = instance.currency
            if user_currency and stored_currency and stored_currency != user_currency:
                try:
                    rate = get_exchange_rate(stored_currency, user_currency)
                    data['starting_price'] = round(
                        Decimal(str(instance.starting_price)) * Decimal(str(rate)), 2
                    )
                    data['currency'] = user_currency
                except Exception:
                    pass
        return data

    def create(self, validated_data):
        units_data = validated_data.pop('units_to_add', [])
        project = super().create(validated_data)
        if units_data:
            unit_serializer = UnitWriteSerializer(data=units_data, many=True)
            unit_serializer.is_valid(raise_exception=True)
            unit_serializer.save(
                project=project,
                currency=project.currency,
                created_by=project.created_by,
            )
        return project

    def get_is_visible_to_company(self, obj):
        request = self.context.get('request')

        user = request.user
        if user.is_superuser:
            return False

        if user.role in (user.Role.AXIYON_ADMIN, user.Role.COMPANY_ADMIN) and user.company_id:
            return obj.visible_to_companies.filter(id=user.company_id).exists()

        return None

class UserProjectSerializer(serializers.ModelSerializer):
    is_assigned = serializers.SerializerMethodField()
    assignment = serializers.SerializerMethodField()

    class Meta:
        model = Project
        fields = [
            'id', 'title', 'description', 'location', 'developer',
            'status', 'starting_price', 'currency', 'yield_percentage',
            'has_active_promotion', 'active_promotion_discount', 'promotion_title', 'promotion_status',
            'image', 'project_type', 'created_at', 'updated_at',
            'is_assigned', 'assignment',
        ]

    def get_is_assigned(self, obj):
        user_id = self.context.get('target_user_id')
        if not user_id:
            return False
        return obj.agent_assignments.filter(agent_id=user_id).exists()

    def get_assignment(self, obj):
        user_id = self.context.get('target_user_id')
        if not user_id:
            return None
        assignment = obj.agent_assignments.filter(agent_id=user_id).first()
        if assignment:
            return {
                'id': assignment.id,
                'agent_split': assignment.agent_split,
                'company_split': assignment.company_split,
            }
        return None

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get('request')
        data['image'] = _build_image_urls(instance.image, request)
        return data


class PromotionSerializer(serializers.ModelSerializer):
    project_title = serializers.CharField(source='project.title', read_only=True)
    created_by_name = serializers.CharField(source='created_by.full_name', read_only=True)
    created_by_email = serializers.EmailField(source='created_by.email', read_only=True)

    promotion_title = serializers.CharField(source='title', required=True)
    discount_percentage = serializers.IntegerField(source='discount', required=True)
    leads_generated = serializers.SerializerMethodField()

    class Meta:
        model = Promotion
        fields = [
            'id',
            'project',
            'project_title',
            'promotion_title',
            'discount_percentage',
            'start_date',
            'end_date',
            'status',
            'leads_generated',
            'created_by',
            'created_by_name',
            'created_by_email',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id',
            'project_title',
            'status',
            'leads_generated',
            'created_by',
            'created_by_name',
            'created_by_email',
            'created_at',
            'updated_at',
        ]

    def get_leads_generated(self, obj):
        from users.models import Lead
        from django.db.models import Q
        return Lead.objects.filter(
            Q(project=obj.project) | Q(projects=obj.project),
            created_at__gte=obj.start_date,
            created_at__lte=obj.end_date,
        ).distinct().count()

    def to_internal_value(self, data):
        data = data.copy()
        if 'title' in data and 'promotion_title' not in data:
            data['promotion_title'] = data['title']
        elif 'promotion_type' in data and 'promotion_title' not in data:
            data['promotion_title'] = data['promotion_type']

        if 'discount' in data and 'discount_percentage' not in data:
            data['discount_percentage'] = data['discount']

        if 'start_time' in data and 'start_date' not in data:
            data['start_date'] = data['start_time']

        if 'end_time' in data and 'end_date' not in data:
            data['end_date'] = data['end_time']

        return super().to_internal_value(data)

    def validate_discount_percentage(self, value):
        if value <= 0 or value > 100:
            raise serializers.ValidationError('Discount percentage must be between 1 and 100.')
        return value

    def validate_end_date(self, value):
        if value and value.hour == 0 and value.minute == 0 and value.second == 0:
            value = value.replace(hour=23, minute=59, second=59)
        return value

    def validate(self, attrs):
        start_date = attrs.get('start_date') or (self.instance.start_date if self.instance else None)
        end_date = attrs.get('end_date') or (self.instance.end_date if self.instance else None)

        if start_date and end_date and end_date <= start_date:
            raise serializers.ValidationError({'end_date': 'End date must be after start date.'})

        return attrs

    def to_representation(self, instance):
        instance.sync_status()
        return super().to_representation(instance)



class AssignAgentSerializer(serializers.Serializer):
    agent = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.filter(role__in=['agent', 'team_manager']),
    )
    agent_split = serializers.DecimalField(max_digits=5, decimal_places=2)
    company_split = serializers.DecimalField(max_digits=5, decimal_places=2)

    def validate(self, attrs):
        if attrs['agent_split'] + attrs['company_split'] != 100:
            raise serializers.ValidationError(
                "agent_split + company_split must equal 100."
            )
        return attrs


class AssignAgentsSerializer(serializers.Serializer):
    assignments = AssignAgentSerializer(many=True)


class UserProjectDetailSerializer(serializers.ModelSerializer):
    is_assigned = serializers.SerializerMethodField()
    assignment = serializers.SerializerMethodField()
    units_count = serializers.IntegerField(source='units.count', read_only=True)
    created_by_name = serializers.CharField(source='created_by.full_name', read_only=True)

    class Meta:
        model = Project
        fields = [
            'id', 'title', 'description', 'location', 'developer',
            'status', 'starting_price', 'currency', 'yield_percentage',
            'has_active_promotion', 'active_promotion_discount', 'promotion_title', 'promotion_status',
            'image', 'project_type',
            'created_by', 'created_by_name',
            'created_at', 'updated_at',
            'units_count', 'is_assigned', 'assignment',
        ]

    def get_is_assigned(self, obj):
        user_id = self.context.get('target_user_id')
        if not user_id:
            return False
        return obj.agent_assignments.filter(agent_id=user_id).exists()

    def get_assignment(self, obj):
        user_id = self.context.get('target_user_id')
        if not user_id:
            return None
        assignment = obj.agent_assignments.filter(agent_id=user_id).first()
        if assignment:
            return {
                'id': assignment.id,
                'agent_split': assignment.agent_split,
                'company_split': assignment.company_split,
            }
        return None

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get('request')
        data['image'] = _build_image_urls(instance.image, request)
