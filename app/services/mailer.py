"""Sending the invitations. Without a configured mail server nothing is sent and the caller says so to the client, which then
shows the link to hand over by hand. The text of a mail never holds a password or a key: the drive's password is passed
separately by the person who shares the drive."""
import asyncio
import html
import logging
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

from app.core.config import Settings

logger = logging.getLogger(__name__)

# The logo (the owl and the name) travels inside the mail as an inline image, so that it needs no address that the reader's mail program could block.
_LOGO = Path(__file__).parent / "mail_assets" / "logo.png"
_LOGO_CID = "owload-logo"


def mail_enabled(settings: Settings) -> bool:
    return bool(settings.smtp_host and settings.smtp_from)


def _send(settings: Settings, to: str, subject: str, body: str, html_body: str | None = None) -> None:
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    if html_body is not None:
        message.add_alternative(html_body, subtype="html")
        html_part = message.get_body(preferencelist=("html",))
        if html_part is not None and _LOGO.is_file():
            html_part.add_related(_LOGO.read_bytes(), maintype="image", subtype="png", cid=f"<{_LOGO_CID}>", disposition="inline", filename="owload.png")
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
        if settings.smtp_starttls:
            smtp.starttls(context=ssl.create_default_context())
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(message)


async def send_mail(settings: Settings, to: str, subject: str, body: str, html_body: str | None = None) -> bool:
    """Sends a mail: the plain text, and the same in HTML when `html_body` is given. False when mail is not configured or sending failed (the
    failure is logged without the address)."""
    if not mail_enabled(settings):
        return False
    try:
        await asyncio.to_thread(_send, settings, to, subject, body, html_body)
        return True
    except Exception as exc:  # a mail server down must not fail the sharing itself
        logger.warning("Sending a mail failed: %s", type(exc).__name__)
        return False


def _one_line(text: str) -> str:
    """A name that goes into a mail header must not carry a line break (it would end the header)."""
    return " ".join(text.split())


# The look of the invitation: the layout of the design for the mail that confirms an address (a light page, a bordered white card, a small
# label above the heading, a yellow button, the link in a monospace face, the list of what Owload does with the files), flush to the top left.
_INK, _MUTED, _LINE, _PAGE, _YELLOW = "#1D1C19", "#6B6A63", "#E3E1D8", "#F2F2EF", "#FADB58"
_HEAD = "'Outfit', 'Inter', -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
_BODY = "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
_MONO = "ui-monospace, 'SFMono-Regular', Menlo, Consolas, monospace"
SUPPORT_EMAIL = "info@owload.com"

_PROMISES = (
    "Files are encrypted on your device before upload.",
    "Our servers receive only encrypted content.",
    "No keys or unencrypted data are sent to us.",
)


def _invitation_html(inviter: str, drive_title: str, role: str, link: str, start: str, button: str, recipient: str | None) -> str:
    """The invitation as an HTML mail. Everything that came from a person (the names, the role, the addresses) is escaped; the styles are
    inline and the layout is made of tables, which is what mail programs draw reliably."""
    who, title, role_text = html.escape(inviter), html.escape(drive_title), html.escape(role)
    href, shown_link = html.escape(link, quote=True), html.escape(link)
    body_style = f"font-family: {_BODY}; color: {_INK};"
    muted_style = f"font-family: {_BODY}; color: {_MUTED};"
    promises = "".join(
        f'<tr><td width="30" valign="top" style="padding: 0 0 10px"><div style="width: 20px; height: 20px; border-radius: 10px; background: {_INK}; color: {_YELLOW}; '
        f'font-family: {_BODY}; font-size: 12px; line-height: 20px; font-weight: 700; text-align: center">&#10003;</div></td>'
        f'<td style="padding: 0 0 10px; {body_style} font-size: 14px; line-height: 20px">{html.escape(text)}</td></tr>'
        for text in _PROMISES
    )
    sent_to = (
        f'<p style="margin: 0; padding: 0 0 8px; {muted_style} font-size: 12px; line-height: 1.6;">This message was sent to '
        f'<a href="mailto:{html.escape(recipient, quote=True)}" style="color: {_MUTED}; text-decoration: underline">{html.escape(recipient)}</a> '
        f'because {who} shared a drive with this address.</p>'
        if recipient else ""
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<meta name="color-scheme" content="light"><title>{who} shared a drive with you</title></head>'
        f'<body style="margin: 0; padding: 0; background: {_PAGE}">'
        f'<div style="display: none; max-height: 0; overflow: hidden; mso-hide: all; font-size: 1px; line-height: 1px; color: {_PAGE}">'
        f'{who} gave you access to the drive “{title}”.</div>'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background: {_PAGE}"><tr>'
        '<td align="left" valign="top" style="padding: 24px 20px 40px">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="max-width: 600px">'
        f'<tr><td align="left" style="padding: 0 8px 20px"><img src="cid:{_LOGO_CID}" alt="owload" height="32" '
        f'style="display: block; height: 32px; width: auto; border: 0; outline: none; text-decoration: none; font-family: {_HEAD}; font-size: 22px; font-weight: 600; color: {_INK}"></td></tr>'
        f'<tr><td align="left" style="padding: 32px 30px 26px; border: 1px solid {_LINE}; border-radius: 16px; background: #FFFFFF">'
        f'<p style="margin: 0; padding: 0 0 10px; font-family: {_HEAD}; font-size: 12px; line-height: 1.4; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase; color: {_MUTED}">Drive invitation</p>'
        f'<h1 style="margin: 0; padding: 0 0 14px; font-family: {_HEAD}; font-size: 26px; line-height: 1.25; font-weight: 600; color: {_INK}">{who} shared a drive with you</h1>'
        f'<p style="margin: 0; padding: 0 0 12px; {body_style} font-size: 15px; line-height: 1.6;">{who} gave you access to the drive <b>“{title}”</b> as <b>{role_text}</b>.</p>'
        f'<p style="margin: 0; padding: 0 0 16px; {body_style} font-size: 15px; line-height: 1.6;">{html.escape(start)}</p>'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin: 8px 0 20px"><tr><td style="border-radius: 10px; background: {_YELLOW}">'
        f'<a href="{href}" style="display: inline-block; padding: 14px 26px; font-family: {_HEAD}; font-size: 15px; line-height: 1.2; font-weight: 600; color: {_INK}; text-decoration: none; border-radius: 10px">{html.escape(button)}</a>'
        '</td></tr></table>'
        f'<p style="margin: 0; padding: 0 0 6px; {muted_style} font-size: 13px; line-height: 1.6;">Or paste this link into your browser:</p>'
        f'<p style="margin: 0; padding: 0 0 20px; font-family: {_MONO}; font-size: 12px; line-height: 1.6; color: {_INK}; word-break: break-all">{shown_link}</p>'
        f'<p style="margin: 0; padding: 0 0 22px; {muted_style} font-size: 13px; line-height: 1.6;"><b style="color: {_INK}">The drive is encrypted.</b> '
        'Ask the person who shared it for its password: it is not in this message, and nobody at Owload can recover it.</p>'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"><tr><td style="padding: 8px 0 22px"><div style="height: 1px; line-height: 1px; font-size: 1px; background: {_LINE}">&nbsp;</div></td></tr></table>'
        f'<p style="margin: 0; padding: 0 0 12px; {body_style} font-size: 14px; line-height: 1.6; font-weight: 600;">How Owload keeps your files</p>'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">{promises}</table>'
        '</td></tr>'
        f'<tr><td align="left" style="padding: 22px 8px 0">{sent_to}'
        f'<p style="margin: 0; padding: 0; {muted_style} font-size: 12px; line-height: 1.6;">Owload · '
        f'<a href="mailto:{SUPPORT_EMAIL}" style="color: {_MUTED}; text-decoration: underline">{SUPPORT_EMAIL}</a></p></td></tr>'
        '</table></td></tr></table></body></html>'
    )


def invitation_mail(inviter: str, drive_title: str, role: str, link: str, has_account: bool, recipient: str | None = None) -> tuple[str, str, str]:
    """The subject, the plain text and the HTML of an invitation. The text of a mail never holds a password or a key."""
    inviter, drive_title = _one_line(inviter), _one_line(drive_title)
    subject = f"{inviter} shared the drive “{drive_title}” with you"
    start = "Open Owload to see it:" if has_account else "Sign up or sign in to Owload with this email address to see it:"
    body = (
        f"{inviter} gave you access to the drive “{drive_title}” as {role}.\n\n"
        f"{start}\n{link}\n\n"
        "The drive is encrypted. Ask the person who shared it for its password: it is not in this message, "
        "and nobody at Owload can recover it.\n"
    )
    button = "Open Owload" if has_account else "Sign up or sign in"
    return subject, body, _invitation_html(inviter, drive_title, role, link, start.rstrip(":") + ".", button, recipient)
