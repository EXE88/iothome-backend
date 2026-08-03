from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from .emails import send_otp_email
from .models import EmailOTP
from .validators import normalize_email, validate_gmail_address

User = get_user_model()


class GmailField(serializers.EmailField):
    """EmailField that canonicalises the address and enforces the Gmail rule."""

    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        try:
            validate_gmail_address(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages)
        return normalize_email(value)


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = (
            "id",
            "email",
            "full_name",
            "phone",
            "is_email_verified",
            "date_joined",
        )
        read_only_fields = ("id", "email", "is_email_verified", "date_joined")


class RegisterSerializer(serializers.Serializer):
    email = GmailField()
    password = serializers.CharField(write_only=True, min_length=8, max_length=128)
    password_confirm = serializers.CharField(write_only=True)
    full_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    phone = serializers.CharField(max_length=20, required=False, allow_blank=True)

    def validate_email(self, value):
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError("This email is already registered.")
        return value

    def validate(self, attrs):
        if attrs["password"] != attrs["password_confirm"]:
            raise serializers.ValidationError(
                {"password_confirm": "Passwords do not match."}
            )
        try:
            validate_password(attrs["password"])
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"password": list(exc.messages)})
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        validated_data.pop("password_confirm")
        password = validated_data.pop("password")
        user = User.objects.create_user(password=password, **validated_data)
        otp, code = EmailOTP.issue(user, EmailOTP.PURPOSE_VERIFY)
        # Sent inside the transaction on purpose: with the console backend a
        # failure here should roll the half-made account back.
        send_otp_email(user, EmailOTP.PURPOSE_VERIFY, code)
        return user


class EmailOnlySerializer(serializers.Serializer):
    email = GmailField()


class OTPSerializer(serializers.Serializer):
    """Shared code-checking logic for email verification and password reset."""

    email = GmailField()
    code = serializers.CharField(min_length=4, max_length=8)
    purpose = None

    def validate(self, attrs):
        user = User.objects.filter(email=attrs["email"]).first()
        otp = (
            EmailOTP.objects.filter(user=user, purpose=self.purpose)
            .order_by("-created_at")
            .first()
            if user
            else None
        )
        # One generic error for every failure mode, so the endpoint cannot be
        # used to tell registered addresses from unregistered ones.
        if otp is None or not otp.verify(attrs["code"]):
            raise serializers.ValidationError(
                {"code": "The code is invalid or has expired."}
            )
        attrs["user"] = user
        return attrs


class VerifyEmailSerializer(OTPSerializer):
    purpose = EmailOTP.PURPOSE_VERIFY

    def save(self, **kwargs):
        user = self.validated_data["user"]
        if not user.is_email_verified:
            user.is_email_verified = True
            user.save(update_fields=["is_email_verified"])
        return user


class PasswordResetConfirmSerializer(OTPSerializer):
    purpose = EmailOTP.PURPOSE_RESET
    new_password = serializers.CharField(write_only=True, min_length=8, max_length=128)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        try:
            validate_password(attrs["new_password"], user=attrs["user"])
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"new_password": list(exc.messages)})
        return attrs

    def save(self, **kwargs):
        user = self.validated_data["user"]
        user.set_password(self.validated_data["new_password"])
        # Completing a reset also proves the address works.
        user.is_email_verified = True
        user.save(update_fields=["password", "is_email_verified"])
        return user


class ChangePasswordSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True, min_length=8, max_length=128)

    def validate_current_password(self, value):
        if not self.context["request"].user.check_password(value):
            raise serializers.ValidationError("Current password is incorrect.")
        return value

    def validate_new_password(self, value):
        try:
            validate_password(value, user=self.context["request"].user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages))
        return value

    def save(self, **kwargs):
        user = self.context["request"].user
        user.set_password(self.validated_data["new_password"])
        user.save(update_fields=["password"])
        return user


class LoginSerializer(TokenObtainPairSerializer):
    """Adds the unverified-email gate and returns the profile with the tokens."""

    def validate(self, attrs):
        attrs[self.username_field] = normalize_email(attrs.get(self.username_field, ""))
        data = super().validate(attrs)
        if not self.user.is_email_verified:
            raise serializers.ValidationError(
                {
                    "email": "This address is not verified yet.",
                    "code": "email_not_verified",
                }
            )
        data["user"] = UserSerializer(self.user).data
        return data

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["email"] = user.email
        return token
