#!/usr/bin/env python3
"""Fake ESP32 units, for driving the panel without owning any hardware.

Run it and it behaves the way the real firmware has to: it performs the HMAC
handshake, keeps a heartbeat, holds its own state, answers commands by
actually changing that state, and reports itself when the server asks. Every
frame in and out is printed.

    cd D:\\PythonFiles\\iothome\\backend
    env\\Scripts\\python.exe simulators\\simulate.py

Only ``websockets`` is needed — this is a plain script, not a Django command,
so it can be copied to another machine and pointed at a deployed server.

    python -m pip install websockets

Everything you might change lives in the SETTINGS block below. Deliberately
no .env: a device's identity is burned into it, and pretending otherwise here
would misrepresent what the firmware does.

Useful flags:

    --only lamp              run one device (matches on its name)
    --list                   print the configured devices and exit
    --server ws://host:8000  override SERVER for one run
    --quiet                  drop the frame-by-frame lines, keep events
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import random
import secrets
import sys
import time

try:
    import websockets
except ImportError:  # pragma: no cover - a setup problem, not a runtime one
    sys.exit("websockets is not installed. Run: python -m pip install websockets")


# ===========================================================================
# SETTINGS
#
# Replace the uid/secret pairs with your own. `provision_gadget` prints a new
# pair:
#
#   cd backend\iothome
#   ..\env\Scripts\python.exe manage.py provision_gadget --email you@gmail.com --type camera
# ===========================================================================

#: Where Django's ASGI server is. Use wss:// once it is behind TLS.
SERVER = "ws://127.0.0.1:8000"

#: The units this process pretends to be. `name` is only for the log column.
#:
#: `telemetry_interval` is how often the device speaks unprompted, in seconds;
#: 0 means it only answers. A lamp has no reason to report on a timer — it
#: reports when it changes and when it is asked — while a thermometer is a
#: sensor and does nothing else.
DEVICES = [
    {
        "name": "thermometer",
        "uid": "c5f1525c-4bc2-4927-a7e1-cf454bec89f1",
        "secret": "b866b9377dc8cd5e080eaa89285c71724aa6c8a69cf742c8ceb02ba06a9442f3",
        "telemetry_interval": 4,
    },
    {
        "name": "lamp",
        "uid": "1703d3ce-ebc3-4843-8c53-f38b493c0b4a",
        "secret": "c38d779fac183dfb91cca67722f10714248a2ce0220c4633ed575a76ae52c1ac",
        "telemetry_interval": 0,
    },
    {
        "name": "camera",
        "uid": "70cb7356-0128-47e8-8ec8-fc6551c5e475",
        "secret": "a7f2a1e31e21645e7cee3bba5d4c781d1067f87f316c19cb083b05360c4265db",
        "telemetry_interval": 9,
    },
]

#: Seconds between heartbeats. The server decides a unit is gone after the
#: gadget type's own timeout, which is comfortably longer than this.
HEARTBEAT_INTERVAL = 30

#: Firmware version reported at the handshake; it shows up in the panel.
FIRMWARE_VERSION = "sim-2.0"

#: How a sensor's value moves between readings. Keyed by capability key, with
#: a fallback for anything the server declares that is not listed here.
#:
#: A real sensor drifts; it does not teleport around its whole range. `start`
#: is where it boots, `step` the most it can move in one reading, and
#: `floor`/`ceiling` a plausible band inside the capability's own limits.
SENSOR_PROFILES = {
    "temperature": {"start": 21.5, "step": 0.4, "floor": 16.0, "ceiling": 29.0},
    "humidity": {"start": 44.0, "step": 1.5, "floor": 28.0, "ceiling": 68.0},
    # Motion is not a dial. It is quiet, then it is not, then it is quiet
    # again — so it gets its own model below rather than a drift band.
    "motion": {"probability": 0.18, "hold_readings": 2},
}
DEFAULT_SENSOR_PROFILE = {"step": 1.0}

#: Reconnect backoff, in seconds. Grows to the last value and stays there.
RECONNECT_BACKOFF = [1, 2, 5, 10, 30]


# ===========================================================================
# Nothing below here needs editing.
# ===========================================================================

log = logging.getLogger("sim")


def sign(secret: str, *parts) -> str:
    """The scheme in core/signatures.py: HMAC-SHA256 over pipe-joined parts."""
    message = "|".join("" if part is None else str(part) for part in parts)
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def show(value) -> str:
    """Readings as a person reads them, not as Python repr."""
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def show_all(readings: dict) -> str:
    return " ".join(f"{key}={show(value)}" for key, value in sorted(readings.items()))


class Sensor:
    """A telemetry key nobody can set — it changes on its own.

    Two shapes cover everything the catalogue has: a number that drifts inside
    a band, and a boolean that fires occasionally and then goes quiet.
    """

    def __init__(self, key: str, spec: dict):
        self.key = key
        self.value_type = spec["value_type"]
        profile = SENSOR_PROFILES.get(key, DEFAULT_SENSOR_PROFILE)

        low = spec["min"] if spec["min"] is not None else 0.0
        high = spec["max"] if spec["max"] is not None else 100.0
        self.floor = max(low, profile.get("floor", low))
        self.ceiling = min(high, profile.get("ceiling", high))
        if self.floor > self.ceiling:
            self.floor, self.ceiling = low, high

        self.step = profile.get("step", DEFAULT_SENSOR_PROFILE["step"])
        self.probability = profile.get("probability", 0.15)
        self.hold_readings = profile.get("hold_readings", 1)
        self._held = 0

        start = profile.get("start", (self.floor + self.ceiling) / 2)
        self.value = self._coerce(min(max(start, self.floor), self.ceiling))
        if self.value_type == "bool":
            self.value = False

    def _coerce(self, number):
        return int(round(number)) if self.value_type == "int" else round(number, 2)

    def next(self):
        if self.value_type == "bool":
            # Stay on for a couple of readings once it fires, so the panel has
            # time to show it rather than flickering between two frames.
            if self._held > 0:
                self._held -= 1
                self.value = True
            else:
                self.value = random.random() < self.probability
                if self.value:
                    self._held = self.hold_readings - 1
            return self.value

        drift = random.uniform(-self.step, self.step)
        self.value = self._coerce(
            min(max(self.value + drift, self.floor), self.ceiling)
        )
        return self.value


class Actuator:
    """A key the owner sets. It holds its value until told otherwise.

    This is the whole point of the rewrite: the previous simulator randomised
    every boolean it could report, which meant the camera switched its own
    recording off a few seconds after it was switched on.
    """

    def __init__(self, key: str, spec: dict):
        self.key = key
        self.value_type = spec["value_type"]
        self.spec = spec
        self.value = self._boot_value()

    def _boot_value(self):
        if self.value_type == "bool":
            return False
        if self.value_type in ("int", "float"):
            low = self.spec["min"] if self.spec["min"] is not None else 0
            return int(low) if self.value_type == "int" else float(low)
        if self.value_type == "enum" and self.spec["choices"]:
            return self.spec["choices"][0]
        return ""

    def apply(self, value):
        self.value = value
        return self.value


class Device:
    """One simulated unit, from handshake to reconnect."""

    def __init__(self, config: dict, server: str):
        self.name = config["name"]
        self.uid = config["uid"]
        self.secret = config["secret"]
        self.telemetry_interval = config.get("telemetry_interval", 0)
        self.server = server.rstrip("/")

        self.sensors: dict[str, Sensor] = {}
        self.actuators: dict[str, Actuator] = {}
        #: Keys the server will accept in a telemetry frame. Anything else in
        #: one gets the whole frame rejected as an unknown capability.
        self.telemetry_keys: set[str] = set()

    # -- logging ----------------------------------------------------------

    def say(self, message, level=logging.INFO):
        log.log(level, message, extra={"device": self.name})

    def sent(self, message):
        self.say(f"-> {message}", logging.DEBUG)

    def got(self, message):
        self.say(f"<- {message}", logging.DEBUG)

    # -- lifecycle --------------------------------------------------------

    async def run(self):
        """Connect, and keep connecting. A power cut is not a crash."""
        attempt = 0
        while True:
            url = f"{self.server}/ws/device/{self.uid}/"
            try:
                self.say(f"connecting to {url}")
                async with websockets.connect(url) as socket:
                    await self.handshake(socket)
                    attempt = 0
                    await asyncio.gather(
                        self.heartbeat_loop(socket),
                        self.telemetry_loop(socket),
                        self.read_loop(socket),
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                delay = RECONNECT_BACKOFF[min(attempt, len(RECONNECT_BACKOFF) - 1)]
                self.say(f"disconnected ({exc}); retrying in {delay}s", logging.WARNING)
                attempt += 1
                await asyncio.sleep(delay)

    async def send(self, socket, frame: dict):
        await socket.send(json.dumps(frame))

    async def handshake(self, socket):
        timestamp = int(time.time())
        nonce = secrets.token_hex(8)
        await self.send(
            socket,
            {
                "type": "auth",
                "device": self.uid,
                "timestamp": timestamp,
                "nonce": nonce,
                "signature": sign(self.secret, self.uid, timestamp, nonce),
                "firmware_version": FIRMWARE_VERSION,
            },
        )
        self.sent("auth")

        reply = json.loads(await socket.recv())
        if reply.get("type") != "auth.ok":
            # A wrong secret, a disabled unit, or a clock more than a minute
            # out. Raising here lets the reconnect loop back off instead of
            # hammering a server that will keep saying no.
            raise RuntimeError(f"handshake refused: {reply}")

        self.build_hardware(reply.get("capabilities", {}))
        self.say(
            "authenticated - "
            f"{len(self.actuators)} control(s), {len(self.sensors)} sensor(s)"
        )
        if self.state:
            self.say(f"boot state: {show_all(self.state)}")

    def build_hardware(self, capabilities: dict):
        """Work out what this unit is from what the server says it accepts.

        A key that appears as both a command and a telemetry reading is an
        actuator: the owner sets it and the device reports it back. A key that
        is telemetry only is a sensor. A command with no telemetry twin is a
        write-only control, which the panel could never show the state of —
        worth a warning, because it is almost always a catalogue mistake.
        """
        telemetry = capabilities.get("telemetry", {})
        commands = capabilities.get("command", {})
        self.telemetry_keys = set(telemetry)

        for key, spec in commands.items():
            self.actuators[key] = Actuator(key, spec)
            if key not in telemetry:
                self.say(
                    f"'{key}' is accepted as a command but not reported as "
                    "telemetry, so its state cannot survive a reload",
                    logging.WARNING,
                )

        for key, spec in telemetry.items():
            if key not in self.actuators:
                self.sensors[key] = Sensor(key, spec)

    @property
    def state(self) -> dict:
        """Everything this unit can report, right now.

        Only keys the type declares as telemetry — the server rejects a frame
        carrying anything else, and rightly so.
        """
        readings = {key: sensor.value for key, sensor in self.sensors.items()}
        for key, actuator in self.actuators.items():
            if key in self.telemetry_keys:
                readings[key] = actuator.value
        return readings

    # -- loops ------------------------------------------------------------

    async def heartbeat_loop(self, socket):
        while True:
            await self.send(socket, {"type": "heartbeat"})
            self.sent("heartbeat")
            await asyncio.sleep(HEARTBEAT_INTERVAL)

    async def telemetry_loop(self, socket):
        """Only sensors go on the timer.

        An actuator's value has not changed just because time passed, and
        re-sending it would be the device arguing with the last command.
        """
        if self.telemetry_interval <= 0 or not self.sensors:
            return
        while True:
            await asyncio.sleep(self.telemetry_interval)
            readings = self.sample_sensors()
            if readings:
                await self.send(
                    socket, {"type": "telemetry", "readings": readings}
                )
                self.sent(f"telemetry {show_all(readings)}")

    def sample_sensors(self) -> dict:
        readings = {}
        for key, sensor in self.sensors.items():
            # A camera with its alerts switched off does not send motion. The
            # switch has to mean something, or it is decoration.
            gate = self.actuators.get(f"{key}_alerts")
            if gate is not None and gate.value is False:
                continue
            readings[key] = sensor.next()
        return readings

    async def read_loop(self, socket):
        async for raw in socket:
            try:
                message = json.loads(raw)
            except ValueError:
                self.say(f"ignoring unparseable frame: {raw!r}", logging.WARNING)
                continue
            await self.on_message(socket, message)

    async def on_message(self, socket, message: dict):
        kind = message.get("type")

        if kind == "command":
            await self.on_command(socket, message)
        elif kind == "state_request":
            self.got("state_request")
            await self.report_state(socket, "polled")
        elif kind == "pong":
            self.got("pong")
        elif kind == "error":
            self.say(
                f"server refused a frame: {message.get('code')} — "
                f"{message.get('detail')}",
                logging.ERROR,
            )
        else:
            self.got(f"{kind} {message}")

    async def on_command(self, socket, message: dict):
        key, value = message.get("key"), message.get("value")
        request_id = message.get("request_id")
        self.got(f"command {key}={show(value)}")

        actuator = self.actuators.get(key)
        if actuator is None:
            await self.send(
                socket,
                {
                    "type": "command_result",
                    "request_id": request_id,
                    "status": "error",
                    "error": f"this unit has no '{key}'",
                },
            )
            self.say(f"refused unknown command '{key}'", logging.WARNING)
            return

        # Where real firmware would drive the hardware and wait for it. The
        # delay is what makes the panel's "waiting for the device" state
        # visible instead of instantaneous.
        await asyncio.sleep(random.uniform(0.15, 0.5))
        actuator.apply(value)

        await self.send(
            socket,
            {
                "type": "command_result",
                "request_id": request_id,
                "status": "ok",
                "value": actuator.value,
            },
        )
        self.sent(f"command_result ok {key}={show(actuator.value)}")

        # Report the change as an ordinary reading. The panel never assumes a
        # command worked; it waits for the hardware to say so.
        if key in self.telemetry_keys:
            await self.send(
                socket, {"type": "telemetry", "readings": {key: actuator.value}}
            )
            self.sent(f"telemetry {key}={show(actuator.value)}")

        # Switching the alerts off should silence the sensor immediately,
        # rather than after one more reading gets through.
        if key.endswith("_alerts") and value is False:
            sensor = self.sensors.get(key[: -len("_alerts")])
            if sensor is not None:
                sensor.value = False

    async def report_state(self, socket, why: str):
        readings = self.state
        if not readings:
            self.say("nothing to report", logging.DEBUG)
            return
        await self.send(socket, {"type": "telemetry", "readings": readings})
        self.sent(f"telemetry ({why}) {show_all(readings)}")


class DeviceFormatter(logging.Formatter):
    """Keeps the device name in its own column so three units stay readable."""

    def format(self, record):
        record.device = getattr(record, "device", "-")
        return super().format(record)


def configure_logging(quiet: bool):
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        DeviceFormatter(
            "%(asctime)s %(device)-12s %(message)s", datefmt="%H:%M:%S"
        )
    )
    log.addHandler(handler)
    log.setLevel(logging.INFO if quiet else logging.DEBUG)
    log.propagate = False


async def main(devices):
    tasks = [asyncio.create_task(device.run()) for device in devices]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass


def parse_args():
    parser = argparse.ArgumentParser(
        description="Simulate the SmartLife devices listed in this file."
    )
    parser.add_argument("--server", default=SERVER, help=f"default: {SERVER}")
    parser.add_argument(
        "--only",
        action="append",
        metavar="NAME",
        help="run only this device; repeatable",
    )
    parser.add_argument("--list", action="store_true", help="list devices and exit")
    parser.add_argument(
        "--quiet", action="store_true", help="hide the per-frame lines"
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    selected = DEVICES
    if args.only:
        wanted = {name.lower() for name in args.only}
        selected = [d for d in DEVICES if d["name"].lower() in wanted]
        if not selected:
            sys.exit(f"no device named {', '.join(sorted(wanted))} in DEVICES")

    if args.list:
        for entry in DEVICES:
            print(f"{entry['name']:<12} {entry['uid']}")
        sys.exit(0)

    configure_logging(args.quiet)
    log.info("server %s", args.server, extra={"device": "-"})

    try:
        asyncio.run(main([Device(entry, args.server) for entry in selected]))
    except KeyboardInterrupt:
        log.info("stopped", extra={"device": "-"})
