<div align="center">

# 🏠 SmartLife — Backend

**The API, the device network and the shop behind a smart home with no setup step.**

Django + DRF for REST, Channels for the live socket, Redis for presence,
Celery for the sweeps, Zarinpal for payments — and a signed binary-free
protocol that real ESP32 firmware can speak.

[![Django](https://img.shields.io/badge/Django-6.0-092E20?logo=django&logoColor=white)](https://www.djangoproject.com)
[![DRF](https://img.shields.io/badge/DRF-3.17-A30000?logo=django&logoColor=white)](https://www.django-rest-framework.org)
[![Channels](https://img.shields.io/badge/Channels-4.3-092E20?logo=django&logoColor=white)](https://channels.readthedocs.io)
[![Celery](https://img.shields.io/badge/Celery-5.6-37814A?logo=celery&logoColor=white)](https://docs.celeryq.dev)
[![Redis](https://img.shields.io/badge/Redis-8-DC382D?logo=redis&logoColor=white)](https://redis.io)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org)

[![Tests](https://img.shields.io/badge/tests-131%20passing-2b8a3e)](#-checks)
[![Auth](https://img.shields.io/badge/auth-JWT%20%2B%20per--command%20HMAC-c2255c)](#-signature-scheme)
[![Encryption](https://img.shields.io/badge/secrets-Fernet%20at%20rest-6741d9)](#-notes)
[![Payments](https://img.shields.io/badge/payments-Zarinpal-0b7285)](#-the-three-flows)

</div>

---

## 📑 Contents

- [The apps](#-the-apps)
- [Running it](#-running-it)
- [Background jobs](#-background-jobs)
- [The three flows](#-the-three-flows)
- [Signature scheme](#-signature-scheme)
- [Message types](#-message-types)
- [Capabilities](#-capabilities)
- [Presence and offline detection](#-presence-and-offline-detection)
- [What gets stored, and what does not](#-what-gets-stored-and-what-does-not)
- [Deploying](#-deploying)
- [Notes](#-notes)
- [Still to do](#-still-to-do)

---

## 🧱 The apps

| Package | Responsibility |
|---|---|
| `accounts/` | Gmail-only user, emailed OTP verification, JWT with server-side logout |
| `purchases/` | Catalogue, multi-item orders, Zarinpal, gadget provisioning |
| `gadgets/` | Device model + capability contract, WebSocket consumers, presence |
| `core/` | Fernet encryption, HMAC helpers, shared permissions |
| `simulators/` | A standalone fake ESP32 — no Django import, runs anywhere |


## 🚀 Running it

Redis and Postgres run in containers; Django runs on the host so you keep the
debugger and auto-reload. On Windows that means WSL2 + Docker Desktop:

```powershell
wsl --install
```

Reboot, then install Docker Desktop (`winget install Docker.DockerDesktop`, or
from docker.com) and start it once so it finishes wiring itself to WSL2. It
does not start with Windows unless you turn that on in its settings, so
`docker compose` fails after every reboot until you open it.

### One-time setup

```bash
cd D:\PythonFiles\iothome\backend && env\Scripts\python.exe -m pip install -r requirements-dev.txt
```

```bash
cd D:\PythonFiles\iothome\backend && cp iothome\.env.example iothome\.env
```

Then fill in `.env`. The one that matters is `FIELD_ENCRYPTION_KEY`: change it
later and every stored device secret and Wi-Fi password becomes unreadable.

```bash
cd D:\PythonFiles\iothome\backend && env\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

`ZARINPAL_MERCHANT_ID` accepts any UUID while `ZARINPAL_SANDBOX=True` — see
[the sandbox docs](https://www.zarinpal.com/docs/paymentGateway/sandBox.html).
Generate one with `python -c "import uuid; print(uuid.uuid4())"`. Swap in the
real merchant id from the Zarinpal panel before going live.

The frontend has its own `.env`; copy `front/.env.example` to `front/.env`.
Its `NEXT_PUBLIC_API_BASE` must be an address the *browser* can reach, and
`REFRESH_COOKIE_MAX_AGE_DAYS` must equal this repo's
`REFRESH_TOKEN_LIFETIME_DAYS`.

```bash
cd D:\PythonFiles\iothome\front && npm install && npm run frames
```

`npm run frames` builds the landing page's render sequences out of `assets/`;
they are gitignored because they are derived.

### The pieces

| Piece | Port | What it does | Needed for |
|---|---|---|---|
| Postgres | 5432 | Accounts, orders, gadgets, command history | everything |
| Redis | 6379 | Which devices are online, replay nonces, the channel layer | everything live |
| Django (ASGI) | 8000 | REST **and** both WebSocket endpoints | everything |
| Next.js | 3000 | Landing, shop, checkout, auth, panel | the UI |
| Device simulators | — | Pretend to be ESP32s | seeing the panel do anything |
| Celery worker + beat | — | Marks silent devices offline, times out commands | detecting a device that dies *without* closing its socket |

Django serves HTTP and WebSocket from one process — there is no separate
socket server to start.

### Starting up

Order matters only for the first two.

**1 — Postgres and Redis.** Start Docker Desktop first and wait for the whale
icon to settle.

```bash
cd D:\PythonFiles\iothome\backend && docker compose up -d && docker compose ps
```

**2 — Django.**

```bash
cd D:\PythonFiles\iothome\backend\iothome && ..\env\Scripts\python.exe manage.py runserver 0.0.0.0:8000
```

`0.0.0.0` rather than the default so a phone on the same network can reach it.
**Keep this terminal visible** — verification and password-reset emails are
printed here, not sent anywhere. Confirm
<http://127.0.0.1:8000/api/health/> returns `{"status": "ok"}`.

First run only:

```bash
cd D:\PythonFiles\iothome\backend\iothome && ..\env\Scripts\python.exe manage.py migrate && ..\env\Scripts\python.exe manage.py seed_catalog && ..\env\Scripts\python.exe manage.py createsuperuser
```

**3 — Next.js.**

```bash
cd D:\PythonFiles\iothome\front && npm run dev
```

Open <http://127.0.0.1:3000>. A bare `/` redirects to `/fa` or `/en` depending
on the browser's language.

**4 — Device simulators.** One process runs all of them.

```bash
cd D:\PythonFiles\iothome\backend && env\Scripts\python.exe simulators\simulate.py
```

It prints every frame in and out. `--only lamp` runs one, `--list` shows what
is configured, `--quiet` drops the per-frame lines. The uid/secret pairs and
the server address live in a SETTINGS block at the top of that file — it is a
plain script with no Django import, so it can be copied to another machine and
pointed at a deployed server. Run **one** copy at a time: two processes
claiming the same device uid evict each other in a loop (close code 4006).

**5 — Celery.**

```bash
cd D:\PythonFiles\iothome\backend\iothome && ..\env\Scripts\python.exe -m celery -A iothome worker -l info -P solo
```

```bash
cd D:\PythonFiles\iothome\backend\iothome && ..\env\Scripts\python.exe -m celery -A iothome beat -l info
```

Full reference — what each job does, and how to switch one off — is in
[Background jobs](#-background-jobs) below. In short, two things only happen
if these are running:

- A device that dies *without* closing its socket — power cut, cable pulled —
  is only noticed by the sweeper. Ctrl+C on a simulator is a clean disconnect
  and is detected instantly either way.
- Abandoned checkouts are only released by `expire_stale_orders`. Stock is
  reserved the moment an order is created, so without this a buyer who closes
  the gateway tab holds those units indefinitely. See *Abandoned checkouts*
  below.

### Test accounts

| Email | Password | What it shows |
|---|---|---|
| `panel@gmail.com` | `panel-test-pass` | Three devices, all controllable |
| `empty@gmail.com` | `panel-test-pass` | The empty state, for a user who owns nothing |

Both are verified already. To make your own, sign up at `/fa/signup` and read
the 6-digit code out of the Django terminal.

### Worth trying

1. **Log in** at `/fa/login` and watch the header: «در حال اتصال…», then
   «متصل» once the signed WebSocket handshake is accepted.
2. **Watch the thermometer.** Its numbers drift every 4 seconds. Nothing is
   polling — the device pushes and the server relays.
3. **Press a lamp control.** The switch does not move when you press it. It
   moves when the device answers. The panel shows hardware state, never a
   hopeful guess.
4. **Set a brightness, then reload.** It comes back. The server stores no
   state; the panel asks the device and the device answers.
5. **Kill a simulator with Ctrl+C.** Within a second that device goes grey and
   its controls lock. The others are unaffected. Restart it and it comes back
   on its own.
6. **Buy something.** `/fa/shop` → basket → checkout → the Zarinpal sandbox.
   One order can hold several products and settles as one payment.
7. **Open the same account in two tabs** and press a control in one. Both
   update.

### Making more devices

```bash
cd D:\PythonFiles\iothome\backend\iothome && ..\env\Scripts\python.exe manage.py provision_gadget --email panel@gmail.com --type camera
```

It prints a uid and secret to paste into `simulators/simulate.py`. `--type` is
one of `thermometer`, `smart-lamp`, `camera`. This skips payment, which is
what makes it a development tool.

### Checks

```bash
cd D:\PythonFiles\iothome\backend\iothome && ..\env\Scripts\python.exe manage.py test --settings=iothome.settings_test
```

Needs neither Redis nor Celery — fakeredis and an in-memory channel layer
stand in.

```bash
cd D:\PythonFiles\iothome\front && node scripts\panel-e2e.mjs fa
```

Drives a real browser against the running stack: logs in, waits for the signed
socket, presses every switch, confirms each device answered. Needs everything
above to be up.

```bash
cd D:\PythonFiles\iothome\front && npm run verify
```

Fails the build if anything would load a font, script or style from another
host, then builds.

### Testing on a phone

The browser talks to Django directly, so `localhost` would mean the phone
itself. With a machine address of `100.126.109.83`:

1. `backend\iothome\.env`:

   ```
   ALLOWED_HOSTS=localhost,127.0.0.1,100.126.109.83
   CORS_ALLOWED_ORIGINS=http://localhost:3000,http://100.126.109.83:3000
   ```

2. `front\.env`:

   ```
   NEXT_PUBLIC_API_BASE=http://100.126.109.83:8000
   DEV_ORIGINS=localhost,127.0.0.1,100.126.109.83
   ```

3. Restart both servers so they re-read their `.env`.

### Serving over plain http

Anything that is not https and not localhost is an **insecure context**, and
browsers withhold part of the Web Crypto API there. A VPS reached at
`http://<public-ip>` is the usual case, and it is the usual case for this to
be discovered late, because development happens on localhost where everything
is present.

Two APIs are affected and both have fallbacks in `front/src/lib/hmac.ts`:
`crypto.subtle`, which signs the socket handshake and every command, and
`crypto.randomUUID`, which mints the `request_id` pairing a command with its
answer. The fallbacks are built on `crypto.getRandomValues`, which carries no
such restriction. They are slower, not different — signatures are verified
byte-for-byte against Node's crypto.

Never call either API directly; use `hmacHex` and `randomUUID` from that
module. To check the whole path a bare-IP deployment takes:

```bash
cd D:\PythonFiles\iothome\front && npm run e2e:insecure
```

It strips both APIs before the app loads and then presses real controls
against the real backend. Put the site behind TLS for anything real — this
keeps a plain-http staging box working, it is not a substitute for https.

### When something is wrong

**Login answers 404.** `API_BASE_SERVER` is on `localhost`. Node resolves that
to `::1` first and `runserver 0.0.0.0:8000` listens on IPv4 only. Use
`127.0.0.1`.

**The panel says «ارتباط زنده برقرار نشد».** Redis is down, or Django is not
running. Check `docker compose ps` first.

**Devices show offline although a simulator is running.** Look at the
simulator's terminal — usually a wrong secret, a disabled gadget, or a second
copy of the script fighting it for the same uid.

**A control snaps back a moment after you set it.** The gadget type is missing
the telemetry twin for that command key; see the note at the top of
`seed_catalog.py`. Re-run `seed_catalog`.

**`docker compose up` cannot reach the daemon.** Docker Desktop is not
running.

**Emails never arrive.** They are not sent. `EMAIL_BACKEND` is the console
backend, so codes are printed in the Django terminal.

**Login is refused with "not verified".** Use the code from the Django
terminal at `/fa/verify`, or:

```bash
cd D:\PythonFiles\iothome\backend\iothome && ..\env\Scripts\python.exe manage.py shell -c "from django.contrib.auth import get_user_model; U=get_user_model(); U.objects.filter(email='you@gmail.com').update(is_email_verified=True)"
```

### Stopping

Ctrl+C each terminal, then:

```bash
cd D:\PythonFiles\iothome\backend && docker compose down
```

`down` keeps the data. `down -v` deletes the volumes, which means re-running
`migrate`, `seed_catalog` and `createsuperuser`.

## ⏱ Background jobs

Everything periodic runs on Celery. Nothing here is decoration: each one exists
because some state can otherwise get stuck, and the column that matters is
**"if it never runs"**.

Start the two processes:

```bash
cd D:\PythonFiles\iothome\backend\iothome && ..\env\Scripts\python.exe -m celery -A iothome worker -l info -P solo
```

```bash
cd D:\PythonFiles\iothome\backend\iothome && ..\env\Scripts\python.exe -m celery -A iothome beat -l info
```

`-P solo` is for Windows only; use the default pool (or `-c N`) on Linux.
The **worker** executes jobs, the **beat** decides when. You need both.

### The schedule

| Task | Runs | What it does | If it never runs |
|---|---|---|---|
| `gadgets.sweep_offline_devices` | 30s | Marks devices offline once they stop heartbeating past their type's timeout, closes the socket, tells the owner | A device that dies *without* closing its socket — power cut, cable pulled — stays "online" for ever. A clean Ctrl+C is still detected instantly |
| `gadgets.expire_stale_commands` | 60s | Closes out commands the device never acknowledged | A command whose per-command timer was lost sits `pending` for ever and its control stays stuck mid-flight |
| `gadgets.reconcile_last_seen` | 5 min | Copies Redis heartbeats into `Gadget.last_seen_at` | "Last seen" goes stale in the UI. Nothing live breaks — Redis is the live truth |
| `purchases.expire_stale_orders` | 5 min | Releases stock from checkouts abandoned past `PENDING_ORDER_TIMEOUT_MINUTES` — **after asking the gateway whether they were actually paid** | 🔴 Stock is reserved at checkout, so every closed gateway tab holds its units for ever and the shop slowly sells out of nothing |
| `purchases.reconcile_unverified_payments` | hourly | Asks Zarinpal for money it took that we never confirmed, and turns those into real orders | 🔴 A lost callback means the buyer paid and has no order. Zarinpal reverses it after 72h, so it self-heals financially — but silently, and the customer is left with neither |
| `accounts.purge_unverified_accounts` | daily 04:20 | Deletes signups that never entered their emailed code, after `UNVERIFIED_ACCOUNT_TTL_DAYS` | Abandoned rows accumulate, each holding an email address nobody can re-register |
| `accounts.purge_expired_otps` | daily 04:30 | Drops codes that can no longer be used | One dead row per signup, resend and reset, kept for the life of the system |
| `gadgets.prune_old_commands` | daily 04:00 | Drops command history past 90 days | Command history grows without bound |

`gadgets.expire_command` is the odd one out: it is **not** on the schedule. It
is queued with a countdown the moment a command is sent, and
`expire_stale_commands` is its backstop for when the broker loses it.

### Turning one off

The defaults above live in `iothome/celery.py`, but the live schedule is stored
in the database by `django-celery-beat`. So:

- **Permanently, in code** — comment the entry out of `beat_schedule` in
  `iothome/celery.py`, then delete its row under
  **Admin → Periodic Tasks**, because entries already written to the database
  are not removed by editing the file.
- **Temporarily, without a deploy** — **Admin → Periodic Tasks**, untick
  *Enabled*. Takes effect on the beat's next tick.
- **Change how often** — same screen: edit the interval or crontab. Preferred
  over editing the file, since the database is what beat actually reads.
- **Everything at once** — stop the beat process. The worker stays up, so
  commands still get their individual `expire_command` countdown.

### Running one by hand

Useful when debugging, and the only way to run them at all if you are not
running Celery:

```bash
cd D:\PythonFiles\iothome\backend\iothome && ..\env\Scripts\python.exe manage.py shell -c "from purchases.tasks import expire_stale_orders; print(expire_stale_orders())"
```

Every task is a plain function. Calling it directly runs it in-process,
synchronously, with no broker involved. `expire_stale_orders`,
`reconcile_unverified_payments`, `purge_unverified_accounts` and
`purge_expired_otps` all return a count of what they touched.

### Tuning

| Setting | Default | Governs |
|---|---|---|
| `PENDING_ORDER_TIMEOUT_MINUTES` | 30 | How long an unpaid order holds its stock. Keep it well clear of a real checkout — the sweep cannot tell an abandoned tab from someone still typing a card number |
| `UNVERIFIED_ACCOUNT_TTL_DAYS` | 7 | How long an unverified signup keeps its email address |
| `COMMAND_TIMEOUT_SECONDS` | 15 | How long a command waits for the device's ack |
| `DEFAULT_OFFLINE_TIMEOUT_SECONDS` | 180 | Fallback offline threshold for types that do not set their own |

---

## 🌐 Deploying

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

## 🔄 The three flows

**1. Buying.** `POST /api/purchases/checkout/` takes an `items` list — the
basket, one line per product — plus the shipping details and the buyer's Wi-Fi
SSID/password. Stock for every line is reserved in one transaction, so a
basket with one out-of-stock line reserves nothing at all; each line's price is
frozen onto it, and the response carries `payment_url`. One basket is one order
and one payment, however many different products it holds. The
browser goes to Zarinpal; Zarinpal returns it to
`/api/purchases/payments/verify/`, which verifies the transaction, marks the
order paid, creates one `Gadget` per unit across every line — each with its own secret key
and a copy of the Wi-Fi credentials — and redirects to
`FRONTEND_PAYMENT_RESULT_URL`.
A cancelled or failed payment returns the reserved stock.

Staff read `GET /api/gadgets/<uid>/provisioning/` at assembly time; that is the
only endpoint that reveals a secret key or a Wi-Fi password.

**Abandoned checkouts.** Closing the gateway tab sends nothing anywhere —
Zarinpal has no "the buyer left" callback — so from the server an abandoned
checkout and one still in progress look identical, and only time separates
them. `expire_stale_orders` runs every five minutes and takes any order still
unpaid after `PENDING_ORDER_TIMEOUT_MINUTES` (30). Before releasing anything it
**asks the gateway whether that authority was actually paid**, because the
nastiest version of this is the buyer who paid and *then* closed the tab;
cancelling that would take their money and give them nothing. Paid ones are
finalised and provisioned as usual, the rest are cancelled and their stock
goes back.

`reconcile_unverified_payments` runs hourly as the backstop: it asks Zarinpal
for transactions it settled that we never confirmed — a callback lost to a
restart, say — and turns them into real orders. Zarinpal reverses those after
72 hours, so this has to run more often than that.

If a sweep releases an order and the gateway then confirms it anyway, the
payment wins: the order is reinstated and the stock is taken back off the
shelf. If someone else bought the last unit in between, that is logged as an
error naming the order, because only a human can resolve it.

**Abandoned signups.** The user row has to exist before the address is proven —
the code has to be sent to somebody — so accounts that never verify are a
permanent fact of the system, not an edge case. An unverified account can do
nothing at all: it cannot log in, and every endpoint that matters sits behind
`IsEmailVerified`. Its only real effect is to reserve an email address.

That made it a trap. Coming back and signing up again answered "already
registered", and logging in answered "not verified" — so the address was
unusable by the only person entitled to it. Now registering an address that is
already waiting to be verified **re-sends the code** and returns 200 with
`code: verification_pending`, pointing the browser at the verify screen. It
never sets a password, because letting an unauthenticated request overwrite the
password of an existing account would be a takeover of any signup somebody else
had started; whoever owns the mailbox can use "forgot password", which also
marks the address verified.

`purge_unverified_accounts` then deletes anything still unverified after
`UNVERIFIED_ACCOUNT_TTL_DAYS`, releasing the address. Accounts with an order or
a gadget attached are skipped regardless of the flag.

**Sessions.** Access tokens last a day, refresh tokens seven, and rotation is
**off** — so the seven days are absolute rather than sliding, and a user who
signed in a week ago signs in again. `POST /api/auth/logout/` blacklists the
refresh token, which is what makes logging out apply to every browser holding
it rather than only the one that dropped its cookie. Run
`manage.py flushexpiredtokens` occasionally to keep the blacklist table
small.

**One hostname.** `localhost:3000` and `127.0.0.1:3000` are one site to a
person and two to a browser: separate cookie jars, so separate logins. Sign
out of one, sign in as somebody else, type the other into the address bar, and
the first account is still there — which reads as the app swapping accounts by
itself. Two things guard against it: the frontend pins a host via
`NEXT_PUBLIC_SITE_ORIGIN`, and the payment callback returns the buyer to the
origin they checked out from rather than a fixed one. That origin is checked
against `CORS_ALLOWED_ORIGINS` before use — unchecked, it would make the
callback an open redirect.

**2. The device.** Firmware holds `uid`, `secret_key`, SSID and password. On
boot it joins Wi-Fi and opens `ws://host/ws/device/<uid>/`, then proves itself
(below). After that it heartbeats, pushes telemetry, and answers commands.

**3. The user.** The browser authenticates over REST, then opens
`ws://host/ws/user/` and signs the handshake with its access token. It receives
telemetry and status changes for every gadget it owns, and signs each command
individually.

## 🔐 Signature scheme

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

`simulators/simulate.py` is a working client and the reference for the
firmware: it performs this handshake, holds its own state, and answers
`state_request`.

## 📨 Message types

Device → server: `auth`, `heartbeat`, `telemetry`, `command_result`.
Server → device: `auth.ok`, `command`, `state_request`, `error`.
User → server: `auth`, `command`, `refresh_state`, `heartbeat`.
Server → user: `auth.ok`, `telemetry`, `device.status`, `command.status`,
`state.unknown`, `refresh.ack`, `pong`, `error`.

`auth.ok` for a device includes its capability contract; `auth.ok` for a user
includes every gadget it owns with a live `online` flag. Full payload shapes
are in `gadgets/protocol.py`.

## 🧩 Capabilities

A `GadgetType` owns a list of `Capability` rows — one per readable value
(`direction=telemetry`) or accepted command (`direction=command`), each with a
`value_type` (`bool`/`int`/`float`/`string`/`enum`), optional range, unit and
choices. Everything crossing a socket in either direction is coerced and
range-checked against them, so a device cannot inject a string into a numeric
series and a user cannot push a brightness of 500. The frontend reads the same
contract from `GET /api/gadgets/types/` to know how to render controls.

Two shapes are covered: types that stream (`mode=telemetry`, e.g. thermometer)
and types that only answer (`mode=action`, e.g. lamp). Both heartbeat.

## 📡 Presence and offline detection

A connected device is registered in Redis (`devices:online` plus
`device:online:<uid>`) with its own timeout, taken from its type's heartbeat
interval times the missed-beat allowance. Every heartbeat refreshes it.
`sweep_offline_devices` runs every 30s, drops anything silent past its timeout,
closes the socket if it is somehow still alive, and pushes `device.status` to
the owner. A clean disconnect removes the entry immediately. The Redis key also
carries its own TTL, so a worker dying mid-connection cannot leave a ghost.

Commands that go unanswered for `COMMAND_TIMEOUT_SECONDS` are closed out by
`expire_command`, with `expire_stale_commands` as the backstop.

## 💾 What gets stored, and what does not

**Commands are stored** — the command, who issued it, and the device's answer.
They are human-initiated, so the volume is small, and this is the trail that
answers "who turned this on" later. `prune_old_commands` drops them after 90
days.

**Telemetry is not stored.** A reading is validated against the gadget type's
capabilities and relayed to the owner's socket; if no socket is open it is
dropped. Nothing is written to disk. A thermometer reporting every few seconds
would otherwise fill the database with values nobody reads back, and the write
would sit in the path between the device and the dashboard.

**State is pulled, not cached.** When a user's socket finishes its handshake,
the server sends `state_request` to each of that user's *online* gadgets, and
each answers with an ordinary telemetry frame that lands on the dashboard a
moment later. A gadget that comes online while the dashboard is already open
gets polled the same way — that request is issued by the user's own consumer
when it sees the `device.status` event, so if nobody is watching, nothing is
sent and no user-presence bookkeeping is needed. Firmware keeps its own state
across the connection and speaks only when asked.

If a polled gadget stays silent for `STATE_REQUEST_TIMEOUT_SECONDS`, the
dashboard gets `state.unknown` for it and can show "online, state unknown"
instead of waiting forever. The connection is left alone: failing to answer a
poll is not evidence of death, and liveness is the heartbeat's job. Any later
reading — solicited or not — clears the flag.

A user can re-poll at will by sending `refresh_state` (a refresh button, for
the gadgets that came back unknown). With no `gadgets` list it covers
everything they own; the reply is `refresh.ack` with what was polled and what
was skipped as offline. It is unsigned — it reads, it changes nothing on the
hardware — but rate-limited by `STATE_REFRESH_MIN_INTERVAL_SECONDS`, since one
frame fans out to every gadget on the account.

This is why there is no server-side "last known value" anywhere: nothing to go
stale, nothing lost if Redis restarts, no write on the telemetry path, and no
unsolicited burst from a device that connects hours before anyone looks at it.
Offline gadgets simply do not answer, which is the right thing for the UI to
show for them.

Firmware must implement `state_request` — a device that ignores it will look
blank on a freshly opened dashboard until its next telemetry frame.

## 📝 Notes

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

## 🚧 Still to do

- Serve behind TLS: `wss://` and a `Secure` cookie posture. The reverse proxy
  should also do the canonical-host redirect, which the app currently only
  does client-side.
- A server-time endpoint. Signatures need clocks within
  `SIGNATURE_MAX_SKEW_SECONDS`, and a phone with a wrong clock fails with an
  unhelpful `auth_failed`.
- An OpenAPI schema; a mobile client would want one.
- Rate-limit the register endpoint per address, not just per IP. It is throttled
  at the `otp` scope, but a resend loop against one mailbox is still cheap.
- Order status has no way to move past `paid` except the admin — no
  fulfilment flow, and `tracking_code` is entered by hand.
