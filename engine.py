"""
The core loop. One cycle =
  fetch readings → classify per city → compute desired states →
  apply only the diffs → log every change with its reason.

Failure policy (the part that earns trust):
  * Fetch fails but last good reading is FRESH (< 15 min): hold current
    states, log a warning. A brief blip should not thrash the campaign.
  * Fetch fails and data is STALE (>= 15 min): enter SAFE MODE for that
    city — activate the generic creative, pause the conditional ones.
    Rationale: the generic ad is correct in all weathers; a stale "beat
    the heat" might be running into a downpour. When we don't know the
    world, run the ad that can't be wrong.
  * Manual overrides always win: automation never touches a pinned item.
"""

from datetime import datetime, timezone

import config
import db
import decision
import weather


def _minutes_since(iso_ts):
    then = datetime.fromisoformat(iso_ts)
    return (datetime.now(timezone.utc) - then).total_seconds() / 60.0


def _apply_desired(conn, city, desired, trigger, reason_prefix):
    """Diff desired vs current for a city's line items; change only diffs."""
    changes = 0
    items = conn.execute(
        "SELECT * FROM line_items WHERE city = ?", (city,)
    ).fetchall()
    for li in items:
        want = desired.get(li["creative_id"])
        if want is None or want == li["state"]:
            continue
        if li["override"] is not None:
            db.log_event(
                conn, "info",
                f"{city} · {li['creative_name']} ({li['line_item_id']}): automation "
                f"wanted '{want}' but item is pinned '{li['override']}' by manual "
                f"override — not touched",
            )
            continue
        db.log_transition(conn, li, want, trigger, f"{reason_prefix}")
        changes += 1
    return changes


def run_cycle(conn):
    """Run one full sense→decide→act cycle. Returns a summary dict."""
    city_coords = db.get_cities(conn)
    readings = weather.get_readings(conn, city_coords)
    summary = {"changes": 0, "cities": {}}

    for city, r in readings.items():
        prev = db.latest_snapshot(conn, city)
        prev_condition = prev["condition"] if prev else None

        if "error" in r:
            # --- failure path -------------------------------------------
            if prev and _minutes_since(prev["fetched_at"]) < config.STALE_MINUTES:
                db.log_event(
                    conn, "warning",
                    f"{city}: weather fetch failed ({r['error']}); last reading is "
                    f"{_minutes_since(prev['fetched_at']):.0f} min old (< "
                    f"{config.STALE_MINUTES} min tolerance) — holding current states",
                )
                summary["cities"][city] = {"status": "held", "error": r["error"]}
            else:
                age = f"{_minutes_since(prev['fetched_at']):.0f} min old" if prev else "absent"
                reason = (
                    f"SAFE MODE: weather data for {city} is {age} (tolerance "
                    f"{config.STALE_MINUTES} min) and refresh failed ({r['error']}). "
                    f"Activating all-weather creative; pausing weather-dependent ones."
                )
                desired = decision.desired_states("NORMAL")  # generic active
                summary["changes"] += _apply_desired(conn, city, desired, "safe_mode", reason)
                db.log_event(conn, "error", f"{city}: {reason}")
                summary["cities"][city] = {"status": "safe_mode", "error": r["error"]}
            continue

        # --- happy path -------------------------------------------------
        condition = decision.classify(r["temp_c"], r["precip_mm"], prev_condition)
        conn.execute(
            """INSERT INTO weather_snapshots (city, temp_c, precip_mm, condition, source, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (city, r["temp_c"], r["precip_mm"], condition, r["source"], db.now_iso()),
        )
        reason = (
            f"{city}: {decision.explain(condition, r['temp_c'], r['precip_mm'])} "
            f"[source: {r['source']}]"
        )
        desired = decision.desired_states(condition)
        summary["changes"] += _apply_desired(conn, city, desired, "weather", reason)
        summary["cities"][city] = {
            "status": "ok", "condition": condition,
            "temp_c": r["temp_c"], "precip_mm": r["precip_mm"], "source": r["source"],
        }

    db.log_event(
        conn, "info",
        f"Cycle complete: {summary['changes']} state change(s) across "
        f"{len(readings)} cities",
    )
    conn.commit()
    return summary


# ---------------------------------------------------------------- overrides

def set_override(conn, line_item_id, pinned_state):
    li = conn.execute(
        "SELECT * FROM line_items WHERE id = ?", (line_item_id,)
    ).fetchone()
    if li is None:
        return False
    conn.execute(
        "UPDATE line_items SET override = ? WHERE id = ?",
        (pinned_state, line_item_id),
    )
    if li["state"] != pinned_state:
        db.log_transition(
            conn, li, pinned_state, "override",
            f"Manual override by CoolSip team: pinned '{pinned_state}'. "
            f"Automation will not touch this line item until released.",
        )
    else:
        db.log_event(
            conn, "info",
            f"{li['city']} · {li['creative_name']} pinned '{pinned_state}' by "
            f"manual override (state unchanged)",
        )
    conn.commit()
    return True


def release_override(conn, line_item_id):
    li = conn.execute(
        "SELECT * FROM line_items WHERE id = ?", (line_item_id,)
    ).fetchone()
    if li is None:
        return False
    conn.execute(
        "UPDATE line_items SET override = NULL WHERE id = ?", (line_item_id,)
    )
    db.log_event(
        conn, "info",
        f"{li['city']} · {li['creative_name']}: manual override released — "
        f"automation resumes control next cycle",
    )
    conn.commit()
    return True
