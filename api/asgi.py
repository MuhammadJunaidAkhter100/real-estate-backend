"""
ASGI config for api project.

Routes HTTP through Django and WebSocket connections through Django Channels.
"""
import os

import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'api.settings')
django.setup()

from django.core.asgi import get_asgi_application  # noqa: E402

django_asgi_app = get_asgi_application()

try:
    from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402

    from billing.middleware import JWTSubscriptionStack  # noqa: E402
    from calling_agent.routing import (  # noqa: E402
        websocket_urlpatterns as calling_agent_ws_urls,
    )
    from notifications.routing import (  # noqa: E402
        websocket_urlpatterns as notification_ws_urls,
    )
    from whatsapp.routing import (  # noqa: E402
        websocket_urlpatterns as whatsapp_ws_urls,
    )

    application = ProtocolTypeRouter({
        "http": django_asgi_app,
        # JWT resolution first, then subscription enforcement, so a company that
        # cannot pay is disconnected instead of streaming.
        "websocket": JWTSubscriptionStack(
            URLRouter(
                notification_ws_urls
                + whatsapp_ws_urls
                + calling_agent_ws_urls
            )
        ),
    })
except ImportError:
    # `channels` is optional during local HTTP-only development.
    application = django_asgi_app
