from channels.security.websocket import AllowedHostsOriginValidator
from django.urls import re_path

from . import consumers

UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

# Firmware clients do not send an Origin header, and the origin validator
# rejects connections without one — so the browser-facing route is wrapped
# individually instead of guarding the whole router.
device_websocket_urlpatterns = [
    re_path(
        rf"^ws/device/(?P<device_uid>{UUID_RE})/$",
        consumers.DeviceConsumer.as_asgi(),
    ),
]

user_websocket_urlpatterns = [
    re_path(
        r"^ws/user/$",
        AllowedHostsOriginValidator(consumers.UserConsumer.as_asgi()),
    ),
]

websocket_urlpatterns = device_websocket_urlpatterns + user_websocket_urlpatterns
