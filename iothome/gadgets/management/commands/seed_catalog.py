"""Create the reference gadget types and their products.

    python manage.py seed_catalog

Idempotent — run it as often as you like.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from gadgets.models import Capability, GadgetType
from purchases.models import Product

# Every key a device *accepts* must also be a key it *reports*.
#
# The server keeps no last-known value by design: when a dashboard opens it
# asks each device to report itself, and the device answers with a telemetry
# frame. A telemetry frame may only carry keys the type declares as telemetry,
# so an actuator key that exists only as a command can never be reported —
# which is what made a lamp's brightness snap back to nothing on reload. Each
# command capability below therefore has a telemetry twin.
CATALOG = [
    {
        "slug": "thermometer",
        "name": "Smart Thermometer",
        "mode": GadgetType.MODE_TELEMETRY,
        "description": "Reports temperature and humidity continuously.",
        "heartbeat_interval_seconds": 60,
        "capabilities": [
            {
                "key": "temperature",
                "label": "Temperature",
                "direction": Capability.DIRECTION_TELEMETRY,
                "value_type": Capability.TYPE_FLOAT,
                "unit": "C",
                "min_value": -40,
                "max_value": 125,
            },
            {
                "key": "humidity",
                "label": "Humidity",
                "direction": Capability.DIRECTION_TELEMETRY,
                "value_type": Capability.TYPE_FLOAT,
                "unit": "%",
                "min_value": 0,
                "max_value": 100,
            },
        ],
        "product": {
            "name": "SmartLife Thermometer",
            "name_fa": "دماسنج اسمارت‌لایف",
            "slug": "smartlife-thermometer",
            "price": 850000,
            "stock": 50,
            "description": "Wi-Fi temperature and humidity sensor for any room.",
            "description_fa": "سنسور دما و رطوبت وای‌فای، برای هر اتاقی.",
        },
    },
    {
        "slug": "smart-lamp",
        "name": "Smart Lamp",
        "mode": GadgetType.MODE_ACTION,
        "description": "Answers on/off, brightness and colour commands.",
        "heartbeat_interval_seconds": 60,
        "capabilities": [
            {
                "key": "power",
                "label": "Power",
                "direction": Capability.DIRECTION_COMMAND,
                "value_type": Capability.TYPE_BOOL,
            },
            {
                "key": "brightness",
                "label": "Brightness",
                "direction": Capability.DIRECTION_COMMAND,
                "value_type": Capability.TYPE_INT,
                "unit": "%",
                "min_value": 0,
                "max_value": 100,
            },
            {
                "key": "color",
                "label": "Colour",
                "direction": Capability.DIRECTION_COMMAND,
                "value_type": Capability.TYPE_ENUM,
                "choices": ["warm", "neutral", "cool"],
            },
            # The lamp does not stream, but it echoes its whole state after
            # each command and whenever it is polled, so the UI is never
            # guessing and a reload does not lose the setting.
            {
                "key": "power",
                "label": "Power state",
                "direction": Capability.DIRECTION_TELEMETRY,
                "value_type": Capability.TYPE_BOOL,
            },
            {
                "key": "brightness",
                "label": "Brightness state",
                "direction": Capability.DIRECTION_TELEMETRY,
                "value_type": Capability.TYPE_INT,
                "unit": "%",
                "min_value": 0,
                "max_value": 100,
            },
            {
                "key": "color",
                "label": "Colour state",
                "direction": Capability.DIRECTION_TELEMETRY,
                "value_type": Capability.TYPE_ENUM,
                "choices": ["warm", "neutral", "cool"],
            },
        ],
        "product": {
            "name": "SmartLife Smart Lamp",
            "name_fa": "لامپ هوشمند اسمارت‌لایف",
            "slug": "smartlife-smart-lamp",
            "price": 1250000,
            "stock": 30,
            "description": "Dimmable Wi-Fi lamp with three colour temperatures.",
            "description_fa": "لامپ وای‌فای با شدت نور قابل تنظیم و سه رنگ نور.",
        },
    },
    {
        "slug": "camera",
        "name": "Security Camera",
        # Both directions: it answers commands and also reports motion on its
        # own, without being asked.
        "mode": GadgetType.MODE_HYBRID,
        "description": "Records on demand and reports motion as it happens.",
        "heartbeat_interval_seconds": 60,
        "capabilities": [
            {
                "key": "recording",
                "label": "Recording",
                "direction": Capability.DIRECTION_COMMAND,
                "value_type": Capability.TYPE_BOOL,
            },
            {
                "key": "motion_alerts",
                "label": "Motion alerts",
                "direction": Capability.DIRECTION_COMMAND,
                "value_type": Capability.TYPE_BOOL,
            },
            {
                "key": "recording",
                "label": "Recording state",
                "direction": Capability.DIRECTION_TELEMETRY,
                "value_type": Capability.TYPE_BOOL,
            },
            {
                "key": "motion_alerts",
                "label": "Motion alerts state",
                "direction": Capability.DIRECTION_TELEMETRY,
                "value_type": Capability.TYPE_BOOL,
            },
            # The only reading here that is genuinely a sensor: it changes on
            # its own and nobody sets it.
            {
                "key": "motion",
                "label": "Motion",
                "direction": Capability.DIRECTION_TELEMETRY,
                "value_type": Capability.TYPE_BOOL,
            },
        ],
        "product": {
            "name": "SmartLife Security Camera",
            "name_fa": "دوربین امنیتی اسمارت‌لایف",
            "slug": "smartlife-security-camera",
            "price": 2400000,
            "stock": 20,
            "description": "Wi-Fi camera that records on command and flags motion.",
            "description_fa": "دوربین وای‌فای که با درخواست شما ضبط می‌کند و حرکت را گزارش می‌دهد.",
        },
    },
]


class Command(BaseCommand):
    help = "Seed reference gadget types, capabilities and products."

    @transaction.atomic
    def handle(self, *args, **options):
        for entry in CATALOG:
            gadget_type, created = GadgetType.objects.update_or_create(
                slug=entry["slug"],
                defaults={
                    "name": entry["name"],
                    "mode": entry["mode"],
                    "description": entry["description"],
                    "heartbeat_interval_seconds": entry["heartbeat_interval_seconds"],
                },
            )
            for capability in entry["capabilities"]:
                Capability.objects.update_or_create(
                    gadget_type=gadget_type,
                    direction=capability["direction"],
                    key=capability["key"],
                    defaults={
                        k: v
                        for k, v in capability.items()
                        if k not in ("direction", "key")
                    },
                )
            product = entry["product"]
            Product.objects.update_or_create(
                slug=product["slug"],
                defaults={**product, "gadget_type": gadget_type},
            )
            self.stdout.write(
                self.style.SUCCESS(
                    f"{'created' if created else 'updated'} {gadget_type.slug}"
                )
            )
