from django.urls import path
from rest_framework.routers import DefaultRouter

from users.country_views import CompanyCountriesView, CompanyCountryDetailView, CountryListView
from users.property_news import GulfNewsPropertyView, KhaleejTimesPropertyView, PropertyWireView
from users.views import (
    AdminCompanyViewSet,
    CommissionAnalyticsView,
    CompanyScrapingStatusView,
    DashboardSummaryView,
    LatestCommissionsView,
    LeadViewSet,
    StreamActivityView,
    SupportContactView,
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
    path('countries/', CountryListView.as_view(), name='countries'),
    path('company/countries/', CompanyCountriesView.as_view(), name='company-countries'),
    path('company/countries/<str:country>/', CompanyCountryDetailView.as_view(), name='company-country-detail'),
    path('company/scraping-status/', CompanyScrapingStatusView.as_view(), name='company-scraping-status'),
    path('support/', SupportContactView.as_view(), name='support-contact'),
]

urlpatterns += router.urls
