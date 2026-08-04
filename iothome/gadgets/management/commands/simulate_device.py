"""Pretend to be an ESP32. Doubles as the reference for the firmware.

    python manage.py simulate_device --uid <uuid> --secret <hex>

The handshake, heartbeat cadence and telemetry frames here are exactly what
the real device has to implement.
"""

import asyncio
import hashlib
import hmac
import json
import random
import secrets
import time

import websockets
from django.core.management.base import BaseCommand


def sign(secret, *parts):
    message = "|".join("" if p is None else str(p) for p in parts)
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


class Command(BaseCommand):
    help = "Connect to the device WebSocket as a fake gadget."

    def add_arguments(self, parser):
        parser.add_argument("--uid", required=True)
        parser.add_argument("--secret", required=True)
        parser.add_argument("--host", default="localhost:8000")
        parser.add_argument("--scheme", default="ws", choices=["ws", "wss"])
        parser.add_argument(
            "--telemetry-interval",
            type=float,
            default=0,
            help="Seconds between fake readings; 0 disables telemetry.",
        )
        parser.add_argument("--heartbeat-interval", type=float, default=30)

    def handle(self, *args, **options):
        try:
            asyncio.run(self.run(options))
        except KeyboardInterrupt:
            self.stdout.write("stopped")

    async def run(self, options):
        url = f"{options['scheme']}://{options['host']}/ws/device/{options['uid']}/"
        self.stdout.write(f"connecting to {url}")
        async with websockets.connect(url) as socket:
            await self.authenticate(socket, options)
            await asyncio.gather(
                self.heartbeat_loop(socket, options["heartbeat_interval"]),
                self.telemetry_loop(socket, options["telemetry_interval"]),
                self.read_loop(socket),
            )

    async def authenticate(self, socket, options):
        uid, secret = options["uid"], options["secret"]
        timestamp = int(time.time())
        nonce = secrets.token_hex(8)
        await socket.send(
            json.dumps(
                {
                    "type": "auth",
                    "device": uid,
                    "timestamp": timestamp,
                    "nonce": nonce,
                    "signature": sign(secret, uid, timestamp, nonce),
                    "firmware_version": "sim-1.0",
                }
            )
        )
        reply = json.loads(await socket.recv())
        if reply.get("type") != "auth.ok":
            raise SystemExit(f"handshake refused: {reply}")
        self.stdout.write(self.style.SUCCESS("authenticated"))
        self.capabilities = reply.get("capabilities", {})
        # Real firmware boots with a known state and keeps it across the
        # connection; nothing is sent until someone asks.
        self.state = {
            key: self.default_for(spec)
            for key, spec in self.capabilities.get("telemetry", {}).items()
        }

    def default_for(self, spec):
        if spec["value_type"] == "bool":
            return False
        if spec["value_type"] in ("int", "float"):
            low = spec["min"] if spec["min"] is not None else 0
            return int(low) if spec["value_type"] == "int" else float(low)
        if spec["value_type"] == "enum" and spec["choices"]:
            return spec["choices"][0]
        return ""

    async def heartbeat_loop(self, socket, interval):
        while True:
            await socket.send(json.dumps({"type": "heartbeat"}))
            await asyncio.sleep(interval)

    async def telemetry_loop(self, socket, interval):
        if interval <= 0:
            return
        while True:
            await asyncio.sleep(interval)
            readings = {}
            for key, spec in self.capabilities.get("telemetry", {}).items():
                if spec["value_type"] in ("int", "float"):
                    low = spec["min"] if spec["min"] is not None else 0
                    high = spec["max"] if spec["max"] is not None else 100
                    value = round(random.uniform(low, min(high, low + 40)), 2)
                    readings[key] = int(value) if spec["value_type"] == "int" else value
                elif spec["value_type"] == "bool":
                    readings[key] = random.choice([True, False])
            if readings:
                self.state.update(readings)
                await socket.send(
                    json.dumps({"type": "telemetry", "readings": readings})
                )
                self.stdout.write(f"-> telemetry {readings}")

    async def read_loop(self, socket):
        async for raw in socket:
            message = json.loads(raw)
            kind = message.get("type")
            if kind == "command":
                self.stdout.write(f"<- command {message['key']}={message['value']}")
                # A real device would drive the hardware here, then report back.
                await socket.send(
                    json.dumps(
                        {
                            "type": "command_result",
                            "request_id": message["request_id"],
                            "status": "ok",
                            "value": message["value"],
                        }
                    )
                )
                # Echo the new state only for keys the type also reports as
                # telemetry — anything else would be rejected as unknown.
                if message["key"] in self.capabilities.get("telemetry", {}):
                    self.state[message["key"]] = message["value"]
                    await socket.send(
                        json.dumps(
                            {
                                "type": "telemetry",
                                "readings": {message["key"]: message["value"]},
                            }
                        )
                    )
            elif kind == "state_request":
                # An owner opened a dashboard. Report where we stand.
                self.stdout.write(f"<- state_request, replying {self.state}")
                if self.state:
                    await socket.send(
                        json.dumps({"type": "telemetry", "readings": self.state})
                    )
            elif kind == "error":
                self.stdout.write(self.style.ERROR(f"<- error {message}"))
