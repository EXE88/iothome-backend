import uuid

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models

from core.fields import EncryptedTextField


class Product(models.Model):
    """A sellable listing for one gadget type."""

    gadget_type = models.ForeignKey(
        "gadgets.GadgetType", on_delete=models.PROTECT, related_name="products"
    )
    # The catalogue is sold in Persian first and English second, so the copy
    # is a column rather than a translation layer: a shop owner adds a product
    # in the admin and writes both, and the storefront picks by locale. An
    # empty Persian field falls back to the English one rather than blanking
    # the card.
    name = models.CharField(max_length=255)
    name_fa = models.CharField(max_length=255, blank=True)
    slug = models.SlugField(max_length=255, unique=True)
    description = models.TextField(blank=True)
    description_fa = models.TextField(blank=True)
    image_url = models.URLField(blank=True)
    # Toman. Zarinpal works in Rial, so the gateway layer multiplies by 10.
    price = models.DecimalField(
        max_digits=12, decimal_places=0, validators=[MinValueValidator(0)]
    )
    stock = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name",)

    def __str__(self):
        return self.name

    @property
    def is_available(self):
        return self.is_active and self.stock > 0


class Order(models.Model):
    """A basket that has been placed, including the Wi-Fi flashed at assembly.

    One order carries any number of lines (``OrderItem``) and settles as a
    single payment, because a buyer who put a lamp and a camera in one basket
    should visit the gateway once. The Wi-Fi and shipping details belong to
    the order rather than the line: every unit in a basket is going to the
    same house and onto the same network.
    """

    STATUS_PENDING_PAYMENT = "pending_payment"
    STATUS_PAID = "paid"
    STATUS_FAILED = "failed"
    STATUS_CANCELED = "canceled"
    STATUS_ASSEMBLING = "assembling"
    STATUS_SHIPPED = "shipped"
    STATUS_DELIVERED = "delivered"
    STATUS_CHOICES = (
        (STATUS_PENDING_PAYMENT, "Pending payment"),
        (STATUS_PAID, "Paid"),
        (STATUS_FAILED, "Payment failed"),
        (STATUS_CANCELED, "Canceled"),
        (STATUS_ASSEMBLING, "Assembling"),
        (STATUS_SHIPPED, "Shipped"),
        (STATUS_DELIVERED, "Delivered"),
    )

    uid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="orders"
    )
    # The sum of its lines, frozen at checkout. Stored rather than computed so
    # a later price change cannot rewrite what was actually charged, and so
    # the amount sent to the gateway and the amount verified on the way back
    # are read from the same column.
    total_amount = models.DecimalField(max_digits=12, decimal_places=0)

    # Burned into the firmware, never exposed back through the API.
    wifi_ssid = EncryptedTextField()
    wifi_password = EncryptedTextField()

    receiver_name = models.CharField(max_length=150)
    receiver_phone = models.CharField(max_length=20)
    shipping_address = models.TextField()
    postal_code = models.CharField(max_length=20, blank=True)

    status = models.CharField(
        max_length=24, choices=STATUS_CHOICES, default=STATUS_PENDING_PAYMENT
    )
    tracking_code = models.CharField(max_length=64, blank=True)

    # Which origin the buyer checked out from, so the gateway callback can
    # send them back to the same one. The site is reachable under more than
    # one hostname in development (localhost and 127.0.0.1 at least), and
    # those do not share cookies — bouncing someone to the other one drops
    # them into whatever session that hostname happens to be holding. Only
    # ever set from the CORS allowlist, never from user input.
    return_origin = models.URLField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["user", "-created_at"])]

    def __str__(self):
        return f"Order {self.uid} - {self.user.email}"

    @property
    def is_payable(self):
        return self.status == self.STATUS_PENDING_PAYMENT

    @property
    def unit_count(self):
        """How many physical devices this order becomes."""
        return sum(item.quantity for item in self.items.all())

    @property
    def summary(self):
        """One line for the gateway's description field and the admin list."""
        return ", ".join(
            f"{item.product.name} x{item.quantity}" for item in self.items.all()
        )


class OrderItem(models.Model):
    """One product line inside an order."""

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, related_name="order_items"
    )
    quantity = models.PositiveSmallIntegerField(default=1)
    # Frozen at checkout so a later price change never rewrites history.
    unit_price = models.DecimalField(max_digits=12, decimal_places=0)

    class Meta:
        ordering = ("id",)
        constraints = [
            # A basket holds one line per product; adding the same lamp twice
            # raises its quantity instead of opening a second line.
            models.UniqueConstraint(
                fields=["order", "product"], name="one_line_per_product_per_order"
            )
        ]

    def __str__(self):
        return f"{self.product.name} x{self.quantity}"

    @property
    def line_total(self):
        return self.unit_price * self.quantity


class Payment(models.Model):
    GATEWAY_ZARINPAL = "zarinpal"
    GATEWAY_CHOICES = ((GATEWAY_ZARINPAL, "Zarinpal"),)

    STATUS_PENDING = "pending"
    STATUS_SUCCESS = "success"
    STATUS_FAILED = "failed"
    STATUS_CANCELED = "canceled"
    STATUS_CHOICES = (
        (STATUS_PENDING, "Pending"),
        (STATUS_SUCCESS, "Success"),
        (STATUS_FAILED, "Failed"),
        (STATUS_CANCELED, "Canceled by user"),
    )

    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="payments")
    gateway = models.CharField(
        max_length=32, choices=GATEWAY_CHOICES, default=GATEWAY_ZARINPAL
    )
    amount = models.DecimalField(max_digits=12, decimal_places=0)
    authority = models.CharField(max_length=128, blank=True, db_index=True)
    ref_id = models.CharField(max_length=64, blank=True)
    card_pan = models.CharField(max_length=32, blank=True)
    fee = models.DecimalField(max_digits=12, decimal_places=0, default=0)
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING
    )
    error_code = models.CharField(max_length=32, blank=True)
    error_message = models.CharField(max_length=255, blank=True)
    gateway_response = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            # One successful payment per order — the safety net behind the
            # idempotency check in the verify view.
            models.UniqueConstraint(
                fields=["order"],
                condition=models.Q(status="success"),
                name="one_successful_payment_per_order",
            )
        ]

    def __str__(self):
        return f"{self.gateway}:{self.authority or '-'} [{self.status}]"
