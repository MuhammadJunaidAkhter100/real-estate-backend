from rest_framework.routers import DefaultRouter
from django.urls import path
from projects.views import ProjectViewSet, PromotionViewSet, UnitViewSet
from projects.zoho_views import (
    fetch_leads
)

router = DefaultRouter()
router.register(r'development_portfolio', ProjectViewSet, basename='development-portfolio')
router.register(r'units', UnitViewSet, basename='unit')
router.register(r'promotions', PromotionViewSet, basename='promotion')

# Zoho CRM Integration URLs
zoho_urls = [
    path('zoho/leads/', fetch_leads, name='fetch_leads'),
]

urlpatterns = router.urls + zoho_urls
