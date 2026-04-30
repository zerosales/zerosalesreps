"""
email_engine.py — Tenant-aware email sending with open/click tracking.

Flow:
  send_sequence_email(lead, step_def, app)
    → resolves tenant config
    → renders Jinja2 template
    → creates EmailLog with tracking tokens
    → sends via SendGrid (primary) or SMTP (fallback)
    → schedules next email on lead
"""

import os
import smtplib
import logging
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from flask import render_template

from models import db, Lead, EmailLog

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────

def _tracking_url(base_url: str, token: str, kind: str) -> str:
    """Build an absolute tracking URL."""
    return f"{base_url.rstrip('/')}/track/{kind}/{token}"


def _resolve_cta_url(tenant, cta_url_key: str) -> str:
    """Get the CTA URL from tenant config by key (trial_url, app_url, pricing_url)."""
    urls = tenant.get_config("urls", {})
    return urls.get(cta_url_key, tenant.get_config("urls", {}).get("trial_url", "#"))


def _fill_subject(subject: str, tenant) -> str:
    """Replace {tool_name} and {pain_point} placeholders in subject."""
    return subject.format(
        tool_name=tenant.get_config("name", ""),
        pain_point=tenant.get_config("pain_point", ""),
    )


# ─────────────────────────────────────────────
#  Main send function
# ─────────────────────────────────────────────

def send_sequence_email(lead: Lead, step_def: dict, app) -> bool:
    """
    Render and send one sequence email to a lead.
    Returns True on success, False on failure.
    """
    tenant = lead.tenant

    with app.app_context():
        # Build EmailLog record with tracking tokens
        log_entry = EmailLog(
            tenant_id=tenant.id,
            lead_id=lead.id,
            sequence=step_def["sequence_name"],
            step=step_def["step"],
            subject=_fill_subject(step_def["subject"], tenant),
            template=step_def["template"],
        )
        db.session.add(log_entry)
        db.session.flush()   # get log_entry.id without committing

        base_url = app.config.get("BASE_URL", "http://localhost:5000")
        open_pixel = _tracking_url(base_url, log_entry.open_token, "open")
        click_redirect = _tracking_url(base_url, log_entry.click_token, "click")
        cta_raw_url = _resolve_cta_url(tenant, step_def.get("cta_url_key", "trial_url"))
        log_entry.cta_url = cta_raw_url

        # Build unsubscribe URL
        unsubscribe_url = f"{base_url.rstrip('/')}/unsubscribe/{lead.unsubscribe_token}"

        # Render HTML
        try:
            html_body = render_template(
                f"emails/{step_def['template']}.html",
                lead=lead,
                config=type("Config", (), tenant.config or {})(),  # dict → attr access
                cta_url=click_redirect,
                unsubscribe_url=unsubscribe_url,
                open_pixel_url=open_pixel,
                subject=log_entry.subject,
            )
        except Exception as exc:
            log.error("Template render failed for %s: %s", step_def["template"], exc)
            # Fall back to generic template
            html_body = render_template(
                "emails/generic.html",
                lead=lead,
                config=type("Config", (), tenant.config or {})(),
                cta_url=click_redirect,
                unsubscribe_url=unsubscribe_url,
                open_pixel_url=open_pixel,
                subject=log_entry.subject,
                body_text=f"Here's an update from {tenant.name}.",
                cta_label="Visit " + tenant.name,
            )

        # Send
        from_email = tenant.get_config("from_email") or app.config.get("SMTP_USER", "noreply@example.com")
        from_name  = tenant.get_config("from_name") or tenant.name
        success = _send(
            from_email=from_email,
            from_name=from_name,
            to_email=lead.email,
            subject=log_entry.subject,
            html_body=html_body,
            app=app,
        )

        if success:
            log_entry.status = "sent"
            lead.emails_sent += 1
            schedule_next_email(lead, step_def)
        else:
            log_entry.status = "failed"

        db.session.commit()
        return success


def schedule_next_email(lead: Lead, current_step: dict):
    """Set lead.next_email_at based on the next step's day_offset."""
    from sequences import get_next_step
    nxt = get_next_step(lead.sequence_name, current_step["step"])
    if nxt:
        gap_days = nxt["day_offset"] - current_step["day_offset"]
        lead.next_email_at = datetime.utcnow() + timedelta(days=max(gap_days, 1))
        lead.sequence_step = current_step["step"]
    else:
        # Sequence complete
        lead.next_email_at = None
        lead.sequence_paused = True


# ─────────────────────────────────────────────
#  Transport: SendGrid → SMTP fallback
# ─────────────────────────────────────────────

def _send(from_email, from_name, to_email, subject, html_body, app) -> bool:
    sg_key = app.config.get("SENDGRID_API_KEY", "")
    if sg_key:
        return _send_sendgrid(sg_key, from_email, from_name, to_email, subject, html_body)
    return _send_smtp(from_email, from_name, to_email, subject, html_body, app)


def _send_sendgrid(api_key, from_email, from_name, to_email, subject, html_body) -> bool:
    try:
        import sendgrid
        from sendgrid.helpers.mail import Mail, Email, To, Content
        sg = sendgrid.SendGridAPIClient(api_key=api_key)
        message = Mail(
            from_email=Email(from_email, from_name),
            to_emails=To(to_email),
            subject=subject,
            html_content=Content("text/html", html_body),
        )
        resp = sg.client.mail.send.post(request_body=message.get())
        return resp.status_code in (200, 201, 202)
    except Exception as exc:
        log.error("SendGrid send failed: %s", exc)
        return False


def _send_smtp(from_email, from_name, to_email, subject, html_body, app) -> bool:
    try:
        host = app.config.get("SMTP_HOST")
        port = app.config.get("SMTP_PORT", 587)
        user = app.config.get("SMTP_USER")
        pwd  = app.config.get("SMTP_PASS")
        if not host or not user:
            log.warning("No email transport configured")
            return False

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"{from_name} <{from_email}>"
        msg["To"]      = to_email
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP(host, port) as server:
            server.ehlo()
            server.starttls()
            server.login(user, pwd)
            server.sendmail(from_email, to_email, msg.as_string())
        return True
    except Exception as exc:
        log.error("SMTP send failed: %s", exc)
        return False
