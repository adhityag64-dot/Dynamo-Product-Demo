"""
alerts.py — Email alerting for DynaMo via Resend API (HTTP, works on Railway).

Recipient is read live from the Supabase `settings` table (key=alert_email).
Falls back to CMO_EMAIL env var if not configured.

Env vars needed:
    RESEND_API_KEY=re_...
    CMO_EMAIL=fallback@example.com   (optional)
"""

import logging
import os
import httpx

logger = logging.getLogger(__name__)


def _get_recipient() -> str:
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
            f"— DynaMo"
        ),
        to_email=to_email,
    )


def alert_full_city_pause(city: str, items: list, to_email: str = "") -> None:
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
            f"Reasons:\n{reasons}\n\n"
            f"Check the DynaMo dashboard for the current status.\n\n"
            f"— DynaMo"
        ),
        to_email=to_email,
    )


def alert_override_set(li: dict, override: str, note: str, to_email: str = "") -> None:
    label = "FORCE ON" if override == "force_active" else "FORCE OFF"
    effect = (
        "forced live and will keep running regardless of weather"
        if override == "force_active"
        else "paused and will not run until the override is cleared"
    )
    note_line = f"\nNote: \"{note.strip()}\"\n" if note.strip() else ""
    send_critical_alert(
        subject=f"[DynaMo] Manual control activated — {li['city']} / {li['creative_name']}",
        body=(
            f"Hi,\n\n"
            f"A manual override ({label}) was just set on {li['creative_name']} in {li['city']}.\n\n"
            f"This creative is now {effect}."
            f"{note_line}\n\n"
            f"Clear the override from the DynaMo dashboard to restore auto weather targeting.\n\n"
            f"— DynaMo"
        ),
        to_email=to_email,
    )


def alert_override_cleared(li: dict, to_email: str = "") -> None:
    send_critical_alert(
        subject=f"[DynaMo] Auto control restored — {li['city']} / {li['creative_name']}",
        body=(
            f"Hi,\n\n"
            f"The manual override on {li['creative_name']} in {li['city']} has been cleared.\n\n"
            f"DynaMo will now manage this creative automatically based on live weather.\n\n"
            f"— DynaMo"
        ),
        to_email=to_email,
    )


def alert_override_stuck(li: dict, label: str, hours: int, to_email: str = "") -> None:
    send_critical_alert(
        subject=f"[DynaMo] Heads-up: {li['city']} / {li['creative_name']} on manual control for {hours}h+",
        body=(
            f"Hi,\n\n"
            f"{li['creative_name']} in {li['city']} has been set to [{label}] for more than {hours} hours.\n\n"
            f"While this override is active, DynaMo cannot automatically switch this creative based on weather. "
            f"Open the dashboard and set it back to Auto if you want weather targeting to resume.\n\n"
            f"— DynaMo"
        ),
        to_email=to_email,
    )
