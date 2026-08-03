import asyncio
import json
import logging
import time

from asgiref.sync import sync_to_async
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone

from core.signatures import SignatureError, averify_frame, build_message

from . import presence, protocol
from .models import Capability, Command, Gadget, TelemetryReading

logger = logging.getLogger("iothome.ws")


def canonical(value):
    """Stable text form of a command value, for signing on both ends.

    JSON is used so ``true``/``100``/``"warm"`` look the same in the firmware,
    the browser and here — Python's ``str(True)`` would not match ``true``.
    """
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


class BaseSignedConsumer(AsyncJsonWebsocketConsumer):
    """Shared plumbing: frame limits, auth deadline, per-connection throttle."""

    auth_scope = "base"

    async def connect(self):
        self.authenticated = False
        self._window_start = time.monotonic()
        self._frames_in_window = 0
        await self.accept()
        # Nothing is trusted until the first frame proves itself.
        self._auth_deadline = asyncio.create_task(self._close_if_unauthenticated())

    async def _close_if_unauthenticated(self):
        try:
            await asyncio.sleep(settings.WS_AUTH_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            return
        if not self.authenticated:
            await self.send_error("auth_timeout", "Handshake not completed in time.")
            await self.close(code=protocol.CLOSE_AUTH_TIMEOUT)

    async def receive(self, text_data=None, bytes_data=None):
        if bytes_data is not None:
            await self.fail("bad_payload", "Binary frames are not supported.")
            return
        if text_data is None or len(text_data) > protocol.MAX_FRAME_BYTES:
            await self.fail("bad_payload", "Frame is empty or too large.")
            return
        if not await self._allow_frame():
            return
        try:
            payload = json.loads(text_data)
        except ValueError:
            await self.fail("bad_payload", "Frame is not valid JSON.")
            return
        if not isinstance(payload, dict):
            await self.fail("bad_payload", "Frame must be a JSON object.")
            return
        await self.handle(payload)

    async def _allow_frame(self):
        now = time.monotonic()
        if now - self._window_start >= 60:
            self._window_start = now
            self._frames_in_window = 0
        self._frames_in_window += 1
        if self._frames_in_window > settings.WS_MAX_FRAMES_PER_MINUTE:
            await self.send_error("rate_limited", "Too many frames.")
            await self.close(code=protocol.CLOSE_RATE_LIMITED)
            return False
        return True

    async def handle(self, payload):  # pragma: no cover - overridden
        raise NotImplementedError

    async def send_error(self, code, detail, request_id=None):
        message = {"type": protocol.MSG_ERROR, "code": code, "detail": detail}
        if request_id:
            message["request_id"] = request_id
        try:
            await self.send_json(message)
        except Exception:  # socket already gone
            pass

    async def fail(self, code, detail, close_code=protocol.CLOSE_BAD_PAYLOAD):
        await self.send_error(code, detail)
        await self.close(code=close_code)

    @property
    def client_ip(self):
        for name, value in self.scope.get("headers", []):
            if name == b"x-forwarded-for":
                return value.decode().split(",")[0].strip()
        client = self.scope.get("client")
        return client[0] if client else None

    async def disconnect(self, code):
        task = getattr(self, "_auth_deadline", None)
        if task and not task.done():
            task.cancel()


class DeviceConsumer(BaseSignedConsumer):
    """Endpoint the ESP32 units connect to: ``/ws/device/<uid>/``.

    The device proves itself once per connection with an HMAC of its uid,
    timestamp and nonce keyed by its hardcoded secret. After that the socket
    itself is the credential, which is what keeps the firmware cheap.
    """

    auth_scope = "device"

    async def connect(self):
        self.uid = self.scope["url_route"]["kwargs"]["device_uid"]
        self.gadget = None
        await super().connect()

    async def handle(self, payload):
        message_type = payload.get("type")
        if not self.authenticated:
            if message_type != protocol.MSG_AUTH:
                await self.fail(
                    "unauthenticated",
                    "Send an auth frame first.",
                    protocol.CLOSE_AUTH_FAILED,
                )
                return
            await self.do_auth(payload)
            return

        if message_type == protocol.MSG_HEARTBEAT:
            await self.on_heartbeat()
        elif message_type == protocol.MSG_TELEMETRY:
            await self.on_telemetry(payload)
        elif message_type == protocol.MSG_COMMAND_RESULT:
            await self.on_command_result(payload)
        else:
            await self.send_error("unknown_type", f"Unsupported type '{message_type}'.")

    # -- handshake --------------------------------------------------------

    async def do_auth(self, payload):
        gadget = await self.load_gadget()
        if gadget is None:
            # Same message whether the uid is unknown or disabled, so the
            # endpoint cannot be used to enumerate valid device ids.
            await self.fail(
                "auth_failed", "Authentication failed.", protocol.CLOSE_AUTH_FAILED
            )
            return
        try:
            await averify_frame(
                gadget.secret_key,
                f"device:{self.uid}",
                payload,
                parts=[payload.get("device")],
            )
        except SignatureError as exc:
            logger.warning("device %s failed handshake: %s", self.uid, exc)
            await self.fail(
                "auth_failed", "Authentication failed.", protocol.CLOSE_AUTH_FAILED
            )
            return
        if str(payload.get("device")) != str(self.uid):
            await self.fail(
                "auth_failed", "Authentication failed.", protocol.CLOSE_AUTH_FAILED
            )
            return

        self.gadget = gadget
        self.authenticated = True
        self._auth_deadline.cancel()

        # Evict an older socket for the same unit before we join the group,
        # so a reconnect after a half-open TCP session wins cleanly.
        await self.channel_layer.group_send(
            presence.device_group(self.uid),
            {"type": protocol.EVENT_DEVICE_DISCONNECT, "reason": "superseded"},
        )
        await self.channel_layer.group_add(
            presence.device_group(self.uid), self.channel_name
        )
        await presence.amark_online(
            self.uid,
            owner_id=gadget.owner_id,
            channel_name=self.channel_name,
            timeout_seconds=self.offline_timeout,
            ip=self.client_ip,
        )
        firmware = payload.get("firmware_version") or ""
        await self.touch_gadget(firmware)

        await self.send_json(
            {
                "type": protocol.MSG_AUTH_OK,
                "device": str(self.uid),
                "server_time": int(time.time()),
                "heartbeat_interval": gadget.gadget_type.heartbeat_interval_seconds,
                "capabilities": self.capability_map,
            }
        )
        await self.broadcast_status(True, "connected")
        logger.info("device %s connected", self.uid)

    @database_sync_to_async
    def load_gadget(self):
        gadget = (
            Gadget.objects.select_related("gadget_type", "owner", "product")
            .filter(uid=self.uid)
            .first()
        )
        if gadget is None or not gadget.can_connect:
            return None
        # Cache the capability contract while we are on a DB-capable thread.
        self._capabilities = {
            (c.direction, c.key): c for c in gadget.gadget_type.capabilities.all()
        }
        self.offline_timeout = (
            gadget.gadget_type.offline_timeout_seconds
            or settings.DEFAULT_OFFLINE_TIMEOUT_SECONDS
        )
        return gadget

    @database_sync_to_async
    def touch_gadget(self, firmware_version):
        if firmware_version and firmware_version != self.gadget.firmware_version:
            self.gadget.firmware_version = firmware_version[:32]
            self.gadget.save(update_fields=["firmware_version"])
        self.gadget.mark_seen(ip=self.client_ip)

    @property
    def capability_map(self):
        out = {"telemetry": {}, "command": {}}
        for (direction, key), cap in self._capabilities.items():
            out[direction][key] = {
                "label": cap.label or key,
                "value_type": cap.value_type,
                "unit": cap.unit,
                "min": cap.min_value,
                "max": cap.max_value,
                "choices": cap.choices,
            }
        return out

    # -- inbound frames ---------------------------------------------------

    async def on_heartbeat(self):
        known = await presence.atouch(self.uid, self.offline_timeout)
        if not known:
            # The sweeper already wrote this device off; re-register instead of
            # letting it linger as a ghost connection.
            await presence.amark_online(
                self.uid,
                owner_id=self.gadget.owner_id,
                channel_name=self.channel_name,
                timeout_seconds=self.offline_timeout,
                ip=self.client_ip,
            )
            await self.broadcast_status(True, "reconnected")
        await self.send_json(
            {"type": protocol.MSG_PONG, "server_time": int(time.time())}
        )

    async def on_telemetry(self, payload):
        readings = payload.get("readings")
        if not isinstance(readings, dict) or not readings:
            await self.send_error("bad_payload", "'readings' must be a non-empty object.")
            return
        if len(readings) > protocol.MAX_READINGS_PER_FRAME:
            await self.send_error("bad_payload", "Too many readings in one frame.")
            return

        cleaned = {}
        for key, value in readings.items():
            cap = self._capabilities.get((Capability.DIRECTION_TELEMETRY, key))
            if cap is None:
                await self.send_error(
                    "unknown_capability", f"'{key}' is not a telemetry key."
                )
                return
            try:
                cleaned[key] = cap.validate_value(value)
            except ValidationError as exc:
                await self.send_error("invalid_value", "; ".join(exc.messages))
                return

        recorded_at = timezone.now()
        await self.store_readings(cleaned, recorded_at)
        await presence.atouch(self.uid, self.offline_timeout)
        await self.channel_layer.group_send(
            presence.user_group(self.gadget.owner_id),
            {
                "type": protocol.EVENT_USER_TELEMETRY,
                "gadget": str(self.uid),
                "readings": cleaned,
                "recorded_at": recorded_at.isoformat(),
            },
        )

    @database_sync_to_async
    def store_readings(self, cleaned, recorded_at):
        TelemetryReading.objects.bulk_create(
            [
                TelemetryReading(
                    gadget=self.gadget,
                    key=key,
                    recorded_at=recorded_at,
                    value_bool=value if isinstance(value, bool) else None,
                    value_number=(
                        float(value)
                        if isinstance(value, (int, float)) and not isinstance(value, bool)
                        else None
                    ),
                    value_text="" if not isinstance(value, str) else value[:255],
                )
                for key, value in cleaned.items()
            ]
        )
        # last_seen_at is deliberately *not* touched here: a sensor reporting
        # every few seconds would double the write load for a column the
        # reconcile_last_seen task refreshes from Redis anyway.

    async def on_command_result(self, payload):
        request_id = payload.get("request_id")
        status = payload.get("status")
        if not request_id or status not in ("ok", "error"):
            await self.send_error("bad_payload", "Invalid command_result frame.")
            return
        result = await self.record_result(
            request_id,
            status,
            payload.get("value"),
            str(payload.get("error", ""))[:255],
        )
        if result is None:
            await self.send_error("unknown_command", "No such pending command.")
            return
        await self.channel_layer.group_send(
            presence.user_group(self.gadget.owner_id),
            {
                "type": protocol.EVENT_USER_COMMAND_STATUS,
                "request_id": str(request_id),
                "gadget": str(self.uid),
                "status": result["status"],
                "response": result["response"],
                "error": result["error"],
            },
        )

    @database_sync_to_async
    def record_result(self, request_id, status, value, error):
        command = Command.objects.filter(
            request_id=request_id, gadget=self.gadget
        ).first()
        if command is None:
            return None
        command.status = Command.STATUS_ACKED if status == "ok" else Command.STATUS_FAILED
        command.response = {"value": value} if value is not None else None
        command.error = error
        command.responded_at = timezone.now()
        command.save(update_fields=["status", "response", "error", "responded_at"])
        return {
            "status": command.status,
            "response": command.response,
            "error": command.error,
        }

    # -- channel-layer events --------------------------------------------

    async def device_command(self, event):
        await self.send_json(
            {
                "type": protocol.MSG_COMMAND,
                "request_id": event["request_id"],
                "key": event["key"],
                "value": event["value"],
            }
        )

    async def device_disconnect(self, event):
        reason = event.get("reason", "closed")
        code = (
            protocol.CLOSE_SUPERSEDED
            if reason == "superseded"
            else protocol.CLOSE_OFFLINE_SWEEP
        )
        # Skip the cleanup in disconnect(): the connection that replaced us
        # already owns the presence entry.
        self._suppress_offline = reason == "superseded"
        await self.close(code=code)

    async def broadcast_status(self, online, reason):
        await self.channel_layer.group_send(
            presence.user_group(self.gadget.owner_id),
            {
                "type": protocol.EVENT_USER_DEVICE_STATUS,
                "gadget": str(self.uid),
                "online": online,
                "reason": reason,
            },
        )

    async def disconnect(self, code):
        await super().disconnect(code)
        if not self.authenticated or self.gadget is None:
            return
        await self.channel_layer.group_discard(
            presence.device_group(self.uid), self.channel_name
        )
        if getattr(self, "_suppress_offline", False):
            return
        await presence.amark_offline(self.uid)
        await self.broadcast_status(False, "disconnected")
        logger.info("device %s disconnected (%s)", self.uid, code)


class UserConsumer(BaseSignedConsumer):
    """Endpoint the browser connects to: ``/ws/user/``.

    The handshake is signed with the JWT access token, and every command frame
    is signed again — so a command cannot be replayed or reordered even by
    someone who captured an earlier frame.
    """

    auth_scope = "user"

    async def connect(self):
        self.user = None
        self.token = None
        self.token_expires_at = 0
        await super().connect()

    async def handle(self, payload):
        message_type = payload.get("type")
        if not self.authenticated:
            if message_type != protocol.MSG_AUTH:
                await self.fail(
                    "unauthenticated",
                    "Send an auth frame first.",
                    protocol.CLOSE_AUTH_FAILED,
                )
                return
            await self.do_auth(payload)
            return

        if message_type == protocol.MSG_COMMAND:
            await self.on_command(payload)
        elif message_type == protocol.MSG_HEARTBEAT:
            await self.send_json(
                {"type": protocol.MSG_PONG, "server_time": int(time.time())}
            )
        else:
            await self.send_error("unknown_type", f"Unsupported type '{message_type}'.")

    async def do_auth(self, payload):
        token = payload.get("token")
        if not isinstance(token, str) or not token:
            await self.fail(
                "auth_failed", "Authentication failed.", protocol.CLOSE_AUTH_FAILED
            )
            return
        user, expires_at = await self.resolve_token(token)
        if user is None:
            await self.fail(
                "auth_failed", "Authentication failed.", protocol.CLOSE_AUTH_FAILED
            )
            return
        try:
            await averify_frame(token, f"user:{user.id}", payload)
        except SignatureError as exc:
            logger.warning("user %s failed handshake: %s", user.id, exc)
            await self.fail(
                "auth_failed", "Authentication failed.", protocol.CLOSE_AUTH_FAILED
            )
            return

        self.user = user
        self.token = token
        self.token_expires_at = expires_at
        self.authenticated = True
        self._auth_deadline.cancel()

        await self.channel_layer.group_add(
            presence.user_group(user.id), self.channel_name
        )
        gadgets = await self.list_gadgets()
        online = await presence.aonline_uids([g["uid"] for g in gadgets])
        for gadget in gadgets:
            gadget["online"] = gadget["uid"] in online
        await self.send_json(
            {
                "type": protocol.MSG_AUTH_OK,
                "user_id": user.id,
                "server_time": int(time.time()),
                "gadgets": gadgets,
            }
        )

    @database_sync_to_async
    def resolve_token(self, raw_token):
        from rest_framework_simplejwt.authentication import JWTAuthentication
        from rest_framework_simplejwt.exceptions import (
            AuthenticationFailed,
            InvalidToken,
            TokenError,
        )

        auth = JWTAuthentication()
        try:
            validated = auth.get_validated_token(raw_token)
            user = auth.get_user(validated)
        except (InvalidToken, TokenError, AuthenticationFailed):
            return None, 0
        if not user.is_active or not user.is_email_verified:
            return None, 0
        return user, int(validated.payload.get("exp", 0))

    @database_sync_to_async
    def list_gadgets(self):
        return [
            {
                "uid": str(g.uid),
                "name": g.display_name,
                "type": g.gadget_type.slug,
                "mode": g.gadget_type.mode,
                "status": g.status,
                "last_seen_at": g.last_seen_at.isoformat() if g.last_seen_at else None,
            }
            for g in Gadget.objects.select_related("gadget_type", "product").filter(
                owner=self.user
            )
        ]

    # -- commands ---------------------------------------------------------

    async def on_command(self, payload):
        request_id = payload.get("request_id")
        gadget_uid = payload.get("gadget")
        key = payload.get("key")
        value = payload.get("value")

        if not all(
            isinstance(field, str) and field
            for field in (request_id, gadget_uid, key)
        ):
            await self.send_error(
                "bad_payload", "request_id, gadget and key are required strings."
            )
            return
        if time.time() >= self.token_expires_at:
            await self.fail(
                "token_expired",
                "Access token expired; reconnect with a fresh one.",
                protocol.CLOSE_AUTH_FAILED,
            )
            return

        try:
            await averify_frame(
                self.token,
                f"user:{self.user.id}",
                payload,
                parts=[request_id, gadget_uid, key, canonical(value)],
            )
        except SignatureError as exc:
            await self.send_error("bad_signature", str(exc), request_id)
            return

        # Ownership is re-checked here rather than trusted from the auth frame:
        # a gadget can change hands while a socket is open.
        outcome = await self.prepare_command(request_id, gadget_uid, key, value)
        if "error" in outcome:
            await self.send_error(outcome["code"], outcome["error"], request_id)
            return

        if not await presence.ais_online(gadget_uid):
            await self.mark_command_failed(request_id, "Device is offline.")
            await self.send_json(
                {
                    "type": protocol.MSG_COMMAND_STATUS,
                    "request_id": str(request_id),
                    "gadget": gadget_uid,
                    "status": Command.STATUS_FAILED,
                    "error": "Device is offline.",
                }
            )
            return

        await self.channel_layer.group_send(
            presence.device_group(gadget_uid),
            {
                "type": protocol.EVENT_DEVICE_COMMAND,
                "request_id": str(request_id),
                "key": key,
                "value": outcome["value"],
            },
        )
        await self.mark_command_sent(request_id)
        # If the device never answers, a Celery task closes the command out so
        # the frontend is not left with a spinner forever.
        await self._schedule_timeout(request_id)
        await self.send_json(
            {
                "type": protocol.MSG_COMMAND_STATUS,
                "request_id": str(request_id),
                "gadget": gadget_uid,
                "status": Command.STATUS_SENT,
            }
        )

    @database_sync_to_async
    def prepare_command(self, request_id, gadget_uid, key, value):
        gadget = (
            Gadget.objects.select_related("gadget_type")
            .filter(uid=gadget_uid, owner=self.user)
            .first()
        )
        if gadget is None:
            return {"code": "forbidden", "error": "Unknown gadget or not yours."}
        if gadget.status == Gadget.STATUS_DISABLED:
            return {"code": "device_disabled", "error": "This gadget is disabled."}
        cap = gadget.capability(Capability.DIRECTION_COMMAND, key)
        if cap is None:
            return {"code": "unknown_capability", "error": f"'{key}' is not a command."}
        try:
            cleaned = cap.validate_value(value)
        except ValidationError as exc:
            return {"code": "invalid_value", "error": "; ".join(exc.messages)}
        if Command.objects.filter(request_id=request_id).exists():
            return {"code": "duplicate", "error": "This request_id was already used."}
        Command.objects.create(
            request_id=request_id,
            gadget=gadget,
            issued_by=self.user,
            key=key,
            value=cleaned,
        )
        return {"value": cleaned}

    @database_sync_to_async
    def mark_command_sent(self, request_id):
        Command.objects.filter(
            request_id=request_id, status=Command.STATUS_PENDING
        ).update(status=Command.STATUS_SENT)

    @database_sync_to_async
    def mark_command_failed(self, request_id, error):
        Command.objects.filter(request_id=request_id).update(
            status=Command.STATUS_FAILED, error=error, responded_at=timezone.now()
        )

    async def _schedule_timeout(self, request_id):
        from .tasks import expire_command

        await sync_to_async(expire_command.apply_async)(
            args=[str(request_id)], countdown=settings.COMMAND_TIMEOUT_SECONDS
        )

    # -- channel-layer events --------------------------------------------

    async def user_telemetry(self, event):
        await self.send_json(
            {
                "type": protocol.MSG_TELEMETRY,
                "gadget": event["gadget"],
                "readings": event["readings"],
                "recorded_at": event["recorded_at"],
            }
        )

    async def user_device_status(self, event):
        await self.send_json(
            {
                "type": protocol.MSG_DEVICE_STATUS,
                "gadget": event["gadget"],
                "online": event["online"],
                "reason": event.get("reason", ""),
            }
        )

    async def user_command_status(self, event):
        await self.send_json(
            {
                "type": protocol.MSG_COMMAND_STATUS,
                "request_id": event["request_id"],
                "gadget": event["gadget"],
                "status": event["status"],
                "response": event.get("response"),
                "error": event.get("error", ""),
            }
        )

    async def disconnect(self, code):
        await super().disconnect(code)
        if self.user is not None:
            await self.channel_layer.group_discard(
                presence.user_group(self.user.id), self.channel_name
            )
