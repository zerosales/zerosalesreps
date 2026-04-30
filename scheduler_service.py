"""
scheduler_service.py — Background jobs that run across ALL active tenants.

Jobs:
  _run_sequences    every 15 min  — send due emails for every active tenant
  _stale_leads      daily 9am UTC — move inactive leads to reengagement
  _trial_expiry     hourly        — mark expired trials; trigger churned status
  _auto_prospect    daily 6am UTC — autonomous ICP-based prospecting via Hunter.io
"""

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron         import CronTrigger
from apscheduler.triggers.interval     import IntervalTrigger

log = logging.getLogger(__name__)
_scheduler = None


def start_scheduler(app):
    global _scheduler
    if _scheduler and _scheduler.running:
        return

    _scheduler = BackgroundScheduler(timezone="UTC")

    interval = int(app.config.get("SCHEDULER_INTERVAL", 900))   # default 15 min
    _scheduler.add_job(_run_sequences,  IntervalTrigger(seconds=interval),    args=[app], id="sequences",    replace_existing=True)
    _scheduler.add_job(_stale_leads,    CronTrigger(hour=9, minute=0),        args=[app], id="stale_leads",  replace_existing=True)
    _scheduler.add_job(_trial_expiry,   IntervalTrigger(hours=1),             args=[app], id="trial_expiry", replace_existing=True)
    _scheduler.add_job(_auto_prospect,  CronTrigger(hour=6, minute=0),        args=[app], id="auto_prospect",replace_existing=True)

    _scheduler.start()
    log.info("Scheduler started — sequence interval %ds", interval)


# ─────────────────────────────────────────────
#  Job: run due email steps for all tenants
# ─────────────────────────────────────────────

def _run_sequences(app):
    from models import db, Tenant, Lead
    from email_engine import send_sequence_email
    from sequences import get_step

    with app.app_context():
        now = datetime.utcnow()
        active_tenants = Tenant.query.filter_by(status="active").all()

        for tenant in active_tenants:
            due_leads = (
                Lead.query
                .filter_by(tenant_id=tenant.id, unsubscribed=False, sequence_paused=False)
                .filter(Lead.next_email_at <= now)
                .filter(Lead.next_email_at.isnot(None))
                .all()
            )

            for lead in due_leads:
                next_step_num = (lead.sequence_step or 0) + 1
                step_def = get_step(lead.sequence_name, next_step_num)
                if step_def:
                    step_def = dict(step_def, sequence_name=lead.sequence_name)
                    try:
                        send_sequence_email(lead, step_def, app)
                        log.info("Sent %s step %d to %s (tenant %s)",
                                 lead.sequence_name, next_step_num, lead.email, tenant.slug)
                    except Exception as exc:
                        log.error("Failed sending to %s: %s", lead.email, exc)
                else:
                    # No more steps — pause
                    lead.next_email_at  = None
                    lead.sequence_paused = True
                    db.session.commit()


# ─────────────────────────────────────────────
#  Job: move stale leads into reengagement
# ─────────────────────────────────────────────

def _stale_leads(app):
    from models import db, Tenant, Lead

    with app.app_context():
        cutoff = datetime.utcnow() - timedelta(days=30)
        active_tenants = Tenant.query.filter_by(status="active").all()

        for tenant in active_tenants:
            stale = (
                Lead.query
                .filter_by(tenant_id=tenant.id, unsubscribed=False)
                .filter(Lead.status.in_(["nurturing", "trial"]))
                .filter(Lead.updated_at < cutoff)
                .filter(Lead.sequence_name != "reengagement")
                .all()
            )
            for lead in stale:
                lead.sequence_name   = "reengagement"
                lead.sequence_step   = 0
                lead.sequence_paused = False
                lead.next_email_at   = datetime.utcnow() + timedelta(hours=1)

            if stale:
                db.session.commit()
                log.info("Moved %d stale leads to reengagement (tenant %s)", len(stale), tenant.slug)


# ─────────────────────────────────────────────
#  Job: mark expired trials
# ─────────────────────────────────────────────

def _trial_expiry(app):
    from models import db, Tenant, Lead

    with app.app_context():
        now = datetime.utcnow()
        active_tenants = Tenant.query.filter_by(status="active").all()

        for tenant in active_tenants:
            trial_days = (tenant.config or {}).get("trial_days", 14)
            expiry_cutoff = now - timedelta(days=trial_days)

            expired = (
                Lead.query
                .filter_by(tenant_id=tenant.id, status="trial")
                .filter(Lead.updated_at < expiry_cutoff)
                .all()
            )
            for lead in expired:
                lead.status          = "expired"
                lead.sequence_name   = "reengagement"
                lead.sequence_step   = 0
                lead.sequence_paused = False
                lead.next_email_at   = now + timedelta(hours=2)

            if expired:
                db.session.commit()
                log.info("Expired %d trials (tenant %s)", len(expired), tenant.slug)


# ─────────────────────────────────────────────
#  Job: autonomous ICP-based prospecting
#  Runs daily at 6am UTC for all tenants with
#  auto_prospect enabled in their ICP config.
# ─────────────────────────────────────────────

def _auto_prospect(app):
    """
    For each tenant with ICP.auto_prospect == True:
      1. Pull target domains/keywords from ICP config
      2. Call Hunter.io domain-search for each domain
      3. Filter by job_title / industry matches
      4. Add net-new leads (skip duplicates) up to daily_limit
      5. Enrol them in the 'nurture' sequence immediately
    """
    import requests
    from models import db, Tenant, Lead
    from sqlalchemy.exc import IntegrityError

    with app.app_context():
        now = datetime.utcnow()
        active_tenants = Tenant.query.filter_by(status="active").all()

        for tenant in active_tenants:
            icp = tenant.icp or {}
            if not icp.get("auto_prospect"):
                continue

            hunter_key = tenant.hunter_api_key
            if not hunter_key:
                log.warning("Tenant %s has auto_prospect but no Hunter.io key — skipping", tenant.slug)
                continue

            domains      = icp.get("domains", [])
            keywords     = icp.get("keywords", [])
            job_titles   = [j.lower() for j in icp.get("job_titles", [])]
            daily_limit  = int(icp.get("daily_limit", 25))
            added_count  = 0

            # ── Collect prospects from named domains ──────────
            for domain in domains:
                if added_count >= daily_limit:
                    break
                prospects = _hunter_domain_search(hunter_key, domain)
                for p in prospects:
                    if added_count >= daily_limit:
                        break
                    # Job-title filter (optional — if list is empty, accept all)
                    if job_titles:
                        pos = (p.get("position") or "").lower()
                        if not any(jt in pos for jt in job_titles):
                            continue

                    added = _add_prospect(tenant, p, source=f"auto:domain:{domain}", now=now)
                    if added:
                        added_count += 1

            # ── Collect prospects via keyword search ──────────
            for kw in keywords:
                if added_count >= daily_limit:
                    break
                prospects = _hunter_keyword_search(hunter_key, kw)
                for p in prospects:
                    if added_count >= daily_limit:
                        break
                    if job_titles:
                        pos = (p.get("position") or "").lower()
                        if not any(jt in pos for jt in job_titles):
                            continue
                    added = _add_prospect(tenant, p, source=f"auto:keyword:{kw}", now=now)
                    if added:
                        added_count += 1

            if added_count:
                try:
                    db.session.commit()
                    log.info("Auto-prospected %d leads for tenant %s", added_count, tenant.slug)
                except Exception as exc:
                    db.session.rollback()
                    log.error("Auto-prospect commit error (tenant %s): %s", tenant.slug, exc)


# ── Hunter.io helper: domain search ──────────────────────────

def _hunter_domain_search(api_key: str, domain: str) -> list:
    """Return list of prospect dicts from Hunter.io domain-search endpoint."""
    try:
        resp = requests.get(
            "https://api.hunter.io/v2/domain-search",
            params={"domain": domain, "api_key": api_key, "limit": 10},
            timeout=10,
        )
        data = resp.json()
        emails = data.get("data", {}).get("emails", [])
        return [
            {
                "email":      e.get("value"),
                "first_name": e.get("first_name"),
                "last_name":  e.get("last_name"),
                "position":   e.get("position"),
                "company":    data.get("data", {}).get("organization"),
                "confidence": e.get("confidence", 0),
            }
            for e in emails
            if e.get("value") and e.get("confidence", 0) >= 50
        ]
    except Exception as exc:
        import logging; logging.getLogger(__name__).warning("Hunter domain search failed (%s): %s", domain, exc)
        return []


# ── Hunter.io helper: keyword/company search ─────────────────

def _hunter_keyword_search(api_key: str, keyword: str) -> list:
    """Return prospects via Hunter company search by keyword."""
    try:
        resp = requests.get(
            "https://api.hunter.io/v2/domain-search",
            params={"company": keyword, "api_key": api_key, "limit": 10},
            timeout=10,
        )
        data = resp.json()
        emails = data.get("data", {}).get("emails", [])
        return [
            {
                "email":      e.get("value"),
                "first_name": e.get("first_name"),
                "last_name":  e.get("last_name"),
                "position":   e.get("position"),
                "company":    data.get("data", {}).get("organization"),
                "confidence": e.get("confidence", 0),
            }
            for e in emails
            if e.get("value") and e.get("confidence", 0) >= 50
        ]
    except Exception as exc:
        import logging; logging.getLogger(__name__).warning("Hunter keyword search failed (%s): %s", keyword, exc)
        return []


# ── Add a prospect to a tenant (skip duplicates) ─────────────

def _add_prospect(tenant, prospect: dict, source: str, now: datetime) -> bool:
    """
    Insert a new Lead row if the email doesn't already exist for this tenant.
    Enrolls in 'nurture' sequence starting in 1 hour.
    Returns True if a new lead was created, False if it was a duplicate.
    """
    from models import db, Lead
    from sqlalchemy.exc import IntegrityError

    email = (prospect.get("email") or "").strip().lower()
    if not email:
        return False

    existing = Lead.query.filter_by(tenant_id=tenant.id, email=email).first()
    if existing:
        return False

    lead = Lead(
        tenant_id     = tenant.id,
        email         = email,
        first_name    = prospect.get("first_name"),
        last_name     = prospect.get("last_name"),
        company       = prospect.get("company"),
        source        = source,
        status        = "new",
        sequence_name = "nurture",
        sequence_step = 0,
        next_email_at = now + timedelta(hours=1),
    )
    db.session.add(lead)
    try:
        db.session.flush()   # catch unique-constraint violation early
        return True
    except IntegrityError:
        db.session.rollback()
        return False
