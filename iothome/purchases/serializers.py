from rest_framework import serializers

from .models import Order, OrderItem, Payment, Product


class ProductSerializer(serializers.ModelSerializer):
    gadget_type = serializers.SlugRelatedField(slug_field="slug", read_only=True)
    mode = serializers.CharField(source="gadget_type.mode", read_only=True)
    is_available = serializers.BooleanField(read_only=True)

    class Meta:
        model = Product
        fields = (
            "id",
            "name",
            "name_fa",
            "slug",
            "description",
            "description_fa",
            "image_url",
            "price",
            "stock",
            "is_available",
            "gadget_type",
            "mode",
        )


class PaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Payment
        fields = (
            "id",
            "gateway",
            "amount",
            "ref_id",
            "card_pan",
            "status",
            "error_message",
            "created_at",
            "verified_at",
        )
        read_only_fields = fields


class OrderItemSerializer(serializers.ModelSerializer):
    product = ProductSerializer(read_only=True)
    line_total = serializers.DecimalField(
        max_digits=12, decimal_places=0, read_only=True
    )

    class Meta:
        model = OrderItem
        fields = ("product", "quantity", "unit_price", "line_total")
        read_only_fields = fields


class OrderSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True, read_only=True)
    payments = PaymentSerializer(many=True, read_only=True)
    unit_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Order
        # wifi_ssid / wifi_password are deliberately absent: they go in, they
        # never come back out.
        fields = (
            "uid",
            "items",
            "unit_count",
            "total_amount",
            "receiver_name",
            "receiver_phone",
            "shipping_address",
            "postal_code",
            "status",
            "tracking_code",
            "created_at",
            "payments",
        )
        read_only_fields = fields


class BasketLineSerializer(serializers.Serializer):
    product = serializers.IntegerField()
    quantity = serializers.IntegerField(min_value=1, max_value=10, default=1)


class OrderCreateSerializer(serializers.Serializer):
    """The basket, plus where it is going and what network it joins."""

    items = BasketLineSerializer(many=True, allow_empty=False, max_length=20)

    wifi_ssid = serializers.CharField(max_length=32)
    wifi_password = serializers.CharField(max_length=63, allow_blank=True)

    receiver_name = serializers.CharField(max_length=150)
    receiver_phone = serializers.RegexField(r"^09\d{9}$", max_length=20)
    shipping_address = serializers.CharField(max_length=1000)
    postal_code = serializers.RegexField(
        r"^\d{10}$", required=False, allow_blank=True, default=""
    )

    def validate_wifi_ssid(self, value):
        # 32 bytes is the 802.11 limit; a longer SSID would silently truncate
        # on the ESP32 and the device would never find the network.
        if len(value.encode()) > 32:
            raise serializers.ValidationError("SSID is longer than 32 bytes.")
        return value

    def validate_wifi_password(self, value):
        if value and not 8 <= len(value) <= 63:
            raise serializers.ValidationError(
                "WPA passwords are between 8 and 63 characters."
            )
        return value
