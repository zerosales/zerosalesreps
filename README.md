# Autonomous Customer Acquisition Platform
### Zero-to-1,000 Growth Engine for Early-Stage SaaS

A hosted, multi-tenant platform that replaces early sales hires. One deployment. Unlimited SaaS clients. Each client gets a fully autonomous system that **finds prospects, nurtures them, converts them, and manages the lifecycle** — without human involvement.

Built by Principium Technology LLC.

---

## What This Is

Most SaaS tools help you manage customers. This helps you **get** your first customers — automatically.

**The engine, once activated:**
- Searches Hunter.io daily for ideal-fit prospects (based on ICP you define)
- Adds them to automated email sequences immediately
- Tracks opens, clicks, trial starts, and payments
- Upgrades leads to customers when Stripe confirms payment
- Re-engages stale leads and expired trials on its own
- Runs 24/7 — no sales team, no manual outreach

**What a client (tenant) experiences:**
1. Sign up → 5-minute setup wizard
2. Define their ideal customer profile (ICP)
3. Connect Stripe
4. Launch engine

From Day 1, the system works without them.

---

## Architecture

```
yourplatform.com            → Platform homepage (sells the platform to new clients)
admin.yourplatform.com      → Super admin (you — Principium)
home360.yourplatform.com    → Tenant: Home360's growth engine
quickestimate.yourplatform.com → Tenant: QuickEstimate AI's growth engine
[slug].yourplatform.com     → Any future client
```

**Three access layers:**
- **Super Admin** (`/superadmin/`) — Platform owner. Creates tenants, monitors all revenue, impersonates for support.
- **Tenant Admin** (`/dashboard/`) — SaaS client. Sees their Growth Engine dashboard, sets ICP, configures branding + email.
- **Public** (`/`) — End users. Sees branded landing page, signs up, receives automated emails, checks out via Stripe.

---

## Quick Start

### 1. Install
```bash
unzip sales_platform.zip && cd sales_platform
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure
```bash
cp .env.example .env
# Edit .env — fill in SECRET_KEY, DATABASE_URL, STRIPE keys, PLATFORM_DOMAIN, BASE_URL
```

### 3. Initialize Database
```bash
flask --app app init-db
```

### 4. Create Super Admin (you)
```bash
flask --app app create-superadmin
```

### 5. Run
```bash
python app.py
# http://localhost:5000
# Super admin: http://localhost:5000/superadmin/
```

### 6. Create Your First Client (Tenant)
Go to `http://localhost:5000/superadmin/` → **New Tenant**.

The tenant's landing page is live immediately. Direct them to their dashboard to complete the 5-step Setup Wizard.

---

## The 5-Minute Client Setup Wizard

New clients complete this at `/dashboard/onboarding`:

| Step | What it configures |
|---|---|
| 1. Product | Name, tagline, pain point, brand color, URLs |
| 2. Target ICP | Company domains, job titles, daily prospect limit |
| 3. Payments | Stripe Connect OAuth (keeps 95%+ of revenue) |
| 4. Email | From name, from email, SendGrid API key |
| 5. Launch | Activates autonomous prospecting + confirms engine is running |

After launch, the client never needs to touch the system again for it to work.

---

## Autonomous Prospecting (ICP Engine)

Configured at `/dashboard/icp`:

- **Target Domains** — companies to search (e.g. `stripe.com`, `notion.so`)
- **Keywords** — company names/types for Hunter.io company search
- **Job Title Filters** — only add people matching these titles (partial match)
- **Daily Limit** — max new prospects per day (Hunter.io free plan: 25/month)
- **Auto-Prospect Toggle** — enable/disable the daily search job

The `_auto_prospect()` scheduler job runs at 6am UTC daily. It iterates every active tenant with `icp.auto_prospect = True`, calls Hunter.io for each configured domain/keyword, filters by job title, and adds net-new leads to the nurture sequence automatically.

---

## Revenue Model

### Per-Transaction Platform Fee (Stripe Connect)
Every tenant payment routes the platform fee (default 5%, configurable) to your Stripe account. Automatic. No invoicing.

### Platform SaaS Subscription
Charge tenants monthly (Starter $97 / Growth $247 / Scale $597) to use the platform. Visible on the platform homepage.

---

## Production Deployment

### Railway (Recommended)
1. Push to GitHub
2. New project → Deploy from GitHub
3. Add PostgreSQL plugin → copy `DATABASE_URL`
4. Set all environment variables from `.env.example`
5. Start command: `gunicorn app:app --workers 2 --bind 0.0.0.0:$PORT`
6. Add custom domain: `*.yourplatform.com` (wildcard subdomain)

### Wildcard DNS
```
Type: A  |  Name: *  |  Value: [your server IP]  |  TTL: 300
```

### Stripe Connect Setup
1. [Stripe Dashboard → Connect Settings](https://dashboard.stripe.com/settings/connect)
2. Platform type: Express
3. Copy `Connect Client ID` → `.env` as `STRIPE_CONNECT_CLIENT_ID`
4. OAuth redirect: `https://yourplatform.com/dashboard/stripe/callback`
5. Webhook: `https://yourplatform.com/webhook/stripe`
   - Events: `checkout.session.completed`, `customer.subscription.deleted`

---

## Project Structure

```
sales_platform/
├── app.py                         # Flask factory, tenant middleware, blueprints
├── models.py                      # Tenant (with ICP), TenantUser, SuperAdmin,
│                                  # Lead, EmailLog, ImportLog
├── sequences.py                   # 20 email steps across 4 sequences
├── email_engine.py                # SendGrid + SMTP fallback, open/click tracking
├── scheduler_service.py           # 4 background jobs:
│                                  #   _run_sequences (15 min)
│                                  #   _stale_leads   (daily 9am)
│                                  #   _trial_expiry  (hourly)
│                                  #   _auto_prospect (daily 6am) ← NEW
├── requirements.txt
├── .env.example
│
├── blueprints/
│   ├── public/routes.py           # Landing, signup, tracking, Stripe checkout
│   ├── public/widget.py           # Embeddable JS widget + iframe form
│   ├── public/api.py              # Lead intake API, CSV import, Hunter.io search
│   ├── tenant_admin/routes.py     # Growth Engine dashboard, ICP, onboarding wizard,
│   │                              # leads, sequences, settings, Stripe Connect
│   └── super_admin/routes.py     # Platform overview, tenant CRUD, impersonation
│
└── templates/
    ├── platform/home.html         # Marketing homepage (sells the platform)
    ├── public/                    # landing, thankyou, embed, free (lead magnet)
    ├── tenant_admin/              # dashboard (Growth Engine), icp, onboarding,
    │                              # leads, lead_detail, sequences, settings,
    │                              # import, prospect_search, lead_magnet, integrations
    ├── super_admin/               # overview, tenants, tenant_detail, new_tenant
    └── emails/                    # 22 HTML email templates
```

---

## Lead Capture Channels (6 ways pipeline fills)

| Channel | How |
|---|---|
| Landing page | Hosted at `[slug].yourplatform.com/` |
| Embeddable widget | JS snippet `<script src="/widget/[slug].js">` |
| Iframe form | Embed `<iframe src="/embed/[slug]">` |
| CSV import | Dashboard → Lead Capture → Upload CSV |
| Webhook / API | `POST /api/leads` with `X-API-Key` |
| Lead magnet | Free resource at `/free` — email captured before download |
| **Autonomous ICP** | **Hunter.io daily search — no human input** ← NEW |

---

## Scheduler Jobs

| Job | Schedule | What it does |
|---|---|---|
| `_run_sequences` | Every 15 min | Sends due email steps for all active leads |
| `_stale_leads` | Daily 9am UTC | Moves 30-day-inactive leads to reengagement |
| `_trial_expiry` | Hourly | Marks expired trials, triggers re-engagement |
| `_auto_prospect` | Daily 6am UTC | ICP search via Hunter.io, adds new leads automatically |

---

## Key URLs

| URL | Description |
|---|---|
| `[slug].domain/` | Tenant landing page |
| `[slug].domain/signup` | Lead capture POST |
| `[slug].domain/free` | Lead magnet landing |
| `[slug].domain/dashboard/` | Tenant Growth Engine dashboard |
| `[slug].domain/dashboard/onboarding` | 5-step setup wizard |
| `[slug].domain/dashboard/icp` | ICP configuration |
| `yourplatform.com/superadmin/` | Super admin |
| `[slug].domain/api/leads` | Lead intake API |
| `[slug].domain/widget/[slug].js` | Embeddable widget script |
| `/track/open/<token>` | Email open pixel |
| `/track/click/<token>` | Click redirect + tracking |

---

## Support

Principium Technology LLC — principiumtechnology@gmail.com
