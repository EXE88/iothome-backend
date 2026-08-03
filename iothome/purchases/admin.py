from django.contrib import admin

from .models import Order, Payment, Product


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "gadget_type", "price", "stock", "is_active")
    list_filter = ("is_active", "gadget_type")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}


class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0
    readonly_fields = ("authority", "ref_id", "card_pan", "status", "created_at")
    can_delete = False


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("uid", "user", "product", "quantity", "total_amount", "status", "created_at")
    list_filter = ("status", "product")
    search_fields = ("uid", "user__email", "receiver_phone", "tracking_code")
    raw_id_fields = ("user", "product")
    readonly_fields = ("uid", "unit_price", "total_amount", "created_at", "updated_at")
    inlines = [PaymentInline]

    # Wi-Fi credentials are readable here because assembly needs them, but
    # they are never exposed through the customer-facing API.
    fieldsets = (
        (None, {"fields": ("uid", "user", "product", "quantity", "status")}),
        ("Amounts", {"fields": ("unit_price", "total_amount")}),
        ("Provisioning", {"fields": ("wifi_ssid", "wifi_password")}),
        (
            "Shipping",
            {
                "fields": (
                    "receiver_name",
                    "receiver_phone",
                    "shipping_address",
                    "postal_code",
                    "tracking_code",
                )
            },
        ),
        ("Dates", {"fields": ("created_at", "updated_at")}),
    )


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("authority", "order", "amount", "status", "ref_id", "created_at")
    list_filter = ("status", "gateway")
    search_fields = ("authority", "ref_id", "order__uid")
    raw_id_fields = ("order",)
    readonly_fields = tuple(
        field.name for field in Payment._meta.fields if field.name != "id"
    )
