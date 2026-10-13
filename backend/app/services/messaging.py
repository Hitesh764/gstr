"""
Outbound email and SMS, behind provider interfaces.

Email is delivered through Resend. SMS has no provider chosen yet, so it
still writes to the application log.

The `console` adapters remain the default for local development, which keeps
a fresh clone free of third-party accounts. Set `EMAIL_PROVIDER=resend` and
`RESEND_API_KEY` to send for real.

Adding a provider means implementing `EmailProvider` or `SMSProvider` and
registering it in the factory at the bottom. No call site changes.

SMS candidates when you choose one: MSG91 or Gupshup for India, Twilio
internationally. Transactional SMS to Indian numbers additionally requires
DLT registration of the sender ID and template with TRAI, which is a
weeks-long process worth starting before it blocks anything.
"""
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from app.config import settings

logger = logging.getLogger("gstr2b.messaging")


@dataclass
class EmailMessage:
    to: str
    subject: str
    body: str
    html_body: Optional[str] = None
    tags: dict = field(default_factory=dict)


@dataclass
class SMSMessage:
    to: str          # E.164 preferred, e.g. +919876543210
    body: str
    template_id: Optional[str] = None   # DLT template id, required in India


class SendResult:
    def __init__(self, ok: bool, provider: str, reference: Optional[str] = None,
                 error: Optional[str] = None):
        self.ok = ok
        self.provider = provider
        self.reference = reference
        self.error = error

    def __repr__(self) -> str:
        return f"<SendResult ok={self.ok} provider={self.provider} ref={self.reference}>"


# ---------------------------------------------------------------------------
# Interfaces
# ---------------------------------------------------------------------------

class EmailProvider(ABC):
    name = "abstract"

    @abstractmethod
    def send(self, message: EmailMessage) -> SendResult:
        ...


class SMSProvider(ABC):
    name = "abstract"

    @abstractmethod
    def send(self, message: SMSMessage) -> SendResult:
        ...


# ---------------------------------------------------------------------------
# Console adapters (the local-development default)
# ---------------------------------------------------------------------------

class ConsoleEmailProvider(EmailProvider):
    """Logs the email instead of sending it.

    Invitation and password-reset links are printed in full so a developer
    can complete those flows locally without a mail server.
    """
    name = "console"

    def send(self, message: EmailMessage) -> SendResult:
        logger.info(
            "\n"
            "──────────────── EMAIL (not sent: console provider) ────────────────\n"
            "To:      %s\n"
            "From:    %s\n"
            "Subject: %s\n"
            "────────────────────────────────────────────────────────────────────\n"
            "%s\n"
            "────────────────────────────────────────────────────────────────────",
            message.to, settings.email_from, message.subject, message.body,
        )
        return SendResult(ok=True, provider=self.name, reference="console")


class ConsoleSMSProvider(SMSProvider):
    name = "console"

    def send(self, message: SMSMessage) -> SendResult:
        logger.info(
            "\n"
            "──────────────── SMS (not sent: console provider) ────────────────\n"
            "To:   %s\n"
            "Body: %s\n"
            "──────────────────────────────────────────────────────────────────",
            message.to, message.body,
        )
        return SendResult(ok=True, provider=self.name, reference="console")


class ResendEmailProvider(EmailProvider):
    """Sends through Resend's HTTP API.

    Failures are returned as an unsuccessful SendResult rather than raised,
    because the caller (app/notifications.py) treats email as best-effort:
    the in-app notification is already saved, and a mail outage must not
    turn into a failed request for the person who triggered it.
    """
    name = "resend"
    endpoint = "https://api.resend.com/emails"

    def send(self, message: EmailMessage) -> SendResult:
        api_key = settings.resend_api_key
        if not api_key:
            return SendResult(
                ok=False, provider=self.name,
                error="RESEND_API_KEY is not set. Set it, or use EMAIL_PROVIDER=console.",
            )

        payload = {
            "from": settings.email_from,
            "to": [message.to],
            "subject": message.subject,
            "text": message.body,
        }
        if message.html_body:
            payload["html"] = message.html_body

        try:
            import httpx

            response = httpx.post(
                self.endpoint,
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=10.0,
            )
        except Exception as exc:
            logger.warning("Resend request failed: %s", exc)
            return SendResult(ok=False, provider=self.name, error=str(exc))

        if response.status_code >= 400:
            # Resend returns a JSON body describing what was wrong; surfacing
            # it makes a misconfigured sending domain obvious rather than
            # showing a bare status code.
            detail = response.text[:400]
            logger.warning("Resend rejected the message (%s): %s",
                           response.status_code, detail)
            return SendResult(ok=False, provider=self.name,
                              error=f"HTTP {response.status_code}: {detail}")

        try:
            reference = response.json().get("id")
        except Exception:
            reference = None
        return SendResult(ok=True, provider=self.name, reference=reference)


class NullEmailProvider(EmailProvider):
    """Silently discards. For tests that assert on behaviour, not output."""
    name = "null"

    def send(self, message: EmailMessage) -> SendResult:
        return SendResult(ok=True, provider=self.name)


class NullSMSProvider(SMSProvider):
    name = "null"

    def send(self, message: SMSMessage) -> SendResult:
        return SendResult(ok=True, provider=self.name)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_EMAIL_PROVIDERS = {
    "console": ConsoleEmailProvider,
    "resend": ResendEmailProvider,
    "null": NullEmailProvider,
}
_SMS_PROVIDERS = {
    "console": ConsoleSMSProvider,
    "null": NullSMSProvider,
}


def get_email_provider() -> EmailProvider:
    cls = _EMAIL_PROVIDERS.get(settings.email_provider)
    if cls is None:
        raise RuntimeError(
            f"Unknown EMAIL_PROVIDER {settings.email_provider!r}. "
            f"Available: {', '.join(sorted(_EMAIL_PROVIDERS))}."
        )
    return cls()


def get_sms_provider() -> SMSProvider:
    cls = _SMS_PROVIDERS.get(settings.sms_provider)
    if cls is None:
        raise RuntimeError(
            f"Unknown SMS_PROVIDER {settings.sms_provider!r}. "
            f"Available: {', '.join(sorted(_SMS_PROVIDERS))}."
        )
    return cls()


# ---------------------------------------------------------------------------
# Call sites
# ---------------------------------------------------------------------------

def send_invitation_email(*, to: str, firm_name: str, inviter_name: str,
                          role: str, accept_url: str) -> SendResult:
    return get_email_provider().send(EmailMessage(
        to=to,
        subject=f"{inviter_name} invited you to join {firm_name}",
        body=(
            f"{inviter_name} has invited you to join {firm_name} on the GST "
            f"reconciliation platform as {role.replace('_', ' ').title()}.\n\n"
            f"Accept the invitation:\n{accept_url}\n\n"
            f"This link expires in {settings.invitation_ttl_days} days. "
            f"If you were not expecting this, you can ignore this email."
        ),
        tags={"type": "invitation"},
    ))


def send_password_reset_email(*, to: str, name: str, reset_url: str) -> SendResult:
    return get_email_provider().send(EmailMessage(
        to=to,
        subject="Reset your password",
        body=(
            f"Hello {name},\n\n"
            f"A password reset was requested for your account.\n\n"
            f"Reset your password:\n{reset_url}\n\n"
            f"This link expires in {settings.password_reset_ttl_minutes} minutes "
            f"and can be used once.\n\n"
            f"If you did not request this, no action is needed — your password "
            f"has not changed."
        ),
        tags={"type": "password_reset"},
    ))


def send_mobile_otp(*, to: str, otp: str) -> SendResult:
    return get_sms_provider().send(SMSMessage(
        to=to,
        body=f"{otp} is your verification code. It expires in 10 minutes. "
             f"Do not share it with anyone.",
    ))
