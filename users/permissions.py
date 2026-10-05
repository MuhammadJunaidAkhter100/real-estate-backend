from rest_framework.permissions import BasePermission, SAFE_METHODS


class IsSuperAdmin(BasePermission):
    """Allow access only to superadmin users (is_staff=True and is_superuser=True)."""
    message = "Access restricted to superadmins only."

    def has_permission(self, request, view):
        return (
            request.user
            and request.user.is_authenticated
            and request.user.is_staff
            and request.user.is_superuser
        )


class IsSuperAdminOrReadOnly(BasePermission):
    message = "Only superadmins can create, update, or delete projects."

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return request.user and request.user.is_authenticated

        return (
            request.user
            and request.user.is_authenticated
            and request.user.is_staff
            and request.user.is_superuser
        )


class IsCompanyProjectManager(BasePermission):
    """Allow project writes only to company-level administrators."""
    message = "Only company administrators can create or modify projects."

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return request.user and request.user.is_authenticated

        return (
            request.user
            and request.user.is_authenticated
            and request.user.role in ['axiyon_admin', 'company_admin']
        )


class IsSuperAdminOrCompanyAdminOrTeamManager(BasePermission):
    """Blocks agents from accessing user management endpoints entirely."""
    message = "Agents are not permitted to access user management."

    def has_permission(self, request, view):
        return (
            request.user.is_authenticated and
            request.user.role in ['superadmin', 'axiyon_admin', 'company_admin', 'team_manager']
        )


class IsCompanyAdminOrTeamManager(BasePermission):
    """Blocks Superuser from creating, updating or deleting users."""
    message = "Only company admins and team managers can perform this action."

    def has_permission(self, request, view):
        return (
            request.user.is_authenticated and
            request.user.role in ['axiyon_admin', 'company_admin', 'team_manager']
        )


class IsCompanyAdmin(BasePermission):
    message = "Only company admins can perform this action."

    def has_permission(self, request, view):
        return (
            request.user.is_authenticated and
            request.user.role in ['axiyon_admin', 'company_admin']
        )


class IsNotSuperAdmin(BasePermission):
    """Allow all authenticated users except superadmins."""
    message = "Superadmins are not permitted to perform this action."

    def has_permission(self, request, view):
        return (
            request.user.is_authenticated and
            request.user.role in ['axiyon_admin', 'company_admin', 'team_manager', 'agent']
        )
