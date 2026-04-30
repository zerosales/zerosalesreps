"""
blueprints/public/routes.py — Public-facing routes (per tenant).

Routes:
  /           landing page (signup form)
  /signup     lead capture POST
  /thankyou   post-signup confirmation
  /unsubscribe/<token>
  /track/open/<token>    (1×1 pixel)
  /track/click/<token>   (redirect)
  /checkout              Stripe Checkout session
  /checkout/success
  /webhook/stripe        Stripe webhook
"""

import os
import secrets
import logging
from datetime import datetime, timedelta

import stripe
from flask import (Blueprint, render_template, request, redirect,
                   url_for, flash, abort, g, current_app, Response)
from flask_login import login_required

from models import db, Lead, EmailLog, Tenant

log = logging.getLogger(__name__)
public_bp = Blueprint("public", __name__, template_folder="../../templates/public")


# ─────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────

def _require_tenant():
    if not g.tenant:
        abort(404)
    return g.tenant


def _config_obj(tenant: Tenant):
    """Wrap tenant config dict as an object with attribute access for templates."""
    cfg = type("Cfg", (), {})()
    for k, v in (tenant.config or {}).items():
        setattr(cfg, k, v)
    # urls sub-object
    urls_dict = (tenant.config or {}).get("urls", {})
    cfg.urls = type("Urls", (), urls_dict)()
    return cfg


# ─────────────────────────────────────────────
#  Landing page
# ─────────────────────────────────────────────

@public_bp.route("/")
def landing():
    tenant = _require_tenant()
    return render_template("public/landing.html",
                           config=_config_obj(tenant),
                           tool=tenant.slug)


# ─────────────────────────────────────────────
#  Lead signup
# ─────────────────────────────────────────────

@public_bp.route("/signup", methods=["POST"])
def signup():
    tenant = _require_tenant()
    email      = request.form.get("email", "").strip().lower()
    first_name = request.form.get("first_name", "").strip()
    last_name  = request.form.get("last_name", "").strip()

    if not email or "@" not in email:
        flash("Please enter a valid email address.", "error")
        return redirect(url_for("public.landing"))

    # Upsert lead
    lead = Lead.query.filter_by(tenant_id=tenant.id, email=email).first()
    if lead:
        if lead.unsubscribed:
            lead.unsubscribed = False
    else:
        lead = Lead(
            tenant_id=tenant.id,
            email=email,
            first_name=first_name,
            last_name=last_name,
            source=request.form.get("source") or request.referrer,
            utm_source=request.args.get("utm_source"),
            utm_medium=request.args.get("utm_medium"),
            utm_campaign=request.args.get("utm_campaign"),
            sequence_name="nurture",
            sequence_step=0,
            next_email_at=datetime.utcnow(),   # send step 1 immediately
        )
        db.session.add(lead)

    db.session.commit()
    return redirect(url_for("public.thankyou"))


@public_bp.route("/thankyou")
def thankyou():
    tenant = _require_tenant()
    return render_template("public/thankyou.html", config=_config_obj(tenant))


# ─────────────────────────────────────────────
#  Unsubscribe
# ─────────────────────────────────────────────

@public_bp.route("/unsubscribe/<token>")
def unsubscribe(token):
    lead = Lead.query.filter_by(unsubscribe_token=token).first()
    if not lead:
        abort(404)
    lead.unsubscribed    = True
    lead.sequence_paused = True
    lead.next_email_at   = None
    db.session.commit()
    tenant = lead.tenant
    return render_template("public/unsubscribed.html", config=_config_obj(tenant))


# ─────────────────────────────────────────────
#  Email tracking
# ─────────────────────────────────────────────

_PIXEL = (
    b"\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff"
    b"\x00\x00\x00\x21\xf9\x04\x00\x00\x00\x00\x00\x2c\x00\x00\x00\x00"
    b"\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b"
)


@public_bp.route("/track/open/<token>")
def track_open(token):
    log_entry = EmailLog.query.filter_by(open_token=token).first()
    if log_entry and log_entry.status == "sent":
        log_entry.status    = "opened"
        log_entry.opened_at = datetime.utcnow()
        log_entry.lead.emails_opened += 1
        db.session.commit()
    return Response(_PIXEL, mimetype="image/gif",
                    headers={"Cache-Control": "no-store, no-cache"})


@public_bp.route("/track/click/<token>")
def track_click(token):
    log_entry = EmailLog.query.filter_by(click_token=token).first()
    dest = "/"
    if log_entry:
        if log_entry.status in ("sent", "opened"):
            log_entry.status     = "clicked"
            log_entry.clicked_at = datetime.utcnow()
            log_entry.lead.emails_clicked += 1
            db.session.commit()
        dest = log_entry.cta_url or "/"
    return redirect(dest)


# ─────────────────────────────────────────────
#  Stripe Checkout
# ─────────────────────────────────────────────

@public_bp.route("/checkout")
def checkout():
    tenant = _require_tenant()
    plan   = request.args.get("plan", "starter").lower()
    email  = request.args.get("email", "")

    stripe_prices = (tenant.config or {}).get("stripe_prices", {})
    price_id = stripe_prices.get(plan)
    if not price_id:
        flash("Invalid plan selected.", "error")
        return redirect(url_for("public.landing"))

    stripe.api_key = current_app.config["STRIPE_SECRET_KEY"]
    stripe_account = tenant.stripe_account_id   # None → use platform account

    base_url = current_app.config["BASE_URL"].rstrip("/")
    kwargs = dict(
        payment_method_types=["card"],
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        customer_email=email or None,
        success_url=f"{base_url}/checkout/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{base_url}/",
        metadata={"tenant_id": str(tenant.id), "plan": plan},
    )
    # Platform fee via Stripe Connect (if tenant has connected account)
    if stripe_account:
        fee_pct = float(tenant.platform_fee_pct or current_app.config.get("PLATFORM_FEE_PCT", 5.0))
        kwargs["stripe_account"] = stripe_account
        # subscription mode requires application_fee_percent in subscription_data
        kwargs["subscription_data"] = {
            "application_fee_percent": fee_pct,
        }

    try:
        session = stripe.checkout.Session.create(**kwargs)
        return redirect(session.url, code=303)
    except Exception as exc:
        log.error("Stripe checkout error: %s", exc)
        flash("Payment error — please try again.", "error")
        return redirect(url_for("public.landing"))


@public_bp.route("/checkout/success")
def checkout_success():
    tenant = _require_tenant()
    return render_template("public/checkout_success.html", config=_config_obj(tenant))


# ─────────────────────────────────────────────
#  Lead Magnet page
# ─────────────────────────────────────────────

@public_bp.route("/free")
def lead_magnet():
    """
    Configurable free-resource download page.
    Visitor enters email → becomes a lead → gets download link in welcome email.
    Config stored in tenant.lead_magnet: {title, description, file_url, button_text, image_url}
    """
    tenant = _require_tenant()
    magnet = tenant.lead_magnet or {}
    if not magnet.get("title"):
        # No lead magnet configured — redirect to main landing
        return redirect(url_for("public.landing"))
    return render_template("public/lead_magnet.html",
                           config=_config_obj(tenant),
                           magnet=magnet, tool=tenant.slug)


@public_bp.route("/free/signup", methods=["POST"])
def lead_magnet_signup():
    """Capture lead from magnet page and send them to the download."""
    tenant = _require_tenant()
    email      = request.form.get("email", "").strip().lower()
    first_name = request.form.get("first_name", "").strip()

    if not email or "@" not in email:
        flash("Please enter a valid email address.", "error")
        return redirect(url_for("public.lead_magnet"))

    from models import Lead
    lead = Lead.query.filter_by(tenant_id=tenant.id, email=email).first()
    if not lead:
        lead = Lead(
            tenant_id     = tenant.id,
            email         = email,
            first_name    = first_name,
            source        = "lead_magnet",
            sequence_name = "nurture",
            sequence_step = 0,
            next_email_at = datetime.utcnow(),
        )
        db.session.add(lead)
        db.session.commit()

    # Redirect to the download URL
    file_url = (tenant.lead_magnet or {}).get("file_url", "/")
    return redirect(file_url)


# ─────────────────────────────────────────────
#  Stripe Webhook
# ─────────────────────────────────────────────

@public_bp.route("/webhook/stripe", methods=["POST"])
def stripe_webhook():
    payload    = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")
    secret     = current_app.config.get("STRIPE_WEBHOOK_SECRET", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, secret)
    except (ValueError, stripe.error.SignatureVerificationError):
        return {"error": "Invalid signature"}, 400

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        _handle_checkout_complete(session)
    elif event["type"] == "customer.subscription.deleted":
        sub = event["data"]["object"]
        _handle_subscription_cancelled(sub)

    return {"status": "ok"}, 200


def _handle_checkout_complete(session):
    tenant_id = int(session.get("metadata", {}).get("tenant_id", 0))
    plan      = session.get("metadata", {}).get("plan", "starter")
    customer_email = session.get("customer_email") or ""
    stripe_customer_id = session.get("customer")
    stripe_subscription_id = session.get("subscription")

    if not tenant_id or not customer_email:
        return

    lead = Lead.query.filter_by(tenant_id=tenant_id,
                                email=customer_email.lower()).first()
    if not lead:
        return

    lead.status                 = "customer"
    lead.plan                   = plan
    lead.stripe_customer_id     = stripe_customer_id
    lead.stripe_subscription_id = stripe_subscription_id
    lead.sequence_name          = "onboarding"
    lead.sequence_step          = 0
    lead.sequence_paused        = False
    lead.next_email_at          = datetime.utcnow()  # onboarding step 1 immediately

    # Rough MRR from plan name (tenant config)
    tenant = Tenant.query.get(tenant_id)
    if tenant:
        pricing = (tenant.config or {}).get("pricing", [])
        for p in pricing:
            if p.get("plan", "").lower() == plan:
                price_str = p.get("price", "$0").replace("$", "").replace("/mo", "").strip()
                try:
                    lead.mrr = float(price_str)
                except ValueError:
                    pass

    db.session.commit()
    log.info("Lead %s upgraded to customer (tenant %s)", lead.email, tenant_id)


def _handle_subscription_cancelled(sub):
    stripe_subscription_id = sub.get("id")
    if not stripe_subscription_id:
        return
    lead = Lead.query.filter_by(stripe_subscription_id=stripe_subscription_id).first()
    if lead:
        lead.status = "churned"
        lead.mrr    = 0.0
        db.session.commit()
        log.info("Lead %s churned", lead.email)
