"""
app.py — Multi-tenant SalesPilot platform application factory.

Routing strategy:
  - Subdomain routing: client.yourdomain.com  → public/tenant_admin blueprints
  - Super admin:       admin.yourdomain.com   → super_admin blueprint
  - Local dev:         localhost:5000/t/<slug>/ for tenant routes
                       localhost:5000/admin/   for super admin
"""

import os
from flask import Flask, g, request, redirect, url_for
from flask_login import LoginManager
from dotenv import load_dotenv

from models import db, Tenant, TenantUser, SuperAdmin

load_dotenv()


def create_app():
    app = Flask(__name__)

    # ── Config ────────────────────────────────────
    app.config["SECRET_KEY"]          = os.environ["SECRET_KEY"]
    app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///platform.db")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["BASE_URL"]            = os.getenv("BASE_URL", "http://localhost:5000")
    app.config["PLATFORM_DOMAIN"]     = os.getenv("PLATFORM_DOMAIN", "localhost")
    app.config["PLATFORM_FEE_PCT"]    = float(os.getenv("PLATFORM_FEE_PCT", "5.0"))
    app.config["STRIPE_SECRET_KEY"]   = os.getenv("STRIPE_SECRET_KEY", "")
    app.config["STRIPE_PUBLISHABLE_KEY"] = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
    app.config["STRIPE_WEBHOOK_SECRET"]  = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    app.config["STRIPE_CONNECT_CLIENT_ID"] = os.getenv("STRIPE_CONNECT_CLIENT_ID", "")
    app.config["SENDGRID_API_KEY"]    = os.getenv("SENDGRID_API_KEY", "")
    app.config["SMTP_HOST"]           = os.getenv("SMTP_HOST", "")
    app.config["SMTP_PORT"]           = int(os.getenv("SMTP_PORT", "587"))
    app.config["SMTP_USER"]           = os.getenv("SMTP_USER", "")
    app.config["SMTP_PASS"]           = os.getenv("SMTP_PASS", "")

    # ── Extensions ───────────────────────────────
    db.init_app(app)

    # ── Auto-init DB on first startup ────────────
    with app.app_context():
        db.create_all()
        # Auto-create super admin from env vars if none exists
        _email = os.getenv("SUPERADMIN_EMAIL")
        _pass  = os.getenv("SUPERADMIN_PASSWORD")
        if _email and _pass:
            from models import SuperAdmin as _SA
            if not _SA.query.filter_by(email=_email).first():
                _sa = _SA(email=_email)
                _sa.set_password(_pass)
                db.session.add(_sa)
                db.session.commit()

    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = "tenant_admin.login"

    @login_manager.user_loader
    def load_user(user_id: str):
        """Load either a TenantUser (t-<id>) or SuperAdmin (sa-<id>)."""
        if user_id.startswith("sa-"):
            return SuperAdmin.query.get(int(user_id[3:]))
        elif user_id.startswith("t-"):
            return TenantUser.query.get(int(user_id[2:]))
        return None

    # ── Tenant resolution middleware ──────────────
    @app.before_request
    def resolve_tenant():
        """
        Identify which tenant this request belongs to.
        Strategy 1 (production): subdomain  client.platform.com → slug = 'client'
        Strategy 2 (dev):        path prefix /t/<slug>/          → slug = '<slug>'
        Super-admin routes bypass tenant resolution.
        """
        g.tenant = None

        # Skip static files
        if request.path.startswith("/static"):
            return

        host = request.host.split(":")[0]  # strip port
        platform_domain = app.config["PLATFORM_DOMAIN"]

        # Super-admin subdomain or path
        if host == f"admin.{platform_domain}" or request.path.startswith("/superadmin"):
            g.tenant = None
            return

        # Production subdomain routing
        if host.endswith(f".{platform_domain}") and host != platform_domain:
            slug = host.replace(f".{platform_domain}", "")
            g.tenant = Tenant.query.filter_by(slug=slug, status="active").first()
            return

        # Dev path-prefix routing: /t/<slug>/...
        if request.path.startswith("/t/"):
            parts = request.path.split("/")
            if len(parts) >= 3:
                slug = parts[2]
                g.tenant = Tenant.query.filter_by(slug=slug).first()
            return

        # Root domain — no tenant (platform homepage or super admin)
        g.tenant = None

    # ── Blueprints ────────────────────────────────
    from blueprints.public.routes       import public_bp
    from blueprints.public.widget       import widget_bp
    from blueprints.public.api          import api_bp
    from blueprints.tenant_admin.routes import tenant_admin_bp
    from blueprints.super_admin.routes  import super_admin_bp

    app.register_blueprint(public_bp)
    app.register_blueprint(widget_bp)
    app.register_blueprint(api_bp,           url_prefix="/api")
    app.register_blueprint(tenant_admin_bp,  url_prefix="/dashboard")
    app.register_blueprint(super_admin_bp,   url_prefix="/superadmin")

    # ── Root route ────────────────────────────────
    @app.route("/")
    def index():
        if g.tenant:
            return redirect(url_for("public.landing"))
        # Platform marketing homepage
        from flask import render_template as rt
        platform_name   = os.getenv("PLATFORM_NAME", "LaunchEngine")
        platform_domain = os.getenv("PLATFORM_DOMAIN", "yourplatform.com")
        return rt("platform/home.html",
                  platform_name=platform_name,
                  platform_domain=platform_domain)

    # ── Health check ──────────────────────────────
    @app.route("/health")
    def health():
        return {"status": "ok", "tenant": g.tenant.slug if g.tenant else None}

    # ── CLI: init db + seed super admin ──────────
    @app.cli.command("init-db")
    def init_db():
        db.create_all()
        print("Database tables created.")

    @app.cli.command("create-superadmin")
    def create_superadmin():
        email    = input("Super admin email: ").strip()
        password = input("Password: ").strip()
        sa = SuperAdmin(email=email)
        sa.set_password(password)
        db.session.add(sa)
        db.session.commit()
        print(f"Super admin '{email}' created.")

    return app


# ── Entry point ───────────────────────────────────
app = create_app()

if __name__ == "__main__":
    from scheduler_service import start_scheduler
    with app.app_context():
        db.create_all()
    start_scheduler(app)
    app.run(debug=os.getenv("FLASK_DEBUG", "0") == "1", host="0.0.0.0", port=5000)
