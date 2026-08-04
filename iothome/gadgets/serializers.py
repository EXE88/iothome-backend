from rest_framework import serializers

from .models import Capability, Command, Gadget, GadgetType


class CapabilitySerializer(serializers.ModelSerializer):
    class Meta:
        model = Capability
        fields = (
            "key",
            "label",
            "direction",
            "value_type",
            "unit",
            "min_value",
            "max_value",
            "choices",
        )


class GadgetTypeSerializer(serializers.ModelSerializer):
    capabilities = CapabilitySerializer(many=True, read_only=True)

    class Meta:
        model = GadgetType
        fields = (
            "slug",
            "name",
            "description",
            "mode",
            "heartbeat_interval_seconds",
            "capabilities",
        )


class GadgetSerializer(serializers.ModelSerializer):
    gadget_type = GadgetTypeSerializer(read_only=True)
    product_name = serializers.CharField(source="product.name", read_only=True)
    display_name = serializers.CharField(read_only=True)
    # Filled in by the view from Redis in one round trip for the whole page.
    online = serializers.SerializerMethodField()

    class Meta:
        model = Gadget
        fields = (
            "uid",
            "name",
            "display_name",
            "product_name",
            "gadget_type",
            "status",
            "online",
            "firmware_version",
            "last_seen_at",
            "created_at",
        )
        read_only_fields = tuple(f for f in fields if f != "name")

    def get_online(self, obj):
        return str(obj.uid) in self.context.get("online_uids", set())


class CommandSerializer(serializers.ModelSerializer):
    class Meta:
        model = Command
        fields = (
            "request_id",
            "key",
            "value",
            "status",
            "response",
            "error",
            "created_at",
            "responded_at",
        )
        read_only_fields = fields


class ProvisioningSerializer(serializers.ModelSerializer):
    """Everything the assembly line burns into a unit. Staff-only."""

    gadget_type = serializers.SlugRelatedField(slug_field="slug", read_only=True)
    owner_email = serializers.EmailField(source="owner.email", read_only=True)
    ws_path = serializers.SerializerMethodField()

    class Meta:
        model = Gadget
        fields = (
            "uid",
            "gadget_type",
            "owner_email",
            "secret_key",
            "wifi_ssid",
            "wifi_password",
            "ws_path",
            "status",
        )
        read_only_fields = fields

    def get_ws_path(self, obj):
        return f"/ws/device/{obj.uid}/"
