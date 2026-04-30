"""
blueprints/tenant_admin/routes.py — Tenant admin dashboard.

Routes (all prefixed /dashboard):
  GET  /login          login form
  POST /login
  GET  /logout
  GET  /               overview dashboard
  GET  /leads          full lead list + filters
  GET  /lead/<id>      lead detail
  POST /lead/<id>/status
  POST /lead/<id>/send-email
  GET  /export         CSV download
  GET  /settings       tool config editor
  POST /settings
  GET  /stripe/connect   Stripe Connect onboarding
  GET  /stripe/callback  Stripe Connect OAuth callback
  GET  /sequences      view/edit sequences
"""

import csv
import io
import json
import logging
from datetime import datetime, timedelta
from functools import wraps

import stripe
from flask import (Blueprint, render_template, request, redirect,
                   url_for, flash, g, current_app, Response, session)
from flask_login import login_user, logout_user, login_required, current_user

from models import db, Lead, EmailLog, TenantUser, Tenant
from sequences import SEQUENCES

log = logging.getLogger(__name__)
tenant_admin_bp = Blueprint("tenant_admin", __name__,
                             template_folder="../../templates/tenant_admin")


# ─────────────────────────────────────────────
#  Auth helpers
# ─────────────────────────────────────────────

def tenant_login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not isinstance(current_user, TenantUser):
            return redirect(url_for("tenant_admin.login"))
        # Verify user belongs to the current tenant
        if g.tenant and current_user.tenant_id != g.tenant.id:
            return redirect(url_for("tenant_admin.login"))
        return f(*args, **kwargs)
    return decorated


def _get_tenant():
    """Get tenant from g (set by middleware) or from logged-in user."""
    if g.tenant:
        return g.tenant
    if current_user.is_authenticated and isinstance(current_user, TenantUser):
        return current_user.tenant
    return None


# ─────────────────────────────────────────────
#  Login / Logout
# ─────────────────────────────────────────────

@tenant_admin_bp.route("/login", methods=["GET", "POST"])
def login():
    tenant = _get_tenant() or g.tenant
    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = TenantUser.query.filter_by(email=email).first()
        if user and user.check_password(password):
            # If we have a tenant context, verify ownership
            if tenant and user.tenant_id != tenant.id:
                flash("Invalid credentials.", "danger")
                return redirect(url_for("tenant_admin.login"))
            login_user(user)
            return redirect(url_for("tenant_admin.dashboard"))
        flash("Invalid email or password.", "danger")
    return render_template("tenant_admin/login.html", tenant=tenant)


@tenant_admin_bp.route("/logout")
def logout():
    logout_user()
    return redirect(url_for("tenant_admin.login"))


# ─────────────────────────────────────────────
#  Dashboard overview
# ─────────────────────────────────────────────

@tenant_admin_bp.route("/")
@tenant_login_required
def dashboard():
    from sqlalchemy import func, cast, Date
    tenant = current_user.tenant

    total_leads  = Lead.query.filter_by(tenant_id=tenant.id).count()
    nurturing    = Lead.query.filter_by(tenant_id=tenant.id, status="nurturing").count()
    trials       = Lead.query.filter_by(tenant_id=tenant.id, status="trial").count()
    customers    = Lead.query.filter_by(tenant_id=tenant.id, status="customer").count()
    mrr          = db.session.query(db.func.sum(Lead.mrr)).filter_by(tenant_id=tenant.id, status="customer").scalar() or 0

    # Email engagement
    all_logs     = EmailLog.query.filter_by(tenant_id=tenant.id)
    total_sent   = all_logs.count()
    total_opened = all_logs.filter(EmailLog.status.in_(["opened", "clicked"])).count()
    total_clicked= all_logs.filter_by(status="clicked").count()
    open_rate    = round(total_opened / total_sent * 100, 1) if total_sent else 0
    click_rate   = round(total_clicked / total_sent * 100, 1) if total_sent else 0
    conversion_rate = round(customers / total_leads * 100, 1) if total_leads else 0

    # Daily new leads last 30 days
    thirty_ago = datetime.utcnow() - timedelta(days=30)
    rows = (db.session.query(
                cast(Lead.created_at, Date).label("day"),
                func.count(Lead.id).label("cnt"))
            .filter(Lead.tenant_id == tenant.id, Lead.created_at >= thirty_ago)
            .group_by("day").order_by("day").all())
    daily_labels = [str(r.day) for r in rows]
    daily_data   = [r.cnt for r in rows]

    # Sources
    sources = (db.session.query(Lead.source, func.count(Lead.id))
               .filter_by(tenant_id=tenant.id)
               .group_by(Lead.source)
               .order_by(func.count(Lead.id).desc())
               .limit(8).all())

    recent_leads = (Lead.query.filter_by(tenant_id=tenant.id)
                    .order_by(Lead.created_at.desc()).limit(20).all())

    # ── Growth Engine extras ──────────────────────
    icp = tenant.icp or {}
    icp_configured = bool(icp.get("domains") or icp.get("keywords"))
    engine_running = (icp_configured and bool(icp.get("auto_prospect"))
                      and bool(tenant.hunter_api_key)
                      and tenant.onboarding_status == "launched")

    # Auto-prospected leads today
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    prospects_auto = (Lead.query
                      .filter(Lead.tenant_id == tenant.id,
                              Lead.source.like("auto:%"),
                              Lead.created_at >= today_start)
                      .count())

    # Pipeline velocity: leads per day (7-day avg)
    seven_ago = datetime.utcnow() - timedelta(days=7)
    recent_count = Lead.query.filter(
        Lead.tenant_id == tenant.id,
        Lead.created_at >= seven_ago).count()
    leads_per_day = round(recent_count / 7, 1)

    # Days to 100 customers projection
    customer_target = tenant.customer_target or 1000
    customers_needed = max(customer_target - customers, 0)
    if leads_per_day > 0 and conversion_rate > 0:
        leads_needed = customers_needed / (conversion_rate / 100)
        days_to_100  = int(leads_needed / leads_per_day)
    else:
        days_to_100 = 9999

    return render_template("tenant_admin/dashboard.html",
        tenant=tenant,
        total_leads=total_leads, nurturing=nurturing, trials=trials,
        customers=customers, mrr=mrr, open_rate=open_rate,
        click_rate=click_rate, conversion_rate=conversion_rate,
        daily_labels=daily_labels, daily_data=daily_data,
        sources=sources, recent_leads=recent_leads,
        # Growth Engine
        engine_running=engine_running, icp_configured=icp_configured,
        prospects_auto=prospects_auto, leads_per_day=leads_per_day,
        days_to_100=days_to_100, customer_target=customer_target)


# ─────────────────────────────────────────────
#  Lead list
# ─────────────────────────────────────────────

@tenant_admin_bp.route("/leads")
@tenant_login_required
def leads():
    tenant = current_user.tenant
    status_filter = request.args.get("status")
    seq_filter    = request.args.get("seq")
    q             = request.args.get("q", "").strip()

    query = Lead.query.filter_by(tenant_id=tenant.id)
    if status_filter:
        query = query.filter_by(status=status_filter)
    if seq_filter:
        query = query.filter_by(sequence_name=seq_filter)
    if q:
        query = query.filter(Lead.email.ilike(f"%{q}%"))

    leads_list = query.order_by(Lead.created_at.desc()).limit(200).all()
    return render_template("tenant_admin/leads.html",
        tenant=tenant, leads=leads_list,
        status_filter=status_filter, seq_filter=seq_filter, q=q,
        statuses=["new","nurturing","trial","customer","churned","expired"],
        sequences=list(SEQUENCES.keys()))


# ─────────────────────────────────────────────
#  Lead detail
# ─────────────────────────────────────────────

@tenant_admin_bp.route("/lead/<int:lead_id>")
@tenant_login_required
def lead_detail(lead_id):
    tenant = current_user.tenant
    lead = Lead.query.filter_by(id=lead_id, tenant_id=tenant.id).first_or_404()
    logs = lead.logs
    return render_template("tenant_admin/lead_detail.html",
        tenant=tenant, lead=lead, logs=logs,
        statuses=["new","nurturing","trial","customer","churned","expired"],
        sequences=list(SEQUENCES.keys()))


@tenant_admin_bp.route("/lead/<int:lead_id>/status", methods=["POST"])
@tenant_login_required
def update_lead_status(lead_id):
    tenant = current_user.tenant
    lead   = Lead.query.filter_by(id=lead_id, tenant_id=tenant.id).first_or_404()
    new_status = request.form.get("status")
    if new_status in ["new","nurturing","trial","customer","churned","expired"]:
        lead.status = new_status
        db.session.commit()
        flash(f"Status updated to {new_status}.", "success")
    return redirect(url_for("tenant_admin.lead_detail", lead_id=lead_id))


@tenant_admin_bp.route("/lead/<int:lead_id>/send-email", methods=["POST"])
@tenant_login_required
def send_email_manual(lead_id):
    tenant = current_user.tenant
    lead   = Lead.query.filter_by(id=lead_id, tenant_id=tenant.id).first_or_404()
    seq    = request.form.get("sequence", "nurture")
    step   = int(request.form.get("step", 1))

    from sequences import get_step
    from email_engine import send_sequence_email
    step_def = get_step(seq, step)
    if step_def:
        step_def = dict(step_def, sequence_name=seq)
        send_sequence_email(lead, step_def, current_app._get_current_object())
        flash(f"Sent {seq} step {step}.", "success")
    else:
        flash("Step not found.", "danger")
    return redirect(url_for("tenant_admin.lead_detail", lead_id=lead_id))


# ─────────────────────────────────────────────
#  CSV Export
# ─────────────────────────────────────────────

@tenant_admin_bp.route("/export")
@tenant_login_required
def export_csv():
    tenant = current_user.tenant
    leads_list = Lead.query.filter_by(tenant_id=tenant.id).order_by(Lead.created_at.desc()).all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["email","first_name","last_name","company","status","sequence",
                     "step","emails_sent","emails_opened","emails_clicked",
                     "source","utm_campaign","plan","mrr","created_at"])
    for l in leads_list:
        writer.writerow([l.email, l.first_name, l.last_name, l.company, l.status,
                         l.sequence_name, l.sequence_step, l.emails_sent,
                         l.emails_opened, l.emails_clicked, l.source,
                         l.utm_campaign, l.plan, l.mrr,
                         l.created_at.strftime("%Y-%m-%d") if l.created_at else ""])

    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment;filename={tenant.slug}_leads.csv"})


# ─────────────────────────────────────────────
#  Settings (tool config editor)
# ─────────────────────────────────────────────

@tenant_admin_bp.route("/settings", methods=["GET", "POST"])
@tenant_login_required
def settings():
    tenant = current_user.tenant

    if request.method == "POST":
        action = request.form.get("action")

        if action == "branding":
            cfg = tenant.config or {}
            cfg.update({
                "name":          request.form.get("name", "").strip(),
                "tagline":       request.form.get("tagline", "").strip(),
                "pain_point":    request.form.get("pain_point", "").strip(),
                "from_email":    request.form.get("from_email", "").strip(),
                "from_name":     request.form.get("from_name", "").strip(),
                "logo_url":      request.form.get("logo_url", "").strip(),
                "primary_color": request.form.get("primary_color", "#4F46E5").strip(),
                "company":       request.form.get("company", "").strip(),
                "trial_days":    int(request.form.get("trial_days", 14)),
            })
            cfg["urls"] = {
                "trial_url":   request.form.get("trial_url", "").strip(),
                "app_url":     request.form.get("app_url", "").strip(),
                "pricing_url": request.form.get("pricing_url", "").strip(),
            }
            tenant.config = cfg
            db.session.commit()
            flash("Branding settings saved.", "success")

        elif action == "features":
            try:
                features = json.loads(request.form.get("features_json", "[]"))
                cfg = tenant.config or {}
                cfg["features"] = features
                tenant.config = cfg
                db.session.commit()
                flash("Features updated.", "success")
            except json.JSONDecodeError:
                flash("Invalid JSON for features.", "danger")

        elif action == "pricing":
            try:
                pricing = json.loads(request.form.get("pricing_json", "[]"))
                stripe_prices = json.loads(request.form.get("stripe_prices_json", "{}"))
                cfg = tenant.config or {}
                cfg["pricing"] = pricing
                cfg["stripe_prices"] = stripe_prices
                tenant.config = cfg
                db.session.commit()
                flash("Pricing updated.", "success")
            except json.JSONDecodeError:
                flash("Invalid JSON.", "danger")

        elif action == "password":
            new_pw = request.form.get("new_password", "")
            if len(new_pw) >= 8:
                current_user.set_password(new_pw)
                db.session.commit()
                flash("Password updated.", "success")
            else:
                flash("Password must be at least 8 characters.", "danger")

        return redirect(url_for("tenant_admin.settings"))

    config = tenant.config or {}
    return render_template("tenant_admin/settings.html",
        tenant=tenant, config=config,
        features_json=json.dumps(config.get("features", []), indent=2),
        pricing_json=json.dumps(config.get("pricing", []), indent=2),
        stripe_prices_json=json.dumps(config.get("stripe_prices", {}), indent=2))


# ─────────────────────────────────────────────
#  Stripe Connect
# ─────────────────────────────────────────────

@tenant_admin_bp.route("/stripe/connect")
@tenant_login_required
def stripe_connect():
    tenant = current_user.tenant
    client_id = current_app.config.get("STRIPE_CONNECT_CLIENT_ID", "")
    if not client_id:
        flash("Stripe Connect is not configured on this platform.", "danger")
        return redirect(url_for("tenant_admin.settings"))

    base_url = current_app.config["BASE_URL"].rstrip("/")
    callback = f"{base_url}/dashboard/stripe/callback"
    url = (f"https://connect.stripe.com/oauth/authorize"
           f"?response_type=code&client_id={client_id}"
           f"&scope=read_write&redirect_uri={callback}"
           f"&state={tenant.slug}")
    return redirect(url)


@tenant_admin_bp.route("/stripe/callback")
@tenant_login_required
def stripe_callback():
    tenant = current_user.tenant
    code   = request.args.get("code")
    error  = request.args.get("error")

    if error:
        flash(f"Stripe Connect error: {error}", "danger")
        return redirect(url_for("tenant_admin.settings"))

    try:
        stripe.api_key = current_app.config["STRIPE_SECRET_KEY"]
        # stripe-python v7+ returns an OAuthToken object; fall back to dict access
        response = stripe.OAuth.token(grant_type="authorization_code", code=code)
        stripe_user_id = (
            response.stripe_user_id
            if hasattr(response, "stripe_user_id")
            else response["stripe_user_id"]
        )
        tenant.stripe_account_id     = stripe_user_id
        tenant.stripe_connect_status = "connected"
        db.session.commit()
        flash("Stripe account connected successfully!", "success")
    except stripe.oauth_error.OAuthError as exc:
        log.error("Stripe OAuth error: %s", exc)
        flash(f"Stripe Connect error: {exc.user_message or str(exc)}", "danger")
    except Exception as exc:
        log.error("Stripe Connect callback error: %s", exc)
        flash("Failed to connect Stripe account. Please try again.", "danger")

    return redirect(url_for("tenant_admin.settings"))


# ─────────────────────────────────────────────
#  Sequences view
# ─────────────────────────────────────────────

@tenant_admin_bp.route("/sequences")
@tenant_login_required
def sequences_view():
    tenant = current_user.tenant
    return render_template("tenant_admin/sequences.html",
                           tenant=tenant, sequences=SEQUENCES)


# ─────────────────────────────────────────────
#  Lead Capture hub routes (proxied from api blueprint)
# ─────────────────────────────────────────────

@tenant_admin_bp.route("/import")
@tenant_login_required
def leads_import():
    from models import ImportLog
    from sequences import SEQUENCES
    tenant = current_user.tenant
    imports = ImportLog.query.filter_by(tenant_id=tenant.id).order_by(
        ImportLog.created_at.desc()).limit(20).all()
    return render_template("tenant_admin/import.html",
        tenant=tenant, imports=imports,
        sequences=list(SEQUENCES.keys()),
        has_hunter_key=bool(tenant.hunter_api_key),
        request=request)


@tenant_admin_bp.route("/import/csv", methods=["POST"])
@tenant_login_required
def import_csv():
    """Delegate to api blueprint handler."""
    from blueprints.public.api import import_csv as _import
    return _import()


@tenant_admin_bp.route("/lead-magnet", methods=["GET", "POST"])
@tenant_login_required
def lead_magnet_settings():
    import json
    tenant = current_user.tenant

    if request.method == "POST":
        magnet = {
            "title":       request.form.get("title", "").strip(),
            "description": request.form.get("description", "").strip(),
            "file_url":    request.form.get("file_url", "").strip(),
            "button_text": request.form.get("button_text", "").strip(),
            "image_url":   request.form.get("image_url", "").strip(),
            "bullets":     [b.strip() for b in request.form.get("bullets", "").split("\n") if b.strip()],
        }
        tenant.lead_magnet = magnet
        db.session.commit()
        flash("Lead magnet saved.", "success")
        return redirect(url_for("tenant_admin.lead_magnet_settings"))

    magnet = tenant.lead_magnet or {}
    return render_template("tenant_admin/lead_magnet.html", tenant=tenant, magnet=magnet)


@tenant_admin_bp.route("/integrations", methods=["GET", "POST"])
@tenant_login_required
def integrations():
    tenant = current_user.tenant
    if request.method == "POST":
        tenant.hunter_api_key = request.form.get("hunter_api_key", "").strip()
        db.session.commit()
        flash("Integration settings saved.", "success")
        return redirect(url_for("tenant_admin.integrations"))
    return render_template("tenant_admin/integrations.html", tenant=tenant)


# ─────────────────────────────────────────────
#  ICP Configuration
# ─────────────────────────────────────────────

@tenant_admin_bp.route("/icp", methods=["GET", "POST"])
@tenant_login_required
def icp_settings():
    tenant = current_user.tenant

    if request.method == "POST":
        icp = {
            "domains":      [d.strip() for d in request.form.get("domains", "").split("\n") if d.strip()],
            "keywords":     [k.strip() for k in request.form.get("keywords", "").split("\n") if k.strip()],
            "job_titles":   [j.strip() for j in request.form.get("job_titles", "").split("\n") if j.strip()],
            "daily_limit":  int(request.form.get("daily_limit", 25)),
            "auto_prospect": bool(request.form.get("auto_prospect")),
        }
        tenant.icp = icp
        tenant.customer_target = int(request.form.get("customer_target", 1000))

        # Advance onboarding status
        if tenant.onboarding_status in (None, "pending"):
            tenant.onboarding_status = "icp_set"

        db.session.commit()
        flash("ICP saved. Your growth engine will search for matching prospects daily.", "success")
        return redirect(url_for("tenant_admin.icp_settings"))

    return render_template("tenant_admin/icp.html",
                           tenant=tenant, icp=tenant.icp or {})


# ─────────────────────────────────────────────
#  Day 0 Onboarding Wizard
# ─────────────────────────────────────────────

@tenant_admin_bp.route("/onboarding", methods=["GET", "POST"])
@tenant_login_required
def onboarding():
    import os
    tenant = current_user.tenant
    current_step = int(request.args.get("step", 1))

    if request.method == "POST":
        step = int(request.form.get("step", 1))
        action = request.form.get("action")

        if step == 1:
            # Product info
            cfg = tenant.config or {}
            cfg.update({
                "name":          request.form.get("name", "").strip(),
                "tagline":       request.form.get("tagline", "").strip(),
                "pain_point":    request.form.get("pain_point", "").strip(),
                "primary_color": request.form.get("primary_color", "#4F46E5").strip(),
                "urls": {
                    "app_url":   request.form.get("app_url", "").strip(),
                    "trial_url": request.form.get("trial_url", "").strip(),
                }
            })
            tenant.config = cfg
            db.session.commit()
            return redirect(url_for("tenant_admin.onboarding", step=2))

        elif step == 2:
            # ICP
            icp = {
                "domains":      [d.strip() for d in request.form.get("domains", "").split("\n") if d.strip()],
                "job_titles":   [j.strip() for j in request.form.get("job_titles", "").split("\n") if j.strip()],
                "daily_limit":  int(request.form.get("daily_limit", 25)),
                "auto_prospect": False,   # activated at final launch step
            }
            tenant.icp = icp
            if tenant.onboarding_status in (None, "pending"):
                tenant.onboarding_status = "icp_set"
            db.session.commit()
            return redirect(url_for("tenant_admin.onboarding", step=3))

        elif step == 3:
            # Stripe — just advance (connect happens via OAuth redirect)
            cfg = tenant.config or {}
            cfg["trial_days"] = int(request.form.get("trial_days", 14))
            tenant.config = cfg
            db.session.commit()
            return redirect(url_for("tenant_admin.onboarding", step=4))

        elif step == 4:
            # Email config — saved to .env at runtime (env is read-only here, so store in app config)
            cfg = tenant.config or {}
            cfg["from_name"]  = request.form.get("from_name", "").strip()
            cfg["from_email"] = request.form.get("from_email", "").strip()
            tenant.config = cfg
            # Hunter / SendGrid keys go to integrations; SMTP goes to platform env
            if tenant.onboarding_status in (None, "pending", "icp_set"):
                tenant.onboarding_status = "email_set"
            db.session.commit()
            return redirect(url_for("tenant_admin.onboarding", step=5))

        elif step == 5 and action == "launch":
            # Enable auto-prospect and mark as launched
            icp = tenant.icp or {}
            if icp.get("domains") or icp.get("keywords"):
                icp["auto_prospect"] = True
                tenant.icp = icp
            tenant.onboarding_status = "launched"
            db.session.commit()
            flash("🚀 Growth Engine launched! Autonomous prospecting starts tomorrow morning.", "success")
            return redirect(url_for("tenant_admin.dashboard"))

        # fallback
        return redirect(url_for("tenant_admin.onboarding", step=min(step + 1, 5)))

    # GET
    current_step = int(request.args.get("step", 1))
    return render_template("tenant_admin/onboarding.html",
        tenant=tenant,
        current_step=current_step,
        config=tenant.config or {},
        icp=tenant.icp or {},
        sendgrid_key=current_app.config.get("SENDGRID_API_KEY", ""),
        smtp_host=current_app.config.get("SMTP_HOST", ""),
        smtp_port=current_app.config.get("SMTP_PORT", 587),
        smtp_user=current_app.config.get("SMTP_USER", ""),
    )
