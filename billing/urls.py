from django.urls import path

from . import views

urlpatterns = [
    path('checkout/', views.BillingCheckoutView.as_view(), name='billing-checkout'),
    path('portal/', views.BillingPortalView.as_view(), name='billing-portal'),
    path('status/', views.BillingStatusView.as_view(), name='billing-status'),
    path(
        'verify-session/',
        views.VerifyCheckoutSessionView.as_view(),
        name='billing-verify-session',
    ),
]

# Exposed at the project root rather than under /api/billing/ so the public URL
# matches the Stripe dashboard configuration.
public_urlpatterns = [
    path(
        'webhooks/stripe/',
        views.StripeWebhookView.as_view(),
        name='stripe-webhook',
    ),
    # Accept the slashless URL too: Stripe must never be redirected because
    # redirects can discard the signed POST body.
    path(
        'webhooks/stripe',
        views.StripeWebhookView.as_view(),
        name='stripe-webhook-no-slash',
    ),
]