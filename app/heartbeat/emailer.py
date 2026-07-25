"""The nightly Kiefer note — sent from Paul's Gmail via SMTP app-password
(simple, no OAuth; the full Gmail API lands in Phase 2)."""
from __future__ import annotations

import asyncio
import logging
import smtplib
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


class Emailer:
    def __init__(self, gmail_address: str, app_password: str) -> None:
        self._address = gmail_address
        self._password = app_password

    @property
    def configured(self) -> bool:
        return bool(self._address and self._password)

    async def send(self, to: str, subject: str, body: str) -> bool:
        if not self.configured or not to:
            logger.warning("Email not configured — skipping '%s'", subject)
            return False

        def _send() -> None:
            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = subject
            msg["From"] = self._address
            msg["To"] = to
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
                smtp.login(self._address, self._password)
                smtp.sendmail(self._address, [to], msg.as_string())

        try:
            await asyncio.to_thread(_send)
            return True
        except Exception:
            logger.exception("Email send failed")
            return False
