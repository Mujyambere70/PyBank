"""
notify.py - transaction alerts by email and SMS.

Every alert is always saved in the `alerts` table, so the customer sees it in Net Banking (Alerts page).
It is also really sent when a provider is configured with environment variables:

  Email (choose one)
    BREVO_API_KEY + ALERT_EMAIL_FROM          Brevo email API over HTTPS (free plan; works on Render free)
    SMTP_HOST [+ SMTP_PORT, SMTP_USER,        normal SMTP server (works on your computer; Render's free
               SMTP_PASSWORD] + ALERT_EMAIL_FROM   plan blocks SMTP ports 25/465/587)
  SMS
    TWILIO_ACCOUNT_SID + TWILIO_AUTH_TOKEN + TWILIO_FROM     Twilio SMS API over HTTPS
  Push (phones that installed the PyBank app and allowed notifications)
    nothing to configure: the VAPID keys are made automatically (push.py)

Sending happens in a background thread after the database commit, so a slow or failing
provider never slows down or breaks a payment.
"""

import base64
import json
import os
import smtplib
import threading
import urllib.parse
import urllib.request
from email.message import EmailMessage


class Notifier:
    def __init__(self, env=None, smtp_factory=None, http=None, background=True):
        self.env = os.environ if env is None else env
        self.smtp_factory = smtp_factory or smtplib.SMTP
        self.http = http or urllib.request.urlopen
        self.background = background

    # ---------- configuration
    def email_provider(self):
        if self.env.get("BREVO_API_KEY") and self.env.get("ALERT_EMAIL_FROM"):
            return "brevo"
        if self.env.get("SMTP_HOST") and self.env.get("ALERT_EMAIL_FROM"):
            return "smtp"
        return None

    def sms_provider(self):
        if all(self.env.get(k) for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM")):
            return "twilio"
        return None

    def status(self):
        return {"email": self.email_provider(), "sms": self.sms_provider()}

    # ---------- sending
    def send_email(self, to, subject, text, attachment=None):
        """attachment = (file name, bytes) or None."""
        provider = self.email_provider()
        sender = self.env["ALERT_EMAIL_FROM"]
        if provider == "brevo":
            payload = {"sender": {"name": "PyBank Alerts", "email": sender}, "to": [{"email": to}],
                       "subject": subject, "textContent": text}
            if attachment:
                payload["attachment"] = [{"name": attachment[0], "content": base64.b64encode(attachment[1]).decode()}]
            body = json.dumps(payload).encode()
            req = urllib.request.Request("https://api.brevo.com/v3/smtp/email", data=body, method="POST",
                                         headers={"api-key": self.env["BREVO_API_KEY"], "Content-Type": "application/json",
                                                  "Accept": "application/json"})
            with self.http(req, timeout=15) as resp:
                if resp.status >= 300:
                    raise RuntimeError(f"Brevo answered {resp.status}")
        elif provider == "smtp":
            msg = EmailMessage()
            msg["From"], msg["To"], msg["Subject"] = sender, to, subject
            msg.set_content(text)
            if attachment:
                sub = "pdf" if attachment[0].lower().endswith(".pdf") else "gzip"
                msg.add_attachment(attachment[1], maintype="application", subtype=sub, filename=attachment[0])
            port = int(self.env.get("SMTP_PORT", "587"))
            with self.smtp_factory(self.env["SMTP_HOST"], port, timeout=15) as s:
                if port != 25:
                    s.starttls()
                if self.env.get("SMTP_USER"):
                    s.login(self.env["SMTP_USER"], self.env.get("SMTP_PASSWORD", ""))
                s.send_message(msg)
        else:
            raise RuntimeError("no email provider configured")

    def send_sms(self, to, text):
        if self.sms_provider() != "twilio":
            raise RuntimeError("no SMS provider configured")
        sid, token = self.env["TWILIO_ACCOUNT_SID"], self.env["TWILIO_AUTH_TOKEN"]
        number = to if to.startswith("+") else "+91" + to[-10:]          # Indian mobile numbers
        data = urllib.parse.urlencode({"To": number, "From": self.env["TWILIO_FROM"], "Body": text}).encode()
        req = urllib.request.Request(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json", data=data,
                                     method="POST", headers={
                                         "Authorization": "Basic " + base64.b64encode(f"{sid}:{token}".encode()).decode()})
        with self.http(req, timeout=15) as resp:
            if resp.status >= 300:
                raise RuntimeError(f"Twilio answered {resp.status}")

    def send_push(self, sub, title, text):
        import push
        push.send(sub, title, text, http=self.http)

    def deliver(self, jobs, on_result):
        """jobs: list of (alert_id, channel, to, subject, text). on_result(alert_id, status) saves the outcome.
        For push, `to` is the subscription (endpoint, keys and the server's VAPID keys)."""
        def work():
            for alert_id, channel, to, subject, text in jobs:
                try:
                    if channel == "email":
                        self.send_email(to, subject, text)
                    elif channel == "push":
                        self.send_push(to, subject, text)
                    else:
                        self.send_sms(to, text)
                    on_result(alert_id, "Sent")
                except Exception as e:                       # keep the reason, never crash
                    on_result(alert_id, f"Failed: {str(e)[:120]}")
        if not jobs:
            return
        if self.background:
            threading.Thread(target=work, daemon=True).start()
        else:
            work()
