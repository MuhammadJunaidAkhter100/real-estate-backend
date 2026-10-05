from django.urls import re_path

from .consumers import CallTranscriptConsumer

websocket_urlpatterns = [
    re_path(
        r'^ws/calling-agent/calls/(?P<public_id>[0-9a-fA-F-]+)/transcript/?$',
        CallTranscriptConsumer.as_asgi(),
    ),
]
