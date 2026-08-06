"""Create the reference gadget types and their products.

    python manage.py seed_catalog

Idempotent — run it as often as you like.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from gadgets.models import Capability, GadgetType
from purchases.models import Product

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
            "slug": "smartlife-thermometer",
            "price": 850000,
            "stock": 50,
            "description": "Wi-Fi temperature and humidity sensor for any room.",
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
            # The lamp does not stream, but it echoes its state after each
            # command so the UI is never guessing.
            {
                "key": "power",
                "label": "Power state",
                "direction": Capability.DIRECTION_TELEMETRY,
                "value_type": Capability.TYPE_BOOL,
            },
        ],
        "product": {
            "name": "SmartLife Smart Lamp",
            "slug": "smartlife-smart-lamp",
            "price": 1250000,
            "stock": 30,
            "description": "Dimmable Wi-Fi lamp with three colour temperatures.",
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
                "key": "motion",
                "label": "Motion",
                "direction": Capability.DIRECTION_TELEMETRY,
                "value_type": Capability.TYPE_BOOL,
            },
        ],
        "product": {
            "name": "SmartLife Security Camera",
            "slug": "smartlife-security-camera",
            "price": 2400000,
            "stock": 20,
            "description": "Wi-Fi camera that records on command and flags motion.",
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
