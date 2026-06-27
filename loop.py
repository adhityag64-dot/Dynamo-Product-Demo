"""
loop.py — DynaMo runner loop.

Usage:
    python loop.py        # runs every 10 minutes via APScheduler
    python loop.py --once # single cycle then exit
"""

import sys
import os
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
from supabase import create_client

from weather import get_weather
from engine import compute_city_condition, decide_state
from alerts import send_critical_alert

load_dotenv()
supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

CMO_EMAIL = os.environ.get("CMO_EMAIL", "")

# Per-city condition persisted across scheduler ticks within the same process.
# On first run every city starts as 'normal'; hysteresis kicks in from tick 2 onward.
_previous_condition: dict[str, str] = {}

# Alert dedup: tracks which alert keys are currently "active" so we only email
# on the FIRST occurrence, not every cycle while the condition persists.
# Keys: "weather_fail:{city}", "full_pause:{city}", "budget:{id}", "override_stuck:{id}"
_alerted: set[str] = set()

OVERRIDE_STUCK_HOURS = 4   # alert if manual override has been set for longer than this


def _maybe_alert(key: str, subject: str, body: str) -> None:
    """Send alert only if this condition hasn't been alerted yet."""
    if key not in _alerted:
        _alerted.add(key)
        if CMO_EMAIL:
            send_critical_alert(subject, body, CMO_EMAIL)


def _clear_alert(key: str) -> None:
    """Clear a previous alert key so it re-fires if the condition returns."""
    _alerted.discard(key)


def run_cycle() -> None:
    now = datetime.now(timezone.utc).isoformat()
    print(f"\n{'=' * 70}")
    print(f"DynaMo cycle — {now}")
    print(f"{'=' * 70}")

    # 1. Fetch line items and city configs
    line_items = supabase.table("line_items").select("*").execute().data
    city_configs = supabase.table("city_config").select("*").execute().data
    cfg_by_city = {row["city"]: row for row in city_configs}

    # Group line items by city
    items_by_city: dict[str, list] = defaultdict(list)
    for li in line_items:
        items_by_city[li["city"]].append(li)

    # 2-4. Process each city
    for city in sorted(items_by_city):
        items = items_by_city[city]
        cfg = cfg_by_city.get(city)

        print(f"\n  {city}")

        if not cfg:
            print(f"    WARNING: no city_config row — skipping")
            continue

        # One weather call per city
        lat, lon = items[0]["latitude"], items[0]["longitude"]
        weather = get_weather(lat, lon)

        weather_fail_key = f"weather_fail:{city}"

        if weather["ok"]:
            prev = _previous_condition.get(city, "normal")
            condition = compute_city_condition(weather, cfg, prev)
            _previous_condition[city] = condition
            weather_str = (
                f"temp={weather['temperature']:.1f}°C  "
                f"feels_like={weather['feels_like']:.1f}°C  "
                f"precip={weather['precip']:.1f}mm  →  {condition}"
            )
            _clear_alert(weather_fail_key)
        else:
            condition = "unknown"
            weather_str = "WEATHER API FAILED — fail-safe active"
            _maybe_alert(
                weather_fail_key,
                subject=f"[DynaMo] Weather data lost for {city} — safe ad is running",
                body=(
                    f"Hi,\n\n"
                    f"DynaMo lost its weather data feed for {city} and has automatically "
                    f"switched all ads in that city to your safe generic creative. "
                    f"No wrong ad is running — the campaign is protected.\n\n"
                    f"This usually means a temporary outage with the weather provider. "
                    f"DynaMo will switch back to weather-targeted ads automatically once "
                    f"the data comes back.\n\n"
                    f"Check in when you can. No urgent action is needed.\n\n"
                    f"— DynaMo"
                ),
            )

        # Persist live weather reading so the dashboard can read it directly
        supabase.table("city_weather").upsert({
            "city": city,
            "temperature": weather.get("temperature"),
            "feels_like": weather.get("feels_like"),
            "precip": weather.get("precip"),
            "condition": condition,
            "fetched_at": now,
        }, on_conflict="city").execute()

        print(f"    {weather_str}")

        decisions = []
        for li in sorted(items, key=lambda x: x["creative_id"]):
            desired_state, reason = decide_state(li, weather, cfg, condition)
            decisions.append((li, desired_state, reason))

        # Check for full-city pause not caused by weather failure
        full_pause_key = f"full_pause:{city}"
        all_paused = all(state == "paused" for _, state, _ in decisions)
        weather_failed = not weather["ok"]

        if all_paused and not weather_failed:
            _maybe_alert(
                full_pause_key,
                subject=f"[DynaMo] All ads paused in {city} — needs your attention",
                body=(
                    f"Hi,\n\n"
                    f"DynaMo has paused every ad line in {city}. This is not caused by "
                    f"a weather issue — it looks like it may be related to budget or "
                    f"another campaign-level constraint.\n\n"
                    f"No ads are currently running in {city}. Please log in to your "
                    f"campaign dashboard and check the budget and flight dates for "
                    f"{city} when you get a chance.\n\n"
                    f"— DynaMo"
                ),
            )
        else:
            # At least one ad is active — clear the alert so it re-fires if it returns
            _clear_alert(full_pause_key)

        # Apply decisions + per-line-item alerts
        for li, desired_state, reason in decisions:
            current_state = li["state"]
            marker = "▶" if desired_state == "active" else "⏸"

            state_changed = desired_state != current_state
            reason_changed = reason != li.get("current_reason")

            if state_changed:
                # State flipped — full write + transition log
                supabase.table("line_items").update({
                    "state": desired_state,
                    "current_reason": reason,
                    "last_updated": now,
                }).eq("id", li["id"]).execute()
                supabase.table("transitions").insert({
                    "line_item_id": li["id"],
                    "from_state": current_state,
                    "to_state": desired_state,
                    "reason": reason,
                    "timestamp": now,
                }).execute()
                change_tag = f"  [{current_state} → {desired_state}]"
            elif reason_changed:
                # Only the reason string drifted (e.g. temperature ticked 1°C) —
                # update silently without touching last_updated or logging a transition.
                supabase.table("line_items").update({
                    "current_reason": reason,
                }).eq("id", li["id"]).execute()
                change_tag = ""
            else:
                change_tag = ""

            # ALERT: budget exhausted
            budget_key = f"budget:{li['id']}"
            if li["spend_today"] >= li["daily_budget"]:
                _maybe_alert(
                    budget_key,
                    subject=f"[DynaMo] Budget exhausted — {li['city']} / {li['creative_name']}",
                    body=(
                        f"Hi,\n\n"
                        f"The daily budget for {li['creative_name']} in {li['city']} has been "
                        f"fully spent (₹{li['spend_today']:.0f} of ₹{li['daily_budget']:.0f}).\n\n"
                        f"DynaMo has paused this line item for the rest of the day. It will "
                        f"resume automatically tomorrow when spend_today resets to 0.\n\n"
                        f"If you need it running today, increase the daily budget from the dashboard.\n\n"
                        f"— DynaMo"
                    ),
                )
            else:
                _clear_alert(budget_key)

            # ALERT: manual override stuck for too long
            override_key = f"override_stuck:{li['id']}"
            if li.get("override") != "none":
                try:
                    last_updated = datetime.fromisoformat(
                        li["last_updated"].replace("Z", "+00:00")
                    )
                    age = datetime.now(timezone.utc) - last_updated
                    if age > timedelta(hours=OVERRIDE_STUCK_HOURS):
                        label = "FORCE ON" if li["override"] == "force_active" else "FORCE OFF"
                        _maybe_alert(
                            override_key,
                            subject=f"[DynaMo] Manual override still active — {li['city']} / {li['creative_name']}",
                            body=(
                                f"Hi,\n\n"
                                f"Just a heads-up: {li['creative_name']} in {li['city']} has been "
                                f"on a manual [{label}] override for more than {OVERRIDE_STUCK_HOURS} hours.\n\n"
                                f"DynaMo cannot make automatic weather-based decisions for this line item "
                                f"while the override is active. If this was intentional, no action needed. "
                                f"If you forgot to clear it, set the override back to Auto from the dashboard.\n\n"
                                f"— DynaMo"
                            ),
                        )
                except (ValueError, TypeError):
                    pass
            else:
                _clear_alert(override_key)

            print(
                f"    {marker}  [{li['creative_id']}] {li['creative_name']:<22} "
                f"{desired_state:<6}{change_tag}  {reason}"
            )

    print(f"\n{'=' * 70}\n")


def main() -> None:
    if "--once" in sys.argv:
        run_cycle()
        return

    from apscheduler.schedulers.blocking import BlockingScheduler

    scheduler = BlockingScheduler()
    scheduler.add_job(run_cycle, "interval", minutes=5, next_run_time=datetime.now())
    print("DynaMo scheduler started — running every 5 minutes. Ctrl-C to stop.")
    try:
        scheduler.start()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
