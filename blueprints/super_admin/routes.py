"""
blueprints/super_admin/routes.py — Platform owner (Principium) super-admin.

Routes (all prefixed /superadmin):
  GET  /login
  POST /login
  GET  /logout
  GET  /                  platform overview
  GET  /tenants           all tenants list
  GET  /tenant/<id>       tenant detail + stats
  POST /tenant/<id>/status   activate/suspend
  GET  /tenant/new        create tenant form
  POST /tenant/new
  GET  /tenant/<id>/impersonate  log in as tenant admin (for support)
  GET  /api/stats         JSON stats endpoint
"""

import json
import logging
import secrets
from datetime import datetime, timedelta
from functools import wraps

from flask import (Blueprint, render_template, request, redirect,
                   url_for, flash, g, current_app, jsonify, session)
from flask_login import login_user, logout_user, current_user, login_required

from models import db, Tenant, TenantUser, Lead, EmailLog, SuperAdmin

log = logging.getLogger(__name__)
super_admin_bp = Blueprint("super_admin", __name__,
                            template_folder="../../templates/super_admin")


# ─────────────────────────────────────────────
#  Auth guard
# ─────────────────────────────────────────────

def superadmin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not isinstance(current_user, SuperAdmin):
            return redirect(url_for("super_admin.login"))
        return f(*args, **kwargs)
    return decorated


# ─────────────────────────────────────────────
#  Login / Logout
# ─────────────────────────────────────────────

@super_admin_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        sa = SuperAdmin.query.filter_by(email=email).first()
        if sa and sa.check_password(password):
            login_user(sa)
            return redirect(url_for("super_admin.platform_overview"))
        flash("Invalid credentials.", "danger")
    return render_template("super_admin/login.html")


@super_admin_bp.route("/logout")
def logout():
    logout_user()
    return redirect(url_for("super_admin.login"))


# ─────────────────────────────────────────────
#  Platform overview
# ─────────────────────────────────────────────

@super_admin_bp.route("/")
@superadmin_required
def platform_overview():
    total_tenants  = Tenant.query.count()
    active_tenants = Tenant.query.filter_by(status="active").count()
    total_leads    = Lead.query.count()
    total_customers= Lead.query.filter_by(status="customer").count()
    platform_mrr   = db.session.query(
        db.func.sum(Lead.mrr * Tenant.platform_fee_pct / 100)
    ).join(Tenant).filter(Lead.status == "customer").scalar() or 0

    # Emails last 7 days
    week_ago = datetime.utcnow() - timedelta(days=7)
    emails_week = EmailLog.query.filter(EmailLog.sent_at >= week_ago).count()

    # New tenants last 30 days
    from sqlalchemy import func, cast, Date
    thirty_ago = datetime.utcnow() - timedelta(days=30)
    tenant_rows = (db.session.query(
                        cast(Tenant.created_at, Date).label("day"),
                        func.count(Tenant.id).label("cnt"))
                   .filter(Tenant.created_at >= thirty_ago)
                   .group_by("day").order_by("day").all())
    tenant_daily_labels = [str(r.day) for r in tenant_rows]
    tenant_daily_data   = [r.cnt     for r in tenant_rows]

    # Top tenants by lead count
    top_tenants = (db.session.query(Tenant, func.count(Lead.id).label("lead_count"))
                   .outerjoin(Lead, Lead.tenant_id == Tenant.id)
                   .group_by(Tenant.id)
                   .order_by(func.count(Lead.id).desc())
                   .limit(10).all())

    recent_tenants = Tenant.query.order_by(Tenant.created_at.desc()).limit(10).all()

    return render_template("super_admin/overview.html",
        total_tenants=total_tenants, active_tenants=active_tenants,
        total_leads=total_leads, total_customers=total_customers,
        platform_mrr=round(platform_mrr, 2), emails_week=emails_week,
        tenant_daily_labels=tenant_daily_labels, tenant_daily_data=tenant_daily_data,
        top_tenants=top_tenants, recent_tenants=recent_tenants)


# ─────────────────────────────────────────────
#  Tenants list
# ─────────────────────────────────────────────

@super_admin_bp.route("/tenants")
@superadmin_required
def tenants_list():
    status_filter = request.args.get("status")
    q = request.args.get("q", "").strip()

    query = Tenant.query
    if status_filter:
        query = query.filter_by(status=status_filter)
    if q:
        query = query.filter(Tenant.slug.ilike(f"%{q}%"))

    tenants = query.order_by(Tenant.created_at.desc()).all()

    # Attach lead counts
    from sqlalchemy import func
    counts = dict(db.session.query(Lead.tenant_id, func.count(Lead.id))
                  .group_by(Lead.tenant_id).all())
    customer_counts = dict(db.session.query(Lead.tenant_id, func.count(Lead.id))
                           .filter_by(status="customer").group_by(Lead.tenant_id).all())

    return render_template("super_admin/tenants.html",
        tenants=tenants, counts=counts, customer_counts=customer_counts,
        status_filter=status_filter, q=q)


# ─────────────────────────────────────────────
#  Tenant detail
# ─────────────────────────────────────────────

@super_admin_bp.route("/tenant/<int:tenant_id>")
@superadmin_required
def tenant_detail(tenant_id):
    tenant = Tenant.query.get_or_404(tenant_id)
    leads  = Lead.query.filter_by(tenant_id=tenant_id).order_by(Lead.created_at.desc()).limit(50).all()

    total_leads = len(Lead.query.filter_by(tenant_id=tenant_id).all())
    customers   = Lead.query.filter_by(tenant_id=tenant_id, status="customer").count()
    mrr = db.session.query(db.func.sum(Lead.mrr)).filter_by(tenant_id=tenant_id, status="customer").scalar() or 0
    platform_revenue = round(mrr * (tenant.platform_fee_pct or 5.0) / 100, 2)

    total_sent    = EmailLog.query.filter_by(tenant_id=tenant_id).count()
    total_opened  = EmailLog.query.filter_by(tenant_id=tenant_id).filter(
        EmailLog.status.in_(["opened", "clicked"])).count()
    open_rate = round(total_opened / total_sent * 100, 1) if total_sent else 0

    users = TenantUser.query.filter_by(tenant_id=tenant_id).all()

    return render_template("super_admin/tenant_detail.html",
        tenant=tenant, leads=leads, users=users,
        total_leads=total_leads, customers=customers, mrr=round(mrr, 2),
        platform_revenue=platform_revenue, total_sent=total_sent,
        open_rate=open_rate, config_json=json.dumps(tenant.config or {}, indent=2))


# ─────────────────────────────────────────────
#  Update tenant status
# ─────────────────────────────────────────────

@super_admin_bp.route("/tenant/<int:tenant_id>/status", methods=["POST"])
@superadmin_required
def update_tenant_status(tenant_id):
    tenant = Tenant.query.get_or_404(tenant_id)
    new_status = request.form.get("status")
    if new_status in ["active", "suspended", "trial", "cancelled"]:
        tenant.status = new_status
        db.session.commit()
        flash(f"Tenant {tenant.slug} status set to {new_status}.", "success")
    return redirect(url_for("super_admin.tenant_detail", tenant_id=tenant_id))


# ─────────────────────────────────────────────
#  Create new tenant (with onboarding wizard)
# ─────────────────────────────────────────────

@super_admin_bp.route("/tenant/new", methods=["GET", "POST"])
@superadmin_required
def new_tenant():
    if request.method == "POST":
        slug         = request.form.get("slug", "").strip().lower().replace(" ", "-")
        admin_email  = request.form.get("admin_email", "").strip().lower()
        admin_password = request.form.get("admin_password", "")
        fee_pct      = float(request.form.get("fee_pct", 5.0))
        plan         = request.form.get("platform_plan", "starter")

        if Tenant.query.filter_by(slug=slug).first():
            flash(f"Slug '{slug}' is already taken.", "danger")
            return redirect(url_for("super_admin.new_tenant"))

        # Default starter config
        default_config = {
            "name":          request.form.get("name", slug).strip(),
            "tagline":       request.form.get("tagline", "").strip(),
            "pain_point":    request.form.get("pain_point", "").strip(),
            "from_email":    admin_email,
            "from_name":     request.form.get("name", slug).strip(),
            "primary_color": "#4F46E5",
            "company":       request.form.get("name", slug).strip(),
            "trial_days":    14,
            "urls": {"trial_url": "", "app_url": "", "pricing_url": ""},
            "features": [], "testimonials": [], "pricing": [], "stripe_prices": {},
        }

        tenant = Tenant(
            slug=slug,
            status="active",
            platform_plan=plan,
            platform_fee_pct=fee_pct,
            billing_email=admin_email,
            config=default_config,
            trial_ends_at=datetime.utcnow() + timedelta(days=30),
        )
        db.session.add(tenant)
        db.session.flush()

        admin_user = TenantUser(tenant_id=tenant.id, email=admin_email, role="admin")
        admin_user.set_password(admin_password if admin_password else secrets.token_urlsafe(12))
        db.session.add(admin_user)
        db.session.commit()

        flash(f"Tenant '{slug}' created. Admin: {admin_email}", "success")
        return redirect(url_for("super_admin.tenant_detail", tenant_id=tenant.id))

    return render_template("super_admin/new_tenant.html",
                           plans=["starter", "growth", "scale"])


# ─────────────────────────────────────────────
#  Impersonate tenant (support tool)
# ─────────────────────────────────────────────

@super_admin_bp.route("/tenant/<int:tenant_id>/impersonate")
@superadmin_required
def impersonate_tenant(tenant_id):
    tenant = Tenant.query.get_or_404(tenant_id)
    user   = TenantUser.query.filter_by(tenant_id=tenant_id).first()
    if not user:
        flash("No admin user found for this tenant.", "danger")
        return redirect(url_for("super_admin.tenant_detail", tenant_id=tenant_id))
    session["impersonating_as_superadmin"] = True
    login_user(user)
    flash(f"Now impersonating {tenant.slug}. Return via /superadmin/.", "warning")
    return redirect(url_for("tenant_admin.dashboard"))


# ─────────────────────────────────────────────
#  JSON stats API
# ─────────────────────────────────────────────

@super_admin_bp.route("/api/stats")
@superadmin_required
def api_stats():
    from sqlalchemy import func
    tenants = Tenant.query.filter_by(status="active").count()
    leads   = Lead.query.count()
    customers = Lead.query.filter_by(status="customer").count()
    mrr = db.session.query(db.func.sum(Lead.mrr)).filter_by(status="customer").scalar() or 0
    platform_fees = db.session.query(
        db.func.sum(Lead.mrr * Tenant.platform_fee_pct / 100)
    ).join(Tenant).filter(Lead.status == "customer").scalar() or 0

    return jsonify({
        "active_tenants": tenants,
        "total_leads": leads,
        "total_customers": customers,
        "total_mrr": round(float(mrr), 2),
        "platform_revenue": round(float(platform_fees), 2),
    })
