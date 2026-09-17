"""Email channel — free, always addressable when JustPark supplies an address, slow to read."""

from __future__ import annotations

import smtplib
from email.message import EmailMessage

from parking_guard.contacts import Contact
from parking_guard.messaging.base import AUDIENCE_BOTH, Channel, ChannelError, Message


class EmailChannel(Channel):
    name = "email"
    audience = AUDIENCE_BOTH

    def __init__(
        self,
        *,
        host: str,
        port: int = 587,
        username: str = "",
        password: str = "",
        sender: str = "",
        use_tls: bool = True,
        use_ssl: bool = False,
        timeout: float = 20.0,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.sender = sender or username
        self.use_tls = use_tls
        self.use_ssl = use_ssl
        self.timeout = timeout

    def send(self, contact: Contact, message: Message) -> str:
        to = contact.endpoint(self.name)
        if not to:
            raise ChannelError("no email address for this booking")

        mail = EmailMessage()
        mail["Subject"] = message.title
        mail["From"] = self.sender
        mail["To"] = to
        mail.set_content(message.text())

        try:
            with self._connect() as server:
                if self.use_tls and not self.use_ssl:
                    server.starttls()
                if self.username:
                    server.login(self.username, self.password)
                server.send_message(mail)
        except (smtplib.SMTPException, OSError) as exc:
            raise ChannelError(f"email send failed: {exc}") from exc

        return str(mail.get("Message-ID", "") or "")

    def _connect(self) -> smtplib.SMTP:
        if self.use_ssl:
            return smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout)
        return smtplib.SMTP(self.host, self.port, timeout=self.timeout)
