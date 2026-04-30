"""
blueprints/public/api.py — External lead intake API + CSV import.

Endpoints:
  POST /api/leads              Create one lead via JSON (API key auth)
  POST /api/leads/bulk         Create multiple leads via JSON array
  POST /dashboard/import/csv   CSV bulk import (tenant admin only)
  GET  /dashboard/import       Import history page
  GET  /api/prospects/search   Hunter.io email finder (tenant admin only)
  POST /api/prospects/add      Add Hunter result as a lead
"""

import csv
import io
import logging
import requests
from datetime import datetime
from functools import wraps

from flask import (Blueprint, request, jsonify, g, render_template,
                   redirect, url_for, flash, current_app)
from flask_login import current_user, login_required

from models import db, Lead, Tenant, ImportLog

log = logging.getLogger(__name__)
api_bp = Blueprint("api", __name__)


# ─────────────────────────────────────────────
#  API Key authentication helper
# ─────────────────────────────────────────────

def _resolve_tenant_from_api_key():
    """Resolve tenant from X-API-Key header or ?api_key= query param."""
    key = request.headers.get("X-API-Key") or request.args.get("api_key")
    if not key:
        return None
    return Tenant.query.filter_by(api_key=key, status="active").first()


def api_key_required(f):
    """Decorator: require valid API key, set g.tenant."""
    @wraps(f)
    def decorated(*args, **kwargs):
        tenant = _resolve_tenant_from_api_key()
        if not tenant:
            return jsonify({"error": "Invalid or missing API key"}), 401
        g.tenant = tenant
        return f(*args, **kwargs)
    return decorated


def _tenant_admin_required(f):
    """For import routes: require logged-in TenantUser."""
    from models import TenantUser
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not isinstance(current_user, TenantUser):
            return redirect(url_for("tenant_admin.login"))
        return f(*args, **kwargs)
    return decorated


# ─────────────────────────────────────────────
#  Lead creation helper
# ─────────────────────────────────────────────

def _create_or_update_lead(tenant, data: dict, source_override=None) -> tuple:
    """
    Create or upsert a lead from a dict.
    Returns (lead, created: bool, error: str|None)
    """
    email = (data.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return None, False, "invalid email"

    lead = Lead.query.filter_by(tenant_id=tenant.id, email=email).first()
    if lead:
        if lead.unsubscribed:
            lead.unsubscribed = False
            db.session.commit()
        return lead, False, None   # duplicate — skipped

    sequence = data.get("sequence", "nurture")
    lead = Lead(
        tenant_id     = tenant.id,
        email         = email,
        first_name    = (data.get("first_name") or "").strip(),
        last_name     = (data.get("last_name")  or "").strip(),
        company       = (data.get("company")    or "").strip(),
        source        = source_override or data.get("source", "api"),
        utm_source    = data.get("utm_source"),
        utm_medium    = data.get("utm_medium"),
        utm_campaign  = data.get("utm_campaign"),
        sequence_name = sequence,
        sequence_step = 0,
        next_email_at = datetime.utcnow(),
    )
    db.session.add(lead)
    return lead, True, None


# ─────────────────────────────────────────────
#  POST /api/leads  — Single lead intake
# ─────────────────────────────────────────────

@api_bp.route("/leads", methods=["POST"])
@api_key_required
def create_lead():
    """
    Create a single lead.

    Request (JSON):
      { "email": "...", "first_name": "...", "last_name": "...",
        "company": "...", "source": "...", "sequence": "nurture" }

    Headers:
      X-API-Key: <tenant api key>

    Example (curl):
      curl -X POST https://yourplatform.com/api/leads \\
           -H "X-API-Key: YOUR_KEY" \\
           -H "Content-Type: application/json" \\
           -d '{"email":"jane@company.com","first_name":"Jane","sequence":"nurture"}'
    """
    data = request.get_json(silent=True) or {}
    lead, created, error = _create_or_update_lead(g.tenant, data)
    if error:
        return jsonify({"error": error}), 400
    db.session.commit()
    return jsonify({
        "status": "created" if created else "exists",
        "lead_id": lead.id,
        "email": lead.email,
    }), 201 if created else 200


# ─────────────────────────────────────────────
#  POST /api/leads/bulk  — Bulk lead intake
# ─────────────────────────────────────────────

@api_bp.route("/leads/bulk", methods=["POST"])
@api_key_required
def create_leads_bulk():
    """
    Create multiple leads in one call.

    Request (JSON): array of lead objects (same schema as /api/leads)
    Max 500 leads per request.
    """
    data = request.get_json(silent=True)
    if not isinstance(data, list):
        return jsonify({"error": "Expected JSON array"}), 400
    if len(data) > 500:
        return jsonify({"error": "Max 500 leads per request"}), 400

    imported = skipped = errors = 0
    for row in data:
        _, created, error = _create_or_update_lead(g.tenant, row)
        if error:
            errors += 1
        elif created:
            imported += 1
        else:
            skipped += 1

    db.session.commit()
    return jsonify({"imported": imported, "skipped": skipped, "errors": errors}), 200


# ─────────────────────────────────────────────
#  POST /dashboard/import/csv — CSV bulk import
# ─────────────────────────────────────────────

@api_bp.route("/dashboard/import/csv", methods=["POST"])
@_tenant_admin_required
def import_csv():
    from models import TenantUser
    tenant = current_user.tenant

    file       = request.files.get("file")
    source_tag = request.form.get("source_tag", "csv_import").strip()
    sequence   = request.form.get("sequence", "nurture")

    if not file or not file.filename.endswith(".csv"):
        flash("Please upload a valid CSV file.", "danger")
        return redirect(url_for("tenant_admin.leads_import"))

    stream = io.StringIO(file.stream.read().decode("utf-8-sig", errors="replace"))
    reader = csv.DictReader(stream)

    # Normalize headers: lowercase + strip
    rows = []
    for row in reader:
        rows.append({k.lower().strip(): v.strip() for k, v in row.items()})

    total = len(rows)
    imported = skipped = 0

    for row in rows:
        # Accept common column name variants
        email      = row.get("email") or row.get("e-mail") or row.get("email address") or ""
        first_name = row.get("first_name") or row.get("first name") or row.get("firstname") or ""
        last_name  = row.get("last_name")  or row.get("last name")  or row.get("lastname")  or ""
        company    = row.get("company") or row.get("organization") or row.get("org") or ""

        data = {"email": email, "first_name": first_name, "last_name": last_name,
                "company": company, "sequence": sequence}
        _, created, error = _create_or_update_lead(tenant, data, source_override=source_tag)
        if created:
            imported += 1
        else:
            skipped += 1

    db.session.commit()

    # Save import log
    log_entry = ImportLog(
        tenant_id  = tenant.id,
        filename   = file.filename,
        source_tag = source_tag,
        total_rows = total,
        imported   = imported,
        skipped    = skipped,
        sequence   = sequence,
    )
    db.session.add(log_entry)
    db.session.commit()

    flash(f"Import complete: {imported} new leads added, {skipped} duplicates skipped.", "success")
    return redirect(url_for("tenant_admin.leads_import"))


# ─────────────────────────────────────────────
#  GET /dashboard/import — Import history
# ─────────────────────────────────────────────

@api_bp.route("/dashboard/import")
@_tenant_admin_required
def leads_import():
    from models import TenantUser
    from sequences import SEQUENCES
    tenant = current_user.tenant
    imports = ImportLog.query.filter_by(tenant_id=tenant.id).order_by(ImportLog.created_at.desc()).limit(20).all()
    return render_template("tenant_admin/import.html", tenant=tenant,
                           imports=imports, sequences=list(SEQUENCES.keys()))


# ─────────────────────────────────────────────
#  GET /api/prospects/search — Hunter.io email finder
# ─────────────────────────────────────────────

@api_bp.route("/api/prospects/search")
@_tenant_admin_required
def prospect_search():
    from models import TenantUser
    tenant = current_user.tenant
    query  = request.args.get("q", "").strip()
    mode   = request.args.get("mode", "domain")   # domain | email_finder

    results = []
    error   = None
    hunter_key = tenant.hunter_api_key

    if query and hunter_key:
        try:
            if mode == "domain":
                # Find all emails for a company domain
                resp = requests.get(
                    "https://api.hunter.io/v2/domain-search",
                    params={"domain": query, "api_key": hunter_key, "limit": 20},
                    timeout=10,
                )
                data = resp.json()
                for contact in (data.get("data", {}) or {}).get("emails", []):
                    results.append({
                        "email":      contact.get("value"),
                        "first_name": contact.get("first_name", ""),
                        "last_name":  contact.get("last_name", ""),
                        "company":    data.get("data", {}).get("organization", query),
                        "title":      contact.get("position", ""),
                        "confidence": contact.get("confidence", 0),
                    })
            elif mode == "email_finder":
                # Find email for a specific person at a company
                parts = query.split("@")
                if len(parts) == 2:
                    name_parts = parts[0].split()
                    first = name_parts[0] if name_parts else ""
                    last  = name_parts[1] if len(name_parts) > 1 else ""
                    domain = parts[1]
                    resp = requests.get(
                        "https://api.hunter.io/v2/email-finder",
                        params={"domain": domain, "first_name": first,
                                "last_name": last, "api_key": hunter_key},
                        timeout=10,
                    )
                    data = resp.json()
                    email_data = data.get("data", {})
                    if email_data.get("email"):
                        results.append({
                            "email":      email_data["email"],
                            "first_name": first,
                            "last_name":  last,
                            "company":    domain,
                            "title":      "",
                            "confidence": email_data.get("score", 0),
                        })
        except Exception as exc:
            error = f"Hunter.io API error: {exc}"

    already_leads = set(
        r[0] for r in db.session.query(Lead.email).filter_by(tenant_id=tenant.id).all()
    )

    return render_template("tenant_admin/prospect_search.html",
        tenant=tenant, query=query, mode=mode,
        results=results, error=error, already_leads=already_leads,
        has_hunter_key=bool(hunter_key))


# ─────────────────────────────────────────────
#  POST /api/prospects/add — Add Hunter result as lead
# ─────────────────────────────────────────────

@api_bp.route("/api/prospects/add", methods=["POST"])
@_tenant_admin_required
def add_prospect():
    from models import TenantUser
    tenant = current_user.tenant
    data = {
        "email":      request.form.get("email"),
        "first_name": request.form.get("first_name", ""),
        "last_name":  request.form.get("last_name", ""),
        "company":    request.form.get("company", ""),
        "source":     "hunter_io",
        "sequence":   request.form.get("sequence", "nurture"),
    }
    lead, created, error = _create_or_update_lead(tenant, data, source_override="hunter_io")
    if error:
        flash(f"Error: {error}", "danger")
    elif created:
        db.session.commit()
        flash(f"Added {data['email']} to the {data['sequence']} sequence.", "success")
    else:
        flash(f"{data['email']} already exists as a lead.", "info")

    return redirect(url_for("api.prospect_search",
                            q=request.form.get("q", ""),
                            mode=request.form.get("mode", "domain")))
