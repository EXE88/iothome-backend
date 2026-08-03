"""Settings for the test suite: no Redis, no network, fast hashing.

    python manage.py test --settings=iothome.settings_test

WebSocket tests that genuinely need Redis skip themselves when it is absent.
"""

from .settings import *  # noqa: F401,F403

DEBUG = False

CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# Deterministic key so encrypted fields work without touching the dev .env.
FIELD_ENCRYPTION_KEY = "ZmFrZS1rZXktZm9yLXRlc3RzLW9ubHktMzJieXRlcyE="

ZARINPAL_MERCHANT_ID = "0" * 36
ZARINPAL_SANDBOX = True

# Views set throttle_classes explicitly, so clearing the defaults is not
# enough — the rates themselves have to be disabled.
REST_FRAMEWORK = {
    **REST_FRAMEWORK,  # noqa: F405
    "DEFAULT_THROTTLE_CLASSES": (),
    "DEFAULT_THROTTLE_RATES": {"anon": None, "user": None, "otp": None, "login": None},
}
