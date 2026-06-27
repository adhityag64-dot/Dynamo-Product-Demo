"""
app.py — DynaMo visibility dashboard.

Run:  uvicorn app:app --reload --port 8000
"""

import os
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
from supabase import create_client

from weather import get_weather
from engine import compute_city_condition, decide_state
from loop import run_cycle
from alerts import alert_override_set, alert_override_cleared

load_dotenv()
supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

app = FastAPI(title="DynaMo Dashboard")


# ── Data helpers ─────────────────────────────────────────────────────────────

def fetch_data():
    line_items   = supabase.table("line_items").select("*").order("city").execute().data
    transitions  = (supabase.table("transitions").select("*")
                    .order("timestamp", desc=True).limit(100).execute().data)
    city_configs = supabase.table("city_config").select("*").execute().data
    return line_items, transitions, {r["city"]: r for r in city_configs}


def get_city_weather_summary(items_by_city, cfg_by_city):
    """Return {city: {feels_like, precip, condition}} using cached weather."""
    summary = {}
    for city, items in items_by_city.items():
        lat, lon = items[0]["latitude"], items[0]["longitude"]
        w = get_weather(lat, lon)
        cfg = cfg_by_city.get(city, {})
        if w["ok"] and cfg:
            condition = compute_city_condition(w, cfg, "normal")
            summary[city] = {
                "feels_like": w["feels_like"],
                "precip": w["precip"],
                "condition": condition,
                "ok": True,
            }
        else:
            summary[city] = {"ok": False}
    return summary


def compute_alerts(items_by_city, weather_by_city):
    alerts = []
    for city, items in items_by_city.items():
        # (a) failsafe active
        for li in items:
            if "failsafe" in (li.get("current_reason") or ""):
                alerts.append(
                    f"⚠ FAILSAFE: {city} / {li['creative_name']} — weather data unavailable"
                )
                break

        # (b) all items paused
        if all(li["state"] == "paused" for li in items):
            alerts.append(f"⚠ ALL PAUSED: every creative in {city} is paused — no ads serving")

        # (c) manual override active
        for li in items:
            if li.get("override") != "none":
                badge = "FORCE ON" if li["override"] == "force_active" else "FORCE OFF"
                alerts.append(
                    f"⚠ MANUAL OVERRIDE [{badge}]: {city} / {li['creative_name']}"
                )
    return alerts


# ── HTML rendering ────────────────────────────────────────────────────────────

CONDITION_COLORS = {"hot": "#f97316", "rainy": "#3b82f6", "normal": "#10b981", "unknown": "#6b7280"}

def fmt_ts(ts_str):
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.strftime("%H:%M:%S")
    except Exception:
        return ts_str or "—"


def render_page(line_items, transitions, cfg_by_city) -> str:
    items_by_city: dict = defaultdict(list)
    for li in line_items:
        items_by_city[li["city"]].append(li)

    # Also build a lookup from line_item id → city for transitions display
    city_by_id = {li["id"]: li["city"] for li in line_items}
    name_by_id = {li["id"]: li["creative_name"] for li in line_items}

    weather_by_city = get_city_weather_summary(items_by_city, cfg_by_city)
    alerts = compute_alerts(items_by_city, weather_by_city)

    # ── Alerts panel ──────────────────────────────────────────────────────────
    if alerts:
        alerts_html = "".join(
            f'<div class="alert">{a}</div>' for a in alerts
        )
    else:
        alerts_html = '<div class="alert ok">✓ All systems normal — no issues detected.</div>'

    # ── Weather strip ─────────────────────────────────────────────────────────
    weather_cards = ""
    for city in sorted(items_by_city):
        w = weather_by_city.get(city, {})
        if w.get("ok"):
            cond = w["condition"]
            color = CONDITION_COLORS.get(cond, "#6b7280")
            weather_cards += f"""
            <div class="weather-card">
              <div class="city-name">{city}</div>
              <div class="condition-badge" style="background:{color}">{cond.upper()}</div>
              <div class="weather-stat">🌡 {w['feels_like']:.1f}°C feels like</div>
              <div class="weather-stat">🌧 {w['precip']:.1f} mm precip</div>
            </div>"""
        else:
            weather_cards += f"""
            <div class="weather-card warn">
              <div class="city-name">{city}</div>
              <div class="condition-badge" style="background:#6b7280">UNKNOWN</div>
              <div class="weather-stat">Weather API unavailable</div>
            </div>"""

    # ── Line items table ──────────────────────────────────────────────────────
    city_sections = ""
    for city in sorted(items_by_city):
        rows = ""
        for li in sorted(items_by_city[city], key=lambda x: x["creative_id"]):
            state = li["state"]
            state_class = "state-active" if state == "active" else "state-paused"
            state_label = "● ACTIVE" if state == "active" else "○ paused"
            reason = li.get("current_reason") or "—"
            override = li.get("override", "none")
            override_badge = ""
            if override == "force_active":
                override_badge = '<span class="override-badge on">MANUAL ON</span>'
            elif override == "force_paused":
                override_badge = '<span class="override-badge off">MANUAL OFF</span>'

            rows += f"""
            <tr>
              <td>{li['creative_name']} {override_badge}</td>
              <td class="{state_class}">{state_label}</td>
              <td class="reason">{reason}</td>
              <td>
                <select class="override-select" onchange="setOverride({li['id']}, this.value)">
                    <option value="none"         {'selected' if override == 'none'         else ''}>Auto</option>
                    <option value="force_active" {'selected' if override == 'force_active' else ''}>Force ON</option>
                    <option value="force_paused" {'selected' if override == 'force_paused' else ''}>Force OFF</option>
                  </select>
              </td>
            </tr>"""

        city_sections += f"""
        <div class="city-block">
          <h3 class="city-header">{city}</h3>
          <table>
            <thead><tr>
              <th>Creative</th><th>State</th><th>Reason</th><th>Override</th>
            </tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </div>"""

    # ── Transitions feed ──────────────────────────────────────────────────────
    if transitions:
        trans_rows = ""
        for t in transitions:
            city = city_by_id.get(t["line_item_id"], "?")
            name = name_by_id.get(t["line_item_id"], f"item #{t['line_item_id']}")
            arrow_class = "arrow-up" if t["to_state"] == "active" else "arrow-down"
            trans_rows += f"""
            <tr>
              <td class="ts">{fmt_ts(t['timestamp'])}</td>
              <td>{city}</td>
              <td>{name}</td>
              <td class="{arrow_class}">{t['from_state']} → {t['to_state']}</td>
              <td class="reason">{t['reason']}</td>
            </tr>"""
        trans_html = f"""
        <table>
          <thead><tr>
            <th>Time</th><th>City</th><th>Creative</th><th>Change</th><th>Reason</th>
          </tr></thead>
          <tbody>{trans_rows}</tbody>
        </table>"""
    else:
        trans_html = "<p style='color:#6b7280'>No transitions yet — run a cycle to see changes.</p>"

    # ── Full page ─────────────────────────────────────────────────────────────
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>DynaMo Dashboard</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: system-ui, sans-serif; background: #0f172a; color: #e2e8f0; padding: 24px; }}
    h1 {{ font-size: 1.5rem; font-weight: 700; color: #f1f5f9; margin-bottom: 4px; }}
    .subtitle {{ color: #64748b; font-size: 0.85rem; margin-bottom: 24px; }}
    h2 {{ font-size: 1rem; font-weight: 600; color: #94a3b8; text-transform: uppercase;
          letter-spacing: 0.08em; margin: 28px 0 12px; }}
    h3.city-header {{ font-size: 1rem; font-weight: 600; color: #cbd5e1;
                      padding: 8px 0 6px; border-bottom: 1px solid #1e293b; margin-bottom: 8px; }}

    /* alerts */
    .alert {{ padding: 10px 14px; border-radius: 6px; background: #7f1d1d; color: #fca5a5;
              margin-bottom: 8px; font-size: 0.875rem; }}
    .alert.ok {{ background: #14532d; color: #86efac; }}

    /* weather strip */
    .weather-strip {{ display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 8px; }}
    .weather-card {{ background: #1e293b; border-radius: 8px; padding: 14px 18px; min-width: 160px; }}
    .weather-card.warn {{ border: 1px solid #b45309; }}
    .city-name {{ font-weight: 600; color: #f1f5f9; margin-bottom: 6px; }}
    .condition-badge {{ display: inline-block; padding: 2px 10px; border-radius: 999px;
                        color: #fff; font-size: 0.75rem; font-weight: 700;
                        margin-bottom: 8px; letter-spacing: 0.05em; }}
    .weather-stat {{ font-size: 0.8rem; color: #94a3b8; margin-top: 2px; }}

    /* tables */
    table {{ width: 100%; border-collapse: collapse; background: #1e293b;
             border-radius: 8px; overflow: hidden; margin-bottom: 8px; }}
    thead {{ background: #0f172a; }}
    th {{ text-align: left; padding: 10px 14px; font-size: 0.75rem;
          color: #64748b; text-transform: uppercase; letter-spacing: 0.06em; }}
    td {{ padding: 10px 14px; font-size: 0.875rem; border-top: 1px solid #0f172a; vertical-align: middle; }}
    .state-active {{ color: #4ade80; font-weight: 600; }}
    .state-paused {{ color: #64748b; }}
    .reason {{ color: #94a3b8; font-size: 0.8rem; }}
    .ts {{ color: #64748b; font-size: 0.775rem; white-space: nowrap; }}
    .arrow-up {{ color: #4ade80; font-weight: 600; white-space: nowrap; }}
    .arrow-down {{ color: #f87171; white-space: nowrap; }}

    /* override */
    .override-select {{
      background: #0f172a; color: #e2e8f0; border: 1px solid #334155;
      border-radius: 5px; padding: 4px 8px; font-size: 0.8rem; cursor: pointer;
    }}
    .override-badge {{ font-size: 0.65rem; font-weight: 700; padding: 2px 7px;
                       border-radius: 4px; margin-left: 6px; vertical-align: middle; }}
    .override-badge.on  {{ background: #16a34a; color: #dcfce7; }}
    .override-badge.off {{ background: #dc2626; color: #fee2e2; }}

    .city-block {{ margin-bottom: 20px; }}

    /* run button */
    .run-btn {{
      display: inline-block; padding: 10px 22px; background: #2563eb; color: #fff;
      border: none; border-radius: 7px; font-size: 0.9rem; font-weight: 600;
      cursor: pointer; text-decoration: none; margin-bottom: 4px;
    }}
    .run-btn:hover {{ background: #1d4ed8; }}
    .run-note {{ font-size: 0.75rem; color: #64748b; margin-top: 4px; }}
  </style>
</head>
<body>
  <h1>DynaMo Dashboard</h1>
  <p class="subtitle">Context-aware ad decision engine — live view</p>

  <!-- Alerts -->
  <h2>Alerts</h2>
  {alerts_html}

  <!-- Run cycle -->
  <h2>Actions</h2>
  <form method="post" action="/run-cycle">
    <button class="run-btn" type="submit">▶ Run cycle now</button>
  </form>
  <p class="run-note">Re-evaluates all line items against live weather and updates Supabase.</p>

  <!-- Weather -->
  <h2>City weather</h2>
  <div class="weather-strip">{weather_cards}</div>

  <!-- Line items -->
  <h2>Current state</h2>
  {city_sections}

  <!-- Transitions -->
  <h2>Recent changes (last 20)</h2>
  {trans_html}

  <script>
    function setOverride(id, value) {{
      fetch('/override/' + id + '?override=' + value, {{method: 'POST'}})
        .then(() => location.reload());
    }}
  </script>
</body>
</html>"""


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def dashboard():
    return FileResponse(Path(__file__).parent / "dashboard.html", media_type="text/html")


class OverridePayload(BaseModel):
    override: str  # "none" | "force_active" | "force_paused"


@app.post("/override/{item_id}")
def set_override(item_id: int, override: str = None, reason: str = ""):
    """Apply override to a single line item immediately — no full cycle."""
    if override not in ("none", "force_active", "force_paused"):
        raise HTTPException(status_code=400, detail="Invalid override value")

    now = datetime.now(timezone.utc).isoformat()

    # Fetch the item and its city config so we can compute the new state instantly
    li = supabase.table("line_items").select("*").eq("id", item_id).single().execute().data
    cfg = supabase.table("city_config").select("*").eq("city", li["city"]).single().execute().data

    current_state = li["state"]

    # Write the override first so decide_state sees it
    li["override"] = override
    weather = get_weather(li["latitude"], li["longitude"])
    city_weather_row = supabase.table("city_weather").select("condition").eq("city", li["city"]).execute().data
    condition = city_weather_row[0]["condition"] if city_weather_row else "normal"

    desired_state, new_reason = decide_state(li, weather, cfg, condition)

    # Append the CMO's note to the reason if provided
    if reason.strip():
        new_reason = f"{new_reason} — {reason.strip()}"

    update_payload = {
        "override": override,
        "state": desired_state,
        "current_reason": new_reason,
        "last_updated": now,
    }
    supabase.table("line_items").update(update_payload).eq("id", item_id).execute()

    # Log a transition only if the state actually changed
    if desired_state != current_state:
        supabase.table("transitions").insert({
            "line_item_id": item_id,
            "from_state": current_state,
            "to_state": desired_state,
            "reason": new_reason,
            "timestamp": now,
        }).execute()

    # Recipient resolved from DB inside alerts.py — no email arg needed
    if override == "none":
        alert_override_cleared(li)
    else:
        alert_override_set(li, override, reason)

    return {"ok": True, "state": desired_state, "reason": new_reason}


@app.post("/run-cycle")
def trigger_cycle():
    run_cycle()
    return {"ok": True}


@app.get("/api/settings")
def get_settings():
    row = supabase.table("settings").select("value").eq("key", "alert_email").execute().data
    return {"alert_email": row[0]["value"] if row else ""}


@app.post("/api/settings")
def save_settings(alert_email: str = ""):
    supabase.table("settings").upsert({"key": "alert_email", "value": alert_email}).execute()
    return {"ok": True}


@app.get("/api/state")
def api_state():
    """JSON endpoint for programmatic access."""
    try:
        line_items, transitions, cfg_by_city = fetch_data()
        city_weather = supabase.table("city_weather").select("*").execute().data
        last_cycle = supabase.table("cycles").select("*").order("finished_at", desc=True).limit(1).execute().data
        return {
            "line_items": line_items,
            "recent_transitions": transitions,
            "city_weather": city_weather,
            "last_cycle": last_cycle[0] if last_cycle else None,
        }
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Database unreachable: {exc}")
