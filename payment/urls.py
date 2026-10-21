from django.urls import include, path
from rest_framework.routers import DefaultRouter

from payment.views import (
    AdminSubscriptionPlanViewSet,
    AdminSubscriptionsOverviewView,
    BillingPortalView,
    BillingStatusView,
    CheckoutSessionView,
    ChangePlanView,
    ReferralInvitationViewSet,
    StripeWebhookView,
    VerifyCheckoutSessionView,
)

router = DefaultRouter()
router.register('admin/plans', AdminSubscriptionPlanViewSet, basename='admin-plans')
router.register('referrals', ReferralInvitationViewSet, basename='referrals')

urlpatterns = [
    path('admin/subscriptions/', AdminSubscriptionsOverviewView.as_view(), name='admin-subscriptions-overview'),
    path('checkout/', CheckoutSessionView.as_view(), name='checkout'),
    path('change-plan/', ChangePlanView.as_view(), name='change-plan'),
    path('portal/', BillingPortalView.as_view(), name='portal'),
    path('status/', BillingStatusView.as_view(), name='status'),
    path('verify/', VerifyCheckoutSessionView.as_view(), name='verify'),
    path('webhook/', StripeWebhookView.as_view(), name='webhook'),
    path('', include(router.urls)),
]

