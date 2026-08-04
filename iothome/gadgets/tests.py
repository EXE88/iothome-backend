import time
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APITestCase

from core.signatures import (
    SignatureError,
    build_message,
    check_timestamp,
    sign,
    signatures_match,
)
from purchases.models import Product

from .models import Capability, Command, Gadget, GadgetType

User = get_user_model()


def make_catalog():
    lamp = GadgetType.objects.create(
        slug="lamp", name="Lamp", mode=GadgetType.MODE_ACTION
    )
    Capability.objects.create(
        gadget_type=lamp,
        key="power",
        direction=Capability.DIRECTION_COMMAND,
        value_type=Capability.TYPE_BOOL,
    )
    Capability.objects.create(
        gadget_type=lamp,
        key="brightness",
        direction=Capability.DIRECTION_COMMAND,
        value_type=Capability.TYPE_INT,
        min_value=0,
        max_value=100,
    )
    Capability.objects.create(
        gadget_type=lamp,
        key="color",
        direction=Capability.DIRECTION_COMMAND,
        value_type=Capability.TYPE_ENUM,
        choices=["warm", "cool"],
    )
    Capability.objects.create(
        gadget_type=lamp,
        key="temperature",
        direction=Capability.DIRECTION_TELEMETRY,
        value_type=Capability.TYPE_FLOAT,
        min_value=-40,
        max_value=125,
    )
    product = Product.objects.create(
        gadget_type=lamp, name="Lamp", slug="lamp", price=1000, stock=5
    )
    return lamp, product


class CapabilityValidationTests(TestCase):
    def setUp(self):
        self.lamp, self.product = make_catalog()
        self.caps = {
            (c.direction, c.key): c for c in self.lamp.capabilities.all()
        }

    def cap(self, key, direction=Capability.DIRECTION_COMMAND):
        return self.caps[(direction, key)]

    def test_bool_accepts_real_and_stringy_booleans(self):
        power = self.cap("power")
        self.assertIs(power.validate_value(True), True)
        self.assertIs(power.validate_value("false"), False)
        self.assertIs(power.validate_value(1), True)

    def test_bool_rejects_arbitrary_text(self):
        with self.assertRaises(ValidationError):
            self.cap("power").validate_value("on")

    def test_int_range_is_enforced(self):
        brightness = self.cap("brightness")
        self.assertEqual(brightness.validate_value(50), 50)
        with self.assertRaises(ValidationError):
            brightness.validate_value(101)
        with self.assertRaises(ValidationError):
            brightness.validate_value(-1)

    def test_number_rejects_non_numeric_and_booleans(self):
        brightness = self.cap("brightness")
        with self.assertRaises(ValidationError):
            brightness.validate_value("bright")
        with self.assertRaises(ValidationError):
            brightness.validate_value(True)

    def test_float_rejects_infinity(self):
        temperature = self.cap("temperature", Capability.DIRECTION_TELEMETRY)
        with self.assertRaises(ValidationError):
            temperature.validate_value(float("inf"))

    def test_enum_only_accepts_listed_choices(self):
        color = self.cap("color")
        self.assertEqual(color.validate_value("warm"), "warm")
        with self.assertRaises(ValidationError):
            color.validate_value("purple")


class SecretEncryptionTests(TestCase):
    def test_secret_key_is_encrypted_at_rest(self):
        lamp, product = make_catalog()
        user = User.objects.create_user(email="a@gmail.com", password="pass-12345")
        gadget = Gadget.objects.create(
            owner=user, product=product, gadget_type=lamp, wifi_password="hunter2000"
        )
        gadget.refresh_from_db()
        self.assertEqual(len(gadget.secret_key), 64)  # readable through the ORM

        # A raw cursor bypasses from_db_value, so this is what really sits on
        # disk — it must not contain either secret in the clear.
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT secret_key, wifi_password FROM gadgets_gadget WHERE id = %s",
                [gadget.id],
            )
            stored_secret, stored_wifi = cursor.fetchone()
        self.assertNotIn(gadget.secret_key, stored_secret)
        self.assertNotIn("hunter2000", stored_wifi)
        self.assertTrue(stored_secret.startswith("gAAAAA"))  # Fernet token


class SignatureTests(TestCase):
    def test_matching_signature_passes(self):
        secret = "a" * 64
        message = build_message("device-1", 1700000000, "nonce")
        self.assertTrue(signatures_match(secret, message, sign(secret, message)))

    def test_tampered_field_fails(self):
        secret = "a" * 64
        good = sign(secret, build_message("device-1", 1700000000, "nonce"))
        tampered = build_message("device-2", 1700000000, "nonce")
        self.assertFalse(signatures_match(secret, tampered, good))

    def test_wrong_secret_fails(self):
        message = build_message("device-1", 1700000000, "nonce")
        self.assertFalse(signatures_match("b" * 64, message, sign("a" * 64, message)))

    def test_stale_timestamp_is_refused(self):
        with self.assertRaises(SignatureError):
            check_timestamp(int(time.time()) - 3600)

    def test_future_timestamp_is_refused(self):
        with self.assertRaises(SignatureError):
            check_timestamp(int(time.time()) + 3600)

    def test_current_timestamp_passes(self):
        self.assertTrue(check_timestamp(int(time.time())))

    def test_empty_parts_keep_the_layout_fixed(self):
        # "a||b" must not collide with a field that literally contains "|".
        self.assertEqual(build_message("a", None, "b"), "a||b")


class GadgetAPITests(APITestCase):
    def setUp(self):
        self.lamp, self.product = make_catalog()
        self.owner = User.objects.create_user(
            email="owner@gmail.com", password="pass-12345", is_email_verified=True
        )
        self.other = User.objects.create_user(
            email="other@gmail.com", password="pass-12345", is_email_verified=True
        )
        self.gadget = Gadget.objects.create(
            owner=self.owner, product=self.product, gadget_type=self.lamp
        )
        patcher = mock.patch("gadgets.views.presence")
        self.presence = patcher.start()
        self.presence.online_uids.return_value = set()
        self.presence.is_online.return_value = False
        self.addCleanup(patcher.stop)

    def test_list_requires_authentication(self):
        self.assertEqual(self.client.get(reverse("gadgets:list")).status_code, 401)

    def test_owner_sees_only_their_gadgets(self):
        self.client.force_authenticate(self.other)
        response = self.client.get(reverse("gadgets:list"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 0)

        self.client.force_authenticate(self.owner)
        response = self.client.get(reverse("gadgets:list"))
        self.assertEqual(response.data["count"], 1)

    def test_unverified_email_blocks_access(self):
        unverified = User.objects.create_user(
            email="new@gmail.com", password="pass-12345"
        )
        self.client.force_authenticate(unverified)
        self.assertEqual(self.client.get(reverse("gadgets:list")).status_code, 403)

    def test_stranger_cannot_read_another_gadget(self):
        self.client.force_authenticate(self.other)
        url = reverse("gadgets:detail", kwargs={"uid": self.gadget.uid})
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_provisioning_is_staff_only(self):
        url = reverse("gadgets:provisioning", kwargs={"uid": self.gadget.uid})
        self.client.force_authenticate(self.owner)
        self.assertEqual(self.client.get(url).status_code, 403)

        staff = User.objects.create_superuser(
            email="staff@gmail.com", password="pass-12345"
        )
        self.client.force_authenticate(staff)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["secret_key"], self.gadget.secret_key)

    def test_command_history_is_readable(self):
        Command.objects.create(
            gadget=self.gadget, issued_by=self.owner, key="power", value=True
        )
        self.client.force_authenticate(self.owner)
        response = self.client.get(
            reverse("gadgets:commands", kwargs={"uid": self.gadget.uid})
        )
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["key"], "power")

    def test_disable_blocks_future_connections(self):
        self.client.force_authenticate(self.owner)
        layer = mock.Mock(group_send=mock.AsyncMock())
        with mock.patch("gadgets.views.get_channel_layer", return_value=layer):
            response = self.client.post(
                reverse("gadgets:disable", kwargs={"uid": self.gadget.uid})
            )
        self.assertEqual(response.status_code, 200)
        self.gadget.refresh_from_db()
        self.assertFalse(self.gadget.can_connect)


class CommandExpiryTests(TestCase):
    def setUp(self):
        self.lamp, self.product = make_catalog()
        self.user = User.objects.create_user(
            email="cmd@gmail.com", password="pass-12345", is_email_verified=True
        )
        self.gadget = Gadget.objects.create(
            owner=self.user, product=self.product, gadget_type=self.lamp
        )

    def test_unanswered_command_times_out(self):
        from .tasks import expire_command

        command = Command.objects.create(
            gadget=self.gadget,
            issued_by=self.user,
            key="power",
            value=True,
            status=Command.STATUS_SENT,
        )
        with mock.patch("gadgets.tasks._notify"):
            self.assertTrue(expire_command(str(command.request_id)))
        command.refresh_from_db()
        self.assertEqual(command.status, Command.STATUS_TIMEOUT)

    def test_retention_drops_commands_past_ninety_days(self):
        from datetime import timedelta

        from django.utils import timezone

        from .tasks import prune_old_commands

        old = Command.objects.create(
            gadget=self.gadget, issued_by=self.user, key="power", value=True
        )
        recent = Command.objects.create(
            gadget=self.gadget, issued_by=self.user, key="power", value=False
        )
        # created_at is auto_now_add, so age it after the fact.
        Command.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(days=120)
        )

        self.assertEqual(prune_old_commands(), 1)
        self.assertFalse(Command.objects.filter(pk=old.pk).exists())
        self.assertTrue(Command.objects.filter(pk=recent.pk).exists())

    def test_acked_command_is_left_alone(self):
        from .tasks import expire_command

        command = Command.objects.create(
            gadget=self.gadget,
            issued_by=self.user,
            key="power",
            value=True,
            status=Command.STATUS_ACKED,
        )
        with mock.patch("gadgets.tasks._notify"):
            self.assertFalse(expire_command(str(command.request_id)))
        command.refresh_from_db()
        self.assertEqual(command.status, Command.STATUS_ACKED)
