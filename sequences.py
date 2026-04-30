"""
sequences.py — Email sequence definitions (tenant-aware).

Each sequence is a list of steps. Steps reference Jinja2 template names
under templates/emails/. Subject lines support {tool_name} and {pain_point}
placeholders that are filled at send time from the tenant config.
"""

SEQUENCES = {
    "nurture": [
        {"step": 1, "day_offset": 0,  "template": "nurture_1_welcome",   "subject": "Welcome to {tool_name} — here's what happens next",             "cta_url_key": "trial_url"},
        {"step": 2, "day_offset": 2,  "template": "nurture_2_benefit",   "subject": "The #1 thing {tool_name} users stop worrying about",             "cta_url_key": "trial_url"},
        {"step": 3, "day_offset": 4,  "template": "nurture_3_social",    "subject": "What others are saying about {tool_name}",                       "cta_url_key": "trial_url"},
        {"step": 4, "day_offset": 6,  "template": "nurture_4_objection", "subject": '"But what if it doesn\'t work for me?" — answered',              "cta_url_key": "trial_url"},
        {"step": 5, "day_offset": 8,  "template": "nurture_5_cta",       "subject": "Your free trial is ready — claim it today",                      "cta_url_key": "trial_url"},
        {"step": 6, "day_offset": 11, "template": "nurture_6_urgency",   "subject": "Last reminder: your {tool_name} access is waiting",              "cta_url_key": "trial_url"},
        {"step": 7, "day_offset": 14, "template": "nurture_7_lastchance","subject": "This is my last email (unless you want more)",                   "cta_url_key": "trial_url"},
    ],
    "trial": [
        {"step": 1, "day_offset": 0,  "template": "trial_1_started",   "subject": "Your {tool_name} trial has started — let's get you set up",        "cta_url_key": "app_url"},
        {"step": 2, "day_offset": 3,  "template": "trial_2_tip",        "subject": "Quick tip: the one feature that makes the biggest difference",      "cta_url_key": "app_url"},
        {"step": 3, "day_offset": 7,  "template": "trial_3_success",    "subject": "Halfway through your trial — how's it going?",                     "cta_url_key": "pricing_url"},
        {"step": 4, "day_offset": 11, "template": "trial_4_expiring",   "subject": "Your {tool_name} trial expires in 3 days",                         "cta_url_key": "pricing_url"},
        {"step": 5, "day_offset": 14, "template": "trial_5_expired",    "subject": "Your trial has ended — pick up where you left off",                 "cta_url_key": "pricing_url"},
    ],
    "onboarding": [
        {"step": 1, "day_offset": 0,  "template": "onboard_1_welcome",  "subject": "Welcome to {tool_name} — you're officially a customer 🎉",         "cta_url_key": "app_url"},
        {"step": 2, "day_offset": 3,  "template": "onboard_2_quickwin", "subject": "Your first quick win with {tool_name}",                            "cta_url_key": "app_url"},
        {"step": 3, "day_offset": 7,  "template": "onboard_3_advanced", "subject": "Going deeper: advanced features you'll love",                      "cta_url_key": "app_url"},
        {"step": 4, "day_offset": 14, "template": "onboard_4_community","subject": "You're not alone — meet other {tool_name} users",                  "cta_url_key": "app_url"},
        {"step": 5, "day_offset": 30, "template": "onboard_5_month",    "subject": "One month with {tool_name} — how are you doing?",                  "cta_url_key": "app_url"},
    ],
    "reengagement": [
        {"step": 1, "day_offset": 0,  "template": "reeng_1_missyou",   "subject": "We miss you — is {tool_name} still working for you?",               "cta_url_key": "app_url"},
        {"step": 2, "day_offset": 5,  "template": "reeng_2_update",    "subject": "A lot has changed at {tool_name} — come see",                       "cta_url_key": "app_url"},
        {"step": 3, "day_offset": 10, "template": "reeng_3_finalask",  "subject": "One last note before I go quiet",                                   "cta_url_key": "trial_url"},
    ],
}


def get_next_step(sequence_name: str, current_step: int):
    """Return the next step dict, or None if sequence is complete."""
    steps = SEQUENCES.get(sequence_name, [])
    for step in steps:
        if step["step"] > current_step:
            return step
    return None


def get_step(sequence_name: str, step_number: int):
    """Return a specific step dict by number."""
    for step in SEQUENCES.get(sequence_name, []):
        if step["step"] == step_number:
            return step
    return None
