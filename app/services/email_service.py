"""Outgoing email: verification links and password reset links.

Backends (EMAIL_BACKEND):
* console - the email is written to the application log. Development only:
            the link is visible to anyone who can read the logs.
* smtp    - sent through SMTP_HOST. docker-compose runs Mailpit, a local
            "catch-all" SMTP server with a web inbox (http://localhost:8025),
            so the real SMTP code path is exercised without sending real mail.

Emails are sent from a FastAPI BackgroundTask, i.e. AFTER the HTTP response
has been sent: a slow mail server never slows the request down, and the
password-reset endpoint answers equally fast whether or not the email exists
(no account enumeration through response timing).
"""

import smtplib
from email.message import EmailMessage
from html import escape
from urllib.parse import quote

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def send_email(to: str, subject: str, text: str, html: str | None = None) -> bool:
    """Never raises: a failed email is logged, not turned into an API error
    (the response has usually been sent already)."""
    s = get_settings()
    if s.email_backend == "console":
        logger.info("EMAIL (console backend) to=%s subject=%r\n%s", to, subject, text)
        return True

    message = EmailMessage()
    message["From"] = s.email_from
    message["To"] = to
    message["Subject"] = subject
    message.set_content(text)
    if html:
        message.add_alternative(html, subtype="html")
    try:
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=s.smtp_timeout_seconds) as smtp:
            if s.smtp_starttls:
                smtp.starttls()
            if s.smtp_username and s.smtp_password:
                smtp.login(s.smtp_username, s.smtp_password.get_secret_value())
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException):
        logger.exception("Could not send email %r", subject)  # recipient not logged (personal data)
        return False
    return True


def _link(path: str, token: str) -> str:
    return f"{get_settings().frontend_base_url.rstrip('/')}{path}?token={quote(token)}"


def _html(name: str, intro: str, link: str, button: str, outro: str) -> str:
    return (
        f"<p>Hi {escape(name)},</p><p>{escape(intro)}</p>"
        f'<p><a href="{escape(link)}" style="display:inline-block;padding:10px 16px;border:1px solid #1d4ed8;'
        f'border-radius:8px;color:#1e3a8a;background:#eff6ff;text-decoration:none">{escape(button)}</a></p>'
        f"<p style=\"color:#555\">{escape(outro)}</p>"
    )


def send_verification_email(to: str, name: str, token: str) -> bool:
    link = _link("/verify-email", token)
    hours = get_settings().email_verification_ttl_hours
    intro = "Please confirm your email address for DocuMind AI."
    outro = f"The link is valid for {hours} hours. If you did not create an account, ignore this email."
    text = f"Hi {name},\n\n{intro}\n\n{link}\n\n{outro}\n"
    return send_email(to, "Confirm your email address", text, _html(name, intro, link, "Confirm email", outro))


def send_password_reset_email(to: str, name: str, token: str) -> bool:
    link = _link("/reset-password", token)
    minutes = get_settings().password_reset_ttl_minutes
    intro = "Someone (hopefully you) asked to reset your DocuMind AI password."
    outro = (f"The link is valid for {minutes} minutes and works once. "
             "If you did not ask for this, ignore this email - your password stays the same.")
    text = f"Hi {name},\n\n{intro}\n\n{link}\n\n{outro}\n"
    return send_email(to, "Reset your password", text, _html(name, intro, link, "Choose a new password", outro))
