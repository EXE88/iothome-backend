"""Shared Redis handles.

Two clients: a blocking one for Celery tasks / DRF views, and an asyncio one
for the WebSocket consumers. Both are created lazily and reused.
"""

import redis
import redis.asyncio as aioredis
from django.conf import settings

_sync_client = None
_async_client = None


def get_redis():
    global _sync_client
    if _sync_client is None:
        _sync_client = redis.Redis.from_url(
            settings.REDIS_URL, decode_responses=True
        )
    return _sync_client


def get_async_redis():
    global _async_client
    if _async_client is None:
        _async_client = aioredis.Redis.from_url(
            settings.REDIS_URL, decode_responses=True
        )
    return _async_client
