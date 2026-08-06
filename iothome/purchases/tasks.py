"""Background jobs that stop abandoned checkouts from holding stock.

Stock is reserved the moment an order is created, before the buyer ever
reaches the gateway — that is deliberate, so two people cannot be sent to pay
for the same last unit. The cost of that choice is this file: something has to
decide, later, that a buyer who never came back is not coming back.
"""

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from .gateways import zarinpal
from .models import Order, Payment
from .services import CheckoutError, finalize_payment, release_order

logger = logging.getLogger("iothome.purchases")


@shared_task
def expire_stale_orders():
    """Close out orders that have sat unpaid past the payment window.

    Closing the gateway tab sends nothing anywhere: Zarinpal has no "the
    buyer left" callback, so from here an abandoned checkout and one that is
    still in progress look identical. The only thing that distinguishes them
    is time.

    Before releasing anything this **asks the gateway** whether that authority
    was in fact paid. It is the case that matters most and the one no callback
    covers: the buyer paid, then closed the tab before Zarinpal could redirect
    them back. Cancelling that order would take someone's money and give them
    nothing.
    """
    cutoff = timezone.now() - timedelta(
        minutes=settings.PENDING_ORDER_TIMEOUT_MINUTES
    )
    stale = (
        Order.objects.filter(
            status=Order.STATUS_PENDING_PAYMENT, created_at__lt=cutoff
        )
        .prefetch_related("payments", "items")
        .order_by("created_at")
    )

    rescued = expired = 0
    for order in stale:
        authority = (
            order.payments.filter(status=Payment.STATUS_PENDING)
            .exclude(authority="")
            .values_list("authority", flat=True)
            .first()
        )

        if authority:
            try:
                # finalize_payment does the verify, and is idempotent, so a
                # callback arriving at the same moment cannot double-provision.
                payment, gadgets = finalize_payment(
                    authority=authority, gateway_status="OK"
                )
            except CheckoutError as exc:
                logger.warning("could not settle order %s: %s", order.uid, exc)
            else:
                if payment.status == Payment.STATUS_SUCCESS:
                    rescued += 1
                    logger.info(
                        "order %s was paid after all; %s gadget(s) provisioned",
                        order.uid,
                        len(gadgets),
                    )
                    continue
                # A verify failure already released the stock and marked the
                # order failed, which is the outcome we wanted anyway.
                expired += 1
                continue

        # No authority at all — the gateway was never reached — or the verify
        # blew up. Either way the buyer is not coming back to this order.
        release_order(order, Order.STATUS_CANCELED)
        expired += 1
        logger.info("released stock for abandoned order %s", order.uid)

    if rescued or expired:
        logger.info(
            "pending-order sweep: %s rescued, %s expired", rescued, expired
        )
    return {"rescued": rescued, "expired": expired}


@shared_task
def reconcile_unverified_payments():
    """Ask Zarinpal for money it took that we never confirmed.

    The gateway keeps a list of transactions it settled but that were never
    verified — the callback got lost, the server was down, the buyer closed
    the tab at exactly the wrong second. Left alone Zarinpal reverses them
    after 72 hours, and the buyer's order never appears. This is the backstop
    that turns them into real orders instead.
    """
    try:
        sessions = zarinpal.unverified_payments()
    except zarinpal.ZarinpalError as exc:
        logger.warning("could not list unverified payments: %s", exc)
        return 0

    settled = 0
    for session in sessions:
        authority = session.get("authority")
        if not authority:
            continue
        try:
            payment, _ = finalize_payment(authority=authority, gateway_status="OK")
        except CheckoutError as exc:
            # Not one of ours, or already closed out. Nothing to do.
            logger.debug("skipping unverified %s: %s", authority, exc)
            continue
        if payment.status == Payment.STATUS_SUCCESS:
            settled += 1
            logger.info("settled previously unverified payment %s", authority)

    return settled
