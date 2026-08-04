import secrets
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from core.fields import EncryptedTextField


class GadgetType(models.Model):
    """Technical definition of a device family (what a lamp/thermometer *is*).

    Kept separate from ``purchases.Product`` so the same hardware can be sold
    under several listings without duplicating its protocol description.
    """

    MODE_TELEMETRY = "telemetry"  # pushes readings on its own, e.g. thermometer
    MODE_ACTION = "action"  # only answers commands, e.g. lamp
    MODE_HYBRID = "hybrid"  # does both
    MODE_CHOICES = (
        (MODE_TELEMETRY, "Streams telemetry"),
        (MODE_ACTION, "Action / response only"),
        (MODE_HYBRID, "Both"),
    )

    slug = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    mode = models.CharField(max_length=16, choices=MODE_CHOICES)
    # How often the firmware is expected to report in; the offline sweeper
    # uses this instead of one global timeout, because a battery sensor and a
    # mains-powered lamp do not have the same rhythm.
    heartbeat_interval_seconds = models.PositiveIntegerField(default=60)
    offline_after_missed_heartbeats = models.PositiveSmallIntegerField(default=3)

    class Meta:
        ordering = ("name",)

    def __str__(self):
        return self.name

    @property
    def offline_timeout_seconds(self):
        return self.heartbeat_interval_seconds * self.offline_after_missed_heartbeats


class Capability(models.Model):
    """One readable value or one accepted command of a gadget type.

    This is the contract the frontend reads to know how to render a control
    and what the backend validates every inbound value against.
    """

    DIRECTION_TELEMETRY = "telemetry"  # device -> server
    DIRECTION_COMMAND = "command"  # server -> device
    DIRECTION_CHOICES = (
        (DIRECTION_TELEMETRY, "Telemetry"),
        (DIRECTION_COMMAND, "Command"),
    )

    TYPE_BOOL = "bool"
    TYPE_INT = "int"
    TYPE_FLOAT = "float"
    TYPE_STRING = "string"
    TYPE_ENUM = "enum"
    TYPE_CHOICES = (
        (TYPE_BOOL, "Boolean"),
        (TYPE_INT, "Integer"),
        (TYPE_FLOAT, "Float"),
        (TYPE_STRING, "String"),
        (TYPE_ENUM, "Enum"),
    )

    gadget_type = models.ForeignKey(
        GadgetType, on_delete=models.CASCADE, related_name="capabilities"
    )
    key = models.SlugField(max_length=48)
    label = models.CharField(max_length=120, blank=True)
    direction = models.CharField(max_length=16, choices=DIRECTION_CHOICES)
    value_type = models.CharField(max_length=16, choices=TYPE_CHOICES)
    unit = models.CharField(max_length=16, blank=True)
    min_value = models.FloatField(null=True, blank=True)
    max_value = models.FloatField(null=True, blank=True)
    max_length = models.PositiveSmallIntegerField(default=64)
    choices = models.JSONField(default=list, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["gadget_type", "direction", "key"],
                name="unique_capability_per_type_direction",
            )
        ]
        ordering = ("gadget_type", "direction", "key")

    def __str__(self):
        return f"{self.gadget_type.slug}.{self.direction}.{self.key}"

    def clean(self):
        if self.value_type == self.TYPE_ENUM and not self.choices:
            raise ValidationError({"choices": "Enum capabilities need choices."})
        if (
            self.min_value is not None
            and self.max_value is not None
            and self.min_value > self.max_value
        ):
            raise ValidationError({"min_value": "min_value exceeds max_value."})

    def validate_value(self, value):
        """Coerce and range-check a value, or raise ``ValidationError``.

        Everything crossing the WebSocket — both device readings and user
        commands — goes through here, so a device cannot inject a string into
        a numeric series and a user cannot push an out-of-range brightness.
        """
        if self.value_type == self.TYPE_BOOL:
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.lower() in ("true", "false"):
                return value.lower() == "true"
            if value in (0, 1):
                return bool(value)
            raise ValidationError(f"'{self.key}' expects a boolean.")

        if self.value_type in (self.TYPE_INT, self.TYPE_FLOAT):
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                raise ValidationError(f"'{self.key}' expects a number.")
            try:
                number = int(value) if self.value_type == self.TYPE_INT else float(value)
            except (TypeError, ValueError):
                raise ValidationError(f"'{self.key}' expects a number.")
            if number != number or number in (float("inf"), float("-inf")):
                raise ValidationError(f"'{self.key}' must be finite.")
            if self.min_value is not None and number < self.min_value:
                raise ValidationError(f"'{self.key}' is below {self.min_value}.")
            if self.max_value is not None and number > self.max_value:
                raise ValidationError(f"'{self.key}' is above {self.max_value}.")
            return number

        if self.value_type == self.TYPE_ENUM:
            if value not in self.choices:
                raise ValidationError(
                    f"'{self.key}' must be one of {', '.join(map(str, self.choices))}."
                )
            return value

        if not isinstance(value, str):
            raise ValidationError(f"'{self.key}' expects a string.")
        if len(value) > self.max_length:
            raise ValidationError(f"'{self.key}' is longer than {self.max_length}.")
        return value


def generate_secret_key():
    return secrets.token_hex(32)


class Gadget(models.Model):
    """A single physical unit, created once its order is paid for."""

    STATUS_AWAITING_ASSEMBLY = "awaiting_assembly"
    STATUS_ASSEMBLED = "assembled"
    STATUS_SHIPPED = "shipped"
    STATUS_ACTIVE = "active"
    STATUS_DISABLED = "disabled"
    STATUS_CHOICES = (
        (STATUS_AWAITING_ASSEMBLY, "Awaiting assembly"),
        (STATUS_ASSEMBLED, "Assembled"),
        (STATUS_SHIPPED, "Shipped"),
        (STATUS_ACTIVE, "Active"),
        (STATUS_DISABLED, "Disabled"),
    )

    uid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="gadgets"
    )
    product = models.ForeignKey(
        "purchases.Product", on_delete=models.PROTECT, related_name="gadgets"
    )
    gadget_type = models.ForeignKey(
        GadgetType, on_delete=models.PROTECT, related_name="gadgets"
    )
    order = models.ForeignKey(
        "purchases.Order",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="gadgets",
    )

    name = models.CharField(max_length=120, blank=True)
    # Hardcoded into the firmware together with the Wi-Fi credentials; the
    # server keeps it encrypted because it must recompute the device's HMAC.
    secret_key = EncryptedTextField(default=generate_secret_key)
    wifi_ssid = EncryptedTextField(blank=True, default="")
    wifi_password = EncryptedTextField(blank=True, default="")

    status = models.CharField(
        max_length=24, choices=STATUS_CHOICES, default=STATUS_AWAITING_ASSEMBLY
    )
    firmware_version = models.CharField(max_length=32, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    last_ip = models.GenericIPAddressField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["owner", "status"])]

    def __str__(self):
        return f"{self.display_name} ({self.uid})"

    @property
    def display_name(self):
        return self.name or self.product.name

    @property
    def can_connect(self):
        """Disabled units are refused at the handshake, whatever they send."""
        return self.status != self.STATUS_DISABLED

    def capability(self, direction, key):
        return self.gadget_type.capabilities.filter(
            direction=direction, key=key
        ).first()

    def mark_seen(self, ip=None):
        self.last_seen_at = timezone.now()
        fields = ["last_seen_at", "updated_at"]
        if ip:
            self.last_ip = ip
            fields.append("last_ip")
        if self.status in (self.STATUS_ASSEMBLED, self.STATUS_SHIPPED):
            self.status = self.STATUS_ACTIVE
            fields.append("status")
        self.save(update_fields=fields)


class Command(models.Model):
    """One user-issued command and whatever the device answered.

    This is the only history the system keeps. Commands are human-initiated,
    so the volume is small, and the audit trail is what answers "who turned
    this on" later. Streamed telemetry is relayed live and never stored.
    """

    STATUS_PENDING = "pending"
    STATUS_SENT = "sent"
    STATUS_ACKED = "acked"
    STATUS_FAILED = "failed"
    STATUS_TIMEOUT = "timeout"
    STATUS_CHOICES = (
        (STATUS_PENDING, "Pending"),
        (STATUS_SENT, "Sent to device"),
        (STATUS_ACKED, "Acknowledged"),
        (STATUS_FAILED, "Failed"),
        (STATUS_TIMEOUT, "Timed out"),
    )

    request_id = models.UUIDField(default=uuid.uuid4, unique=True)
    gadget = models.ForeignKey(
        Gadget, on_delete=models.CASCADE, related_name="commands"
    )
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="commands",
    )
    key = models.SlugField(max_length=48)
    value = models.JSONField(null=True, blank=True)
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING
    )
    response = models.JSONField(null=True, blank=True)
    error = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    responded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["gadget", "-created_at"])]

    def __str__(self):
        return f"{self.key}={self.value} -> {self.gadget_id} [{self.status}]"
