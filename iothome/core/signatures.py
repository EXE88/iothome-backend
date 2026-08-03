"""HMAC handshake helpers shared by the device and user WebSocket endpoints.

Every authenticated frame carries ``timestamp``, ``nonce`` and ``signature``.
The signature is ``HMAC-SHA256(secret, "field1|field2|...")`` over an ordered
list of fields, hex-encoded. Two independent guards stop replays:

* the timestamp must be within ``SIGNATURE_MAX_SKEW_SECONDS`` of server time;
* the nonce must not have been seen before (tracked in Redis for twice the
  skew window, so a nonce cannot outlive its own timestamp check).
"""

import hashlib
import hmac
import time

from django.conf import settings

from .redis_client import get_async_redis, get_redis

NONCE_KEY = "sig:nonce:{scope}:{nonce}"


class SignatureError(Exception):
    """Raised when a frame fails authentication. The message is client-safe."""


def build_message(*parts):
    """Join fields into the canonical string that gets signed.

    ``None`` becomes an empty field so the layout stays fixed, and every part
    is stringified the same way on both ends.
    """
    return "|".join("" if p is None else str(p) for p in parts)


def sign(secret, message):
    return hmac.new(
        secret.encode() if isinstance(secret, str) else secret,
        message.encode(),
        hashlib.sha256,
    ).hexdigest()


def signatures_match(secret, message, signature):
    if not signature or not isinstance(signature, str):
        return False
    return hmac.compare_digest(sign(secret, message), signature.lower())


def check_timestamp(timestamp):
    """Validate the client clock is close enough to ours."""
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        raise SignatureError("invalid timestamp")
    if abs(time.time() - ts) > settings.SIGNATURE_MAX_SKEW_SECONDS:
        raise SignatureError("timestamp outside the accepted window")
    return ts


def _nonce_args(scope, nonce):
    if not nonce or not isinstance(nonce, str) or len(nonce) > 128:
        raise SignatureError("invalid nonce")
    return (
        NONCE_KEY.format(scope=scope, nonce=nonce),
        settings.SIGNATURE_MAX_SKEW_SECONDS * 2,
    )


def consume_nonce(scope, nonce):
    key, ttl = _nonce_args(scope, nonce)
    if not get_redis().set(key, "1", nx=True, ex=ttl):
        raise SignatureError("nonce already used")


async def aconsume_nonce(scope, nonce):
    key, ttl = _nonce_args(scope, nonce)
    if not await get_async_redis().set(key, "1", nx=True, ex=ttl):
        raise SignatureError("nonce already used")


async def averify_frame(secret, scope, payload, parts=()):
    """Full check for one signed frame: clock, replay, then HMAC.

    ``parts`` are the frame-specific values that take part in the signature,
    in a fixed order; ``timestamp`` and ``nonce`` are always appended last.
    """
    ts = check_timestamp(payload.get("timestamp"))
    nonce = payload.get("nonce")
    await aconsume_nonce(scope, nonce)
    message = build_message(*parts, ts, nonce)
    if not signatures_match(secret, message, payload.get("signature")):
        raise SignatureError("signature mismatch")
    return message
