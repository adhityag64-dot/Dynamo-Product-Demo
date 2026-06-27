"""
scratch_test.py — Smoke test for weather.py + engine.py.

Pulls all line items and city configs from Supabase, fetches live Open-Meteo weather
for each city, prints the decision for every line item.
"""

import os
from dotenv import load_dotenv
from supabase import create_client

from weather import get_weather
from engine import compute_city_condition, decide_state

load_dotenv()
supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

# --- Load data from Supabase ---
line_items = supabase.table("line_items").select("*").execute().data
city_configs = supabase.table("city_config").select("*").execute().data

# Index city config by city name for fast lookup
cfg_by_city = {row["city"]: row for row in city_configs}

# Group line items by city
from collections import defaultdict
items_by_city: dict = defaultdict(list)
for li in line_items:
    items_by_city[li["city"]].append(li)

# Previous condition is unknown on first run — default to 'normal'
# (a real scheduler would persist this between runs)
previous_condition_by_city: dict = {city: "normal" for city in items_by_city}

print("=" * 70)
print("DynaMo Decision Engine — Scratch Test")
print("=" * 70)

for city, items in sorted(items_by_city.items()):
    cfg = cfg_by_city.get(city)
    if not cfg:
        print(f"\n[{city}] WARNING: no city_config row found, skipping")
        continue

    # Pick coords from the first line item (all items in a city share the same coords)
    lat = items[0]["latitude"]
    lon = items[0]["longitude"]

    weather = get_weather(lat, lon)

    if weather["ok"]:
        condition = compute_city_condition(
            weather, cfg, previous_condition_by_city[city]
        )
        weather_summary = (
            f"feels_like={weather['feels_like']:.1f}°C  "
            f"precip={weather['precip']:.1f}mm"
        )
    else:
        condition = "unknown"
        weather_summary = "WEATHER API FAILED"

    print(f"\n{'─' * 70}")
    print(f"City       : {city}")
    print(f"Thresholds : hot≥{cfg['hot_threshold']}°C  "
          f"hot_clear<{cfg['hot_clear_below']}°C  "
          f"rain≥{cfg['rainy_threshold']}mm")
    print(f"Weather    : {weather_summary}")
    print(f"Condition  : {condition}")
    print()

    for li in sorted(items, key=lambda x: x["creative_id"]):
        desired_state, reason = decide_state(li, weather, cfg, condition)
        marker = "▶" if desired_state == "active" else "⏸"
        print(
            f"  {marker}  [{li['creative_id']}] {li['creative_name']:<20} "
            f"→ {desired_state:<6}  ({reason})"
        )

print(f"\n{'=' * 70}")
print("Done.")
