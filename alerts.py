"""
alerts.py — Email alerting for DynaMo via Resend HTTP API.

Uses Resend (HTTP) instead of Gmail SMTP — works on Railway and all cloud hosts.

Env vars:
    RESEND_API_KEY=re_...
    CMO_EMAIL=fallback@example.com   (used if settings table has no email)
"""

import logging
import os
import httpx

logger = logging.getLogger(__name__)


def _get_recipient() -> str:
    """Read alert_email from DB settings. Falls back to CMO_EMAIL env var."""
    try:
        from supabase import create_client
        sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
        row = sb.table("settings").select("value").eq("key", "alert_email").execute().data
        if row and row[0].get("value"):
            return row[0]["value"]
    except Exception:
        pass
    return os.environ.get("CMO_EMAIL", "")


def send_critical_alert(subject: str, body: str, to_email: str = "") -> None:
    api_key = os.environ.get("RESEND_API_KEY", "")
    if not api_key:
        logger.warning("RESEND_API_KEY not set — skipping alert")
        return

    recipient = to_email or _get_recipient()
    if not recipient:
        logger.warning("No alert recipient configured — skipping alert")
        return

    try:
        resp = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "from": "DynaMo <onboarding@resend.dev>",
                "to": [recipient],
                "subject": subject,
                "text": body,
            },
            timeout=10.0,
        )
        resp.raise_for_status()
        logger.info("Alert sent to %s: %s", recipient, subject)
    except Exception as exc:
        logger.error("Failed to send alert email (%s): %s", subject, exc)


# ── Named alert functions ─────────────────────────────────────────────────────

def alert_weather_fail(city: str, to_email: str = "") -> None:
    """Weather API is down for a city — generic creative is running as fallback."""
    send_critical_alert(
        subject=f"[DynaMo] Weather signal lost for {city} — generic ad is now running",
        body=(
            f"Hi,\n\n"
            f"DynaMo just lost its live weather feed for {city}.\n\n"
            f"To keep the campaign protected, it has automatically switched every line "
            f"in {city} to your safe generic creative (CR-NORM). No wrong ad is "
            f"showing — the campaign is still live.\n\n"
            f"DynaMo will switch back to weather-targeted creatives the moment the "
            f"weather signal comes back. You don't need to do anything.\n\n"
            f"If this keeps happening, the weather provider may be experiencing an "
            f"outage. You can check the DynaMo dashboard for the latest condition.\n\n"
            f"— DynaMo"
        ),
        to_email=to_email,
    )


def alert_full_city_pause(city: str, items: list, to_email: str = "") -> None:
    """Every line item in a city is paused for a non-weather reason."""
    reasons = "\n".join(
        f"  • {li['creative_name']}: {li.get('current_reason') or 'unknown'}"
        for li in items
    )
    send_critical_alert(
        subject=f"[DynaMo] No ads are running in {city} right now",
        body=(
            f"Hi,\n\n"
            f"DynaMo has paused every creative in {city}. This is NOT a weather issue — "
            f"the weather data is fine.\n\n"
            f"Here is what each line is showing as the reason:\n"
            f"{reasons}\n\n"
            f"The most common cause is that all three creatives hit their daily budget "
            f"at the same time. If that's the case, the lines will resume automatically "
            f"tomorrow when budgets reset — or you can increase the daily budget from "
            f"the dashboard if you need coverage today.\n\n"
            f"Check the DynaMo dashboard for the current status.\n\n"
            f"— DynaMo"
        ),
        to_email=to_email,
    )


def alert_override_set(li: dict, override: str, note: str, to_email: str = "") -> None:
    """A manual override was just applied to a line item."""
    label = "FORCE ON" if override == "force_active" else "FORCE OFF"
    effect = (
        "forced live and will keep running regardless of weather"
        if override == "force_active"
        else "paused and will not run until the override is cleared"
    )
    note_line = f"\nNote you provided: \"{note.strip()}\"\n" if note.strip() else ""
    send_critical_alert(
        subject=f"[DynaMo] Manual control activated — {li['city']} / {li['creative_name']}",
        body=(
            f"Hi,\n\n"
            f"A manual override ({label}) was just set on {li['creative_name']} in {li['city']}.\n\n"
            f"What this means: this creative is now {effect}. "
            f"DynaMo will not make any automatic weather-based decisions for it "
            f"until the override is cleared back to Auto.\n"
            f"{note_line}\n"
            f"If this was intentional — great, no action needed. "
            f"If this was set by mistake, open the DynaMo dashboard and set the "
            f"override back to Auto to restore normal weather targeting.\n\n"
            f"— DynaMo"
        ),
        to_email=to_email,
    )


def alert_override_cleared(li: dict, to_email: str = "") -> None:
    """A manual override was cleared — auto control restored."""
    send_critical_alert(
        subject=f"[DynaMo] Auto control restored — {li['city']} / {li['creative_name']}",
        body=(
            f"Hi,\n\n"
            f"The manual override on {li['creative_name']} in {li['city']} has been cleared.\n\n"
            f"DynaMo will now manage this creative automatically based on live weather. "
            f"No further action is needed.\n\n"
            f"— DynaMo"
        ),
        to_email=to_email,
    )


def alert_override_stuck(li: dict, label: str, hours: int, to_email: str = "") -> None:
    """A manual override has been active longer than the warning threshold."""
    send_critical_alert(
        subject=f"[DynaMo] Heads-up: {li['city']} / {li['creative_name']} has been on manual control for {hours}h+",
        body=(
            f"Hi,\n\n"
            f"Just a reminder: {li['creative_name']} in {li['city']} has been set to "
            f"[{label}] for more than {hours} hours.\n\n"
            f"While this override is active, DynaMo cannot automatically switch this "
            f"creative based on weather changes. If conditions shift in {li['city']} "
            f"(say, it starts raining), this creative won't respond.\n\n"
            f"If the override was intentional and you still want it, no action needed. "
            f"If you're done with the manual control, open the dashboard and set it "
            f"back to Auto — DynaMo will take over immediately.\n\n"
            f"— DynaMo"
        ),
        to_email=to_email,
    )
