from django.contrib import admin

from . import presence
from .models import Capability, Command, Gadget, GadgetType, TelemetryReading


class CapabilityInline(admin.TabularInline):
    model = Capability
    extra = 1


@admin.register(GadgetType)
class GadgetTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "mode", "heartbeat_interval_seconds")
    list_filter = ("mode",)
    prepopulated_fields = {"slug": ("name",)}
    inlines = [CapabilityInline]


@admin.register(Gadget)
class GadgetAdmin(admin.ModelAdmin):
    list_display = ("uid", "owner", "product", "status", "online", "last_seen_at")
    list_filter = ("status", "gadget_type")
    search_fields = ("uid", "owner__email", "name")
    raw_id_fields = ("owner", "product", "order")
    readonly_fields = ("uid", "created_at", "updated_at", "last_seen_at", "last_ip")

    @admin.display(boolean=True, description="Online")
    def online(self, obj):
        return presence.is_online(obj.uid)


@admin.register(TelemetryReading)
class TelemetryReadingAdmin(admin.ModelAdmin):
    list_display = ("gadget", "key", "value", "recorded_at")
    list_filter = ("key",)
    raw_id_fields = ("gadget",)
    date_hierarchy = "recorded_at"


@admin.register(Command)
class CommandAdmin(admin.ModelAdmin):
    list_display = ("request_id", "gadget", "key", "value", "status", "created_at")
    list_filter = ("status", "key")
    raw_id_fields = ("gadget", "issued_by")
    readonly_fields = ("request_id", "created_at", "responded_at")
