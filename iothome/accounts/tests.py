import re
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from .models import EmailOTP
from .tasks import purge_expired_otps, purge_unverified_accounts
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

    def register_alias(self):
        return self.client.post(
            reverse("accounts:register"),
            {
                "email": "bu.yer+promo@gmail.com",
                "password": "sup3r-secret-pass",
                "password_confirm": "sup3r-secret-pass",
            },
            format="json",
        )

    def test_dotted_alias_cannot_duplicate_an_account(self):
        """The invariant is one account per mailbox, whatever it is spelled as.

        Gmail ignores dots and everything after a `+`, so `bu.yer+promo@` and
        `buyer@` are one inbox. Asserting on the count rather than the status
        code, because the status depends on whether the existing account was
        ever verified — see the two tests below — while "no second account"
        never does.
        """
        User.objects.create_user(email="buyer@gmail.com", password="sup3r-secret-pass")
        self.register_alias()
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(User.objects.get().email, "buyer@gmail.com")

    def test_an_alias_of_a_verified_account_is_refused(self):
        User.objects.create_user(
            email="buyer@gmail.com",
            password="sup3r-secret-pass",
            is_email_verified=True,
        )
        response = self.register_alias()
        self.assertEqual(response.status_code, 400)
        self.assertIn("email", response.data)

    def test_an_alias_of_an_unverified_account_resends_its_code(self):
        """Same mailbox, still waiting to be verified — help, do not refuse."""
        User.objects.create_user(email="buyer@gmail.com", password="sup3r-secret-pass")
        mail.outbox.clear()
        response = self.register_alias()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["code"], "verification_pending")
        self.assertEqual(mail.outbox[0].to, ["buyer@gmail.com"])

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


class LogoutTests(APITestCase):
    """Logging out has to end the session everywhere, not just in one browser.

    The site answers on more than one hostname and each keeps its own cookie
    jar, so "drop the cookie" only ever meant "drop it here" — you could log
    out, sign in as someone else, and find the first account still live under
    the other hostname.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            email="out@gmail.com", password="pass-12345", is_email_verified=True
        )
        response = self.client.post(
            reverse("accounts:login"),
            {"email": "out@gmail.com", "password": "pass-12345"},
            format="json",
        )
        self.refresh = response.data["refresh"]

    def test_the_token_works_before_logging_out(self):
        response = self.client.post(
            reverse("accounts:token-refresh"), {"refresh": self.refresh}, format="json"
        )
        self.assertEqual(response.status_code, 200)

    def test_logging_out_kills_the_token(self):
        self.assertEqual(
            self.client.post(
                reverse("accounts:logout"), {"refresh": self.refresh}, format="json"
            ).status_code,
            200,
        )
        response = self.client.post(
            reverse("accounts:token-refresh"), {"refresh": self.refresh}, format="json"
        )
        self.assertEqual(response.status_code, 401)

    def test_logging_out_twice_is_not_an_error(self):
        for _ in range(2):
            response = self.client.post(
                reverse("accounts:logout"), {"refresh": self.refresh}, format="json"
            )
            self.assertEqual(response.status_code, 200)

    def test_a_nonsense_token_is_accepted_quietly(self):
        response = self.client.post(
            reverse("accounts:logout"), {"refresh": "not-a-token"}, format="json"
        )
        self.assertEqual(response.status_code, 200)

    def test_the_refresh_token_is_not_rotated(self):
        """The seven-day window is absolute, so the same token keeps working."""
        response = self.client.post(
            reverse("accounts:token-refresh"), {"refresh": self.refresh}, format="json"
        )
        self.assertNotIn("refresh", response.data)
        again = self.client.post(
            reverse("accounts:token-refresh"), {"refresh": self.refresh}, format="json"
        )
        self.assertEqual(again.status_code, 200)


class AbandonedSignupTests(APITestCase):
    """Someone who signs up and never enters the emailed code.

    The row has to exist before the address is proven — the code has to be
    sent to somebody — so this is a state the system will always have. What
    matters is that it does not become a trap.
    """

    def signup(self, email="ghost@gmail.com"):
        return self.client.post(
            reverse("accounts:register"),
            {"email": email, "password": "pass-12345", "password_confirm": "pass-12345"},
            format="json",
        )

    def test_an_unverified_account_can_do_nothing(self):
        self.signup()
        user = User.objects.get(email="ghost@gmail.com")
        self.assertFalse(user.is_email_verified)

        response = self.client.post(
            reverse("accounts:login"),
            {"email": "ghost@gmail.com", "password": "pass-12345"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(str(response.data["code"][0]), "email_not_verified")

    def test_signing_up_again_resends_instead_of_refusing(self):
        """Refusing would strand them: cannot register, cannot log in."""
        self.signup()
        mail.outbox.clear()

        response = self.signup()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["code"], "verification_pending")
        self.assertEqual(len(mail.outbox), 1)
        # Still one account, not a second one for the same address.
        self.assertEqual(User.objects.filter(email="ghost@gmail.com").count(), 1)

    def test_the_second_attempt_does_not_change_the_password(self):
        """Otherwise anyone could take over a signup somebody else started."""
        self.signup()
        self.client.post(
            reverse("accounts:register"),
            {
                "email": "ghost@gmail.com",
                "password": "attacker-pass-9",
                "password_confirm": "attacker-pass-9",
            },
            format="json",
        )
        user = User.objects.get(email="ghost@gmail.com")
        self.assertTrue(user.check_password("pass-12345"))
        self.assertFalse(user.check_password("attacker-pass-9"))

    def test_a_verified_address_is_still_refused(self):
        self.signup()
        User.objects.filter(email="ghost@gmail.com").update(is_email_verified=True)
        response = self.signup()
        self.assertEqual(response.status_code, 400)
        self.assertIn("email", response.data)

    def test_the_resent_code_actually_works(self):
        self.signup()
        mail.outbox.clear()
        self.signup()
        code = re.search(r"\b(\d{6})\b", mail.outbox[0].body).group(1)

        response = self.client.post(
            reverse("accounts:verify-email"),
            {"email": "ghost@gmail.com", "code": code},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(User.objects.get(email="ghost@gmail.com").is_email_verified)


class AccountPurgeTests(TestCase):
    """Unverified rows are released rather than kept for ever."""

    def make(self, email, *, verified, age_days):
        user = User.objects.create_user(
            email=email, password="pass-12345", is_email_verified=verified
        )
        User.objects.filter(pk=user.pk).update(
            date_joined=timezone.now() - timedelta(days=age_days)
        )
        return user

    def test_old_unverified_accounts_are_deleted(self):
        self.make("gone@gmail.com", verified=False, age_days=30)
        purge_unverified_accounts(days=7)
        self.assertFalse(User.objects.filter(email="gone@gmail.com").exists())

    def test_recent_signups_are_left_alone(self):
        """Somebody may still be looking for the email."""
        self.make("fresh@gmail.com", verified=False, age_days=1)
        purge_unverified_accounts(days=7)
        self.assertTrue(User.objects.filter(email="fresh@gmail.com").exists())

    def test_verified_accounts_are_never_touched(self):
        self.make("real@gmail.com", verified=True, age_days=900)
        purge_unverified_accounts(days=7)
        self.assertTrue(User.objects.filter(email="real@gmail.com").exists())

    def test_staff_are_never_touched(self):
        user = self.make("admin@gmail.com", verified=False, age_days=900)
        User.objects.filter(pk=user.pk).update(is_staff=True)
        purge_unverified_accounts(days=7)
        self.assertTrue(User.objects.filter(email="admin@gmail.com").exists())

    def test_purging_frees_the_address_for_a_fresh_signup(self):
        """The whole point: the person who walked away can come back."""
        self.make("second@gmail.com", verified=False, age_days=30)
        purge_unverified_accounts(days=7)
        response = self.client.post(
            reverse("accounts:register"),
            {
                "email": "second@gmail.com",
                "password": "pass-12345",
                "password_confirm": "pass-12345",
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)

    def test_expired_codes_are_dropped(self):
        user = self.make("codes@gmail.com", verified=False, age_days=0)
        otp, _ = EmailOTP.issue(user, EmailOTP.PURPOSE_VERIFY)
        EmailOTP.objects.filter(pk=otp.pk).update(
            expires_at=timezone.now() - timedelta(days=5)
        )
        purge_expired_otps(days=1)
        self.assertEqual(EmailOTP.objects.count(), 0)

    def test_live_codes_are_kept(self):
        user = self.make("live@gmail.com", verified=False, age_days=0)
        EmailOTP.issue(user, EmailOTP.PURPOSE_VERIFY)
        purge_expired_otps(days=1)
        self.assertEqual(EmailOTP.objects.count(), 1)
