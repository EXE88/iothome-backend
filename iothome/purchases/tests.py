from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APITestCase

from gadgets.models import Gadget, GadgetType

from .gateways import zarinpal
from .models import Order, Payment, Product
from .services import CheckoutError, create_order, finalize_payment, start_payment

User = get_user_model()

VALID_ORDER = {
    "quantity": 1,
    "wifi_ssid": "HomeNet",
    "wifi_password": "wifi-pass-1234",
    "receiver_name": "Test Buyer",
    "receiver_phone": "09121234567",
    "shipping_address": "Somewhere in Tehran",
}


def make_product(stock=5, price=1000):
    gadget_type = GadgetType.objects.create(
        slug="lamp", name="Lamp", mode=GadgetType.MODE_ACTION
    )
    return Product.objects.create(
        gadget_type=gadget_type, name="Lamp", slug="lamp", price=price, stock=stock
    )


class CheckoutTests(TestCase):
    def setUp(self):
        self.product = make_product()
        self.user = User.objects.create_user(
            email="buyer@gmail.com", password="pass-12345", is_email_verified=True
        )

    def test_stock_is_reserved_at_checkout(self):
        create_order(
            user=self.user, product_id=self.product.id, **VALID_ORDER
        )
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 4)

    def test_price_is_frozen_on_the_order(self):
        order = create_order(user=self.user, product_id=self.product.id, **VALID_ORDER)
        self.product.price = 9999
        self.product.save()
        order.refresh_from_db()
        self.assertEqual(int(order.unit_price), 1000)

    def test_out_of_stock_is_refused(self):
        self.product.stock = 0
        self.product.save()
        with self.assertRaises(CheckoutError):
            create_order(user=self.user, product_id=self.product.id, **VALID_ORDER)

    def test_wifi_credentials_never_come_back_out(self):
        order = create_order(user=self.user, product_id=self.product.id, **VALID_ORDER)
        from .serializers import OrderSerializer

        data = OrderSerializer(order).data
        self.assertNotIn("wifi_ssid", data)
        self.assertNotIn("wifi_password", data)


class PaymentFlowTests(TestCase):
    def setUp(self):
        self.product = make_product()
        self.user = User.objects.create_user(
            email="buyer@gmail.com", password="pass-12345", is_email_verified=True
        )
        self.order = create_order(
            user=self.user, product_id=self.product.id, **VALID_ORDER
        )

    def start(self):
        with mock.patch.object(
            zarinpal, "request_payment", return_value=("A0001", {"code": 100})
        ):
            return start_payment(self.order, "http://cb/")

    def test_successful_payment_provisions_a_gadget(self):
        payment, url = self.start()
        self.assertIn("A0001", url)

        with mock.patch.object(
            zarinpal,
            "verify_payment",
            return_value={"code": 100, "ref_id": 777, "card_pan": "6037****1234"},
        ):
            payment, gadgets = finalize_payment(authority="A0001", gateway_status="OK")

        self.assertEqual(payment.status, Payment.STATUS_SUCCESS)
        self.assertEqual(payment.ref_id, "777")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.STATUS_PAID)
        self.assertEqual(len(gadgets), 1)

        gadget = gadgets[0]
        self.assertEqual(gadget.owner, self.user)
        self.assertEqual(gadget.wifi_ssid, "HomeNet")
        self.assertEqual(gadget.status, Gadget.STATUS_AWAITING_ASSEMBLY)
        self.assertEqual(len(gadget.secret_key), 64)

    def test_each_gadget_gets_its_own_secret(self):
        self.order.quantity = 3
        self.order.save()
        self.start()
        with mock.patch.object(
            zarinpal, "verify_payment", return_value={"code": 100, "ref_id": 1}
        ):
            _, gadgets = finalize_payment(authority="A0001", gateway_status="OK")
        self.assertEqual(len({g.secret_key for g in gadgets}), 3)

    def test_replayed_callback_does_not_double_provision(self):
        self.start()
        with mock.patch.object(
            zarinpal, "verify_payment", return_value={"code": 100, "ref_id": 1}
        ) as verify:
            finalize_payment(authority="A0001", gateway_status="OK")
            _, gadgets = finalize_payment(authority="A0001", gateway_status="OK")
        # The second callback never reaches the gateway at all.
        self.assertEqual(verify.call_count, 1)
        self.assertEqual(len(gadgets), 1)
        self.assertEqual(Gadget.objects.count(), 1)

    def test_canceled_payment_returns_the_stock(self):
        self.start()
        payment, gadgets = finalize_payment(authority="A0001", gateway_status="NOK")
        self.assertEqual(payment.status, Payment.STATUS_CANCELED)
        self.assertEqual(gadgets, [])
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 5)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.STATUS_CANCELED)
        self.assertEqual(Gadget.objects.count(), 0)

    def test_failed_verification_returns_the_stock(self):
        self.start()
        with mock.patch.object(
            zarinpal,
            "verify_payment",
            side_effect=zarinpal.ZarinpalError("nope", code=-51),
        ):
            payment, _ = finalize_payment(authority="A0001", gateway_status="OK")
        self.assertEqual(payment.status, Payment.STATUS_FAILED)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 5)

    def test_unknown_authority_is_rejected(self):
        with self.assertRaises(CheckoutError):
            finalize_payment(authority="does-not-exist", gateway_status="OK")

    def test_gateway_failure_marks_the_payment_failed(self):
        with mock.patch.object(
            zarinpal,
            "request_payment",
            side_effect=zarinpal.ZarinpalError("gateway down"),
        ):
            with self.assertRaises(CheckoutError):
                start_payment(self.order, "http://cb/")
        self.assertEqual(
            Payment.objects.get(order=self.order).status, Payment.STATUS_FAILED
        )


class CheckoutAPITests(APITestCase):
    def setUp(self):
        self.product = make_product()
        self.user = User.objects.create_user(
            email="buyer@gmail.com", password="pass-12345", is_email_verified=True
        )

    def test_checkout_returns_a_payment_url(self):
        self.client.force_authenticate(self.user)
        with mock.patch.object(
            zarinpal, "request_payment", return_value=("A1", {"code": 100})
        ):
            response = self.client.post(
                reverse("purchases:checkout"),
                {"product": self.product.id, **VALID_ORDER},
                format="json",
            )
        self.assertEqual(response.status_code, 201)
        self.assertIn("payment_url", response.data)

    def test_checkout_requires_a_verified_email(self):
        unverified = User.objects.create_user(
            email="new@gmail.com", password="pass-12345"
        )
        self.client.force_authenticate(unverified)
        response = self.client.post(
            reverse("purchases:checkout"),
            {"product": self.product.id, **VALID_ORDER},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_bad_phone_number_is_rejected(self):
        self.client.force_authenticate(self.user)
        response = self.client.post(
            reverse("purchases:checkout"),
            {**VALID_ORDER, "product": self.product.id, "receiver_phone": "12345"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_oversized_ssid_is_rejected(self):
        self.client.force_authenticate(self.user)
        response = self.client.post(
            reverse("purchases:checkout"),
            {**VALID_ORDER, "product": self.product.id, "wifi_ssid": "x" * 40},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_short_wifi_password_is_rejected(self):
        self.client.force_authenticate(self.user)
        response = self.client.post(
            reverse("purchases:checkout"),
            {**VALID_ORDER, "product": self.product.id, "wifi_password": "abc"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_products_are_public(self):
        self.assertEqual(
            self.client.get(reverse("purchases:product-list")).status_code, 200
        )

    def test_orders_are_scoped_to_the_caller(self):
        stranger = User.objects.create_user(
            email="stranger@gmail.com", password="pass-12345", is_email_verified=True
        )
        create_order(user=self.user, product_id=self.product.id, **VALID_ORDER)
        self.client.force_authenticate(stranger)
        response = self.client.get(reverse("purchases:order-list"))
        self.assertEqual(response.data["count"], 0)


class ZarinpalClientTests(TestCase):
    def test_amount_is_converted_to_rial(self):
        with mock.patch.object(
            zarinpal, "_post", return_value={"code": 100, "authority": "A1"}
        ) as post:
            zarinpal.request_payment(
                amount_toman=1000, description="d", callback_url="http://cb/"
            )
        self.assertEqual(post.call_args[0][1]["amount"], 10000)

    def test_already_verified_counts_as_success(self):
        with mock.patch.object(zarinpal, "_post", return_value={"code": 101}):
            data = zarinpal.verify_payment(amount_toman=1000, authority="A1")
        self.assertEqual(data["code"], 101)

    def test_other_codes_raise(self):
        with mock.patch.object(zarinpal, "_post", return_value={"code": -51}):
            with self.assertRaises(zarinpal.ZarinpalError):
                zarinpal.verify_payment(amount_toman=1000, authority="A1")
