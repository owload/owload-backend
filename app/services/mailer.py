"""Sending the invitations. Without a configured mail server nothing is sent and the caller says so to the client, which then
shows the link to hand over by hand. The text of a mail never holds a password or a key: the drive's password is passed
separately by the person who shares the drive."""
import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage

from app.core.config import Settings

logger = logging.getLogger(__name__)


def mail_enabled(settings: Settings) -> bool:
    return bool(settings.smtp_host and settings.smtp_from)


def _send(settings: Settings, to: str, subject: str, body: str) -> None:
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
        if settings.smtp_starttls:
            smtp.starttls(context=ssl.create_default_context())
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(message)


async def send_mail(settings: Settings, to: str, subject: str, body: str) -> bool:
    """Sends a plain-text mail; False when mail is not configured or sending failed (the failure is logged without the address)."""
    if not mail_enabled(settings):
        return False
    try:
        await asyncio.to_thread(_send, settings, to, subject, body)
        return True
    except Exception as exc:  # a mail server down must not fail the sharing itself
        logger.warning("Sending a mail failed: %s", type(exc).__name__)
        return False


def _one_line(text: str) -> str:
    """A name that goes into a mail header must not carry a line break (it would end the header)."""
    return " ".join(text.split())


def invitation_mail(inviter: str, drive_title: str, role: str, link: str, has_account: bool) -> tuple[str, str]:
    inviter, drive_title = _one_line(inviter), _one_line(drive_title)
    subject = f"{inviter} shared the drive “{drive_title}” with you"
    start = "Open Owload to see it:" if has_account else "Sign up or sign in to Owload with this email address to see it:"
    body = (
        f"{inviter} gave you access to the drive “{drive_title}” as {role}.\n\n"
        f"{start}\n{link}\n\n"
        "The drive is encrypted. Ask the person who shared it for its password: it is not in this message, "
        "and nobody at Owload can recover it.\n"
    )
    return subject, body
