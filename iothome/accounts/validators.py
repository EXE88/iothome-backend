from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

# Only Google-hosted mailboxes are accepted for now.
ALLOWED_EMAIL_DOMAINS = ("gmail.com", "googlemail.com")


def normalize_email(email):
    """Lowercase the whole address and strip Gmail's dots / +tags.

    Gmail treats ``a.b+x@gmail.com`` and ``ab@gmail.com`` as the same mailbox,
    so we store a single canonical form to keep ``email`` genuinely unique.
    """
    email = (email or "").strip()
    if "@" not in email:
        return email.lower()
    local, _sep, domain = email.rpartition("@")
    domain = domain.lower()
    local = local.lower()
    if domain in ALLOWED_EMAIL_DOMAINS:
        local = local.split("+", 1)[0].replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"


def validate_gmail_address(value):
    domain = (value or "").rpartition("@")[2].lower()
    if domain not in ALLOWED_EMAIL_DOMAINS:
        raise ValidationError(
            _("Only Gmail addresses are accepted (%(domains)s)."),
            params={"domains": ", ".join(ALLOWED_EMAIL_DOMAINS)},
            code="email_domain_not_allowed",
        )
