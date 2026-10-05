# Django API Project Template

This repository is a reusable template for initializing Django API projects. It comes pre-configured with essential packages for building robust, secure, and scalable REST APIs.

## Included Python Packages

- **Django==5.2.3**  
  The core web framework for rapid development and clean, pragmatic design.

- **djangorestframework==3.16.0**  
  Powerful and flexible toolkit for building Web APIs in Django.

- **django-filter==25.1**  
  Provides filtering capabilities for Django REST Framework APIs.

- **python-decouple==3.8**  
  Helps separate settings from code by reading configuration from environment variables or `.env` files.

- **drf-yasg==1.21.10**  
  Automatically generates real Swagger/OpenAPI 2.0 specifications from Django REST Framework code.

- **whitenoise==6.9.0**  
  Simplifies static file serving for Django applications, especially useful for deployment.

- **django-cors-headers==4.7.0**  
  Handles Cross-Origin Resource Sharing (CORS), making it easy to allow or restrict resource sharing between different domains.

- **djangorestframework_simplejwt==5.5.0**  
  Provides JSON Web Token (JWT) authentication for Django REST Framework.

---

## Project Initialization Steps

Follow these steps to set up your project:

1. **Clone the repository (internal use):**
   ```sh
   git clone git@github-samiu11ah:samiu11ah/my-api-template.git# Django API Project Template

This repository is a reusable template for initializing Django API projects. It comes pre-configured with essential packages for building robust, secure, and scalable REST APIs.

## Included Python Packages

- **Django==5.2.3**  
  The core web framework for rapid development and clean, pragmatic design.

- **djangorestframework==3.16.0**  
  Powerful and flexible toolkit for building Web APIs in Django.

- **django-filter==25.1**  
  Provides filtering capabilities for Django REST Framework APIs.

- **python-decouple==3.8**  
  Helps separate settings from code by reading configuration from environment variables or `.env` files.

- **drf-yasg==1.21.10**  
  Automatically generates real Swagger/OpenAPI 2.0 specifications from Django REST Framework code.

- **whitenoise==6.9.0**  
  Simplifies static file serving for Django applications, especially useful for deployment.

- **django-cors-headers==4.7.0**  
  Handles Cross-Origin Resource Sharing (CORS), making it easy to allow or restrict resource sharing between different domains.

- **djangorestframework_simplejwt==5.5.0**  
  Provides JSON Web Token (JWT) authentication for Django REST Framework.

---

## Project Initialization Steps

Follow these steps to set up your project:

1. **Clone the repository (internal use):**
   ```sh
   git clone git@github-samiu11ah:samiu11ah/my-api-template.git
   ```

2. **Navigate to the project directory:**
   ```sh
   cd my-api-template
   ```

3. **Create a virtual environment:**
   ```sh
   python -m venv .venv
   ```

4. **Activate the virtual environment:**
   ```sh
   source .venv/bin/activate
   ```

5. **Install dependencies:**
   ```sh
   pip install -r requirements.txt
   ```

6. **Apply database migrations:**
   ```sh
   python manage.py migrate
   ```

7. **Run the development server:**
   ```sh
   python manage.py runserver
   ```

---

## Stripe billing (self-serve signup)

Self-serve company signup and subscriptions are handled by the `billing` app.
Without Stripe credentials the endpoints return `503 BILLING_UNAVAILABLE`; the
Basic (free) plan works with no Stripe configuration at all.

Configure these in `.env` (see `.env_sample`):

| Variable | Purpose |
| --- | --- |
| `STRIPE_SECRET_KEY` | Stripe API key (`sk_test_...`). |
| `STRIPE_WEBHOOK_SECRET` | Signing secret for webhook verification. |
| `STRIPE_PRICE_PRO_MONTHLY` | Stripe Price ID for the monthly Professional plan. |
| `STRIPE_PRICE_PRO_ANNUAL` | Stripe Price ID for the annual Professional plan. |
| `BILLING_GRACE_PERIOD_DAYS` | Days a `past_due` company keeps working. Default 5. |
| `BILLING_PENDING_COMPANY_TTL_HOURS` | Hours before an unpaid signup is removed. Default 48. |
| `BILLING_CACHE_URL` | Shared Redis URL for billing state. Must be shared across workers. |

Price IDs are resolved **server-side only**; clients never send them, so a user
cannot choose what they are charged. Only these events are handled:

- `checkout.session.completed`, `invoice.paid`, `customer.subscription.updated`
- `invoice.payment_failed` → `past_due` with a grace window
- `customer.subscription.deleted` → downgrade to Basic

The webhook is the only component that activates a company. It reads the raw
body first, verifies the signature with `STRIPE_WEBHOOK_SECRET`, and records
each `event.id` in `billing_processedstripeevent` so redeliveries are no-ops.

Forward events while developing:

```sh
stripe listen --forward-to http://localhost:8000/webhooks/stripe
```

Point the Stripe webhook endpoint at `https://<your-host>/webhooks/stripe/`
(the path is intentionally outside the `/api/` mount).

Subscription enforcement is global: `billing.middleware.SubscriptionGuardMiddleware`
returns HTTP 402 before a view runs when the caller's company is `pending_payment`,
`suspended` or past due beyond its grace window. Super admins and `manual` plan
companies are never blocked, and the auth, billing, webhook, `/me` and health
routes stay reachable so a blocked company can still pay.

### Endpoints

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| `POST` | `/api/auth/register-company/` | none | Self-serve signup. Returns tokens, the new user, and `checkoutUrl` for Professional. |
| `POST` | `/api/billing/checkout/` | yes | Start a Checkout Session (`{"interval": "monthly"\|"annual"}`). |
| `POST` | `/api/billing/portal/` | yes | Open the Stripe customer portal. |
| `GET` | `/api/billing/status/` | yes | Billing state, plan limits and current usage. |
| `GET` | `/api/billing/verify-session/?session_id=...` | yes | Confirm a Checkout Session belongs to the caller and was paid. Activates nothing. |
| `POST` | `/webhooks/stripe/` | signature | Stripe events. The only activation authority. |

Billing errors use a fixed body so clients can branch without string matching:

```jsonc
// 403
{"code": "PLAN_LIMIT_REACHED", "detail": "...", "kind": "aiCredits", "limit": 500, "current": 500}
// 402
{"code": "PAYMENT_REQUIRED", "detail": "...", "status": "past_due", "allowed": false}
```

### Metered operations

Plan caps live in `billing/plan_limits.py`. Quotas (`ai_proposals`) and AI
credits are counted per UTC calendar month; `users` and `team_managers` are
standing roster caps.

| Operation | Cost | Charged at |
| --- | --- | --- |
| Chat turn | 1 | `chatbot/views.py`, before the SSE stream opens |
| Proposal generation | 10 | `new_proposal/tasks.py`, in the Celery worker |
| AI calling agent call | 5 | `calling_agent/services.py`, after the provider dials |
| Transcript analysis | 1 | `calling_agent/tasks.py` |
| KB document extraction | 1 | `chatbot/tasks.py` |

Every charge is idempotent through `billing.CreditCharge.task_id`, so a retried
Celery task never bills twice. Work that fails before completing refunds what it
consumed, and a refused charge is reported as a `PLAN_LIMIT_REACHED` 403 rather
than a silent background failure. Unlimited plans still record usage.

---

You now have a ready-to-use Django API project with best practices and essential packages included!