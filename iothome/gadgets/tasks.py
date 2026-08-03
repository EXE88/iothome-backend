"""Background jobs that keep the live view honest."""

import logging
from datetime import timedelta

from asgiref.sync import async_to_sync
from celery import shared_task
from channels.layers import get_channel_layer
from django.conf import settings
from django.utils import timezone

from . import presence, protocol
from .models import Command, Gadget, TelemetryReading

logger = logging.getLogger("iothome.tasks")


def _notify(group, message):
    async_to_sync(get_channel_layer().group_send)(group, message)


@shared_task
def sweep_offline_devices():
    """Drop devices that stopped sending heartbeats.

    A socket can stay half-open for a long time — the peer is gone but no FIN
    ever arrives — so silence, not the TCP state, is what marks a device dead.
    """
    dropped = 0
    for uid, info in presence.iter_online_devices():
        timeout = float(
            info.get("timeout") or settings.DEFAULT_OFFLINE_TIMEOUT_SECONDS
        )
        if presence.seconds_since_heartbeat(info) <= timeout:
            continue

        presence.mark_offline(uid)
        # Close the socket if it is somehow still alive on a worker.
        _notify(
            presence.device_group(uid),
            {"type": protocol.EVENT_DEVICE_DISCONNECT, "reason": "offline_sweep"},
        )
        owner_id = info.get("owner_id")
        if owner_id:
            _notify(
                presence.user_group(owner_id),
                {
                    "type": protocol.EVENT_USER_DEVICE_STATUS,
                    "gadget": str(uid),
                    "online": False,
                    "reason": "heartbeat_timeout",
                },
            )
        dropped += 1
        logger.info("device %s marked offline (no heartbeat)", uid)

    if dropped:
        logger.info("offline sweep dropped %s device(s)", dropped)
    return dropped


@shared_task
def expire_command(request_id):
    """Close out a single command the device never acknowledged."""
    command = (
        Command.objects.select_related("gadget")
        .filter(
            request_id=request_id,
            status__in=(Command.STATUS_PENDING, Command.STATUS_SENT),
        )
        .first()
    )
    if command is None:
        return False
    command.status = Command.STATUS_TIMEOUT
    command.error = "Device did not respond in time."
    command.responded_at = timezone.now()
    command.save(update_fields=["status", "error", "responded_at"])
    _notify(
        presence.user_group(command.gadget.owner_id),
        {
            "type": protocol.EVENT_USER_COMMAND_STATUS,
            "request_id": str(command.request_id),
            "gadget": str(command.gadget.uid),
            "status": command.status,
            "response": None,
            "error": command.error,
        },
    )
    return True


@shared_task
def expire_stale_commands():
    """Backstop for commands whose per-command timer was lost.

    ``expire_command`` is scheduled with a countdown; if the broker drops that
    message this periodic sweep still cleans the row up.
    """
    cutoff = timezone.now() - timedelta(seconds=settings.COMMAND_TIMEOUT_SECONDS * 4)
    stale = Command.objects.filter(
        status__in=(Command.STATUS_PENDING, Command.STATUS_SENT),
        created_at__lt=cutoff,
    ).values_list("request_id", flat=True)
    return sum(expire_command(str(request_id)) for request_id in stale)


@shared_task
def prune_old_telemetry(days=90):
    """Keep the readings table from growing without bound."""
    cutoff = timezone.now() - timedelta(days=days)
    deleted, _ = TelemetryReading.objects.filter(recorded_at__lt=cutoff).delete()
    if deleted:
        logger.info("pruned %s telemetry rows older than %s days", deleted, days)
    return deleted


@shared_task
def reconcile_last_seen():
    """Copy Redis heartbeats into the database.

    Redis is the live source of truth; this keeps ``last_seen_at`` fresh for
    the REST API without writing a row on every heartbeat.
    """
    updated = 0
    for uid, info in presence.iter_online_devices():
        seconds = presence.seconds_since_heartbeat(info)
        if seconds == float("inf"):
            continue
        seen_at = timezone.now() - timedelta(seconds=seconds)
        updated += Gadget.objects.filter(uid=uid).update(last_seen_at=seen_at)
    return updated
