"""Who is online, tracked in Redis.

The database keeps the slow-moving truth (``Gadget.last_seen_at``); Redis
keeps the live picture that the heartbeat sweeper and the user consumers read.

Layout::

    devices:online              SET of gadget uids currently holding a socket
    device:online:<uid>         HASH {last_heartbeat, connected_at, channel,
                                      timeout, owner_id}

Each hash also carries its own ``timeout`` so the sweeper can decide a device
is gone without touching the database.
"""

import time

from django.conf import settings

from core.redis_client import get_async_redis, get_redis

ONLINE_SET = "devices:online"
DEVICE_KEY = "device:online:{uid}"
# Safety net: if a worker dies without cleaning up, the key still expires.
KEY_TTL_MULTIPLIER = 4


def device_key(uid):
    return DEVICE_KEY.format(uid=uid)


def device_group(uid):
    return f"device.{uid}"


def user_group(user_id):
    return f"user.{user_id}"


async def amark_online(uid, *, owner_id, channel_name, timeout_seconds, ip=None):
    r = get_async_redis()
    now = time.time()
    key = device_key(uid)
    async with r.pipeline(transaction=True) as pipe:
        pipe.hset(
            key,
            mapping={
                "last_heartbeat": now,
                "connected_at": now,
                "channel": channel_name,
                "timeout": timeout_seconds,
                "owner_id": owner_id,
                "ip": ip or "",
            },
        )
        pipe.expire(key, int(timeout_seconds * KEY_TTL_MULTIPLIER))
        pipe.sadd(ONLINE_SET, str(uid))
        await pipe.execute()


async def atouch(uid, timeout_seconds=None):
    """Record a heartbeat. Returns False if the device was not registered."""
    r = get_async_redis()
    key = device_key(uid)
    if not await r.exists(key):
        return False
    ttl = int(
        (timeout_seconds or settings.DEFAULT_OFFLINE_TIMEOUT_SECONDS)
        * KEY_TTL_MULTIPLIER
    )
    async with r.pipeline(transaction=True) as pipe:
        pipe.hset(key, "last_heartbeat", time.time())
        pipe.expire(key, ttl)
        await pipe.execute()
    return True


async def amark_offline(uid):
    r = get_async_redis()
    async with r.pipeline(transaction=True) as pipe:
        pipe.delete(device_key(uid))
        pipe.srem(ONLINE_SET, str(uid))
        await pipe.execute()


async def ais_online(uid):
    return bool(await get_async_redis().exists(device_key(uid)))


async def aonline_uids(uids):
    """Filter a list of uids down to the ones currently connected."""
    if not uids:
        return set()
    r = get_async_redis()
    async with r.pipeline(transaction=False) as pipe:
        for uid in uids:
            pipe.exists(device_key(uid))
        results = await pipe.execute()
    return {str(uid) for uid, alive in zip(uids, results) if alive}


# -- sync variants, used by Celery tasks and DRF views --------------------


def mark_offline(uid):
    r = get_redis()
    with r.pipeline(transaction=True) as pipe:
        pipe.delete(device_key(uid))
        pipe.srem(ONLINE_SET, str(uid))
        pipe.execute()


def is_online(uid):
    return bool(get_redis().exists(device_key(uid)))


def online_uids(uids):
    if not uids:
        return set()
    r = get_redis()
    with r.pipeline(transaction=False) as pipe:
        for uid in uids:
            pipe.exists(device_key(uid))
        results = pipe.execute()
    return {str(uid) for uid, alive in zip(uids, results) if alive}


def iter_online_devices():
    """Yield ``(uid, info)`` for every device Redis believes is connected."""
    r = get_redis()
    for uid in r.smembers(ONLINE_SET):
        info = r.hgetall(device_key(uid))
        if not info:
            # Key expired but the set entry lingered — clean it up in passing.
            r.srem(ONLINE_SET, uid)
            continue
        yield uid, info


def seconds_since_heartbeat(info):
    try:
        return time.time() - float(info.get("last_heartbeat", 0))
    except (TypeError, ValueError):
        return float("inf")
