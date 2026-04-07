from django.urls import path

from .consumers import CallStreamConsumer

websocket_urlpatterns = [
    path("ws/call/", CallStreamConsumer.as_asgi()),
]
