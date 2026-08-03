import re

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APITestCase

from .models import EmailOTP
from .validators import normalize_email

User = get_user_model()


def extract_code(message):
    return re.search(r"\b(\d{6})\b", message.body).group(1)


class EmailNormalizationTests(TestCase):
    def test_gmail_dots_and_tags_collapse(self):
        self.assertEqual(
            normalize_email("A.li.Reza+shop@GMail.com"), "alireza@gmail.com"
        )

    def test_googlemail_maps_to_gmail(self):
        self.assertEqual(normalize_email("someone@googlemail.com"), "someone@gmail.com")

    def test_non_gmail_only_lowercased(self):
        self.assertEqual(normalize_email("A.B@Example.COM"), "a.b@example.com")


class RegistrationFlowTests(APITestCase):
    def test_full_signup_verify_login(self):
        response = self.client.post(
            reverse("accounts:register"),
            {
                "email": "buyer@gmail.com",
                "password": "sup3r-secret-pass",
                "password_confirm": "sup3r-secret-pass",
                "full_name": "Test Buyer",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(mail.outbox), 1)

        user = User.objects.get(email="buyer@gmail.com")
        self.assertFalse(user.is_email_verified)

        # Unverified accounts cannot log in.
        login = self.client.post(
            reverse("accounts:login"),
            {"email": "buyer@gmail.com", "password": "sup3r-secret-pass"},
            format="json",
        )
        self.assertEqual(login.status_code, 400)

        verify = self.client.post(
            reverse("accounts:verify-email"),
            {"email": "buyer@gmail.com", "code": extract_code(mail.outbox[0])},
            format="json",
        )
        self.assertEqual(verify.status_code, 200)

        login = self.client.post(
            reverse("accounts:login"),
            {"email": "buyer@gmail.com", "password": "sup3r-secret-pass"},
            format="json",
        )
        self.assertEqual(login.status_code, 200)
        self.assertIn("access", login.data)

    def test_non_gmail_is_rejected(self):
        response = self.client.post(
            reverse("accounts:register"),
            {
                "email": "buyer@yahoo.com",
                "password": "sup3r-secret-pass",
                "password_confirm": "sup3r-secret-pass",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("email", response.data)

    def test_dotted_alias_cannot_duplicate_an_account(self):
        User.objects.create_user(email="buyer@gmail.com", password="sup3r-secret-pass")
        response = self.client.post(
            reverse("accounts:register"),
            {
                "email": "bu.yer+promo@gmail.com",
                "password": "sup3r-secret-pass",
                "password_confirm": "sup3r-secret-pass",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_password_mismatch_rejected(self):
        response = self.client.post(
            reverse("accounts:register"),
            {
                "email": "buyer@gmail.com",
                "password": "sup3r-secret-pass",
                "password_confirm": "something-else",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)


class OTPTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="otp@gmail.com", password="sup3r-secret-pass"
        )

    def test_wrong_code_fails_and_counts_attempts(self):
        otp, _code = EmailOTP.issue(self.user, EmailOTP.PURPOSE_VERIFY)
        self.assertFalse(otp.verify("000000000"))
        otp.refresh_from_db()
        self.assertEqual(otp.attempts, 1)
        self.assertIsNone(otp.consumed_at)

    def test_code_is_single_use(self):
        otp, code = EmailOTP.issue(self.user, EmailOTP.PURPOSE_VERIFY)
        self.assertTrue(otp.verify(code))
        self.assertFalse(otp.verify(code))

    def test_issuing_invalidates_the_previous_code(self):
        old, old_code = EmailOTP.issue(self.user, EmailOTP.PURPOSE_VERIFY)
        EmailOTP.issue(self.user, EmailOTP.PURPOSE_VERIFY)
        old.refresh_from_db()
        self.assertFalse(old.verify(old_code))

    def test_attempts_are_capped(self):
        otp, code = EmailOTP.issue(self.user, EmailOTP.PURPOSE_VERIFY)
        for _ in range(5):
            otp.verify("111111")
        # Even the right code is refused once the budget is spent.
        self.assertFalse(otp.verify(code))


class PasswordResetTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="reset@gmail.com",
            password="old-password-123",
            is_email_verified=True,
        )

    def test_reset_flow(self):
        self.client.post(
            reverse("accounts:password-reset"),
            {"email": "reset@gmail.com"},
            format="json",
        )
        code = extract_code(mail.outbox[0])
        response = self.client.post(
            reverse("accounts:password-reset-confirm"),
            {
                "email": "reset@gmail.com",
                "code": code,
                "new_password": "brand-new-pass-9",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("brand-new-pass-9"))

    def test_unknown_address_gives_the_same_answer(self):
        response = self.client.post(
            reverse("accounts:password-reset"),
            {"email": "nobody@gmail.com"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)
