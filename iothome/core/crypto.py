"""Symmetric encryption for secrets we must be able to read back.

Device secret keys and Wi-Fi credentials cannot be hashed: the backend has to
recompute HMACs with the secret, and an operator has to flash the Wi-Fi
password into the device. So they are encrypted at rest with Fernet using
``settings.FIELD_ENCRYPTION_KEY``.
"""

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


@lru_cache(maxsize=1)
def _fernet():
    key = getattr(settings, "FIELD_ENCRYPTION_KEY", None)
    if not key:
        raise ImproperlyConfigured(
            "FIELD_ENCRYPTION_KEY is not set. Generate one with:\n"
            "  python -c \"from cryptography.fernet import Fernet;"
            " print(Fernet.generate_key().decode())\""
        )
    if isinstance(key, str):
        key = key.encode()
    return Fernet(key)


def encrypt(plaintext):
    if plaintext is None or plaintext == "":
        return ""
    return _fernet().encrypt(str(plaintext).encode()).decode()


def decrypt(ciphertext):
    if not ciphertext:
        return ""
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        # Wrong/rotated key — surface it instead of silently returning junk.
        raise ImproperlyConfigured(
            "Could not decrypt a stored secret: FIELD_ENCRYPTION_KEY does not "
            "match the key the value was written with."
        )
