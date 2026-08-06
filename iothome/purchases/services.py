"""Checkout orchestration: order -> gateway -> provisioned gadgets."""

import logging

from django.db import models, transaction
from django.utils import timezone

from gadgets.models import Gadget

from .gateways import zarinpal
from .models import Order, OrderItem, Payment, Product

logger = logging.getLogger("iothome.purchases")


class CheckoutError(Exception):
    pass


@transaction.atomic
def create_order(*, user, items, wifi_ssid, wifi_password, **shipping):
    """Reserve stock and freeze prices into one pending order.

    ``items`` is ``[{"product": <id>, "quantity": <n>}, ...]`` — the basket.
    Either the whole basket is reserved or none of it is: the transaction plus
    ``select_for_update`` means a buyer never ends up paying for an order in
    which one line quietly went out of stock between the checks.
    """
    if not items:
        raise CheckoutError("The basket is empty.")

    wanted = {}
    for line in items:
        # A basket that lists the same product twice is one line with the
        # quantities added, which is also what the unique constraint requires.
        wanted[line["product"]] = wanted.get(line["product"], 0) + line["quantity"]

    # Locked in a stable order, so two concurrent baskets holding the same two
    # products cannot each take one lock and wait on the other.
    products = {
        product.pk: product
        for product in Product.objects.select_for_update()
        .filter(pk__in=wanted, is_active=True)
        .order_by("pk")
    }

    total = 0
    for product_id, quantity in wanted.items():
        product = products.get(product_id)
        if product is None:
            raise CheckoutError("One of these products is not available.")
        if product.stock < quantity:
            raise CheckoutError(f"Not enough stock for {product.name}.")
        total += product.price * quantity

    order = Order.objects.create(
        user=user,
        total_amount=total,
        wifi_ssid=wifi_ssid,
        wifi_password=wifi_password,
        **shipping,
    )

    for product_id, quantity in wanted.items():
        product = products[product_id]
        # Stock is held from checkout, not from payment, so two buyers cannot
        # be sent to the gateway for the same last unit. Cancellation returns
        # it.
        product.stock -= quantity
        product.save(update_fields=["stock"])
        OrderItem.objects.create(
            order=order,
            product=product,
            quantity=quantity,
            unit_price=product.price,
        )

    return order


def start_payment(order, callback_url):
    """Create a pending Payment and hand back the gateway redirect URL."""
    if not order.is_payable:
        raise CheckoutError("This order is not awaiting payment.")

    payment = Payment.objects.create(
        order=order,
        amount=order.total_amount,
        status=Payment.STATUS_PENDING,
    )
    try:
        authority, raw = zarinpal.request_payment(
            amount_toman=order.total_amount,
            description=f"Order {order.uid} - {order.summary}",
            callback_url=callback_url,
            email=order.user.email,
            mobile=order.receiver_phone,
        )
    except zarinpal.ZarinpalError as exc:
        payment.status = Payment.STATUS_FAILED
        payment.error_message = str(exc)[:255]
        payment.error_code = str(exc.code or "")[:32]
        payment.save(update_fields=["status", "error_message", "error_code"])
        raise CheckoutError(str(exc))

    payment.authority = authority
    payment.gateway_response = raw
    payment.save(update_fields=["authority", "gateway_response"])
    return payment, zarinpal.start_payment_url(authority)


@transaction.atomic
def finalize_payment(*, authority, gateway_status):
    """Handle the gateway callback. Safe to call more than once.

    Returns ``(payment, created_gadgets)``; ``payment.status`` tells the caller
    what happened.
    """
    payment = (
        Payment.objects.select_for_update()
        .select_related("order", "order__user")
        .prefetch_related("order__items__product")
        .filter(authority=authority)
        .first()
    )
    if payment is None:
        raise CheckoutError("Unknown payment reference.")

    if payment.status == Payment.STATUS_SUCCESS:
        # Zarinpal re-delivered a callback we already processed.
        return payment, list(payment.order.gadgets.all())

    order = payment.order

    if str(gateway_status).upper() != "OK":
        payment.status = Payment.STATUS_CANCELED
        payment.error_message = "Payment was canceled at the gateway."
        payment.save(update_fields=["status", "error_message"])
        _release_order(order, Order.STATUS_CANCELED)
        return payment, []

    try:
        data = zarinpal.verify_payment(
            amount_toman=payment.amount, authority=authority
        )
    except zarinpal.ZarinpalError as exc:
        payment.status = Payment.STATUS_FAILED
        payment.error_message = str(exc)[:255]
        payment.error_code = str(exc.code or "")[:32]
        payment.save(update_fields=["status", "error_message", "error_code"])
        _release_order(order, Order.STATUS_FAILED)
        return payment, []

    payment.status = Payment.STATUS_SUCCESS
    payment.ref_id = str(data.get("ref_id", ""))[:64]
    payment.card_pan = str(data.get("card_pan", ""))[:32]
    payment.fee = data.get("fee") or 0
    payment.gateway_response = data
    payment.verified_at = timezone.now()
    payment.save()

    order.status = Order.STATUS_PAID
    order.save(update_fields=["status"])

    gadgets = provision_gadgets(order)
    logger.info("order %s paid (ref %s), %s gadget(s)", order.uid, payment.ref_id, len(gadgets))
    return payment, gadgets


def provision_gadgets(order):
    """Create the physical units this order pays for, one per unit per line.

    Each unit gets its own secret key, and the order's Wi-Fi credentials are
    copied onto it — that pair is what gets flashed during assembly.
    """
    existing = list(order.gadgets.all())
    if existing:
        return existing
    return [
        Gadget.objects.create(
            owner=order.user,
            product=item.product,
            gadget_type=item.product.gadget_type,
            order=order,
            wifi_ssid=order.wifi_ssid,
            wifi_password=order.wifi_password,
            status=Gadget.STATUS_AWAITING_ASSEMBLY,
        )
        for item in order.items.select_related("product__gadget_type")
        for _ in range(item.quantity)
    ]


def _release_order(order, status):
    """Return reserved stock, line by line, when a payment does not go through."""
    if order.status != Order.STATUS_PENDING_PAYMENT:
        return
    for item in order.items.all():
        Product.objects.filter(pk=item.product_id).update(
            stock=models.F("stock") + item.quantity
        )
    order.status = status
    order.save(update_fields=["status"])
