from django.conf import settings
from django.core.mail import send_mail

SUBJECTS = {
    "verify_email": "Your IoT Home verification code",
    "reset_password": "Your IoT Home password reset code",
}

BODIES = {
    "verify_email": (
        "Welcome to IoT Home!\n\n"
        "Your verification code is: {code}\n\n"
        "It expires in {minutes} minutes. If you did not sign up, ignore this."
    ),
    "reset_password": (
        "Someone asked to reset the password for this account.\n\n"
        "Your reset code is: {code}\n\n"
        "It expires in {minutes} minutes. If this was not you, ignore this "
        "email — your password stays unchanged."
    ),
}


def send_otp_email(user, purpose, code):
    send_mail(
        subject=SUBJECTS[purpose],
        message=BODIES[purpose].format(
            code=code, minutes=settings.OTP_TTL_SECONDS // 60
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
        fail_silently=False,
    )
