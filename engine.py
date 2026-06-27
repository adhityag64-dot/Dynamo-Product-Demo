"""
engine.py — Deterministic decision engine for DynaMo.

No LLM calls. Given a line item, live weather, city config, and the city's
previously-computed condition, returns the desired state and a human-readable reason.
"""

# Creative IDs that match each weather condition.
CONDITION_CREATIVE = {
    "hot":    "CR-HOT",
    "rainy":  "CR-RAIN",
    "normal": "CR-NORM",
}

GENERIC_CREATIVE = "CR-NORM"


def compute_city_condition(
    weather: dict,
    cfg: dict,
    previous_condition: str,
) -> str:
    """
    Return 'hot' | 'rainy' | 'normal'.

    Rules (applied in priority order):
      1. Rain wins: precip >= rainy_threshold → 'rainy' (brand safety, even if also hot).
      2. Hot with hysteresis:
           - Turn ON  when feels_like >= hot_threshold
           - Turn OFF only when feels_like <  hot_clear_below  (avoids flapping)
      3. Otherwise 'normal'.
    """
    feels_like = weather["feels_like"]
    precip = weather["precip"]

    # Rain beats everything
    if precip >= cfg["rainy_threshold"]:
        return "rainy"

    # Hot with hysteresis
    if previous_condition == "hot":
        # Already hot — stay hot unless temperature drops below the lower bound
        if feels_like >= cfg["hot_clear_below"]:
            return "hot"
    else:
        # Not currently hot — only go hot when we clearly cross the upper threshold
        if feels_like >= cfg["hot_threshold"]:
            return "hot"

    return "normal"


def decide_state(
    line_item: dict,
    city_weather: dict,
    city_cfg: dict,
    current_condition: str,
) -> tuple:
    """
    Return (desired_state, reason).

    Decision order:
      1. Manual override  — human always wins
      2. Budget exhausted — protect spend before anything else
      3. Fail-safe        — if weather is unavailable, only the generic creative runs
      4. Weather rule     — activate the creative that matches the city's condition
    """
    creative_id = line_item["creative_id"]
    override = line_item.get("override", "none")

    # 1. OVERRIDE
    if override == "force_active":
        return ("active", "manual override: forced on")
    if override == "force_paused":
        return ("paused", "manual override: forced off")

    # 2. BUDGET
    if line_item["spend_today"] >= line_item["daily_budget"]:
        return ("paused", "budget exhausted")

    # 3. FAIL-SAFE — weather API unavailable
    if not city_weather.get("ok"):
        if creative_id == GENERIC_CREATIVE:
            return ("active", "failsafe: weather unavailable, defaulting to generic")
        return ("paused", "failsafe: weather unavailable, defaulting to generic")

    # 4. WEATHER RULE
    target_creative = CONDITION_CREATIVE.get(current_condition, GENERIC_CREATIVE)

    if creative_id == target_creative:
        feels_like = city_weather["feels_like"]
        precip = city_weather["precip"]
        if current_condition == "hot":
            return ("active", f"active: hot, feels like {feels_like:.0f}C")
        if current_condition == "rainy":
            return ("active", f"active: rainy, precip {precip:.1f}mm")
        return ("active", f"active: normal conditions")
    else:
        return ("paused", f"paused: city is {current_condition}")
