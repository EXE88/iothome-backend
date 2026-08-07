"""Housekeeping for accounts that were started and never finished.

Signing up creates the user row *before* the address is proven, because the
code has to be sent to somebody. Most people type the code in a minute later.
Some close the tab, and those rows would otherwise sit in the table for ever —
holding an address nobody can use, and slowly turning the user table into a
log of abandoned attempts.
"""

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Exists, OuterRef
from django.utils import timezone

from .models import EmailOTP

logger = logging.getLogger("iothome.accounts")

User = get_user_model()


@shared_task
def purge_unverified_accounts(days=None):
    """Delete accounts that never proved their address.

    An unverified account can do nothing at all — it cannot log in, and every
    endpoint that matters is behind `IsEmailVerified`. Its only real effect is
    to reserve an email address, which is the reason to remove it: the person
    who abandoned the signup should be able to come back and start again.

    Anything with an order or a gadget attached is left alone regardless of
    its flag. That should be impossible — you cannot buy without verifying —
    but deleting a paying customer because of a flag is not a mistake worth
    risking, and `Order.user` is PROTECT, so it would fail loudly anyway.
    """
    # Imported here rather than at module scope: accounts is the app the other
    # two depend on, and importing them back at import time is a cycle.
    from gadgets.models import Gadget
    from purchases.models import Order

    days = days if days is not None else settings.UNVERIFIED_ACCOUNT_TTL_DAYS
    cutoff = timezone.now() - timedelta(days=days)

    stale = (
        User.objects.filter(
            is_email_verified=False,
            is_staff=False,
            is_superuser=False,
            date_joined__lt=cutoff,
        )
        .annotate(
            has_orders=Exists(Order.objects.filter(user=OuterRef("pk"))),
            has_gadgets=Exists(Gadget.objects.filter(owner=OuterRef("pk"))),
        )
        .filter(has_orders=False, has_gadgets=False)
    )

    removed, _ = stale.delete()
    if removed:
        logger.info("purged %s unverified account(s) older than %sd", removed, days)
    return removed


@shared_task
def purge_expired_otps(days=1):
    """Drop codes that can no longer be used.

    Only the SHA-256 of a code is stored, so these are not dangerous to keep —
    they are simply dead weight, one row per signup, resend and password reset
    for the life of the system.
    """
    cutoff = timezone.now() - timedelta(days=days)
    removed, _ = EmailOTP.objects.filter(expires_at__lt=cutoff).delete()
    if removed:
        logger.info("purged %s expired OTP row(s)", removed)
    return removed
