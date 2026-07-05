"""
DynaMo web app — the visibility layer plus control endpoints.

Routes:
  GET  /                      dashboard (state, weather, log — the CMO view)
  POST /run-cycle             run one sense→decide→act cycle now
  POST /override/<id>         pin a line item active/paused (?state=...)
  POST /release/<id>          hand a pinned line item back to automation
  POST /simulate              demo: inject fake weather for a city
  POST /simulate/clear        demo: remove all fake weather
  GET  /api/line-items        JSON — current campaign state
  GET  /api/transitions       JSON — audit trail
  GET  /api/weather           JSON — latest snapshot per city

A background thread runs a cycle every POLL_MINUTES so the system is
autonomous; the Run-cycle button exists for demos and impatience.
"""

import os
import threading
import time
from datetime import datetime, timezone

from flask import Flask, jsonify, redirect, render_template, request

import config
import db
import engine

app = Flask(__name__)
db.init_db()


# ------------------------------------------------------------- helpers

def _minutes_ago(iso_ts):
    then = datetime.fromisoformat(iso_ts)
    return (datetime.now(timezone.utc) - then).total_seconds() / 60.0


def _dashboard_data():
    conn = db.connect()
    items = conn.execute(
        "SELECT * FROM line_items ORDER BY city, creative_id"
    ).fetchall()

    # Latest transition per line item -> "last changed" audit column
    last_change = {}
    for t in conn.execute(
        """SELECT * FROM transitions WHERE id IN
           (SELECT MAX(id) FROM transitions GROUP BY line_item_id)"""):
        last_change[t["line_item_id"]] = t

    cities = {}
    for city in db.get_cities(conn):
        snap = db.latest_snapshot(conn, city)
        stale = None
        if snap:
            age = _minutes_ago(snap["fetched_at"])
            stale = age >= config.STALE_MINUTES
        # Plain-English "why" for whatever is currently active in this city
        why_active = None
        if snap:
            cond = snap["condition"]
            if cond == "RAINY":
                why_active = f"raining in {city} ({snap['precip_mm']:.1f} mm/h)"
            elif cond == "HOT":
                why_active = f"it is {snap['temp_c']:.1f}°C in {city}"
            else:
                why_active = f"neither hot nor rainy in {city} ({snap['temp_c']:.1f}°C, {snap['precip_mm']:.1f} mm/h)"
        cities[city] = {
            "snapshot": snap,
            "age_min": round(_minutes_ago(snap["fetched_at"])) if snap else None,
            "stale": stale,
            "why_active": why_active,
            "items": [i for i in items if i["city"] == city],
        }

    transitions = conn.execute(
        "SELECT * FROM transitions ORDER BY id DESC LIMIT 50"
    ).fetchall()
    snapshots = conn.execute(
        "SELECT * FROM weather_snapshots ORDER BY id DESC LIMIT 16"
    ).fetchall()
    events = conn.execute(
        "SELECT * FROM system_events ORDER BY id DESC LIMIT 40"
    ).fetchall()
    alerts = [e for e in events if e["level"] != "info"][:8]
    last_cycle = next((e for e in events
                       if e["level"] == "info" and "Cycle complete" in e["message"]), None)
    sims = conn.execute("SELECT city FROM simulated_weather").fetchall()
    conn.close()
    return (cities, transitions, events, alerts, last_cycle,
            {s["city"] for s in sims}, items, last_change, snapshots)


# --------------------------------------------------------------- routes

@app.route("/")
def dashboard():
    (cities, transitions, events, alerts, last_cycle, simulated,
     items, last_change, snapshots) = _dashboard_data()
    return render_template(
        "dashboard.html",
        cities=cities,
        transitions=transitions,
        events=events,
        alerts=alerts,
        last_cycle=last_cycle,
        simulated=simulated,
        items=items,
        last_change=last_change,
        snapshots=snapshots,
        config=config,
    )


@app.route("/run-cycle", methods=["POST"])
def run_cycle():
    conn = db.connect()
    engine.run_cycle(conn)
    conn.close()
    return redirect("/")


@app.route("/override/<int:item_id>", methods=["POST"])
def override(item_id):
    state = request.args.get("state")
    if state not in ("active", "paused"):
        return "state must be active|paused", 400
    conn = db.connect()
    engine.set_override(conn, item_id, state)
    conn.close()
    return redirect("/")


@app.route("/release/<int:item_id>", methods=["POST"])
def release(item_id):
    conn = db.connect()
    engine.release_override(conn, item_id)
    conn.close()
    return redirect("/")


@app.route("/simulate", methods=["POST"])
def simulate():
    city = request.form.get("city")
    conn0 = db.connect()
    known = set(db.get_cities(conn0))
    conn0.close()
    if city not in known:
        return "unknown city", 400
    try:
        temp = float(request.form.get("temp_c"))
        precip = float(request.form.get("precip_mm"))
    except (TypeError, ValueError):
        return "temp_c and precip_mm must be numbers", 400
    conn = db.connect()
    conn.execute(
        """INSERT INTO simulated_weather (city, temp_c, precip_mm) VALUES (?, ?, ?)
           ON CONFLICT(city) DO UPDATE SET temp_c=excluded.temp_c,
                                           precip_mm=excluded.precip_mm""",
        (city, temp, precip),
    )
    db.log_event(conn, "warning",
                 f"DEMO: simulated weather set for {city} "
                 f"({temp}°C, {precip} mm/h) — dashboard will show 'simulated'")
    conn.commit()
    engine.run_cycle(conn)  # react immediately so demos are snappy
    conn.close()
    return redirect("/")


@app.route("/simulate/clear", methods=["POST"])
def simulate_clear():
    conn = db.connect()
    conn.execute("DELETE FROM simulated_weather")
    db.log_event(conn, "info", "DEMO: simulated weather cleared — back to live data")
    conn.commit()
    engine.run_cycle(conn)
    conn.close()
    return redirect("/")


# ------------------------------------------------------------- JSON API

@app.route("/api/line-items")
def api_line_items():
    conn = db.connect()
    rows = conn.execute("SELECT * FROM line_items ORDER BY city, creative_id").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/transitions")
def api_transitions():
    conn = db.connect()
    rows = conn.execute("SELECT * FROM transitions ORDER BY id DESC LIMIT 200").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/weather")
def api_weather():
    conn = db.connect()
    out = {}
    for city in db.get_cities(conn):
        snap = db.latest_snapshot(conn, city)
        out[city] = dict(snap) if snap else None
    conn.close()
    return jsonify(out)


# ------------------------------------------------------------ scheduler

def _scheduler():
    while True:
        time.sleep(config.POLL_MINUTES * 60)
        try:
            conn = db.connect()
            engine.run_cycle(conn)
            conn.close()
        except Exception as e:  # keep the loop alive no matter what
            try:
                conn = db.connect()
                db.log_event(conn, "error", f"Scheduler cycle crashed: {e}")
                conn.commit()
                conn.close()
            except Exception:
                pass


if os.environ.get("DISABLE_SCHEDULER") != "1":
    threading.Thread(target=_scheduler, daemon=True).start()


if __name__ == "__main__":
    # Run one cycle at startup so the dashboard is never empty.
    conn = db.connect()
    engine.run_cycle(conn)
    conn.close()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
