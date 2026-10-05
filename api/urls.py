"""
URL configuration for api project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.conf import settings
from django.contrib import admin
from django.urls import include, path
from rest_framework import permissions
from rest_framework_simplejwt.views import TokenRefreshView
from drf_yasg.views import get_schema_view
from drf_yasg import openapi
from django.conf.urls.static import static

from billing.urls import public_urlpatterns as billing_public_urlpatterns
from billing.views import RegisterCompanyView
from users.views import ChangePasswordView, CustomTokenObtainPairView, ForgotPasswordView, MeView, ResetPasswordView, VerifyOTPView

project_name = settings.APP_NAME  # Replace with your project name

schema_view = get_schema_view(
    openapi.Info(
        title=f"{project_name} API",
        default_version='v1',
        description=f"{project_name} API description"
    ),
    public=True,
    permission_classes=(permissions.AllowAny,),
)

urlpatterns = [
    path('api/admin/', admin.site.urls),

    path('api/auth/login/', CustomTokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('api/auth/refresh_token/', TokenRefreshView.as_view(), name='token_refresh'),

    # Forgot password
    path("api/auth/forgot_password/", ForgotPasswordView.as_view(), name="forgot_password"),
    path("api/auth/verify_otp/", VerifyOTPView.as_view(), name="verify_otp"),
    path("api/auth/reset_password/", ResetPasswordView.as_view(), name="reset_password"),
    
    # Change password (authenticated users)
    path("api/auth/change_password/", ChangePasswordView.as_view(), name="change_password"),

    path('api/auth/me/', MeView.as_view(), name='me'),

    # Self-serve company signup (public, rate-limited)
    path('api/auth/register-company/', RegisterCompanyView.as_view(), name='register-company'),

    path('api/users/', include('users.urls')),
    path('api/projects/', include('projects.urls')),
    path('api/chatbot/', include('chatbot.urls')),
    path('api/new-proposal/', include('new_proposal.urls')),
    path('api/notifications/', include('notifications.urls')),
    path('api/whatsapp/', include('whatsapp.urls')),
    path('api/calling-agent/', include('calling_agent.urls')),
    path('api/billing/', include('billing.urls')),

    path('api/swagger<format>/', schema_view.without_ui(cache_timeout=0), name='schema-json'),
    path('api/swagger/', schema_view.with_ui('swagger', cache_timeout=0), name='schema-swagger-ui'),
] + billing_public_urlpatterns + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
