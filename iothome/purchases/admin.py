from django.contrib import admin

from .models import Order, OrderItem, Payment, Product


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


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    raw_id_fields = ("product",)
    # The price is what was charged, not what the product costs today; editing
    # it here would rewrite history.
    readonly_fields = ("unit_price",)


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("uid", "user", "summary", "total_amount", "status", "created_at")
    list_filter = ("status",)
    search_fields = (
        "uid",
        "user__email",
        "receiver_phone",
        "tracking_code",
        "items__product__name",
    )
    raw_id_fields = ("user",)
    readonly_fields = ("uid", "total_amount", "created_at", "updated_at")
    inlines = [OrderItemInline, PaymentInline]

    # Wi-Fi credentials are readable here because assembly needs them, but
    # they are never exposed through the customer-facing API.
    fieldsets = (
        (None, {"fields": ("uid", "user", "status")}),
        ("Amounts", {"fields": ("total_amount",)}),
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
