"""Thin Zarinpal REST v4 client.

Amounts cross this boundary in Toman and are converted to Rial, which is what
the gateway expects. Sandbox and production differ only in host, so one flag
switches them.
"""

import logging

import requests
from django.conf import settings

logger = logging.getLogger("iothome.payments")

PRODUCTION_HOST = "https://payment.zarinpal.com"
SANDBOX_HOST = "https://sandbox.zarinpal.com"

TIMEOUT = 15
CODE_SUCCESS = 100
CODE_ALREADY_VERIFIED = 101


class ZarinpalError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def _host():
    return SANDBOX_HOST if settings.ZARINPAL_SANDBOX else PRODUCTION_HOST


def start_payment_url(authority):
    return f"{_host()}/pg/StartPay/{authority}"


def _post(path, payload):
    if not settings.ZARINPAL_MERCHANT_ID:
        raise ZarinpalError("ZARINPAL_MERCHANT_ID is not configured.")
    url = f"{_host()}{path}"
    body = {"merchant_id": settings.ZARINPAL_MERCHANT_ID, **payload}
    try:
        response = requests.post(
            url,
            json=body,
            timeout=TIMEOUT,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        logger.error("zarinpal %s failed: %s", path, exc)
        raise ZarinpalError("Could not reach the payment gateway.")
    except ValueError:
        raise ZarinpalError("The payment gateway returned an unreadable response.")

    errors = data.get("errors")
    # An empty list means "no errors"; a dict is a real failure.
    if isinstance(errors, dict) and errors:
        raise ZarinpalError(
            errors.get("message", "Payment gateway error."), errors.get("code")
        )
    return data.get("data") or {}


def request_payment(*, amount_toman, description, callback_url, email="", mobile=""):
    """Ask for an authority token. Returns ``(authority, raw_data)``."""
    metadata = {}
    if email:
        metadata["email"] = email
    if mobile:
        metadata["mobile"] = mobile

    data = _post(
        "/pg/v4/payment/request.json",
        {
            "amount": int(amount_toman) * 10,
            "description": description[:255],
            "callback_url": callback_url,
            "metadata": metadata,
        },
    )
    if data.get("code") != CODE_SUCCESS or not data.get("authority"):
        raise ZarinpalError("The gateway refused the payment request.", data.get("code"))
    return data["authority"], data


def verify_payment(*, amount_toman, authority):
    """Confirm a returning payment. Returns the gateway's data block.

    Code 101 means Zarinpal already verified this authority — treated as
    success so a double callback cannot flip a paid order to failed.
    """
    data = _post(
        "/pg/v4/payment/verify.json",
        {"amount": int(amount_toman) * 10, "authority": authority},
    )
    code = data.get("code")
    if code not in (CODE_SUCCESS, CODE_ALREADY_VERIFIED):
        raise ZarinpalError("Payment could not be verified.", code)
    return data
