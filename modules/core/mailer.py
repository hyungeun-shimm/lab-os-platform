"""SMTP email sending for the ELN (password-reset codes, etc.).

Configured entirely through environment variables, set in the systemd unit's
EnvironmentFile (`/opt/labos/secrets/smtp.env` on the VM):

    LABOS_SMTP_HOST       e.g. smtp.gmail.com
    LABOS_SMTP_PORT       587 for STARTTLS (default), or 465 for implicit TLS
    LABOS_SMTP_USER       full Gmail address
    LABOS_SMTP_PASS       a Gmail *App Password* (16 chars; not the login password)
    LABOS_SMTP_FROM       From: address (defaults to LABOS_SMTP_USER)
    LABOS_SMTP_FROM_NAME  From: display name (default "Lab OS")

If host/user/pass are not all present, `smtp_configured()` returns False and callers
fall back to lab-local behavior (returning the code in the API response).
"""

from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage


def _cfg():
    user = (os.environ.get("LABOS_SMTP_USER") or "").strip()
    return {
        "host": (os.environ.get("LABOS_SMTP_HOST") or "").strip(),
        "port": int(os.environ.get("LABOS_SMTP_PORT") or 587),
        "user": user,
        "password": os.environ.get("LABOS_SMTP_PASS") or "",
        "from_addr": (os.environ.get("LABOS_SMTP_FROM") or user).strip(),
        "from_name": (os.environ.get("LABOS_SMTP_FROM_NAME") or "Lab OS").strip(),
    }


def smtp_configured() -> bool:
    c = _cfg()
    return bool(c["host"] and c["user"] and c["password"])


def send_email(to_addr: str, subject: str, body: str) -> None:
    """Send a plain-text email. Raises on failure; the caller decides how to surface it."""
    c = _cfg()
    if not (c["host"] and c["user"] and c["password"]):
        raise RuntimeError("SMTP is not configured")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f'{c["from_name"]} <{c["from_addr"]}>'
    msg["To"] = to_addr
    msg.set_content(body)

    ctx = ssl.create_default_context()
    if c["port"] == 465:
        with smtplib.SMTP_SSL(c["host"], c["port"], context=ctx, timeout=20) as s:
            s.login(c["user"], c["password"])
            s.send_message(msg)
    else:
        with smtplib.SMTP(c["host"], c["port"], timeout=20) as s:
            s.ehlo()
            s.starttls(context=ctx)
            s.login(c["user"], c["password"])
            s.send_message(msg)
