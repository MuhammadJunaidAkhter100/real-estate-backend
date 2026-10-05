from django.urls import path
from rest_framework.routers import DefaultRouter

from users.property_news import GulfNewsPropertyView, KhaleejTimesPropertyView, PropertyWireView
from users.views import (
    AdminCompanyViewSet,
    CommissionAnalyticsView,
    DashboardSummaryView,
    LatestCommissionsView,
    LeadViewSet,
    StreamActivityView,
    TaskViewSet,
    UsersViewSet,
)

router = DefaultRouter()

router.register(r'super_admin_companies', AdminCompanyViewSet, basename='super-admin-companies')
router.register(r'manage_users', UsersViewSet, basename='manage-users')
router.register(r'tasks', TaskViewSet, basename='tasks')
router.register(r'leads', LeadViewSet, basename='leads')


urlpatterns = [
    path('dashboard/summary/', DashboardSummaryView.as_view(), name='dashboard-summary'),
    path('commissions/analytics/', CommissionAnalyticsView.as_view(), name='commission-analytics'),
    path('commissions/latest/', LatestCommissionsView.as_view(), name='latest-commissions'),
    path('stream-activity/', StreamActivityView.as_view(), name='stream-activity'),
    path('property-news/khaleej-times/', KhaleejTimesPropertyView.as_view(), name='property-news-khaleej-times'),
    path('property-news/gulf-news/', GulfNewsPropertyView.as_view(), name='property-news-gulf-news'),
    path('property-news/property-wire/', PropertyWireView.as_view(), name='property-news-property-wire'),
]

urlpatterns += router.urls
