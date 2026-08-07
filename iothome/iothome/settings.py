"""Django settings for the iothome backend."""

from datetime import timedelta
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1"]),
    CORS_ALLOWED_ORIGINS=(list, ["http://localhost:3000", "http://127.0.0.1:3000"]),
    REDIS_URL=(str, "redis://127.0.0.1:6379/0"),
    ZARINPAL_SANDBOX=(bool, True),
)
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("SECRET_KEY", default="django-insecure-dev-only-change-me")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")

INSTALLED_APPS = [
    "daphne",  # must precede staticfiles so runserver speaks ASGI
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    # Makes logging out mean something: without it a refresh token stays valid
    # until it expires, so dropping the cookie only ends the session on the
    # browser that dropped it.
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    "channels",
    "django_celery_beat",
    "accounts",
    "gadgets",
    "purchases",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    # Serves the admin's CSS itself, so a deployment needs no separate static
    # file server just to make /admin/ usable.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "iothome.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "iothome.wsgi.application"
ASGI_APPLICATION = "iothome.asgi.application"

DATABASES = {
    "default": env.db("DATABASE_URL", default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}")
}

AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 8},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Tehran"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
    },
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --------------------------------------------------------------------------
# DRF / JWT
# --------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_THROTTLE_CLASSES": (
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ),
    "DEFAULT_THROTTLE_RATES": {
        "anon": "60/min",
        "user": "300/min",
        "otp": "5/min",
        "login": "10/min",
    },
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
    "PAGE_SIZE": 50,
}

# The access token is what signs every device command from the browser, so it
# has to be readable by JavaScript; the refresh token is the one kept in an
# httpOnly cookie. The frontend trades the access token in a couple of minutes
# before it lapses, and once the refresh token itself expires the user signs in
# again. Keep REFRESH_COOKIE_MAX_AGE_DAYS in the frontend's .env equal to
# REFRESH_TOKEN_LIFETIME_DAYS here.
ACCESS_TOKEN_LIFETIME_MINUTES = env.int("ACCESS_TOKEN_LIFETIME_MINUTES", default=60 * 24)
REFRESH_TOKEN_LIFETIME_DAYS = env.int("REFRESH_TOKEN_LIFETIME_DAYS", default=7)

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=ACCESS_TOKEN_LIFETIME_MINUTES),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=REFRESH_TOKEN_LIFETIME_DAYS),
    # Rotation is off on purpose, for two reasons.
    #
    # It makes the refresh window absolute rather than sliding: seven days
    # after signing in you sign in again, which is the rule this product
    # wants. With rotation, anyone who visits daily never expires at all.
    #
    # And rotation plus blacklisting races against ordinary browsing. Every
    # page load asks for a fresh access token, so opening two tabs at once
    # sends the same refresh token twice; the first rotates it and the second
    # arrives holding a token that has just been blacklisted, and that tab
    # falls out of the session for no reason the user can see.
    "ROTATE_REFRESH_TOKENS": False,
    "BLACKLIST_AFTER_ROTATION": False,
    "AUTH_HEADER_TYPES": ("Bearer",),
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
}

# --------------------------------------------------------------------------
# Channels / Redis
# --------------------------------------------------------------------------
REDIS_URL = env("REDIS_URL")

CHANNEL_LAYERS = {
    # The pubsub layer, not channels_redis.core: the core layer parks on a
    # BRPOP that redis-py 8 aborts on its socket timeout, which drops idle
    # device sockets with a 1011 after a few seconds.
    "default": {
        "BACKEND": "channels_redis.pubsub.RedisPubSubChannelLayer",
        "CONFIG": {"hosts": [REDIS_URL]},
    }
}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
    }
}

# --------------------------------------------------------------------------
# Celery
# --------------------------------------------------------------------------
CELERY_BROKER_URL = env("CELERY_BROKER_URL", default=REDIS_URL)
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default=REDIS_URL)
CELERY_TASK_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = TIME_ZONE
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"

# --------------------------------------------------------------------------
# Email — console for now; swap the backend for SMTP later.
# --------------------------------------------------------------------------
EMAIL_BACKEND = env(
    "EMAIL_BACKEND", default="django.core.mail.backends.console.EmailBackend"
)
EMAIL_HOST = env("EMAIL_HOST", default="")
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="IoT Home <no-reply@iothome.dev>")

# --------------------------------------------------------------------------
# Project-specific knobs
# --------------------------------------------------------------------------
# Encrypts device secret keys and Wi-Fi credentials. Generate with:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
FIELD_ENCRYPTION_KEY = env("FIELD_ENCRYPTION_KEY", default="")

OTP_CODE_LENGTH = 6
OTP_TTL_SECONDS = 10 * 60
OTP_MAX_ATTEMPTS = 5
# How long an account that never verified its address is kept before it is
# deleted. Its only effect while it exists is to reserve that address, so this
# is really "how long we hold the address for someone who walked away".
UNVERIFIED_ACCOUNT_TTL_DAYS = env.int("UNVERIFIED_ACCOUNT_TTL_DAYS", default=7)

# How far a device/browser clock may drift from ours on a signed frame.
SIGNATURE_MAX_SKEW_SECONDS = env.int("SIGNATURE_MAX_SKEW_SECONDS", default=60)
# A device gets this long to complete the handshake before we hang up.
WS_AUTH_TIMEOUT_SECONDS = env.int("WS_AUTH_TIMEOUT_SECONDS", default=10)
# Frames per minute a single connection may send before being disconnected.
WS_MAX_FRAMES_PER_MINUTE = env.int("WS_MAX_FRAMES_PER_MINUTE", default=240)
# How long a command waits for the device's ack before it is marked timed out.
COMMAND_TIMEOUT_SECONDS = env.int("COMMAND_TIMEOUT_SECONDS", default=15)
# How long a dashboard waits for a polled gadget to report its state before
# the UI is told the value is unknown.
STATE_REQUEST_TIMEOUT_SECONDS = env.int("STATE_REQUEST_TIMEOUT_SECONDS", default=10)
# Minimum gap between manual refreshes on one socket. A refresh fans out to
# every gadget the user owns, so an unthrottled button is a way to hammer the
# hardware from the browser.
STATE_REFRESH_MIN_INTERVAL_SECONDS = env.int(
    "STATE_REFRESH_MIN_INTERVAL_SECONDS", default=3
)
# Fallback offline threshold for gadget types that do not define their own.
DEFAULT_OFFLINE_TIMEOUT_SECONDS = env.int("DEFAULT_OFFLINE_TIMEOUT_SECONDS", default=180)
# How long an order may sit unpaid before the sweep releases its stock.
# Comfortably longer than a gateway session, because the sweep cannot tell an
# abandoned checkout from one where the buyer is still typing a card number.
PENDING_ORDER_TIMEOUT_MINUTES = env.int("PENDING_ORDER_TIMEOUT_MINUTES", default=30)

# --------------------------------------------------------------------------
# Zarinpal
# --------------------------------------------------------------------------
ZARINPAL_MERCHANT_ID = env("ZARINPAL_MERCHANT_ID", default="")
ZARINPAL_SANDBOX = env("ZARINPAL_SANDBOX")
ZARINPAL_CALLBACK_URL = env(
    "ZARINPAL_CALLBACK_URL",
    default="http://localhost:8000/api/purchases/payments/verify/",
)
# Where the gateway callback bounces the browser once we know the result.
FRONTEND_PAYMENT_RESULT_URL = env(
    "FRONTEND_PAYMENT_RESULT_URL", default="http://localhost:3000/payment/result"
)

CORS_ALLOWED_ORIGINS = env("CORS_ALLOWED_ORIGINS")
CORS_ALLOW_CREDENTIALS = True

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "[{levelname}] {asctime} {name}: {message}", "style": "{"}
    },
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "simple"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "iothome": {"handlers": ["console"], "level": "DEBUG", "propagate": False},
    },
}

if not DEBUG:
    SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
