"""Lightweight SMTP email sender.

Stdlib only (no new dependency). Two rules:
  * degrade gracefully - if SMTP isn't configured, log and no-op so nothing
    breaks in dev or before credentials are added;
  * never block the request - the actual socket work runs on a daemon
    thread, so a slow or failing mail server can't hang a page.

Callers build absolute links (url_for(_external=True)) before calling, so
the send can run outside the request context.
"""

import smtplib
import ssl
import threading
from email.message import EmailMessage

from flask import current_app, render_template


def _config():
    c = current_app.config
    # Gmail shows App Passwords grouped as "xxxx xxxx xxxx xxxx"; people
    # paste them with the spaces, but the real secret has none. Strip
    # whitespace so a copy-pasted app password just works.
    password = (c.get("MAIL_PASSWORD") or "").replace(" ", "")
    return {
        "host": c.get("MAIL_SERVER"),
        "port": int(c.get("MAIL_PORT") or 587),
        "username": c.get("MAIL_USERNAME"),
        "password": password,
        "sender": c.get("MAIL_DEFAULT_SENDER") or c.get("MAIL_USERNAME"),
        "use_tls": bool(c.get("MAIL_USE_TLS", True)),
    }


def email_enabled():
    c = _config()
    return bool(c["host"] and c["username"] and c["password"])


def send_email(to, subject, body_text, body_html=None):
    """Queue an email for background delivery. Returns True if it was
    queued (SMTP configured), False if email is disabled."""
    if not to:
        return False

    if not email_enabled():
        current_app.logger.info(
            "Email disabled (no SMTP config); skipping '%s' to %s", subject, to
        )
        return False

    cfg = _config()

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["sender"]
    msg["To"] = to if isinstance(to, str) else ", ".join(to)
    msg.set_content(body_text)
    if body_html:
        msg.add_alternative(body_html, subtype="html")

    app = current_app._get_current_object()

    def _deliver():
        try:
            with smtplib.SMTP(cfg["host"], cfg["port"], timeout=20) as server:
                if cfg["use_tls"]:
                    server.starttls(context=ssl.create_default_context())
                server.login(cfg["username"], cfg["password"])
                server.send_message(msg)
        except Exception:
            app.logger.exception("Failed to send email '%s' to %s", subject, to)

    threading.Thread(target=_deliver, daemon=True).start()
    return True


def send_notification_email(user, subject, body, link=None):
    """Convenience wrapper: email a user a notification-style message with
    an optional call-to-action link. Best-effort; safe to call anywhere."""
    if not user or not getattr(user, "email", None):
        return False

    html = render_template(
        "email/notification.html",
        user=user,
        subject=subject,
        body=body,
        link=link,
    )
    text = body + (f"\n\nOpen: {link}" if link else "")
    return send_email(user.email, subject, text, html)
