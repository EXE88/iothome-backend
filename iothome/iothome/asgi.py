"""ASGI entrypoint: plain HTTP for DRF, Channels for the WebSocket endpoints."""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "iothome.settings")

from django.core.asgi import get_asgi_application  # noqa: E402

# Instantiated before importing anything that touches models.
django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402

from gadgets.routing import websocket_urlpatterns  # noqa: E402

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        # Origin checking is applied per-route in gadgets.routing.
        "websocket": URLRouter(websocket_urlpatterns),
    }
)
