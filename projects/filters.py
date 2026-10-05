import django_filters
from django.db.models import Q

from projects.models import Project, Promotion, Unit


class ProjectFilter(django_filters.FilterSet):
    status = django_filters.ChoiceFilter(choices=Project.Status.choices)
    project_type = django_filters.ChoiceFilter(choices=Project.ProjectType.choices)
    project_status = django_filters.ChoiceFilter(choices=Project.ProjectStatus.choices)
    search = django_filters.CharFilter(method='filter_search', label='Search')

    class Meta:
        model = Project
        fields = ['status', 'project_type', 'project_status']

    def filter_search(self, queryset, name, value):
        return queryset.filter(
            Q(title__icontains=value) | Q(description__icontains=value)
        )


class UnitFilter(django_filters.FilterSet):
    project = django_filters.NumberFilter(field_name='project_id')

    class Meta:
        model = Unit
        fields = ['project', 'category', 'status']


class PromotionFilter(django_filters.FilterSet):
    project = django_filters.NumberFilter(field_name='project_id')

    class Meta:
        model = Promotion
        fields = ['project', 'status']

