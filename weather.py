"""
weather.py — Fetch current apparent temperature and precipitation from Open-Meteo.

In-memory cache keyed by rounded (lat, lon) with a 15-minute TTL so rapid calls
for the same city don't re-fetch.
"""

import time
import httpx

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
CACHE_TTL = 900  # 15 minutes in seconds

# { (lat_r, lon_r): {"ts": float, "data": dict} }
_cache: dict = {}


def _round_coord(lat: float, lon: float) -> tuple:
    # Round to 2 decimal places (~1 km precision), enough to share a cache entry
    return (round(lat, 2), round(lon, 2))


def get_weather(lat: float, lon: float) -> dict:
    """
    Return {feels_like: float, precip: float, ok: True} on success.
    Return {ok: False} on any error — never raises.
    """
    key = _round_coord(lat, lon)
    now = time.monotonic()

    cached = _cache.get(key)
    if cached and (now - cached["ts"]) < CACHE_TTL:
        return cached["data"]

    try:
        resp = httpx.get(
            OPEN_METEO_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,apparent_temperature,precipitation",
                "timezone": "auto",
            },
            timeout=5.0,
        )
        resp.raise_for_status()
        body = resp.json()

        current = body["current"]
        result = {
            "temperature": float(current["temperature_2m"]),
            "feels_like": float(current["apparent_temperature"]),
            "precip": float(current["precipitation"]),
            "ok": True,
        }
    except Exception:
        return {"ok": False}

    _cache[key] = {"ts": now, "data": result}
    return result
