"""
alerts.py — Email alerting for DynaMo critical events via Resend REST API.

Only fires on trust-threatening conditions (fail-safe activated, full-city pauses).
Never fires on normal weather-driven switches.
"""

import logging
import os

import httpx

logger = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"
FROM_EMAIL = "onboarding@resend.dev"


def send_critical_alert(subject: str, body: str, to_email: str) -> None:
    api_key = os.environ.get("RESEND_API_KEY", "")
    if not api_key:
        logger.warning("RESEND_API_KEY not set — skipping email alert")
        return

    payload = {
        "from": FROM_EMAIL,
        "to": [to_email],
        "subject": subject,
        "text": body,
    }

    try:
        response = httpx.post(
            RESEND_API_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
            timeout=10,
        )
        response.raise_for_status()
        logger.info("Alert sent: %s", subject)
    except Exception as exc:
        logger.error("Failed to send alert email (%s): %s", subject, exc)
