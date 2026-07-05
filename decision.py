"""
Decision engine. Pure functions — no network, no database, no clock.
Everything here is unit-testable with plain values, and this is the ONLY
place that knows what "hot" or "rainy" means.

Deliberate departures from the brief's working definitions (it asks us to
challenge them):
  1. Rain threshold is 0.1 mm, not >0 — trace readings aren't rain.
  2. Heat has hysteresis (enter at 35, exit below 34) — prevents creative
     flapping when temperature hovers at the threshold.
  3. Rain beats heat when both are true — the wrong-way failure ("Beat the
     heat" in a downpour) is the embarrassing one, so ties break away from it.
"""

import config


def classify(temp_c, precip_mm, previous_condition=None):
    """Map raw readings to HOT / RAINY / NORMAL.

    previous_condition feeds the hysteresis: a city already HOT stays HOT
    until temperature falls below HOT_EXIT_C, not merely below HOT_ENTER_C.
    """
    raining = precip_mm > config.RAIN_ENTER_MM or (
        previous_condition == "RAINY" and precip_mm > config.RAIN_EXIT_MM
    )
    if raining:
        return "RAINY"  # precedence: rain wins over heat

    hot_threshold = (
        config.HOT_EXIT_C if previous_condition == "HOT" else config.HOT_ENTER_C
    )
    if temp_c >= hot_threshold:
        return "HOT"
    return "NORMAL"


def explain(condition, temp_c, precip_mm):
    """The plain-English clause that goes into every log line."""
    if condition == "RAINY":
        return f"precipitation {precip_mm:.1f} mm/h (> {config.RAIN_ENTER_MM} mm) → RAINY"
    if condition == "HOT":
        return f"temperature {temp_c:.1f}°C (≥ {config.HOT_ENTER_C}°C threshold) → HOT"
    return (
        f"temperature {temp_c:.1f}°C and precipitation {precip_mm:.1f} mm/h "
        f"— neither hot nor rainy → NORMAL"
    )


def desired_states(condition):
    """Given a city's condition, return {creative_id: desired_state}.

    Exactly one creative active per city, the rest paused. This is computed
    per CITY, not per line item — the key to scaling decisions (200 cities
    = 200 decisions, regardless of how many line items hang off them).
    """
    active_creative = config.CONDITION_TO_CREATIVE[condition]
    return {
        creative: ("active" if creative == active_creative else "paused")
        for creative in config.CONDITION_TO_CREATIVE.values()
    }
