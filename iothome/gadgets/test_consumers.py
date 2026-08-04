"""End-to-end WebSocket tests.

Redis is swapped for fakeredis and the channel layer for the in-memory one, so
the whole handshake / telemetry / command path runs with no services up.
"""

import hashlib
import hmac
import json
import secrets
import time
from unittest import mock

import fakeredis
import fakeredis.aioredis
from channels.db import database_sync_to_async
from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from django.test import TransactionTestCase, override_settings
from rest_framework_simplejwt.tokens import AccessToken

from core import redis_client

from .consumers import DeviceConsumer, UserConsumer, canonical
from .models import Capability, Command, Gadget, GadgetType
from .presence import user_group
from .tests import make_catalog

User = get_user_model()


def sign(secret, *parts):
    message = "|".join("" if p is None else str(p) for p in parts)
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def signed(secret, payload, *parts):
    """Attach timestamp, nonce and signature to an outgoing frame."""
    timestamp = int(time.time())
    nonce = secrets.token_hex(8)
    return {
        **payload,
        "timestamp": timestamp,
        "nonce": nonce,
        "signature": sign(secret, *parts, timestamp, nonce),
    }


@override_settings(
    CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
)
class ConsumerTestCase(TransactionTestCase):
    """Base that wires fakeredis in and builds an owner with one gadget."""

    def setUp(self):
        server = fakeredis.FakeServer()
        self.redis = fakeredis.FakeRedis(server=server, decode_responses=True)
        self.aredis = fakeredis.aioredis.FakeRedis(
            server=server, decode_responses=True
        )
        patches = [
            mock.patch.object(redis_client, "get_redis", return_value=self.redis),
            mock.patch.object(
                redis_client, "get_async_redis", return_value=self.aredis
            ),
            mock.patch("core.signatures.get_redis", return_value=self.redis),
            mock.patch("core.signatures.get_async_redis", return_value=self.aredis),
            mock.patch("gadgets.presence.get_redis", return_value=self.redis),
            mock.patch("gadgets.presence.get_async_redis", return_value=self.aredis),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.gadget_type, self.product = make_catalog()
        self.owner = User.objects.create_user(
            email="owner@gmail.com", password="pass-12345", is_email_verified=True
        )
        self.gadget = Gadget.objects.create(
            owner=self.owner,
            product=self.product,
            gadget_type=self.gadget_type,
            status=Gadget.STATUS_ASSEMBLED,
        )
        self.secret = self.gadget.secret_key
        self.uid = str(self.gadget.uid)

    async def connect_device(self, secret=None, uid=None):
        uid = uid or self.uid
        communicator = WebsocketCommunicator(
            DeviceConsumer.as_asgi(), f"/ws/device/{uid}/"
        )
        communicator.scope["url_route"] = {"kwargs": {"device_uid": uid}}
        connected, _ = await communicator.connect()
        assert connected
        await communicator.send_json_to(
            signed(secret or self.secret, {"type": "auth", "device": uid}, uid)
        )
        return communicator

    async def connect_user(self, user=None):
        user = user or self.owner
        token = str(await database_sync_to_async(AccessToken.for_user)(user))
        communicator = WebsocketCommunicator(UserConsumer.as_asgi(), "/ws/user/")
        communicator.scope["url_route"] = {"kwargs": {}}
        connected, _ = await communicator.connect()
        assert connected
        await communicator.send_json_to(signed(token, {"type": "auth", "token": token}))
        return communicator, token


class DeviceHandshakeTests(ConsumerTestCase):
    async def test_valid_signature_is_accepted(self):
        device = await self.connect_device()
        reply = await device.receive_json_from()
        self.assertEqual(reply["type"], "auth.ok")
        self.assertIn("temperature", reply["capabilities"]["telemetry"])
        self.assertTrue(self.redis.exists(f"device:online:{self.uid}"))
        await device.disconnect()

    async def test_wrong_secret_is_refused(self):
        device = await self.connect_device(secret="0" * 64)
        reply = await device.receive_json_from()
        self.assertEqual(reply["type"], "error")
        self.assertEqual(reply["code"], "auth_failed")
        # The error frame is followed by a close, not by a usable session.
        closing = await device.receive_output()
        self.assertEqual(closing["type"], "websocket.close")
        self.assertFalse(self.redis.exists(f"device:online:{self.uid}"))
        await device.disconnect()

    async def test_replayed_handshake_is_refused(self):
        frame = signed(self.secret, {"type": "auth", "device": self.uid}, self.uid)
        first = WebsocketCommunicator(
            DeviceConsumer.as_asgi(), f"/ws/device/{self.uid}/"
        )
        first.scope["url_route"] = {"kwargs": {"device_uid": self.uid}}
        await first.connect()
        await first.send_json_to(frame)
        self.assertEqual((await first.receive_json_from())["type"], "auth.ok")

        # Same nonce, captured off the wire.
        second = WebsocketCommunicator(
            DeviceConsumer.as_asgi(), f"/ws/device/{self.uid}/"
        )
        second.scope["url_route"] = {"kwargs": {"device_uid": self.uid}}
        await second.connect()
        await second.send_json_to(frame)
        reply = await second.receive_json_from()
        self.assertEqual(reply["code"], "auth_failed")
        await first.disconnect()
        await second.disconnect()

    async def test_stale_timestamp_is_refused(self):
        device = WebsocketCommunicator(
            DeviceConsumer.as_asgi(), f"/ws/device/{self.uid}/"
        )
        device.scope["url_route"] = {"kwargs": {"device_uid": self.uid}}
        await device.connect()
        stale = int(time.time()) - 7200
        nonce = secrets.token_hex(8)
        await device.send_json_to(
            {
                "type": "auth",
                "device": self.uid,
                "timestamp": stale,
                "nonce": nonce,
                "signature": sign(self.secret, self.uid, stale, nonce),
            }
        )
        self.assertEqual((await device.receive_json_from())["code"], "auth_failed")
        await device.disconnect()

    async def test_disabled_gadget_cannot_connect(self):
        await database_sync_to_async(
            Gadget.objects.filter(uid=self.uid).update
        )(status=Gadget.STATUS_DISABLED)
        device = await self.connect_device()
        self.assertEqual((await device.receive_json_from())["code"], "auth_failed")
        await device.disconnect()

    async def test_traffic_before_auth_is_rejected(self):
        device = WebsocketCommunicator(
            DeviceConsumer.as_asgi(), f"/ws/device/{self.uid}/"
        )
        device.scope["url_route"] = {"kwargs": {"device_uid": self.uid}}
        await device.connect()
        await device.send_json_to({"type": "telemetry", "readings": {"temperature": 20}})
        self.assertEqual((await device.receive_json_from())["code"], "unauthenticated")
        await device.disconnect()

    async def test_oversized_frame_is_rejected(self):
        device = WebsocketCommunicator(
            DeviceConsumer.as_asgi(), f"/ws/device/{self.uid}/"
        )
        device.scope["url_route"] = {"kwargs": {"device_uid": self.uid}}
        await device.connect()
        await device.send_to(text_data="x" * 40000)
        self.assertEqual((await device.receive_json_from())["code"], "bad_payload")
        await device.disconnect()

    async def test_disconnect_clears_presence(self):
        device = await self.connect_device()
        await device.receive_json_from()
        await device.disconnect()
        self.assertFalse(self.redis.exists(f"device:online:{self.uid}"))


class TelemetryTests(ConsumerTestCase):
    async def test_valid_reading_is_forwarded_but_not_stored(self):
        user, _token = await self.connect_user()
        await user.receive_json_from()  # auth.ok
        device = await self.connect_device()
        await device.receive_json_from()  # auth.ok
        await user.receive_json_from()  # device.status online

        await device.send_json_to(
            {"type": "telemetry", "readings": {"temperature": 23.5}}
        )
        pushed = await user.receive_json_from()
        self.assertEqual(pushed["type"], "telemetry")
        self.assertEqual(pushed["readings"]["temperature"], 23.5)

        # Relayed live and gone: the only history the system keeps is commands.
        self.assertEqual(await database_sync_to_async(Command.objects.count)(), 0)
        await device.disconnect()
        await user.disconnect()

    async def test_reading_with_no_listener_is_dropped_silently(self):
        device = await self.connect_device()
        await device.receive_json_from()
        # Nobody is watching; the device must not get an error for it.
        await device.send_json_to(
            {"type": "telemetry", "readings": {"temperature": 21.0}}
        )
        await device.send_json_to({"type": "heartbeat"})
        self.assertEqual((await device.receive_json_from())["type"], "pong")
        await device.disconnect()

    async def test_out_of_range_reading_is_refused(self):
        device = await self.connect_device()
        await device.receive_json_from()
        await device.send_json_to(
            {"type": "telemetry", "readings": {"temperature": 5000}}
        )
        reply = await device.receive_json_from()
        self.assertEqual(reply["code"], "invalid_value")
        await device.disconnect()

    async def test_unknown_key_is_refused(self):
        device = await self.connect_device()
        await device.receive_json_from()
        await device.send_json_to({"type": "telemetry", "readings": {"hacked": 1}})
        self.assertEqual(
            (await device.receive_json_from())["code"], "unknown_capability"
        )
        await device.disconnect()

    async def test_wrong_type_is_refused(self):
        device = await self.connect_device()
        await device.receive_json_from()
        await device.send_json_to(
            {"type": "telemetry", "readings": {"temperature": "hot"}}
        )
        self.assertEqual((await device.receive_json_from())["code"], "invalid_value")
        await device.disconnect()

    async def test_heartbeat_gets_a_pong(self):
        device = await self.connect_device()
        await device.receive_json_from()
        await device.send_json_to({"type": "heartbeat"})
        self.assertEqual((await device.receive_json_from())["type"], "pong")
        await device.disconnect()


class UserHandshakeTests(ConsumerTestCase):
    async def test_valid_token_and_signature_are_accepted(self):
        user, _token = await self.connect_user()
        reply = await user.receive_json_from()
        self.assertEqual(reply["type"], "auth.ok")
        self.assertEqual(len(reply["gadgets"]), 1)
        self.assertFalse(reply["gadgets"][0]["online"])
        await user.disconnect()

    async def test_garbage_token_is_refused(self):
        communicator = WebsocketCommunicator(UserConsumer.as_asgi(), "/ws/user/")
        communicator.scope["url_route"] = {"kwargs": {}}
        await communicator.connect()
        await communicator.send_json_to(
            signed("not-a-token", {"type": "auth", "token": "not-a-token"})
        )
        self.assertEqual((await communicator.receive_json_from())["code"], "auth_failed")
        await communicator.disconnect()

    async def test_signature_from_a_different_token_is_refused(self):
        token = str(await database_sync_to_async(AccessToken.for_user)(self.owner))
        communicator = WebsocketCommunicator(UserConsumer.as_asgi(), "/ws/user/")
        communicator.scope["url_route"] = {"kwargs": {}}
        await communicator.connect()
        # Valid token, but signed with something else.
        await communicator.send_json_to(signed("wrong-key", {"type": "auth", "token": token}))
        self.assertEqual((await communicator.receive_json_from())["code"], "auth_failed")
        await communicator.disconnect()

    async def test_unverified_email_cannot_connect(self):
        unverified = await database_sync_to_async(User.objects.create_user)(
            email="new@gmail.com", password="pass-12345"
        )
        user, _token = await self.connect_user(unverified)
        self.assertEqual((await user.receive_json_from())["code"], "auth_failed")
        await user.disconnect()

    async def test_connecting_asks_online_gadgets_for_their_state(self):
        device = await self.connect_device()
        await device.receive_json_from()  # auth.ok

        user, _token = await self.connect_user()
        await user.receive_json_from()  # auth.ok

        # The device is polled, answers like any telemetry frame, and the
        # answer lands on the dashboard that asked for it.
        asked = await device.receive_json_from()
        self.assertEqual(asked["type"], "state_request")

        await device.send_json_to(
            {"type": "telemetry", "readings": {"temperature": 21.5}}
        )
        pushed = await user.receive_json_from()
        self.assertEqual(pushed["type"], "telemetry")
        self.assertEqual(pushed["readings"]["temperature"], 21.5)

        await device.disconnect()
        await user.disconnect()

    @override_settings(STATE_REQUEST_TIMEOUT_SECONDS=1)
    async def test_silent_gadget_is_reported_as_state_unknown(self):
        device = await self.connect_device()
        await device.receive_json_from()  # auth.ok

        user, _token = await self.connect_user()
        await user.receive_json_from()  # auth.ok
        await device.receive_json_from()  # state_request, deliberately ignored

        unknown = await user.receive_json_from(timeout=4)
        self.assertEqual(unknown["type"], "state.unknown")
        self.assertEqual(unknown["gadget"], self.uid)
        # The socket stays up: not answering a poll is not the same as dying.
        await device.send_json_to({"type": "heartbeat"})
        self.assertEqual((await device.receive_json_from())["type"], "pong")
        await device.disconnect()
        await user.disconnect()

    @override_settings(STATE_REQUEST_TIMEOUT_SECONDS=1)
    async def test_answering_the_poll_suppresses_state_unknown(self):
        device = await self.connect_device()
        await device.receive_json_from()
        user, _token = await self.connect_user()
        await user.receive_json_from()
        await device.receive_json_from()  # state_request

        await device.send_json_to(
            {"type": "telemetry", "readings": {"temperature": 19.0}}
        )
        self.assertEqual((await user.receive_json_from())["type"], "telemetry")
        # Nothing further once the poll has been satisfied.
        self.assertTrue(await user.receive_nothing(timeout=3))
        await device.disconnect()
        await user.disconnect()

    async def test_gadget_coming_online_later_is_polled_too(self):
        user, _token = await self.connect_user()
        await user.receive_json_from()  # auth.ok, nothing online yet

        device = await self.connect_device()
        await device.receive_json_from()  # auth.ok

        status = await user.receive_json_from()
        self.assertEqual(status["type"], "device.status")
        self.assertTrue(status["online"])

        asked = await device.receive_json_from()
        self.assertEqual(asked["type"], "state_request")
        await device.send_json_to(
            {"type": "telemetry", "readings": {"temperature": 25.0}}
        )
        pushed = await user.receive_json_from()
        self.assertEqual(pushed["readings"]["temperature"], 25.0)
        await device.disconnect()
        await user.disconnect()

    async def test_offline_gadget_is_not_polled(self):
        user, _token = await self.connect_user()
        reply = await user.receive_json_from()
        self.assertFalse(reply["gadgets"][0]["online"])
        # No device socket exists; nothing should arrive on this one either.
        self.assertTrue(await user.receive_nothing())
        await user.disconnect()

    async def test_online_gadget_is_reported_on_connect(self):
        device = await self.connect_device()
        await device.receive_json_from()
        user, _token = await self.connect_user()
        reply = await user.receive_json_from()
        self.assertTrue(reply["gadgets"][0]["online"])
        await device.disconnect()
        await user.disconnect()


class RefreshStateTests(ConsumerTestCase):
    async def setup_pair(self):
        device = await self.connect_device()
        await device.receive_json_from()  # auth.ok
        user, token = await self.connect_user()
        await user.receive_json_from()  # auth.ok
        await device.receive_json_from()  # the automatic poll on connect
        return device, user

    async def test_refresh_polls_the_gadget_again(self):
        device, user = await self.setup_pair()

        await user.send_json_to({"type": "refresh_state"})
        ack = await user.receive_json_from()
        self.assertEqual(ack["type"], "refresh.ack")
        self.assertEqual(ack["polled"], [self.uid])
        self.assertEqual(ack["skipped_offline"], [])

        asked = await device.receive_json_from()
        self.assertEqual(asked["type"], "state_request")
        await device.send_json_to(
            {"type": "telemetry", "readings": {"temperature": 30.0}}
        )
        pushed = await user.receive_json_from()
        self.assertEqual(pushed["readings"]["temperature"], 30.0)
        await device.disconnect()
        await user.disconnect()

    async def test_offline_gadget_is_reported_not_polled(self):
        user, _token = await self.connect_user()
        await user.receive_json_from()
        await user.send_json_to({"type": "refresh_state"})
        ack = await user.receive_json_from()
        self.assertEqual(ack["polled"], [])
        self.assertEqual(ack["skipped_offline"], [self.uid])
        await user.disconnect()

    async def test_rapid_second_refresh_is_refused(self):
        device, user = await self.setup_pair()
        await user.send_json_to({"type": "refresh_state"})
        await user.receive_json_from()  # ack
        await device.receive_json_from()  # state_request

        await user.send_json_to({"type": "refresh_state"})
        reply = await user.receive_json_from()
        self.assertEqual(reply["code"], "refresh_too_soon")
        await device.disconnect()
        await user.disconnect()

    @override_settings(STATE_REFRESH_MIN_INTERVAL_SECONDS=0)
    async def test_someone_elses_gadget_is_ignored(self):
        stranger = await database_sync_to_async(User.objects.create_user)(
            email="stranger@gmail.com", password="pass-12345", is_email_verified=True
        )
        other = await database_sync_to_async(Gadget.objects.create)(
            owner=stranger, product=self.product, gadget_type=self.gadget_type
        )
        _device, user = await self.setup_pair()

        await user.send_json_to(
            {"type": "refresh_state", "gadgets": [str(other.uid)]}
        )
        ack = await user.receive_json_from()
        self.assertEqual(ack["polled"], [])
        self.assertEqual(ack["skipped_offline"], [])
        await user.disconnect()

    @override_settings(STATE_REFRESH_MIN_INTERVAL_SECONDS=0)
    async def test_explicit_subset_only_polls_that_gadget(self):
        device, user = await self.setup_pair()
        await user.send_json_to(
            {"type": "refresh_state", "gadgets": [self.uid, "not-a-gadget"]}
        )
        ack = await user.receive_json_from()
        self.assertEqual(ack["polled"], [self.uid])
        self.assertEqual((await device.receive_json_from())["type"], "state_request")
        await device.disconnect()
        await user.disconnect()


class CommandTests(ConsumerTestCase):
    async def drain_state_request(self, device):
        """A user connecting polls every online gadget; skip that frame."""
        frame = await device.receive_json_from()
        assert frame["type"] == "state_request", frame

    async def issue(self, user, token, key, value, gadget_uid=None, request_id=None):
        request_id = request_id or secrets.token_hex(16)
        gadget_uid = gadget_uid or self.uid
        await user.send_json_to(
            signed(
                token,
                {
                    "type": "command",
                    "request_id": request_id,
                    "gadget": gadget_uid,
                    "key": key,
                    "value": value,
                },
                request_id,
                gadget_uid,
                key,
                canonical(value),
            )
        )
        return request_id

    async def test_command_reaches_the_device_and_the_ack_comes_back(self):
        device = await self.connect_device()
        await device.receive_json_from()
        user, token = await self.connect_user()
        await user.receive_json_from()
        await self.drain_state_request(device)

        with mock.patch("gadgets.tasks.expire_command.apply_async"):
            request_id = await self.issue(user, token, "power", True)
            frame = await device.receive_json_from()
            self.assertEqual(frame["type"], "command")
            self.assertEqual(frame["key"], "power")
            self.assertIs(frame["value"], True)
            self.assertEqual((await user.receive_json_from())["status"], "sent")

            await device.send_json_to(
                {
                    "type": "command_result",
                    "request_id": request_id,
                    "status": "ok",
                    "value": True,
                }
            )
            status = await user.receive_json_from()

        self.assertEqual(status["status"], Command.STATUS_ACKED)
        command = await database_sync_to_async(Command.objects.get)(
            request_id=request_id
        )
        self.assertEqual(command.status, Command.STATUS_ACKED)
        await device.disconnect()
        await user.disconnect()

    async def test_out_of_range_command_never_reaches_the_device(self):
        device = await self.connect_device()
        await device.receive_json_from()
        user, token = await self.connect_user()
        await user.receive_json_from()
        await self.drain_state_request(device)

        await self.issue(user, token, "brightness", 500)
        self.assertEqual((await user.receive_json_from())["code"], "invalid_value")
        self.assertTrue(await device.receive_nothing())
        self.assertEqual(await database_sync_to_async(Command.objects.count)(), 0)
        await device.disconnect()
        await user.disconnect()

    async def test_unknown_command_key_is_refused(self):
        user, token = await self.connect_user()
        await user.receive_json_from()
        await self.issue(user, token, "self_destruct", True)
        self.assertEqual(
            (await user.receive_json_from())["code"], "unknown_capability"
        )
        await user.disconnect()

    async def test_user_cannot_command_someone_elses_gadget(self):
        stranger = await database_sync_to_async(User.objects.create_user)(
            email="stranger@gmail.com", password="pass-12345", is_email_verified=True
        )
        user, token = await self.connect_user(stranger)
        await user.receive_json_from()
        await self.issue(user, token, "power", True)
        reply = await user.receive_json_from()
        self.assertEqual(reply["code"], "forbidden")
        self.assertEqual(await database_sync_to_async(Command.objects.count)(), 0)
        await user.disconnect()

    async def test_tampered_command_signature_is_refused(self):
        user, token = await self.connect_user()
        await user.receive_json_from()
        request_id = secrets.token_hex(16)
        frame = signed(
            token,
            {
                "type": "command",
                "request_id": request_id,
                "gadget": self.uid,
                "key": "brightness",
                "value": 10,
            },
            request_id,
            self.uid,
            "brightness",
            canonical(10),
        )
        # Signature was computed for 10; the wire says 100.
        frame["value"] = 100
        await user.send_json_to(frame)
        self.assertEqual((await user.receive_json_from())["code"], "bad_signature")
        await user.disconnect()

    async def test_replayed_command_is_refused(self):
        device = await self.connect_device()
        await device.receive_json_from()
        user, token = await self.connect_user()
        await user.receive_json_from()

        request_id = secrets.token_hex(16)
        frame = signed(
            token,
            {
                "type": "command",
                "request_id": request_id,
                "gadget": self.uid,
                "key": "power",
                "value": True,
            },
            request_id,
            self.uid,
            "power",
            canonical(True),
        )
        with mock.patch("gadgets.tasks.expire_command.apply_async"):
            await user.send_json_to(frame)
            await user.receive_json_from()  # sent
            await user.send_json_to(frame)  # exact replay
            self.assertEqual(
                (await user.receive_json_from())["code"], "bad_signature"
            )
        await device.disconnect()
        await user.disconnect()

    async def test_command_to_an_offline_device_fails_fast(self):
        user, token = await self.connect_user()
        await user.receive_json_from()
        await self.issue(user, token, "power", True)
        reply = await user.receive_json_from()
        self.assertEqual(reply["status"], Command.STATUS_FAILED)
        self.assertIn("offline", reply["error"].lower())
        await user.disconnect()


class OfflineSweepTests(ConsumerTestCase):
    async def test_sweeper_marks_a_silent_device_offline(self):
        from .tasks import sweep_offline_devices

        device = await self.connect_device()
        await device.receive_json_from()
        user, _token = await self.connect_user()
        await user.receive_json_from()  # auth.ok; the device was already up

        # Rewind the heartbeat well past the type's timeout.
        self.redis.hset(f"device:online:{self.uid}", "last_heartbeat", time.time() - 9999)
        await database_sync_to_async(sweep_offline_devices)()

        status = await user.receive_json_from()
        self.assertEqual(status["type"], "device.status")
        self.assertFalse(status["online"])
        self.assertEqual(status["reason"], "heartbeat_timeout")
        self.assertFalse(self.redis.exists(f"device:online:{self.uid}"))
        await device.disconnect()
        await user.disconnect()

    async def test_recent_heartbeat_survives_the_sweep(self):
        from .tasks import sweep_offline_devices

        device = await self.connect_device()
        await device.receive_json_from()
        await database_sync_to_async(sweep_offline_devices)()
        self.assertTrue(self.redis.exists(f"device:online:{self.uid}"))
        await device.disconnect()
