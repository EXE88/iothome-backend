# IoT Home — backend

Django + DRF for the REST side, Channels for the live side, Redis for presence
and the channel layer, Celery for the periodic sweeps, Zarinpal for payments.

```
accounts/   custom email-based user, OTP verification, JWT
purchases/  catalogue, orders, Zarinpal, gadget provisioning
gadgets/    device model + capabilities, WebSocket consumers, presence, tasks
core/       encryption, HMAC helpers, shared permissions
```

## Setting up

Redis and Postgres run in containers; Django runs on the host so you keep the
debugger and auto-reload. On Windows that means WSL2 + Docker Desktop:

```powershell
wsl --install
```

Reboot, then install Docker Desktop (`winget install Docker.DockerDesktop`, or
from docker.com) and start it once so it finishes wiring itself to WSL2.

```bash
docker compose up -d
```

That is Redis on 6379 and Postgres on 5432, both with healthchecks and
persistent volumes. Then:

```bash
pip install -r requirements-dev.txt
cp iothome/.env.example iothome/.env    # then fill in FIELD_ENCRYPTION_KEY
python manage.py migrate
python manage.py seed_catalog
python manage.py createsuperuser
python manage.py runserver              # Daphne/ASGI, so WebSockets work
```

SQLite is the default and is fine to start on; point `DATABASE_URL` at the
Postgres container when you want dev to match production.

Celery in two more terminals:

```bash
celery -A iothome worker -l info -P solo
```

```bash
celery -A iothome beat -l info
```

Tests need neither Redis nor Celery — fakeredis and the in-memory channel
layer stand in:

```bash
python manage.py test --settings=iothome.settings_test
```

## Deploying

`Dockerfile` builds one image that runs all three processes; `web` serves ASGI
with Daphne, and `worker`/`beat` run Celery from the same image. Daphne is
single-process, so scale by adding `web` replicas behind the load balancer.

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

Put nginx, Caddy or a cloud load balancer in front for TLS and proxy the
WebSocket upgrade through to port 8000 — devices should be talking `wss://`,
not `ws://`, since the handshake carries their HMAC.

Before the first real deploy: set `DEBUG=False`, a fresh `SECRET_KEY`, real
`ALLOWED_HOSTS`, `DATABASE_URL` on Postgres, and back up
`FIELD_ENCRYPTION_KEY` somewhere safe — without it every stored device secret
and Wi-Fi password is unrecoverable. `python manage.py check --deploy` should
come back clean.

## The three flows

**1. Buying.** `POST /api/purchases/checkout/` takes the product, the shipping
details and the buyer's Wi-Fi SSID/password. Stock is reserved immediately, the
price is frozen onto the order, and the response carries `payment_url`. The
browser goes to Zarinpal; Zarinpal returns it to
`/api/purchases/payments/verify/`, which verifies the transaction, marks the
order paid, creates one `Gadget` per unit — each with its own secret key and a
copy of the Wi-Fi credentials — and redirects to `FRONTEND_PAYMENT_RESULT_URL`.
A cancelled or failed payment returns the reserved stock.

Staff read `GET /api/gadgets/<uid>/provisioning/` at assembly time; that is the
only endpoint that reveals a secret key or a Wi-Fi password.

**2. The device.** Firmware holds `uid`, `secret_key`, SSID and password. On
boot it joins Wi-Fi and opens `ws://host/ws/device/<uid>/`, then proves itself
(below). After that it heartbeats, pushes telemetry, and answers commands.

**3. The user.** The browser authenticates over REST, then opens
`ws://host/ws/user/` and signs the handshake with its access token. It receives
telemetry and status changes for every gadget it owns, and signs each command
individually.

## Signature scheme

Every signed frame carries `timestamp`, `nonce` and `signature`.

```
signature = HMAC_SHA256(secret, "part1|part2|...|timestamp|nonce")   # hex
```

| Frame | secret | parts before timestamp/nonce |
|---|---|---|
| device auth | the device's `secret_key` | `device` (its uid) |
| user auth | the JWT access token | *(none)* |
| user command | the JWT access token | `request_id`, `gadget`, `key`, `canonical(value)` |

`canonical(value)` is compact JSON: `true`, `100`, `"warm"` — not Python's
`str()`. Empty parts stay in place, so `a||b` never collides with a field that
contains a `|`.

Three things have to hold: the timestamp within
`SIGNATURE_MAX_SKEW_SECONDS` (60) of server time, the nonce unused (tracked in
Redis for twice that window), and the HMAC matching. A device gets
`WS_AUTH_TIMEOUT_SECONDS` to finish the handshake, and any connection is cut
above `WS_MAX_FRAMES_PER_MINUTE`.

`python manage.py simulate_device --uid <uuid> --secret <hex>
--telemetry-interval 5` is a working client and the reference for the firmware.

## Message types

Device → server: `auth`, `heartbeat`, `telemetry`, `command_result`.
Server → device: `auth.ok`, `command`, `error`.
User → server: `auth`, `command`, `heartbeat`.
Server → user: `auth.ok`, `telemetry`, `device.status`, `command.status`,
`pong`, `error`.

`auth.ok` for a device includes its capability contract; `auth.ok` for a user
includes every gadget it owns with a live `online` flag. Full payload shapes
are in `gadgets/protocol.py`.

## Capabilities

A `GadgetType` owns a list of `Capability` rows — one per readable value
(`direction=telemetry`) or accepted command (`direction=command`), each with a
`value_type` (`bool`/`int`/`float`/`string`/`enum`), optional range, unit and
choices. Everything crossing a socket in either direction is coerced and
range-checked against them, so a device cannot inject a string into a numeric
series and a user cannot push a brightness of 500. The frontend reads the same
contract from `GET /api/gadgets/types/` to know how to render controls.

Two shapes are covered: types that stream (`mode=telemetry`, e.g. thermometer)
and types that only answer (`mode=action`, e.g. lamp). Both heartbeat.

## Presence and offline detection

A connected device is registered in Redis (`devices:online` plus
`device:online:<uid>`) with its own timeout, taken from its type's heartbeat
interval times the missed-beat allowance. Every heartbeat refreshes it.
`sweep_offline_devices` runs every 30s, drops anything silent past its timeout,
closes the socket if it is somehow still alive, and pushes `device.status` to
the owner. A clean disconnect removes the entry immediately. The Redis key also
carries its own TTL, so a worker dying mid-connection cannot leave a ghost.

Commands that go unanswered for `COMMAND_TIMEOUT_SECONDS` are closed out by
`expire_command`, with `expire_stale_commands` as the backstop.

## Notes

- Device secret keys and Wi-Fi passwords are Fernet-encrypted at rest
  (`FIELD_ENCRYPTION_KEY`). Lose that key and they are unrecoverable; changing
  it invalidates every stored secret.
- Commands are issued **only** over the WebSocket — there is no REST endpoint
  that sends one, because that would bypass the per-command signature.
  `GET /api/gadgets/<uid>/commands/` is the read-only audit trail.
- Only Gmail addresses register, canonicalised so `a.b+tag@gmail.com` and
  `ab@gmail.com` cannot become two accounts. Codes are stored as SHA-256, are
  single-use, expire in 10 minutes and allow 5 attempts.
- Email is on the console backend; point `EMAIL_BACKEND` at SMTP when ready.
- The channel layer is `channels_redis.pubsub`, not `channels_redis.core`. The
  core layer parks on a `BRPOP` that redis-py 8 aborts on its socket timeout,
  which drops idle device sockets with a 1011 after a few seconds.

## Still to do

- Swap SQLite for Postgres (`DATABASE_URL`) before any real traffic.
- Blacklist refresh tokens on logout (`simplejwt.token_blacklist`).
- Down-sample telemetry instead of only pruning at 90 days.
- Serve behind TLS: `wss://` and a `Secure` cookie posture.
