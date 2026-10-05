from django.urls import re_path

from .consumers import WhatsAppConsumer

websocket_urlpatterns = [
    re_path(r'^ws/whatsapp/?$', WhatsAppConsumer.as_asgi()),
]
