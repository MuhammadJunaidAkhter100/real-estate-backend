"""Which projects a user may see.

Projects belong to the company of the user who created them. Every endpoint
that hands out projects (or things hanging off a project, such as units and
promotions) scopes itself through here so the rule lives in one place.
"""
from projects.models import Project


def visible_projects(user, queryset=None):
    """Return the projects `user` may see.

    - superadmin: every project
    - anyone else: only projects created inside their own company
    - no company / anonymous: nothing
    """
    qs = Project.objects.all() if queryset is None else queryset
    if user is None or not user.is_authenticated:
        return qs.none()
    if user.is_staff and user.is_superuser:
        return qs
    if user.company_id:
        return qs.filter(created_by__company_id=user.company_id)
    return qs.none()
