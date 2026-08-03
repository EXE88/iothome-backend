"""Wire protocol shared by the consumers, the Celery tasks and the firmware.

Device -> server
    auth            {device, timestamp, nonce, signature}
    heartbeat       {}
    telemetry       {readings: {key: value}, recorded_at?}
    command_result  {request_id, status: ok|error, value?, error?}

Server -> device
    auth.ok         {device, heartbeat_interval, capabilities}
    command         {request_id, key, value}
    error           {code, detail}

User -> server
    auth            {token, timestamp, nonce, signature}
    command         {request_id, gadget, key, value, timestamp, nonce, signature}
    subscribe       {gadgets: [uid, ...]}   (optional narrowing; default = all)

Server -> user
    auth.ok         {user_id, gadgets: [{uid, online, ...}]}
    telemetry       {gadget, readings, recorded_at}
    device.status   {gadget, online, reason}
    command.status  {request_id, gadget, status, response?, error?}
    error           {code, detail, request_id?}
"""

# Client -> server
MSG_AUTH = "auth"
MSG_HEARTBEAT = "heartbeat"
MSG_TELEMETRY = "telemetry"
MSG_COMMAND = "command"
MSG_COMMAND_RESULT = "command_result"
MSG_SUBSCRIBE = "subscribe"

# Server -> client
MSG_AUTH_OK = "auth.ok"
MSG_DEVICE_STATUS = "device.status"
MSG_COMMAND_STATUS = "command.status"
MSG_PONG = "pong"
MSG_ERROR = "error"

# Channel-layer event names (the ``type`` of group_send payloads maps to the
# consumer method with dots replaced by underscores).
EVENT_DEVICE_COMMAND = "device.command"
EVENT_DEVICE_DISCONNECT = "device.disconnect"
EVENT_USER_TELEMETRY = "user.telemetry"
EVENT_USER_DEVICE_STATUS = "user.device_status"
EVENT_USER_COMMAND_STATUS = "user.command_status"

# Close codes. 4000-4999 is the application-defined range.
CLOSE_AUTH_FAILED = 4001
CLOSE_AUTH_TIMEOUT = 4002
CLOSE_RATE_LIMITED = 4003
CLOSE_BAD_PAYLOAD = 4004
CLOSE_DEVICE_DISABLED = 4005
CLOSE_SUPERSEDED = 4006  # the same device opened a newer connection
CLOSE_OFFLINE_SWEEP = 4007

# Hard ceilings so a rogue client cannot exhaust memory before validation.
MAX_FRAME_BYTES = 16 * 1024
MAX_READINGS_PER_FRAME = 32
