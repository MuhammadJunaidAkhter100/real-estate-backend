import django_filters
from django.contrib.auth import get_user_model
from django.db.models import Q

from users.models import Lead, Task

User = get_user_model()


class UserFilter(django_filters.FilterSet):
    role = django_filters.ChoiceFilter(choices=User.Role.choices)
    status = django_filters.ChoiceFilter(choices=User.Status.choices)
    company = django_filters.CharFilter(field_name='company__id')
    team = django_filters.NumberFilter(field_name='team__id')
    search = django_filters.CharFilter(method='filter_search', label='Search')

    class Meta:
        model = User
        fields = ['role', 'status', 'company', 'team']

    def filter_search(self, queryset, name, value):
        return queryset.filter(
            Q(email__icontains=value) |
            Q(first_name__icontains=value) |
            Q(last_name__icontains=value)
        )


class TaskFilter(django_filters.FilterSet):
    """
    Supported query params:
      - priority=high|medium|low
      - status=pending|in_progress|completed
      - month=YYYY-MM        → filters by due_date month (e.g. 2026-05)
      - year=YYYY            → filters by due_date year
      - related_lead=<id>    → filter tasks for a specific lead
      - user_role=all|agent|team_manager|company_admin|axiyon_admin|superadmin
                             → filter by the role of the user who created the task
      - search=<text>        → matches task name
    """
    ROLE_CHOICES_WITH_ALL = (('all', 'All'),) + tuple(User.Role.choices)

    priority = django_filters.ChoiceFilter(choices=Task.Priority.choices)
    status = django_filters.ChoiceFilter(choices=Task.Status.choices)
    month = django_filters.CharFilter(method='filter_month', label='Month (YYYY-MM)')
    year = django_filters.NumberFilter(field_name='due_date__year')
    related_lead = django_filters.NumberFilter(field_name='related_lead__id')
    user_role = django_filters.ChoiceFilter(method='filter_user_role', choices=ROLE_CHOICES_WITH_ALL, label='User role')
    search = django_filters.CharFilter(method='filter_search', label='Search')

    class Meta:
        model = Task
        fields = ['priority', 'status', 'related_lead', 'user_role']

    def filter_user_role(self, queryset, name, value):
        if not value or value == 'all':
            return queryset
        return queryset.filter(created_by__role=value)

    def filter_month(self, queryset, name, value):
        value = (value or '').strip()
        if not value:
            return queryset
        try:
            year_str, month_str = value.split('-')
            year, month = int(year_str), int(month_str)
            if not 1 <= month <= 12:
                raise ValueError
        except (ValueError, AttributeError):
            return queryset.none()
        return queryset.filter(due_date__year=year, due_date__month=month)

    def filter_search(self, queryset, name, value):
        return queryset.filter(Q(name__icontains=value))


class LeadFilter(django_filters.FilterSet):
    """
    Supported query params:
      - status=<Lead.Status code> (staged CRM taxonomy)
      - source=<text>             → source match (icontains)
      - country=<text>            → country match (icontains)
      - desired_country=<text>    → desired_country match (icontains)
      - category=<text>           → category match (icontains)
      - type=<text>               → type match (icontains)
      - project=<id>              → filter by project ID
      - user_role=all|agent|team_manager|company_admin|axiyon_admin|superadmin
                                 → filter by lead owner/assignee role
      - month=YYYY-MM             → filters by scheduled_at month
      - year=YYYY                 → filters by scheduled_at year
      - search=<text>             → matches name, email, phone_no, country
    """
    ROLE_CHOICES_WITH_ALL = (('all', 'All'),) + tuple(User.Role.choices)

    is_assign = django_filters.BooleanFilter(field_name='is_assign')
    status = django_filters.ChoiceFilter(choices=Lead.Status.choices)
    source = django_filters.CharFilter(field_name='source', lookup_expr='icontains')
    country = django_filters.CharFilter(field_name='country', lookup_expr='icontains')
    desired_country = django_filters.CharFilter(field_name='desired_country', lookup_expr='icontains')
    category = django_filters.CharFilter(field_name='category', lookup_expr='icontains')
    type = django_filters.CharFilter(field_name='type', lookup_expr='icontains')
    project = django_filters.NumberFilter(method='filter_by_project')
    user_role = django_filters.ChoiceFilter(method='filter_user_role', choices=ROLE_CHOICES_WITH_ALL, label='User role')
    month = django_filters.CharFilter(method='filter_month', label='Month (YYYY-MM)')
    year = django_filters.NumberFilter(field_name='scheduled_at__year')
    search = django_filters.CharFilter(method='filter_search', label='Search')

    class Meta:
        model = Lead
        fields = ['status', 'source', 'country', 'desired_country', 'category', 'type', 'project', 'user_role', 'is_assign']

    def filter_by_project(self, queryset, name, value):
        try:
            project_id = int(value)
        except (TypeError, ValueError):
            return queryset.none()
        return queryset.filter(
            Q(project__id=project_id) | Q(projects__id=project_id)
        ).distinct()

    def filter_user_role(self, queryset, name, value):
        if value == User.Role.AGENT:
            return queryset.filter(Q(assigned_to__role=User.Role.AGENT) | Q(created_by__role=User.Role.AGENT))
        if value == User.Role.TEAM_MANAGER:
            return queryset.filter(Q(assigned_to__role=User.Role.TEAM_MANAGER) | Q(created_by__role=User.Role.TEAM_MANAGER))
        if value in (User.Role.COMPANY_ADMIN, User.Role.AXIYON_ADMIN, 'axiyon_admin', 'company_admin'):
            return queryset.filter(Q(created_by__role=value), is_assign=False)
        if value == User.Role.SUPERADMIN:
            return queryset.filter(Q(created_by__role=User.Role.SUPERADMIN))
        return queryset

    def filter_month(self, queryset, name, value):
        value = (value or '').strip()
        if not value:
            return queryset
        try:
            year_str, month_str = value.split('-')
            year, month = int(year_str), int(month_str)
            if not 1 <= month <= 12:
                raise ValueError
        except (ValueError, AttributeError):
            return queryset.none()
        return queryset.filter(scheduled_at__year=year, scheduled_at__month=month)

    def filter_search(self, queryset, name, value):
        return queryset.filter(
            Q(name__icontains=value) |
            Q(email__icontains=value) |
            Q(phone_no__icontains=value) |
            Q(country__icontains=value) |
            Q(desired_location__icontains=value)
        )
