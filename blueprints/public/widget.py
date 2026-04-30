"""
blueprints/public/widget.py — Embeddable lead capture widget.

Two embed modes:
  1. JS Popup  — <script src="/widget/<slug>.js"></script>
                 Injects a floating "Get Free Access" button + modal form.

  2. Iframe    — <iframe src="/embed/<slug>" ...></iframe>
                 Standalone inline form for embedding in any page.

Both submit to the same /signup endpoint and enter the lead into the
nurture sequence like any other signup.
"""

import os
from flask import Blueprint, Response, render_template, g, current_app
from models import Tenant

widget_bp = Blueprint("widget", __name__)


# ─────────────────────────────────────────────
#  JS Widget  (popup mode)
# ─────────────────────────────────────────────

@widget_bp.route("/widget/<slug>.js")
def widget_js(slug):
    """
    Serve a self-contained JavaScript widget for a specific tenant.
    The JS renders a floating CTA button + modal signup form on any host website.
    Usage: <script src="https://yourplatform.com/widget/home360.js"></script>
    """
    tenant = Tenant.query.filter_by(slug=slug, status="active").first()
    if not tenant:
        return Response("/* tenant not found */", mimetype="application/javascript")

    base_url   = current_app.config.get("BASE_URL", "").rstrip("/")
    color      = tenant.get_config("primary_color", "#4F46E5")
    name       = tenant.get_config("name", slug)
    tagline    = tenant.get_config("tagline", f"Try {name} free")
    pain_point = tenant.get_config("pain_point", "")
    signup_url = f"{base_url}/signup"   # public blueprint signup endpoint

    js = f"""
(function() {{
  if (window.__salespilot_loaded) return;
  window.__salespilot_loaded = true;

  var PRIMARY = '{color}';
  var TOOL    = '{name}';
  var SLUG    = '{slug}';
  var SIGNUP  = '{signup_url}';

  // Inject styles
  var style = document.createElement('style');
  style.innerHTML = [
    '#sp-btn{{position:fixed;bottom:24px;right:24px;z-index:99999;background:' + PRIMARY + ';color:#fff;border:none;border-radius:50px;padding:14px 24px;font-size:14px;font-weight:700;cursor:pointer;box-shadow:0 4px 16px rgba(0,0,0,.2);font-family:system-ui,sans-serif}}',
    '#sp-btn:hover{{filter:brightness(.9)}}',
    '#sp-overlay{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:100000;align-items:center;justify-content:center}}',
    '#sp-overlay.open{{display:flex}}',
    '#sp-modal{{background:#fff;border-radius:16px;padding:32px;width:100%;max-width:420px;margin:16px;font-family:system-ui,sans-serif;box-shadow:0 20px 60px rgba(0,0,0,.3)}}',
    '#sp-modal h3{{margin:0 0 6px;font-size:20px;font-weight:700;color:#0f172a}}',
    '#sp-modal p{{margin:0 0 20px;font-size:14px;color:#64748b}}',
    '#sp-modal input{{display:block;width:100%;box-sizing:border-box;padding:10px 14px;border:1.5px solid #e2e8f0;border-radius:8px;font-size:14px;margin-bottom:10px;outline:none}}',
    '#sp-modal input:focus{{border-color:' + PRIMARY + '}}',
    '#sp-submit{{display:block;width:100%;padding:12px;background:' + PRIMARY + ';color:#fff;border:none;border-radius:8px;font-size:15px;font-weight:700;cursor:pointer}}',
    '#sp-submit:hover{{filter:brightness(.9)}}',
    '#sp-close{{position:absolute;top:12px;right:16px;background:none;border:none;font-size:20px;cursor:pointer;color:#94a3b8}}',
    '#sp-modal .sp-wrap{{position:relative}}'
  ].join('');
  document.head.appendChild(style);

  // Button
  var btn = document.createElement('button');
  btn.id = 'sp-btn';
  btn.textContent = 'Get Free Access →';
  document.body.appendChild(btn);

  // Overlay + Modal
  var overlay = document.createElement('div');
  overlay.id = 'sp-overlay';
  overlay.innerHTML = '<div id="sp-modal"><div class="sp-wrap"><button id="sp-close">✕</button><h3>Try ' + TOOL + ' Free</h3><p>{tagline}</p><form id="sp-form" action="' + SIGNUP + '" method="POST"><input type="hidden" name="tool" value="' + SLUG + '"/><input type="hidden" name="source" value="widget"/><div style="display:flex;gap:8px"><input name="first_name" placeholder="First name" style="flex:1"/><input name="last_name" placeholder="Last name" style="flex:1"/></div><input type="email" name="email" placeholder="Your email address" required/><button type="submit" id="sp-submit">Get Free Access →</button><p style="text-align:center;font-size:11px;color:#94a3b8;margin-top:10px">No credit card · Cancel anytime</p></form></div></div>';
  document.body.appendChild(overlay);

  btn.addEventListener('click', function() {{ overlay.classList.add('open'); }});
  document.getElementById('sp-close').addEventListener('click', function() {{ overlay.classList.remove('open'); }});
  overlay.addEventListener('click', function(e) {{ if (e.target === overlay) overlay.classList.remove('open'); }});
}})();
""".replace("{tagline}", tagline)

    return Response(js.strip(), mimetype="application/javascript",
                    headers={"Cache-Control": "public, max-age=300"})


# ─────────────────────────────────────────────
#  Iframe Embed
# ─────────────────────────────────────────────

@widget_bp.route("/embed/<slug>")
def embed_iframe(slug):
    """
    Standalone iframe-embeddable signup form.
    Usage: <iframe src="https://yourplatform.com/embed/home360"
                   width="100%" height="420" frameborder="0"></iframe>
    """
    tenant = Tenant.query.filter_by(slug=slug, status="active").first()
    if not tenant:
        return Response("<p>Not found</p>", mimetype="text/html"), 404

    base_url = current_app.config.get("BASE_URL", "").rstrip("/")
    return render_template("public/embed.html", tenant=tenant, base_url=base_url)
