"""Sending email (password-reset links). Three back ends, chosen by configuration:

- SMTP: any mail provider (Microsoft 365, Google Workspace, SendGrid, Amazon SES, ...).
- A folder: each message is written to a .eml file. For local development and tests.
- None: email isn't configured; features that need it say so.
"""
import os
import smtplib
import ssl
import time
from email.message import EmailMessage


def _message(sender: str, to: str, subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, to, subject
    # Plain ASCII goes unencoded so a long link stays on one line and can be copied as-is.
    msg.set_content(body, cte="7bit" if body.isascii() else "quoted-printable")
    return msg


class Mailer:
    configured = False

    def send(self, to: str, subject: str, body: str) -> None:
        raise RuntimeError("Email is not configured")


class SMTPMailer(Mailer):
    configured = True

    def __init__(self, host: str, port: int, sender: str, user: str | None = None,
                 password: str | None = None, security: str = "starttls"):
        self.host, self.port, self.sender = host, port, sender
        self.user, self.password, self.security = user, password, security

    def send(self, to: str, subject: str, body: str) -> None:
        msg = _message(self.sender, to, subject, body)
        context = ssl.create_default_context()
        if self.security == "ssl":
            server = smtplib.SMTP_SSL(self.host, self.port, context=context, timeout=20)
        else:
            server = smtplib.SMTP(self.host, self.port, timeout=20)
        with server:
            if self.security == "starttls":
                server.starttls(context=context)
            if self.user:
                server.login(self.user, self.password or "")
            server.send_message(msg)


class FolderMailer(Mailer):
    configured = True

    def __init__(self, folder: str, sender: str = "invoice-review@localhost"):
        self.folder, self.sender = folder, sender

    def send(self, to: str, subject: str, body: str) -> None:
        os.makedirs(self.folder, exist_ok=True)
        msg = _message(self.sender, to, subject, body)
        name = f"{time.time():.6f}-{''.join(c if c.isalnum() else '_' for c in to)}.eml"
        with open(os.path.join(self.folder, name), "wb") as f:
            f.write(bytes(msg))


def from_settings(settings) -> Mailer:
    if settings.smtp_host:
        return SMTPMailer(settings.smtp_host, settings.smtp_port, settings.mail_from,
                          settings.smtp_user, settings.smtp_password, settings.smtp_security)
    if settings.mail_dir:
        return FolderMailer(settings.mail_dir, settings.mail_from)
    return Mailer()
