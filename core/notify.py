"""Out-of-band pick-fire notifications (roadmap #1d).

Email via SMTP and SMS via the Twilio REST API. Both are env-gated and
best-effort — a missing or failing channel never breaks the alert path or the
caller. No new dependencies: stdlib smtplib for email, the already-present
requests for Twilio.

Email env:  ALERT_EMAIL_TO, SMTP_HOST, SMTP_PORT(=587), SMTP_USER, SMTP_PASS, SMTP_FROM
SMS env:    ALERT_SMS_TO, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM
Global:     ALERTS_ENABLED(=1) gates everything (shared with Telegram).
"""
import os
from core.quiet import note_suppressed
import logging
import smtplib
from email.mime.text import MIMEText

LOGGER = logging.getLogger("ghost.notify")


def _alerts_enabled() -> bool:
    return os.getenv("ALERTS_ENABLED", "1") not in ("0", "false", "False", "")


def email_configured() -> bool:
    return bool(os.getenv("SMTP_HOST") and os.getenv("SMTP_FROM") and os.getenv("ALERT_EMAIL_TO"))


def sms_configured() -> bool:
    return bool(os.getenv("TWILIO_ACCOUNT_SID") and os.getenv("TWILIO_AUTH_TOKEN")
                and os.getenv("TWILIO_FROM") and os.getenv("ALERT_SMS_TO"))


def _recipients(env_val: str):
    return [a.strip() for a in (env_val or "").split(",") if a.strip()]


def send_email(subject: str, body: str) -> bool:
    """Send a plaintext alert email via SMTP (STARTTLS). No-op + False unless
    fully configured. Never raises."""
    if not (_alerts_enabled() and email_configured()):
        return False
    try:
        host = os.getenv("SMTP_HOST")
        port = int(os.getenv("SMTP_PORT", "587"))
        user = os.getenv("SMTP_USER", "")
        pw = os.getenv("SMTP_PASS", "")
        frm = os.getenv("SMTP_FROM")
        to = os.getenv("ALERT_EMAIL_TO")
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = frm
        msg["To"] = to
        with smtplib.SMTP(host, port, timeout=15) as s:
            try:
                s.starttls()
            except Exception:
                note_suppressed()  # server may not support STARTTLS (e.g. local relay)
            if user and pw:
                s.login(user, pw)
            s.sendmail(frm, _recipients(to), msg.as_string())
        LOGGER.info("Alert email sent to %s", to)
        return True
    except Exception as e:
        LOGGER.error("Alert email failed: %s", str(e)[:140])
        return False


def send_sms(body: str) -> bool:
    """Send an alert SMS to each ALERT_SMS_TO recipient via the Twilio REST API.
    No-op + False unless fully configured. Never raises."""
    if not (_alerts_enabled() and sms_configured()):
        return False
    try:
        import requests
        sid = os.getenv("TWILIO_ACCOUNT_SID")
        tok = os.getenv("TWILIO_AUTH_TOKEN")
        frm = os.getenv("TWILIO_FROM")
        to = os.getenv("ALERT_SMS_TO")
        url = "https://api.twilio.com/2010-04-01/Accounts/" + sid + "/Messages.json"
        ok_all = True
        for dest in _recipients(to):
            r = requests.post(url, auth=(sid, tok),
                              data={"From": frm, "To": dest, "Body": body[:1500]}, timeout=15)
            if not getattr(r, "ok", False):
                ok_all = False
                LOGGER.error("Twilio SMS to %s failed: %s", dest, getattr(r, "text", "")[:120])
        if ok_all:
            LOGGER.info("Alert SMS sent to %s", to)
        return ok_all
    except Exception as e:
        LOGGER.error("Alert SMS failed: %s", str(e)[:140])
        return False


def notify_pick_fired(subject: str, body: str) -> dict:
    """Dispatch a fired-pick alert to every configured out-of-band channel.
    Returns {"email": bool, "sms": bool}. Best-effort; never raises."""
    return {"email": send_email(subject, body), "sms": send_sms(body)}
