from django.urls import path

from payment.views import (
    BillingPortalView,
    BillingStatusView,
    CheckoutSessionView,
    PlansListView,
    StripeWebhookView,
    VerifyCheckoutSessionView,
)

urlpatterns = [
    path('plans/', PlansListView.as_view(), name='plans'),
    path('checkout/', CheckoutSessionView.as_view(), name='checkout'),
    path('portal/', BillingPortalView.as_view(), name='portal'),
    path('status/', BillingStatusView.as_view(), name='status'),
    path('verify/', VerifyCheckoutSessionView.as_view(), name='verify'),
    path('webhook/', StripeWebhookView.as_view(), name='webhook'),
]
