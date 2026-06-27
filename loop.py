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
from alerts import alert_weather_fail, alert_full_city_pause, alert_override_stuck

load_dotenv()
supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

# Per-city condition persisted across scheduler ticks within the same process.
# On first run every city starts as 'normal'; hysteresis kicks in from tick 2 onward.
_previous_condition: dict[str, str] = {}

# Alert dedup: tracks which alert keys are currently "active" so we only email
# on the FIRST occurrence, not every cycle while the condition persists.
# Keys: "weather_fail:{city}", "full_pause:{city}", "override_stuck:{id}"
_alerted: set[str] = set()

OVERRIDE_STUCK_HOURS = 4   # alert if manual override has been set for longer than this


def _maybe_alert(key: str, fn, *args) -> None:
    """Call fn(*args) only if this condition hasn't been alerted yet this process run.
    Recipient email is resolved inside alerts.py from the DB settings table."""
    if key not in _alerted:
        _alerted.add(key)
        fn(*args)


def _clear_alert(key: str) -> None:
    """Clear a previous alert key so it re-fires if the condition returns."""
    _alerted.discard(key)


def run_cycle(simulate_weather_fail: set = None) -> None:
    cycle_start = datetime.now(timezone.utc)
    now = cycle_start.isoformat()
    print(f"\n{'=' * 70}")
    print(f"DynaMo cycle — {now}")
    print(f"{'=' * 70}")
    _n_cities = 0
    _n_decisions = 0
    _n_transitions = 0

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

        # One weather call per city (or inject a failure for simulation)
        lat, lon = items[0]["latitude"], items[0]["longitude"]
        if simulate_weather_fail and city in simulate_weather_fail:
            weather = {"ok": False}
            print(f"    [SIMULATE] weather-fail injected for {city}")
        else:
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
            _maybe_alert(weather_fail_key, alert_weather_fail, city)

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
            _maybe_alert(full_pause_key, alert_full_city_pause, city, items)
        else:
            # At least one ad is active — clear the alert so it re-fires if it returns
            _clear_alert(full_pause_key)

        _n_cities += 1
        _n_decisions += len(decisions)

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
                _n_transitions += 1
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
                        _maybe_alert(override_key, alert_override_stuck, li, label, OVERRIDE_STUCK_HOURS)
                except (ValueError, TypeError):
                    pass
            else:
                _clear_alert(override_key)

            print(
                f"    {marker}  [{li['creative_id']}] {li['creative_name']:<22} "
                f"{desired_state:<6}{change_tag}  {reason}"
            )

    # Log completed cycle
    finished = datetime.now(timezone.utc).isoformat()
    supabase.table("cycles").insert({
        "started_at":  now,
        "finished_at": finished,
        "cities":      _n_cities,
        "decisions":   _n_decisions,
        "transitions": _n_transitions,
    }).execute()

    print(f"\n{'=' * 70}\n")


def _parse_simulate_args() -> set:
    """Parse --simulate weather-fail:City1,City2 from argv. Returns set of city names."""
    cities = set()
    for i, arg in enumerate(sys.argv):
        if arg == "--simulate" and i + 1 < len(sys.argv):
            spec = sys.argv[i + 1]
            if spec.startswith("weather-fail:"):
                cities = {c.strip() for c in spec[len("weather-fail:"):].split(",")}
    return cities


def main() -> None:
    simulate_weather_fail = _parse_simulate_args()
    if simulate_weather_fail:
        # Clear alerted set so email fires even if a prior run already sent it
        _alerted.clear()
        print(f"[SIMULATE] Injecting weather failure for: {', '.join(sorted(simulate_weather_fail))}")

    if "--once" in sys.argv or simulate_weather_fail:
        run_cycle(simulate_weather_fail=simulate_weather_fail)
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
