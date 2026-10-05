from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from django.contrib.auth.hashers import make_password
from django.contrib.auth import get_user_model
from django.db import transaction
from projects.models import Project, Unit
from users.models import Company, Lead, Task, Team
from users.utils import generate_password

User = get_user_model()


# ── Helpers ──────────────────────────────────────────────────────────────────



def validate_countries(value):

    """
    Accepts:
    ["1", "2", "3"]
    ["1,2,3"]
    "1,2,3"
    and normalizes to ["1", "2", "3"]
    """

    # Case: frontend sends plain comma separated string
    if isinstance(value, str):
        value = [v.strip() for v in value.split(",") if v.strip()]

    # Case: swagger sends ["1,2,3"] as one string item

    elif isinstance(value, list) and len(value) == 1 and isinstance(value[0], str) and "," in value[0]:
        value = [v.strip() for v in value[0].split(",") if v.strip()]

    if not isinstance(value, list):
        raise serializers.ValidationError("Countries must be a list of strings.")

    cleaned = []
    seen = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise serializers.ValidationError("Each country must be a non-empty string.")

        item = item.strip()

        if item.lower() not in seen:
            seen.add(item.lower())
            cleaned.append(item)

    return cleaned


# ── Auth serializers ──────────────────────────────────────────────────────────

class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token['user'] = UserDetailSerializer(user).data
        return token

    def validate(self, attrs):
        email = attrs.get("email")
        password = attrs.get("password")

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            raise AuthenticationFailed("Invalid email or password.")

        if not user.check_password(password):
            raise AuthenticationFailed("Invalid email or password.")

        if user.status == User.Status.INACTIVE:
            raise AuthenticationFailed("Your account is inactive. Please contact admin.")

        if user.status == User.Status.INVITED:
            # it means the user is logging in for the first time after being created by superadmin, so we auto-approve and activate them
            user.status = User.Status.ACTIVE
            user.save()

        # if not user.is_active:
        #     raise AuthenticationFailed("Your account is inactive. Please contact admin.")

        self.user = user
        data = super().validate(attrs)
        data['user'] = UserDetailSerializer(user).data
        return data


class ForgotPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField()


class VerifyOTPSerializer(serializers.Serializer):
    email = serializers.EmailField()
    otp = serializers.CharField(max_length=6)


class ResetPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField()
    reset_uuid = serializers.UUIDField()
    new_password = serializers.CharField(write_only=True, min_length=8)
    confirm_password = serializers.CharField(write_only=True, min_length=8)

    def validate(self, attrs):
        if attrs["new_password"] != attrs["confirm_password"]:
            raise serializers.ValidationError({"confirm_password": "Passwords do not match."})
        return attrs


class ChangePasswordSerializer(serializers.Serializer):
    old_password = serializers.CharField(write_only=True, required=True)
    new_password = serializers.CharField(write_only=True, min_length=8, required=True)
    confirm_password = serializers.CharField(write_only=True, min_length=8, required=True)

    def validate(self, attrs):
        if attrs["new_password"] != attrs["confirm_password"]:
            raise serializers.ValidationError({"confirm_password": "Passwords do not match."})
        return attrs


# ── Superadmin CRUD serializers ───────────────────────────────────────────────
class CompanyAdminCreateSerializer(serializers.Serializer):
    """Inline admin details taken during company creation."""
    email = serializers.EmailField()
    first_name = serializers.CharField(max_length=150)
    last_name = serializers.CharField(max_length=150)

    def validate_email(self, value):
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return value

class CompanyCreateSerializer(serializers.ModelSerializer):
    admin = CompanyAdminCreateSerializer(write_only=True)
    operating_countries = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        default=list,
    )

    class Meta:
        model = Company
        fields = ['id', 'name', 'operating_countries', 'admin', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at']

    def validate_operating_countries(self, value):
        return validate_countries(value)

    @transaction.atomic
    def create(self, validated_data):
        admin_data = validated_data.pop('admin')

        # Create company
        company = Company.objects.create(**validated_data)

        # Generate password for the admin
        password = generate_password()

        # Create company admin user
        admin_user = User(
            email=admin_data['email'],
            first_name=admin_data['first_name'],
            last_name=admin_data['last_name'],
            role=User.Role.COMPANY_ADMIN,
            status=User.Status.ACTIVE,
            countries=company.operating_countries,  # default to company countries
            company=company,
        )
        admin_user.set_password(password)
        admin_user.save()

        # Attach for view to email
        company._admin_user = admin_user
        company._admin_plain_password = password

        return company


class CompanyAdminDetailSerializer(serializers.ModelSerializer):
    """Nested admin info returned in company detail responses."""
    class Meta:
        model = User
        fields = ['id', 'email', 'first_name', 'last_name', 'countries']


class CompanyDetailSerializer(serializers.ModelSerializer):
    """Used for retrieve/list — shows nested admin."""
    company_admin = serializers.SerializerMethodField()

    class Meta:
        model = Company
        fields = ['id', 'name', 'operating_countries', 'company_admin', 'created_at', 'updated_at']

    def get_company_admin(self, obj):
        company_admin = obj.users.filter(role=User.Role.COMPANY_ADMIN).first()
        return CompanyAdminDetailSerializer(company_admin).data


class CompanyAdminUpdateSerializer(serializers.ModelSerializer):
    """Nested serializer for updating the company admin during company PATCH."""

    class Meta:
        model = User
        fields = ['first_name', 'last_name', 'status', 'countries']
        extra_kwargs = {
            'first_name': {'required': False},
            'last_name': {'required': False},
            'status': {'required': False},
            'countries': {'required': False},
        }

    def validate_countries(self, value):
        # Company is passed via context from the parent serializer
        company = self.context.get('company')
        if not value or not company:
            return value

        invalid = set(value) - set(company.operating_countries)
        if invalid:
            raise serializers.ValidationError(
                f"Invalid countries {sorted(invalid)}. "
                f"Allowed: {sorted(company.operating_countries)}."
            )
        return value


class CompanyUpdateSerializer(serializers.ModelSerializer):
    operating_countries = serializers.ListField(
        child=serializers.CharField(),
        required=False,
    )
    admin = CompanyAdminUpdateSerializer(required=False)

    class Meta:
        model = Company
        fields = ['name', 'operating_countries', 'admin']

    def validate_operating_countries(self, value):
        return validate_countries(value)

    def validate(self, attrs):
        """
        If operating_countries is being changed, re-validate existing admin's
        countries to ensure they don't fall outside the new set.
        """
        new_countries = attrs.get('operating_countries')
        admin_attrs = attrs.get('admin', {})
        admin_countries = admin_attrs.get('countries')

        if new_countries is not None:
            # Use incoming admin countries if provided, else check existing admin
            company_admin = self.instance.users.filter(
                role=User.Role.COMPANY_ADMIN
            ).first()

            countries_to_check = admin_countries or (
                company_admin.countries if company_admin else []
            )

            invalid = set(countries_to_check) - set(new_countries)
            if invalid:
                raise serializers.ValidationError({
                    'operating_countries': (
                        f"Cannot shrink operating_countries — "
                        f"company admin still has countries {sorted(invalid)} assigned. "
                        f"Update the admin's countries first or include them in the new set."
                    )
                })

        return attrs

    def update(self, instance, validated_data):
        admin_data = validated_data.pop('admin', None)

        # Update company fields
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        # Update company admin if admin data was provided
        if admin_data:
            company_admin = instance.users.filter(
                role=User.Role.COMPANY_ADMIN
            ).first()

            if company_admin is None:
                raise serializers.ValidationError({
                    'admin': "This company has no admin to update."
                })

            # Pass updated company (with new operating_countries) for country validation
            admin_serializer = CompanyAdminUpdateSerializer(
                company_admin,
                data=admin_data,
                partial=True,
                context={'company': instance},
            )
            admin_serializer.is_valid(raise_exception=True)
            admin_serializer.save()

        return instance


class UserDetailSerializer(serializers.ModelSerializer):
    company = CompanyDetailSerializer()
    class Meta:
        model = User
        fields = [
            'id', 'email', 'first_name', 'last_name',
            'role', 'status', 'company', 'countries', 'current_country', 'is_staff',
            'profile_image',
        ]
        read_only_fields = fields


class MeUpdateSerializer(serializers.ModelSerializer):
    """Lets the authenticated user edit their own profile fields."""

    class Meta:
        model = User
        fields = ['first_name', 'last_name', 'profile_image', 'current_country']
        extra_kwargs = {
            'first_name': {'required': False, 'allow_blank': False, 'max_length': 150},
            'last_name': {'required': False, 'allow_blank': False, 'max_length': 150},
            'profile_image': {'required': False, 'allow_null': True},
            'current_country': {'required': False, 'allow_blank': False},
        }

    def validate_current_country(self, value):
        if value and value != "all" and value not in self.instance.countries:
            raise serializers.ValidationError(
                f"'{value}' is not in your assigned countries: {self.instance.countries}"
            )
        return value


class UserAssignedLeadSerializer(serializers.ModelSerializer):
    """Lightweight lead representation for a user's assigned leads."""
    project_titles = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Lead
        fields = [
            'id', 'name', 'status', 'source', 'country',
            'desired_country', 'desired_location',
            'email', 'phone_no', 'estimated_budget',
            'category', 'type', 'other_type',
            'scheduled_at', 'project', 'projects', 'project_titles',
            'created_at', 'updated_at',
            'is_assign',
        ]

    def get_project_titles(self, obj):
        project_titles = [project.title for project in obj.projects.all()]
        if obj.project and obj.project.title not in project_titles:
            project_titles.insert(0, obj.project.title)
        return project_titles


class UserManagementSerializer(serializers.ModelSerializer):
    """
    Single serializer for all user types.
    - company_admin: can only add users to their own company (team must be null or belong to their company)
    - team_manager:  can only assign users to their own team (company is inherited, not settable)
    - agent:         no write access (enforced at permission level, not here)
    """
    team = serializers.PrimaryKeyRelatedField(
        queryset=Team.objects.all(),
        required=False,
        allow_null=True,
    )
    team_name = serializers.CharField(
        source='team.name',
        read_only=True,
    )
    company_name = serializers.CharField(
        source='company.name',
        read_only=True,
    )
    assigned_leads_count = serializers.IntegerField(
        source='assigned_leads.count',
        read_only=True,
    )
    assigned_leads = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            'id', 'email', 'first_name', 'last_name',
            'role', 'status', 'countries', 'company', 'team', 'team_name', 'company_name',
            'assigned_leads_count',
            'assigned_leads',
            'profile_image',
        ]
        read_only_fields = ['id', 'team_name', 'company_name', 'profile_image']

    def get_assigned_leads(self, obj):
        # Only include the full assigned-leads list on the detail (retrieve) view.
        view = self.context.get('view')
        if not view or getattr(view, 'action', None) != 'retrieve':
            return None

        leads = obj.assigned_leads.select_related(
            'project', 'created_by', 'assigned_to'
        ).prefetch_related('projects').order_by('-created_at')
        return UserAssignedLeadSerializer(leads, many=True, context=self.context).data
        extra_kwargs = {
            'company': {'required': True},
            'countries': {'required': True},
            'role': {'required': True},
        }

    def _get_request_user(self):
        return self.context['request'].user

    def validate_countries(self, value):
        if not value:
            return value

        request_user = self._get_request_user()

        # company_admin cannot update their own countries
        if request_user.role in (User.Role.AXIYON_ADMIN, User.Role.COMPANY_ADMIN) and self.instance:
            if self.instance.pk == request_user.pk:
                raise serializers.ValidationError(
                    "Company admins cannot modify their own countries."
                )

        # Resolve company for operating_countries check
        company = None

        if self.instance:
            company = getattr(self.instance, 'company', None)

        if company is None:
            company_id = self.initial_data.get('company')
            if company_id:
                try:
                    company = Company.objects.get(pk=company_id)
                except Company.DoesNotExist:
                    raise serializers.ValidationError(
                        "The specified company does not exist."
                    )

        # Validate against company's operating_countries for all roles
        if company:
            invalid_for_company = set(value) - set(company.operating_countries)
            if invalid_for_company:
                raise serializers.ValidationError(
                    f"Invalid countries {sorted(invalid_for_company)}. "
                    f"Allowed by company: {sorted(company.operating_countries)}."
                )

        # team_manager: further restrict to only their own countries
        if request_user.role == User.Role.TEAM_MANAGER:
            invalid_for_manager = set(value) - set(request_user.countries)
            if invalid_for_manager:
                raise serializers.ValidationError(
                    f"Invalid countries {sorted(invalid_for_manager)}. "
                    f"As a team manager you can only assign countries from your own: "
                    f"{sorted(request_user.countries)}."
                )

        return value

    def validate(self, attrs):
        user = self._get_request_user()
        role = user.role

        if role in ('axiyon_admin', 'company_admin'):
            attrs = self._validate_company_admin(attrs, user)
        elif role == 'team_manager':
            attrs = self._validate_team_manager(attrs, user)
        # elif role == 'superadmin':
        #     attrs = self._validate_superadmin(attrs)  # ← add this

        return attrs

    def _validate_company_admin(self, attrs, request_user):
        """
        company_admin rules:
        - company is always force-set to their own company (not overridable)
        - cannot assign a team that belongs to a different company
        - cannot assign superadmin or company_admin role to any user
        - cannot change their own role
        - cannot assign countries that are not part of the company
        - cannot reassign an agent from one team to another (once assigned, agent stays with that team)
        """
        # Force company to their own — never trust the payload
        attrs['company'] = request_user.company

        team = attrs.get('team')
        if team is not None and team.company != request_user.company:
            raise serializers.ValidationError({
                'team': "You can only assign users to teams within your own company."
            })

        new_role = attrs.get('role')
        if new_role:
            # Cannot change their own role at all
            if self.instance and self.instance.pk == request_user.pk:
                raise serializers.ValidationError({
                    'role': "You cannot change your own role."
                })

            # Cannot assign superadmin or company_admin to any user
            if new_role in (User.Role.SUPERADMIN, User.Role.COMPANY_ADMIN):
                raise serializers.ValidationError({
                    'role': f"You cannot assign the '{new_role}' role."
                })

        return attrs

    def _validate_team_manager(self, attrs, request_user):
        """
        team_manager rules:
        - must have a managed team; if not, block all writes
        - team is always force-set to their own team (not overridable)
        - company is inherited from their own company
        - cannot assign roles above 'agent'
        """
        # managed_team = getattr(request_user, 'managed_team', None)
        # if managed_team is None:
        #     raise serializers.ValidationError(
        #         "You are not assigned as a manager of any team."
        #     )

        # Force team and company — never trust the payload
        # attrs['team'] = managed_team
        attrs['company'] = request_user.company

        new_role = attrs.get('role')
        if new_role and new_role != User.Role.AGENT:
            raise serializers.ValidationError({
                'role': "Team managers can only add users with the 'agent' role."
            })

        return attrs

    # def _validate_superadmin(self, attrs):
    #     """
    #     superadmin rules:
    #     - only one company_admin allowed per company
    #     """
    #     new_role = attrs.get('role')
    #     company = attrs.get('company')

    #     if new_role == User.Role.COMPANY_ADMIN and company:
    #         existing_qs = User.objects.filter(
    #             company=company,
    #             role=User.Role.COMPANY_ADMIN,
    #         )

    #         # Exclude self on PATCH so editing the existing admin doesn't trip this
    #         if self.instance:
    #             existing_qs = existing_qs.exclude(pk=self.instance.pk)

    #         if existing_qs.exists():
    #             raise serializers.ValidationError({
    #                 'role': (
    #                     f"Company '{company.name}' already has a company admin. "
    #                     "A company can only have one."
    #                 )
    #             })

    #     return attrs

    def create(self, validated_data):
        from billing.services import PlanLimitsService

        validated_data['status'] = "invited"
        # Seat check and INSERT share one transaction, so two admins inviting
        # someone at the same time cannot both slip past the cap.
        with transaction.atomic():
            company = validated_data.get('company')
            if company is not None:
                PlanLimitsService.for_company(company).assert_can_add('users')
                if validated_data.get('role') == User.Role.TEAM_MANAGER:
                    PlanLimitsService.for_company(company).assert_can_add('team_managers')
            return super().create(validated_data)

    def update(self, instance, validated_data):
        from billing.services import PlanLimitsService

        # A PATCH can promote someone to team_manager, so the manager cap has to
        # be re-checked on the same row being changed. Demotion is never
        # blocked - it only frees a seat.
        new_role = validated_data.get('role')
        becoming_manager = (
            new_role == User.Role.TEAM_MANAGER
            and instance.role != User.Role.TEAM_MANAGER
        )
        if becoming_manager:
            with transaction.atomic():
                PlanLimitsService.for_company(instance.company).assert_can_add(
                    'team_managers'
                )
        return super().update(instance, validated_data)

class CompanyAutocompleteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Company
        fields = ['id', 'name', 'operating_countries']


class TeamAutocompleteSerializer(serializers.ModelSerializer):
    company_name = serializers.CharField(source='company.name', read_only=True)

    class Meta:
        model = Team
        fields = ['id', 'name', 'company', 'company_name']


# ── Task serializer ───────────────────────────────────────────────────────────

class TaskSerializer(serializers.ModelSerializer):
    created_by_name = serializers.CharField(source='created_by.full_name', read_only=True)
    created_by_email = serializers.EmailField(source='created_by.email', read_only=True)
    related_lead = serializers.PrimaryKeyRelatedField(
        queryset=Lead.objects.all(),
        required=False,
        allow_null=True,
    )
    related_lead_name = serializers.CharField(
        source='related_lead.name',
        read_only=True,
    )

    class Meta:
        model = Task
        fields = [
            'id',
            'name',
            'description',
            'associated_country',
            'priority',
            'type',
            'due_date',
            'scheduled_at',
            'status',
            'related_lead',
            'related_lead_name',
            'created_by',
            'created_by_name',
            'created_by_email',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id',
            'associated_country',
            'type',
            'created_by',
            'created_by_name',
            'created_by_email',
            'related_lead_name',
            'created_at',
            'updated_at',
        ]
        extra_kwargs = {
            'description': {'required': False, 'allow_blank': True, 'default': ''},
        }


# ── Lead serializer ───────────────────────────────────────────────────────────

class LeadUnitSerializer(serializers.ModelSerializer):
    project_id = serializers.IntegerField(source='project.id', read_only=True)
    project_title = serializers.CharField(source='project.title', read_only=True)

    class Meta:
        model = Unit
        fields = [
            'id',
            'project_id',
            'project_title',
            'label',
            'floor',
            'category',
            'area_ft2',
            'list_price',
            'discounted_price',
            'status',
        ]


class LeadSerializer(serializers.ModelSerializer):
    created_by_name = serializers.CharField(source='created_by.full_name', read_only=True)
    created_by_email = serializers.EmailField(source='created_by.email', read_only=True)
    created_by_role = serializers.CharField(source='created_by.role', read_only=True)
    project_title = serializers.CharField(source='project.title', read_only=True)
    assigned_to_name = serializers.CharField(source='assigned_to.full_name', read_only=True)
    projects = serializers.PrimaryKeyRelatedField(queryset=Project.objects.all(), many=True, required=False)
    project_titles = serializers.SerializerMethodField(read_only=True)
    estimated_budget = serializers.DecimalField(max_digits=15, decimal_places=2, required=True)
    unit = serializers.PrimaryKeyRelatedField(queryset=Unit.objects.all(), required=False, allow_null=True)
    units = serializers.PrimaryKeyRelatedField(queryset=Unit.objects.all(), many=True, required=False)
    unit_details = serializers.SerializerMethodField(read_only=True)
    units_details = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Lead
        fields = [
            'id',
            'name',
            'email',
            'phone_no',
            'source',
            'country',
            # Lead requirements
            'desired_country',
            'desired_location',
            'estimated_budget',
            'category',
            'type',
            'other_type',
            # Suggested projects
            'project',
            'projects',
            'project_title',
            'project_titles',
            # Unit info
            'unit',
            'units',
            'unit_details',
            'units_details',
            # Scheduling & status
            'scheduled_at',
            'status',
            'do_not_contact',
            # Relations
            'created_by',
            'created_by_name',
            'created_by_email',
            'created_by_role',
            'assigned_to',
            'assigned_to_name',
            'is_assign',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id',
            'unit',
            'created_by',
            'created_by_name',
            'created_by_email',
            'created_by_role',
            'project_title',
            'assigned_to_name',
            'is_assign',
            'created_at',
            'updated_at',
        ]
        extra_kwargs = {
            'name': {'required': True},
            'phone_no': {'required': True, 'allow_blank': False},
            'email': {'required': False, 'allow_blank': True, 'default': ''},
            'project': {'read_only': True},
            'projects': {'required': False},
            'assigned_to': {'required': False, 'allow_null': True},
            'source': {'required': False, 'allow_blank': True, 'allow_null': True, 'default': ''},
            'country': {'required': False, 'allow_blank': True, 'allow_null': True, 'default': ''},
            'desired_country': {'required': False, 'allow_blank': True, 'allow_null': True, 'default': ''},
            'desired_location': {'required': False, 'allow_blank': True, 'allow_null': True, 'default': ''},
            'category': {'required': False, 'allow_blank': True, 'allow_null': True, 'default': ''},
            'type': {'required': False, 'allow_blank': True, 'allow_null': True, 'default': ''},
            'other_type': {'required': False, 'allow_blank': True, 'allow_null': True, 'default': ''},
            'scheduled_at': {'required': False, 'allow_null': True},
            'unit': {'required': False, 'allow_null': True},
            'units': {'required': False},
        }

    def get_unit_details(self, obj):
        if not obj.unit_id:
            return None
        return LeadUnitSerializer(obj.unit).data

    def get_units_details(self, obj):
        units_qs = obj.units.all()
        if not units_qs and obj.unit:
            units_qs = [obj.unit]
        return LeadUnitSerializer(units_qs, many=True).data

    def get_project_titles(self, obj):
        project_titles = [project.title for project in obj.projects.all()]
        if obj.project and obj.project.title not in project_titles:
            project_titles.insert(0, obj.project.title)
        return project_titles

    def create(self, validated_data):
        projects = validated_data.pop('projects', None)
        units = validated_data.pop('units', None)

        if units:
            unit_projects = [u.project for u in units if u.project]
            if projects is None:
                projects = list(unit_projects)
            else:
                projects_list = list(projects)
                for up in unit_projects:
                    if up not in projects_list:
                        projects_list.append(up)
                projects = projects_list

        if 'project' not in validated_data or validated_data.get('project') is None:
            if projects:
                validated_data['project'] = projects[0]

        if 'unit' not in validated_data or validated_data.get('unit') is None:
            if units:
                validated_data['unit'] = units[0]

        lead = super().create(validated_data)
        if projects is not None:
            lead.projects.set(projects)
        if units is not None:
            lead.units.set(units)
        return lead

    def update(self, instance, validated_data):
        projects = validated_data.pop('projects', None)
        units = validated_data.pop('units', None)

        if units:
            unit_projects = [u.project for u in units if u.project]
            if projects is None:
                projects = list(unit_projects)
            else:
                projects_list = list(projects)
                for up in unit_projects:
                    if up not in projects_list:
                        projects_list.append(up)
                projects = projects_list

        if 'project' not in validated_data or validated_data.get('project') is None:
            if projects:
                validated_data['project'] = projects[0]

        if 'unit' not in validated_data or validated_data.get('unit') is None:
            if units:
                validated_data['unit'] = units[0]

        lead = super().update(instance, validated_data)
        if projects is not None:
            lead.projects.set(projects)
        if units is not None:
            lead.units.set(units)
        return lead

    def validate(self, attrs):
        # Normalize null values to empty string for blank CharFields
        for key in ('source', 'country', 'desired_country', 'desired_location',
                    'category', 'type', 'other_type'):
            if key in attrs and attrs[key] is None:
                attrs[key] = ''
        return attrs


class UserLeadSerializer(serializers.ModelSerializer):
    is_assigned = serializers.SerializerMethodField()
    projects = serializers.PrimaryKeyRelatedField(queryset=Project.objects.all(), many=True, required=False)
    project_titles = serializers.SerializerMethodField(read_only=True)
    unit_details = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Lead
        fields = [
            'id', 'name', 'status', 'source', 'country',
            'desired_country', 'desired_location',
            'email', 'phone_no', 'estimated_budget',
            'category', 'type', 'other_type',
            'scheduled_at', 'project', 'projects', 'project_titles',
            'unit', 'units', 'unit_details',
            'do_not_contact',
            'created_at', 'updated_at',
            'is_assign',
            'is_assigned',
        ]

    def get_unit_details(self, obj):
        if not obj.unit_id:
            return None
        return LeadUnitSerializer(obj.unit).data

    def get_project_titles(self, obj):
        project_titles = [project.title for project in obj.projects.all()]
        if obj.project and obj.project.title not in project_titles:
            project_titles.insert(0, obj.project.title)
        return project_titles

    def get_is_assigned(self, obj):
        user_id = self.context.get('target_user_id')
        if not user_id:
            return False
        return obj.assigned_to_id == user_id


class UserLeadDetailSerializer(serializers.ModelSerializer):
    is_assigned = serializers.SerializerMethodField()
    created_by_name = serializers.CharField(source='created_by.full_name', read_only=True)
    project_title = serializers.CharField(source='project.title', read_only=True)
    projects = serializers.PrimaryKeyRelatedField(queryset=Project.objects.all(), many=True, required=False)
    project_titles = serializers.SerializerMethodField(read_only=True)
    assigned_to_name = serializers.CharField(source='assigned_to.full_name', read_only=True)
    unit_details = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Lead
        fields = [
            'id', 'name', 'status', 'source', 'country',
            'desired_country', 'desired_location',
            'email', 'phone_no', 'estimated_budget',
            'category', 'type', 'other_type',
            'scheduled_at',
            'do_not_contact',
            'created_by', 'created_by_name',
            'project', 'projects', 'project_title', 'project_titles',
            'unit', 'units', 'unit_details',
            'assigned_to', 'assigned_to_name',
            'is_assign',
            'created_at', 'updated_at',
            'is_assigned',
        ]

    def get_unit_details(self, obj):
        if not obj.unit_id:
            return None
        return LeadUnitSerializer(obj.unit).data

    def get_project_titles(self, obj):
        project_titles = [project.title for project in obj.projects.all()]
        if obj.project and obj.project.title not in project_titles:
            project_titles.insert(0, obj.project.title)
        return project_titles

    def get_is_assigned(self, obj):
        user_id = self.context.get('target_user_id')
        if not user_id:
            return False
        return obj.assigned_to_id == user_id
