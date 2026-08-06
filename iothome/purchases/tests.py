from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from gadgets.models import Gadget, GadgetType

from .gateways import zarinpal
from .models import Order, Payment, Product
from .services import CheckoutError, create_order, finalize_payment, start_payment
from .tasks import expire_stale_orders, reconcile_unverified_payments

User = get_user_model()

VALID_ORDER = {
    "wifi_ssid": "HomeNet",
    "wifi_password": "wifi-pass-1234",
    "receiver_name": "Test Buyer",
    "receiver_phone": "09121234567",
    "shipping_address": "Somewhere in Tehran",
}


def make_product(stock=5, price=1000, slug="lamp"):
    gadget_type = GadgetType.objects.create(
        slug=slug, name=slug.title(), mode=GadgetType.MODE_ACTION
    )
    return Product.objects.create(
        gadget_type=gadget_type, name=slug.title(), slug=slug, price=price, stock=stock
    )


def basket(*lines):
    """`basket((product, 2), ...)` -> the `items` payload checkout expects."""
    return [{"product": p.id, "quantity": q} for p, q in lines]


class CheckoutTests(TestCase):
    def setUp(self):
        self.product = make_product()
        self.user = User.objects.create_user(
            email="buyer@gmail.com", password="pass-12345", is_email_verified=True
        )

    def test_stock_is_reserved_at_checkout(self):
        create_order(
            user=self.user, items=basket((self.product, 1)), **VALID_ORDER
        )
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 4)

    def test_price_is_frozen_on_the_line(self):
        order = create_order(user=self.user, items=basket((self.product, 1)), **VALID_ORDER)
        self.product.price = 9999
        self.product.save()
        order.refresh_from_db()
        self.assertEqual(int(order.items.get().unit_price), 1000)
        self.assertEqual(int(order.total_amount), 1000)

    def test_out_of_stock_is_refused(self):
        self.product.stock = 0
        self.product.save()
        with self.assertRaises(CheckoutError):
            create_order(user=self.user, items=basket((self.product, 1)), **VALID_ORDER)

    def test_a_basket_of_several_products_is_one_order(self):
        camera = make_product(stock=2, price=2400, slug="camera")
        order = create_order(
            user=self.user,
            items=basket((self.product, 2), (camera, 1)),
            **VALID_ORDER,
        )
        self.assertEqual(order.items.count(), 2)
        self.assertEqual(int(order.total_amount), 1000 * 2 + 2400)
        self.assertEqual(order.unit_count, 3)
        self.product.refresh_from_db()
        camera.refresh_from_db()
        self.assertEqual(self.product.stock, 3)
        self.assertEqual(camera.stock, 1)

    def test_one_short_line_reserves_nothing_at_all(self):
        camera = make_product(stock=0, price=2400, slug="camera")
        with self.assertRaises(CheckoutError):
            create_order(
                user=self.user,
                items=basket((self.product, 1), (camera, 1)),
                **VALID_ORDER,
            )
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 5)
        self.assertEqual(Order.objects.count(), 0)

    def test_the_same_product_twice_becomes_one_line(self):
        order = create_order(
            user=self.user,
            items=basket((self.product, 1), (self.product, 2)),
            **VALID_ORDER,
        )
        self.assertEqual(order.items.count(), 1)
        self.assertEqual(order.items.get().quantity, 3)

    def test_an_empty_basket_is_refused(self):
        with self.assertRaises(CheckoutError):
            create_order(user=self.user, items=[], **VALID_ORDER)

    def test_wifi_credentials_never_come_back_out(self):
        order = create_order(user=self.user, items=basket((self.product, 1)), **VALID_ORDER)
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
            user=self.user, items=basket((self.product, 1)), **VALID_ORDER
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
        self.order.items.update(quantity=3)
        self.start()
        with mock.patch.object(
            zarinpal, "verify_payment", return_value={"code": 100, "ref_id": 1}
        ):
            _, gadgets = finalize_payment(authority="A0001", gateway_status="OK")
        self.assertEqual(len({g.secret_key for g in gadgets}), 3)

    def test_a_mixed_basket_provisions_one_gadget_per_unit(self):
        camera = make_product(stock=2, price=2400, slug="camera")
        self.order = create_order(
            user=self.user,
            items=basket((self.product, 2), (camera, 1)),
            **VALID_ORDER,
        )
        self.start()
        with mock.patch.object(
            zarinpal, "verify_payment", return_value={"code": 100, "ref_id": 1}
        ):
            _, gadgets = finalize_payment(authority="A0001", gateway_status="OK")
        self.assertEqual(len(gadgets), 3)
        self.assertEqual(
            sorted(g.product.slug for g in gadgets), ["camera", "lamp", "lamp"]
        )

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
                {"items": basket((self.product, 1)), **VALID_ORDER},
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
            {"items": basket((self.product, 1)), **VALID_ORDER},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_bad_phone_number_is_rejected(self):
        self.client.force_authenticate(self.user)
        response = self.client.post(
            reverse("purchases:checkout"),
            {**VALID_ORDER, "items": basket((self.product, 1)), "receiver_phone": "12345"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_oversized_ssid_is_rejected(self):
        self.client.force_authenticate(self.user)
        response = self.client.post(
            reverse("purchases:checkout"),
            {**VALID_ORDER, "items": basket((self.product, 1)), "wifi_ssid": "x" * 40},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_short_wifi_password_is_rejected(self):
        self.client.force_authenticate(self.user)
        response = self.client.post(
            reverse("purchases:checkout"),
            {**VALID_ORDER, "items": basket((self.product, 1)), "wifi_password": "abc"},
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
        create_order(user=self.user, items=basket((self.product, 1)), **VALID_ORDER)
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


class PendingOrderExpiryTests(TestCase):
    """What happens to an order whose buyer closed the gateway tab."""

    def setUp(self):
        self.product = make_product(stock=5)
        self.user = User.objects.create_user(
            email="buyer@gmail.com", password="pass-12345", is_email_verified=True
        )
        self.order = create_order(
            user=self.user, items=basket((self.product, 2)), **VALID_ORDER
        )
        with mock.patch.object(
            zarinpal, "request_payment", return_value=("A0001", {"code": 100})
        ):
            start_payment(self.order, "http://cb/")

    def age(self, minutes):
        """Backdate the order past the payment window."""
        Order.objects.filter(pk=self.order.pk).update(
            created_at=timezone.now() - timedelta(minutes=minutes)
        )

    def test_a_fresh_order_is_left_alone(self):
        with mock.patch.object(zarinpal, "verify_payment") as verify:
            expire_stale_orders()
        verify.assert_not_called()
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.STATUS_PENDING_PAYMENT)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 3)

    def test_an_abandoned_order_releases_its_stock(self):
        self.age(60)
        with mock.patch.object(
            zarinpal,
            "verify_payment",
            side_effect=zarinpal.ZarinpalError("session is not valid", code=-51),
        ):
            expire_stale_orders()
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.STATUS_FAILED)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 5)
        self.assertEqual(Gadget.objects.count(), 0)

    def test_an_order_that_was_actually_paid_is_rescued(self):
        """The buyer paid, then closed the tab before the callback landed."""
        self.age(60)
        with mock.patch.object(
            zarinpal, "verify_payment", return_value={"code": 100, "ref_id": 42}
        ):
            expire_stale_orders()
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.STATUS_PAID)
        self.assertEqual(Gadget.objects.count(), 2)
        # Stock stays reserved — those two units shipped.
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 3)

    def test_a_late_callback_reinstates_an_expired_order(self):
        """The sweep gave up, then the gateway confirmed anyway."""
        self.age(60)
        with mock.patch.object(
            zarinpal,
            "verify_payment",
            side_effect=zarinpal.ZarinpalError("not valid", code=-51),
        ):
            expire_stale_orders()
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 5)

        with mock.patch.object(
            zarinpal, "verify_payment", return_value={"code": 100, "ref_id": 7}
        ):
            payment, gadgets = finalize_payment(
                authority="A0001", gateway_status="OK"
            )

        self.assertEqual(payment.status, Payment.STATUS_SUCCESS)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.STATUS_PAID)
        self.assertEqual(len(gadgets), 2)
        # The units went back off the shelf rather than being sold twice.
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 3)

    def test_the_sweep_does_not_release_twice(self):
        self.age(60)
        with mock.patch.object(
            zarinpal,
            "verify_payment",
            side_effect=zarinpal.ZarinpalError("not valid", code=-51),
        ):
            expire_stale_orders()
            expire_stale_orders()
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 5)


class UnverifiedReconcileTests(TestCase):
    def setUp(self):
        self.product = make_product()
        self.user = User.objects.create_user(
            email="buyer@gmail.com", password="pass-12345", is_email_verified=True
        )
        self.order = create_order(
            user=self.user, items=basket((self.product, 1)), **VALID_ORDER
        )
        with mock.patch.object(
            zarinpal, "request_payment", return_value=("A0001", {"code": 100})
        ):
            start_payment(self.order, "http://cb/")

    def test_money_taken_without_a_callback_becomes_an_order(self):
        with mock.patch.object(
            zarinpal, "unverified_payments", return_value=[{"authority": "A0001"}]
        ):
            with mock.patch.object(
                zarinpal, "verify_payment", return_value={"code": 100, "ref_id": 9}
            ):
                settled = reconcile_unverified_payments()

        self.assertEqual(settled, 1)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.STATUS_PAID)
        self.assertEqual(Gadget.objects.count(), 1)

    def test_an_authority_we_do_not_know_is_skipped(self):
        with mock.patch.object(
            zarinpal, "unverified_payments", return_value=[{"authority": "SOMEONE-ELSE"}]
        ):
            self.assertEqual(reconcile_unverified_payments(), 0)


class ReturnOriginTests(APITestCase):
    """The gateway must send the buyer back to the hostname they left from.

    localhost and 127.0.0.1 are the same site and different cookie jars. A
    callback that lands on the other one drops the buyer into whatever session
    that hostname was holding — which is how someone ends up looking at an
    account they thought they had logged out of.
    """

    def setUp(self):
        self.product = make_product()
        self.user = User.objects.create_user(
            email="buyer@gmail.com", password="pass-12345", is_email_verified=True
        )
        self.client.force_authenticate(self.user)

    def checkout(self, origin=None):
        headers = {"HTTP_ORIGIN": origin} if origin else {}
        with mock.patch.object(
            zarinpal, "request_payment", return_value=("A1", {"code": 100})
        ):
            return self.client.post(
                reverse("purchases:checkout"),
                {"items": basket((self.product, 1)), **VALID_ORDER},
                format="json",
                **headers,
            )

    def test_a_served_origin_is_remembered(self):
        self.checkout(origin="http://localhost:3000")
        self.assertEqual(
            Order.objects.get().return_origin, "http://localhost:3000"
        )

    def test_an_origin_we_do_not_serve_is_ignored(self):
        """Otherwise the callback is an open redirect."""
        self.checkout(origin="https://phishing.example")
        self.assertEqual(Order.objects.get().return_origin, "")

    def test_the_callback_returns_to_that_origin(self):
        self.checkout(origin="http://localhost:3000")
        with mock.patch.object(
            zarinpal, "verify_payment", return_value={"code": 100, "ref_id": 5}
        ):
            response = self.client.get(
                reverse("purchases:payment-verify"),
                {"Authority": "A1", "Status": "OK"},
            )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith("http://localhost:3000/"))

    def test_without_an_origin_the_configured_url_is_used(self):
        self.checkout()
        with mock.patch.object(
            zarinpal, "verify_payment", return_value={"code": 100, "ref_id": 5}
        ):
            response = self.client.get(
                reverse("purchases:payment-verify"),
                {"Authority": "A1", "Status": "OK"},
            )
        self.assertTrue(
            response["Location"].startswith(settings.FRONTEND_PAYMENT_RESULT_URL)
        )
