import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models
from django.utils import timezone

from .managers import UserManager
from .validators import normalize_email, validate_gmail_address


class User(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(
        unique=True,
        max_length=254,
        validators=[validate_gmail_address],
    )
    full_name = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=20, blank=True)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    is_email_verified = models.BooleanField(default=False)

    date_joined = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        ordering = ("-date_joined",)

    def __str__(self):
        return self.email

    def clean(self):
        super().clean()
        self.email = normalize_email(self.email)

    def get_full_name(self):
        return self.full_name or self.email

    def get_short_name(self):
        return self.full_name.split(" ")[0] if self.full_name else self.email


class EmailOTP(models.Model):
    """Short-lived numeric code emailed to the user.

    Only the SHA-256 of the code is stored, so a database leak does not hand
    out working codes.
    """

    PURPOSE_VERIFY = "verify_email"
    PURPOSE_RESET = "reset_password"
    PURPOSE_CHOICES = (
        (PURPOSE_VERIFY, "Verify email"),
        (PURPOSE_RESET, "Reset password"),
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="otps"
    )
    purpose = models.CharField(max_length=32, choices=PURPOSE_CHOICES)
    code_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)

    class Meta:
        indexes = [models.Index(fields=["user", "purpose", "-created_at"])]

    def __str__(self):
        return f"{self.user.email} / {self.purpose}"

    @staticmethod
    def hash_code(code):
        return hashlib.sha256(code.encode()).hexdigest()

    @classmethod
    def issue(cls, user, purpose):
        """Invalidate previous codes and create a fresh one.

        Returns ``(instance, raw_code)`` — the raw code is never persisted.
        """
        cls.objects.filter(
            user=user, purpose=purpose, consumed_at__isnull=True
        ).update(consumed_at=timezone.now())
        raw = f"{secrets.randbelow(10 ** settings.OTP_CODE_LENGTH):0{settings.OTP_CODE_LENGTH}d}"
        otp = cls.objects.create(
            user=user,
            purpose=purpose,
            code_hash=cls.hash_code(raw),
            expires_at=timezone.now() + timedelta(seconds=settings.OTP_TTL_SECONDS),
        )
        return otp, raw

    @property
    def is_usable(self):
        return (
            self.consumed_at is None
            and self.attempts < settings.OTP_MAX_ATTEMPTS
            and self.expires_at > timezone.now()
        )

    def verify(self, raw_code):
        if not self.is_usable:
            return False
        self.attempts += 1
        matched = secrets.compare_digest(self.code_hash, self.hash_code(raw_code))
        if matched:
            self.consumed_at = timezone.now()
        self.save(update_fields=["attempts", "consumed_at"])
        return matched
