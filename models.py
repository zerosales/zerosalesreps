"""
models.py — Multi-tenant SalesPilot platform database models.

Hierarchy:
  Tenant (one per SaaS client)
    └── TenantUser (admins for that tenant)
    └── Lead (contacts captured via that tenant's landing page)
         └── EmailLog (every email sent to that lead)
  SuperAdmin (platform owner — Principium)
"""

from datetime import datetime
import secrets
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin

db = SQLAlchemy()


# ─────────────────────────────────────────────
#  Tenant
# ─────────────────────────────────────────────

class Tenant(db.Model):
    __tablename__ = "tenants"

    id              = db.Column(db.Integer, primary_key=True)
    slug            = db.Column(db.String(64), unique=True, nullable=False, index=True)
    status          = db.Column(db.String(32), default="active", index=True)

    # API key for webhook/embed authentication (auto-generated)
    api_key         = db.Column(db.String(64), unique=True, default=lambda: secrets.token_urlsafe(32), index=True)

    # Hunter.io API key (per-tenant, optional)
    hunter_api_key  = db.Column(db.String(128))

    # Lead magnet config (stored as JSON: title, description, file_url, button_text)
    lead_magnet     = db.Column(db.JSON, default=dict)

    # Ideal Customer Profile — drives autonomous prospecting
    # icp keys: domains[], keywords[], job_titles[], industries[], company_size_min/max,
    #           auto_prospect (bool), daily_limit (int, default 25)
    icp             = db.Column(db.JSON, default=dict)

    # Onboarding & growth tracking
    onboarding_status = db.Column(db.String(32), default="pending")
    # pending | icp_set | stripe_connected | launched
    customer_target   = db.Column(db.Integer, default=1000)
    # status: active | suspended | trial | cancelled

    # Billing / platform plan
    platform_plan   = db.Column(db.String(32), default="starter")
    # platform_plan: starter | growth | scale
    platform_fee_pct= db.Column(db.Float, default=5.0)   # % taken from tenant revenue
    billing_email   = db.Column(db.String(256))
    platform_stripe_customer_id = db.Column(db.String(128))

    # Stripe Connect (tenant's OWN Stripe account)
    stripe_account_id       = db.Column(db.String(128))
    stripe_connect_status   = db.Column(db.String(32), default="not_connected")
    # stripe_connect_status: not_connected | pending | connected

    # Tool config stored as JSON (replaces YAML files)
    config = db.Column(db.JSON, default=dict)
    # config keys: name, tagline, pain_point, from_email, from_name,
    #              logo_url, primary_color, urls{}, features[], testimonials[],
    #              pricing[], trial_days, stripe_prices{}

    created_at      = db.Column(db.DateTime, default=datetime.utcnow)
    trial_ends_at   = db.Column(db.DateTime)

    # Relationships
    users   = db.relationship("TenantUser", back_populates="tenant", cascade="all, delete-orphan")
    leads   = db.relationship("Lead",       back_populates="tenant", cascade="all, delete-orphan")

    def get_config(self, key, default=None):
        """Safe config accessor."""
        return (self.config or {}).get(key, default)

    @property
    def name(self):
        return self.get_config("name", self.slug)

    @property
    def is_active(self):
        return self.status == "active"

    def __repr__(self):
        return f"<Tenant {self.slug}>"


# ─────────────────────────────────────────────
#  TenantUser  (admin(s) for a tenant)
# ─────────────────────────────────────────────

class TenantUser(UserMixin, db.Model):
    __tablename__ = "tenant_users"

    id          = db.Column(db.Integer, primary_key=True)
    tenant_id   = db.Column(db.Integer, db.ForeignKey("tenants.id"), nullable=False, index=True)
    email       = db.Column(db.String(256), nullable=False, index=True)
    password_hash = db.Column(db.String(256))
    role        = db.Column(db.String(32), default="admin")   # admin | member
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    tenant = db.relationship("Tenant", back_populates="users")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def get_id(self):
        # Prefix with "t-" so Flask-Login can distinguish from SuperAdmin IDs
        return f"t-{self.id}"

    def __repr__(self):
        return f"<TenantUser {self.email} tenant={self.tenant_id}>"


# ─────────────────────────────────────────────
#  SuperAdmin  (platform owner)
# ─────────────────────────────────────────────

class SuperAdmin(UserMixin, db.Model):
    __tablename__ = "super_admins"

    id          = db.Column(db.Integer, primary_key=True)
    email       = db.Column(db.String(256), unique=True, nullable=False)
    password_hash = db.Column(db.String(256))
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def get_id(self):
        return f"sa-{self.id}"

    def __repr__(self):
        return f"<SuperAdmin {self.email}>"


# ─────────────────────────────────────────────
#  Lead
# ─────────────────────────────────────────────

class Lead(db.Model):
    __tablename__ = "leads"

    id          = db.Column(db.Integer, primary_key=True)
    tenant_id   = db.Column(db.Integer, db.ForeignKey("tenants.id"), nullable=False, index=True)

    # Identity
    email       = db.Column(db.String(256), nullable=False, index=True)
    first_name  = db.Column(db.String(128))
    last_name   = db.Column(db.String(128))
    company     = db.Column(db.String(256))

    # Lifecycle
    status      = db.Column(db.String(32), default="new", index=True)
    # new → nurturing → trial → customer → churned | expired

    # Sequence tracking
    sequence_name = db.Column(db.String(64), default="nurture")
    sequence_step = db.Column(db.Integer, default=0)
    next_email_at = db.Column(db.DateTime)
    sequence_paused = db.Column(db.Boolean, default=False)

    # Engagement counters
    emails_sent     = db.Column(db.Integer, default=0)
    emails_opened   = db.Column(db.Integer, default=0)
    emails_clicked  = db.Column(db.Integer, default=0)

    # Attribution
    source          = db.Column(db.String(128))
    utm_campaign    = db.Column(db.String(128))
    utm_medium      = db.Column(db.String(128))
    utm_source      = db.Column(db.String(128))

    # Stripe
    stripe_customer_id      = db.Column(db.String(128))
    stripe_subscription_id  = db.Column(db.String(128))
    plan                    = db.Column(db.String(64))
    mrr                     = db.Column(db.Float, default=0.0)

    # Unsubscribe
    unsubscribe_token   = db.Column(db.String(64), unique=True, default=lambda: secrets.token_urlsafe(32))
    unsubscribed        = db.Column(db.Boolean, default=False)

    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at  = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    tenant  = db.relationship("Tenant", back_populates="leads")
    logs    = db.relationship("EmailLog", back_populates="lead", cascade="all, delete-orphan", order_by="EmailLog.sent_at.desc()")

    @property
    def full_name(self):
        parts = [self.first_name, self.last_name]
        return " ".join(p for p in parts if p) or self.email

    def __repr__(self):
        return f"<Lead {self.email} tenant={self.tenant_id}>"


# ─────────────────────────────────────────────
#  EmailLog
# ─────────────────────────────────────────────

class EmailLog(db.Model):
    __tablename__ = "email_logs"

    id          = db.Column(db.Integer, primary_key=True)
    tenant_id   = db.Column(db.Integer, db.ForeignKey("tenants.id"), nullable=False, index=True)
    lead_id     = db.Column(db.Integer, db.ForeignKey("leads.id"), nullable=False, index=True)

    sequence    = db.Column(db.String(64))
    step        = db.Column(db.Integer)
    subject     = db.Column(db.String(512))
    template    = db.Column(db.String(128))

    status      = db.Column(db.String(32), default="sent")
    # sent | opened | clicked | bounced | failed

    open_token  = db.Column(db.String(64), unique=True, default=lambda: secrets.token_urlsafe(24))
    click_token = db.Column(db.String(64), unique=True, default=lambda: secrets.token_urlsafe(24))
    cta_url     = db.Column(db.String(1024))

    sent_at     = db.Column(db.DateTime, default=datetime.utcnow)
    opened_at   = db.Column(db.DateTime)
    clicked_at  = db.Column(db.DateTime)

    lead = db.relationship("Lead", back_populates="logs")

    def __repr__(self):
        return f"<EmailLog lead={self.lead_id} seq={self.sequence} step={self.step}>"


# ─────────────────────────────────────────────
#  ImportLog — tracks CSV import history per tenant
# ─────────────────────────────────────────────

class ImportLog(db.Model):
    __tablename__ = "import_logs"

    id          = db.Column(db.Integer, primary_key=True)
    tenant_id   = db.Column(db.Integer, db.ForeignKey("tenants.id"), nullable=False, index=True)
    filename    = db.Column(db.String(256))
    source_tag  = db.Column(db.String(128))       # tag applied to imported leads
    total_rows  = db.Column(db.Integer, default=0)
    imported    = db.Column(db.Integer, default=0) # new leads created
    skipped     = db.Column(db.Integer, default=0) # duplicates / invalid
    sequence    = db.Column(db.String(64), default="nurture")
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<ImportLog tenant={self.tenant_id} file={self.filename} imported={self.imported}>"


# ─────────────────────────────────────────────
#  Composite unique constraint: one lead email per tenant
# ─────────────────────────────────────────────
from sqlalchemy import UniqueConstraint
Lead.__table_args__ = (
    UniqueConstraint("tenant_id", "email", name="uq_tenant_lead_email"),
)
