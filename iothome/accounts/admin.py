from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import EmailOTP, User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    ordering = ("-date_joined",)
    list_display = ("email", "full_name", "is_email_verified", "is_staff", "date_joined")
    list_filter = ("is_email_verified", "is_staff", "is_superuser", "is_active")
    search_fields = ("email", "full_name", "phone")
    readonly_fields = ("date_joined", "last_login", "updated_at")

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Profile", {"fields": ("full_name", "phone")}),
        (
            "Permissions",
            {
                "fields": (
                    "is_active",
                    "is_email_verified",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                )
            },
        ),
        ("Dates", {"fields": ("last_login", "date_joined", "updated_at")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "full_name", "password1", "password2"),
            },
        ),
    )


@admin.register(EmailOTP)
class EmailOTPAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "purpose",
        "created_at",
        "expires_at",
        "consumed_at",
        "attempts",
    )
    list_filter = ("purpose",)
    search_fields = ("user__email",)
    # code_hash stays read-only — nobody should be minting codes by hand.
    readonly_fields = ("code_hash", "created_at")
