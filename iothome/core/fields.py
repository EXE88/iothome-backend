from django.db import models

from .crypto import decrypt, encrypt


class EncryptedTextField(models.TextField):
    """TextField whose value is Fernet-encrypted in the database.

    Ciphertext is not order-preserving, so these columns cannot be used for
    ``filter``/``order_by`` beyond exact-null checks — look values up by their
    owning row instead.
    """

    def from_db_value(self, value, expression, connection):
        if value is None:
            return value
        return decrypt(value)

    def to_python(self, value):
        return value

    def get_prep_value(self, value):
        if value is None:
            return None
        return encrypt(value)
