from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import generics, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView

from .emails import send_otp_email
from .models import EmailOTP
from .serializers import (
    ChangePasswordSerializer,
    EmailOnlySerializer,
    LoginSerializer,
    PasswordResetConfirmSerializer,
    RegisterSerializer,
    UserSerializer,
    VerifyEmailSerializer,
)
from .validators import normalize_email, validate_gmail_address

User = get_user_model()

# Replying identically whether or not the address exists keeps these endpoints
# from doubling as an account-enumeration oracle.
GENERIC_OTP_RESPONSE = {
    "detail": "If that address belongs to an account, a code is on its way."
}


class RegisterView(generics.CreateAPIView):
    serializer_class = RegisterSerializer
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "otp"

    def create(self, request, *args, **kwargs):
        # Someone coming back to an address they signed up with and never
        # verified. Refusing them with "already registered" is a dead end:
        # they cannot register, and they cannot log in either, because login
        # is gated on the verification they never finished. So the address
        # stays theirs, a fresh code goes out, and they are pointed at the
        # screen that actually unblocks them.
        #
        # No password is set here on purpose. Letting an unauthenticated
        # request overwrite the password of an existing account — even an
        # unverified one — would be a takeover of any signup someone else had
        # started. Whoever owns the mailbox can use "forgot password", which
        # also marks the address verified.
        pending = self.pending_account(request.data.get("email"))
        if pending is not None:
            _, code = EmailOTP.issue(pending, EmailOTP.PURPOSE_VERIFY)
            send_otp_email(pending, EmailOTP.PURPOSE_VERIFY, code)
            return Response(
                {
                    "user": UserSerializer(pending).data,
                    "detail": "This address is already waiting to be verified. "
                    "A new code is on its way.",
                    "code": "verification_pending",
                },
                status=status.HTTP_200_OK,
            )

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(
            {
                "user": UserSerializer(user).data,
                "detail": "Account created. Check your email for the code.",
                "code": "verification_sent",
            },
            status=status.HTTP_201_CREATED,
        )

    @staticmethod
    def pending_account(raw_email):
        """An existing account for this address that never got verified."""
        if not isinstance(raw_email, str) or not raw_email:
            return None
        try:
            validate_gmail_address(raw_email)
        except DjangoValidationError:
            # Not an address we would accept anyway; let the serializer say so.
            return None
        return User.objects.filter(
            email=normalize_email(raw_email), is_email_verified=False, is_active=True
        ).first()


class VerifyEmailView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "otp"

    def post(self, request):
        serializer = VerifyEmailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(
            {"detail": "Email verified. You can log in now.", "user": UserSerializer(user).data}
        )


class ResendVerificationView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "otp"

    def post(self, request):
        serializer = EmailOnlySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = User.objects.filter(
            email=serializer.validated_data["email"], is_email_verified=False
        ).first()
        if user:
            _, code = EmailOTP.issue(user, EmailOTP.PURPOSE_VERIFY)
            send_otp_email(user, EmailOTP.PURPOSE_VERIFY, code)
        return Response(GENERIC_OTP_RESPONSE)


class PasswordResetRequestView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "otp"

    def post(self, request):
        serializer = EmailOnlySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = User.objects.filter(
            email=serializer.validated_data["email"], is_active=True
        ).first()
        if user:
            _, code = EmailOTP.issue(user, EmailOTP.PURPOSE_RESET)
            send_otp_email(user, EmailOTP.PURPOSE_RESET, code)
        return Response(GENERIC_OTP_RESPONSE)


class PasswordResetConfirmView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "otp"

    def post(self, request):
        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({"detail": "Password updated. You can log in now."})


class LoginView(TokenObtainPairView):
    serializer_class = LoginSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"


class LogoutView(APIView):
    """Retire a refresh token so the session ends everywhere, not just here.

    Dropping the cookie only ends the session in the browser that dropped it.
    The token itself stays valid for its whole lifetime, so any other copy —
    another tab, the same site opened under a different hostname, a saved
    request — keeps working, and the user is told they logged out while they
    demonstrably did not.

    `AllowAny`: the access token has usually expired or been thrown away by
    the time someone logs out, and the refresh token in the body is proof
    enough of what is being retired. A token that is already blacklisted,
    expired or malformed answers 200 as well — logging out twice is not an
    error, and saying "that token is invalid" would turn this into an oracle.
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        token = request.data.get("refresh")
        if token:
            try:
                RefreshToken(token).blacklist()
            except TokenError:
                pass
        return Response({"detail": "Signed out."})


class MeView(generics.RetrieveUpdateAPIView):
    serializer_class = UserSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        return self.request.user


class ChangePasswordView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = ChangePasswordSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({"detail": "Password changed."})
