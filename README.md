# DynaMo — Weather-Driven Ad Decision Engine

DynaMo automatically activates and pauses ad creatives based on live weather conditions across Indian cities. No manual campaign management — the engine reads the weather, picks the right creative, and keeps everything in sync every 5 minutes.

---

## How It Works

Every 5 minutes the scheduler fetches live weather for each city via [Open-Meteo](https://open-meteo.com/) and runs every line item through a decision engine: ///

```
Manual override → Budget exhausted → Weather fail-safe → Weather rule
```

**Weather rules:**
- `feels_like ≥ hot_threshold` → activate CR-HOT, pause the rest
- `precip ≥ rainy_threshold` → activate CR-RAIN, pause the rest (rain beats heat)
- Otherwise → activate CR-NORM, pause the rest

**Fail-safe:** if the weather API is unreachable, only CR-NORM (generic) runs. No wrong ad ever shows.

**Hysteresis:** hot condition only clears below a separate lower threshold (`hot_clear_below`) to prevent flapping when temperature hovers around the threshold.

---

## Tech Stack

| Layer | Tech |
|---|---|
| Backend | Python · FastAPI · Uvicorn |
| Scheduler | APScheduler (5-min interval) |
| Weather | Open-Meteo API (free, no key needed) |
| Database | Supabase (PostgreSQL) |
| Email alerts | Gmail SMTP via smtplib |
| Frontend | Vanilla JS dashboard (single HTML file) |

---

## Project Structure

```
dynamo/
├── app.py          # FastAPI server — dashboard + API endpoints
├── loop.py         # APScheduler — runs run_cycle() every 5 minutes
├── engine.py       # Decision engine — compute_city_condition + decide_state
├── weather.py      # Open-Meteo fetch with 15-min in-memory cache
├── alerts.py       # Gmail SMTP email alerts
├── dashboard.html  # Single-page dashboard UI
├── Procfile        # Railway deployment (web + worker)
└── requirements.txt
```

---

## Database Schema (Supabase)

```sql
-- One row per city, holds weather thresholds
city_config (city, hot_threshold, hot_clear_below, rainy_threshold)

-- 18 rows: 3 creatives × 6 cities
line_items (id, city, creative_id, creative_name, state, override,
            current_reason, spend_today, daily_budget, latitude, longitude, last_updated)

-- Append-only log of every state change
transitions (id, line_item_id, from_state, to_state, reason, timestamp)

-- Live weather snapshot updated every cycle
city_weather (city, temperature, feels_like, precip, condition, fetched_at)

-- Cycle audit log
cycles (id, started_at, finished_at, cities, decisions, transitions)

-- App settings (alert recipient email)
settings (key, value)
```

---

## Environment Variables

| Variable | Description |
|---|---|
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_KEY` | Supabase service role key |
| `GMAIL_USER` | Gmail address to send alerts from |
| `GMAIL_APP_PASSWORD` | Google App Password (16 chars) — not your login password |
| `CMO_EMAIL` | Default alert recipient (overridden by DB settings) |

> **Note:** `GMAIL_APP_PASSWORD` requires 2-Step Verification enabled on your Google account. Generate one at Google Account → Security → App Passwords.

---

## Running Locally

```bash
# 1. Clone and set up
git clone <repo-url>
cd dynamo
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 2. Create .env
cp .env.example .env   # then fill in your values

# 3. Start the dashboard
uvicorn app:app --reload --port 8000

# 4. Start the scheduler (separate terminal)
python loop.py

# 5. Open http://localhost:8000
```

### One-off cycle
```bash
python loop.py --once
```

### Simulate weather failure (for testing emails)
```bash
python loop.py --simulate weather-fail:Mumbai,Delhi
```

---

## Deploying to Railway

1. Push this repo to GitHub
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub repo
3. Add environment variables in Railway dashboard (Settings → Variables):
   - `SUPABASE_URL`
   - `SUPABASE_KEY`
   - `GMAIL_USER`
   - `GMAIL_APP_PASSWORD`
   - `CMO_EMAIL`
4. Railway automatically reads the `Procfile` and starts both processes:
   - `web` — the FastAPI dashboard
   - `worker` — the 5-minute scheduler

---

## Dashboard Features

- **Live weather strip** — temp, feels like, precip, condition per city
- **Line items table** — state, condition, reason, and override control per creative
- **Manual override** — Force ON / Force OFF any line item instantly with a reason note
- **Bell notifications** — active alerts (failsafe, all-paused, overrides) + full changes log
- **Auto-refresh** — polls every 30 seconds, updates the moment a new cycle completes
- **Countdown timer** — shows exactly when the next cycle is due

---

## Email Alerts

Emails fire once per event (deduplicated) and auto-clear when the condition resolves:

| Trigger | When |
|---|---|
| Weather signal lost | Weather API goes down for a city |
| No ads running | All 3 creatives paused in a city (non-weather cause) |
| Manual control activated | Any Force ON / Force OFF set from dashboard |
| Auto control restored | Override cleared back to Auto |
| Override stuck | Manual override active for more than 4 hours |

Alert recipient is configured from the dashboard (Settings gear → Alert Email) and stored in the `settings` table — no code change needed.
