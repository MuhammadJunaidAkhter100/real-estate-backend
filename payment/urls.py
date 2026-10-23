from django.urls import include, path
from rest_framework.routers import DefaultRouter

from payment.views import (
    AdminSubscriptionPlanViewSet,
    AdminSubscriptionsOverviewView,
    BillingPortalView,
    BillingStatusView,
    BillingUsageView,
    CheckoutSessionView,
    ChangePlanView,
    DevelopmentPlanBypassView,
    ReferralInvitationViewSet,
    StripeWebhookView,
    VerifyCheckoutSessionView,
)

router = DefaultRouter()
router.register('admin/plans', AdminSubscriptionPlanViewSet, basename='admin-plans')
router.register('referrals', ReferralInvitationViewSet, basename='referrals')

urlpatterns = [
    path('dev/plan-bypass/', DevelopmentPlanBypassView.as_view(), name='development-plan-bypass'),
    path('admin/subscriptions/', AdminSubscriptionsOverviewView.as_view(), name='admin-subscriptions-overview'),
    path('checkout/', CheckoutSessionView.as_view(), name='checkout'),
    path('change-plan/', ChangePlanView.as_view(), name='change-plan'),
    path('portal/', BillingPortalView.as_view(), name='portal'),
    path('status/', BillingStatusView.as_view(), name='status'),
    path('usage/', BillingUsageView.as_view(), name='billing-usage'),
    path('verify/', VerifyCheckoutSessionView.as_view(), name='verify'),
    path('webhook/', StripeWebhookView.as_view(), name='webhook'),
    path('', include(router.urls)),
]

