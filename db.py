"""
Database layer. SQLite, four tables.

  line_items         — the campaign. The system's OUTPUT is the `state`
                       column; everything else exists to change it well.
  weather_snapshots  — every observation we acted on. Kept forever so any
                       past decision can be audited against its input.
  transitions        — the audit trail. Every state change, with the reason
                       in plain English. This table IS the trust feature.
  system_events      — fetch failures, safe-mode entries, cycle heartbeats.
"""

import csv
import sqlite3
from datetime import datetime, timezone

import config


def connect():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SCHEMA = """
CREATE TABLE IF NOT EXISTS line_items (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    line_item_id     TEXT NOT NULL UNIQUE,   -- external ID, e.g. LI-001
    creative_id      TEXT NOT NULL,          -- CR-HOT / CR-RAIN / CR-NORM
    creative_name    TEXT NOT NULL,          -- human-readable, shown to CoolSip
    city             TEXT NOT NULL,
    latitude         REAL NOT NULL,          -- coords live in the campaign data,
    longitude        REAL NOT NULL,          -- so adding a city = adding CSV rows
    state            TEXT NOT NULL CHECK (state IN ('active','paused')),
    bid_inr          REAL NOT NULL,
    daily_budget_inr REAL NOT NULL,
    -- Manual override: NULL = automation controls this line item.
    -- 'active'/'paused' = a human pinned it; automation must not touch it.
    override         TEXT CHECK (override IN ('active','paused') OR override IS NULL),
    UNIQUE (creative_id, city)
);

CREATE TABLE IF NOT EXISTS weather_snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    city       TEXT NOT NULL,
    temp_c     REAL,
    precip_mm  REAL,
    condition  TEXT NOT NULL,          -- HOT / RAINY / NORMAL
    source     TEXT NOT NULL,          -- 'open-meteo' or 'simulated'
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transitions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    line_item_id  INTEGER NOT NULL REFERENCES line_items(id),
    city          TEXT NOT NULL,
    creative_id   TEXT NOT NULL,
    creative_name TEXT NOT NULL,
    from_state    TEXT NOT NULL,
    to_state      TEXT NOT NULL,
    trigger       TEXT NOT NULL,       -- weather / safe_mode / override / release
    reason        TEXT NOT NULL        -- plain-English why, with the readings
);

CREATE TABLE IF NOT EXISTS system_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    level   TEXT NOT NULL,             -- info / warning / error
    message TEXT NOT NULL
);

-- Demo-only: lets us inject fake weather to demonstrate behaviour on the
-- walkthrough call ("show it breaking gracefully"). Clearly labelled in UI.
CREATE TABLE IF NOT EXISTS simulated_weather (
    city      TEXT PRIMARY KEY,
    temp_c    REAL NOT NULL,
    precip_mm REAL NOT NULL
);
"""


def init_db(seed_csv="line_items.csv"):
    conn = connect()
    conn.executescript(SCHEMA)
    n = conn.execute("SELECT COUNT(*) c FROM line_items").fetchone()["c"]
    if n == 0:
        with open(seed_csv, newline="") as f:
            rows = list(csv.DictReader(f))
        conn.executemany(
            """INSERT INTO line_items
               (line_item_id, creative_id, creative_name, city, latitude, longitude,
                state, bid_inr, daily_budget_inr)
               VALUES (:line_item_id, :creative_id, :creative_name, :city, :latitude,
                       :longitude, :state, :bid_inr, :daily_budget_inr)""",
            rows,
        )
        log_event(conn, "info", f"Seeded {len(rows)} line items from {seed_csv}")
    conn.commit()
    conn.close()


def log_event(conn, level, message):
    conn.execute(
        "INSERT INTO system_events (ts, level, message) VALUES (?, ?, ?)",
        (now_iso(), level, message),
    )


def log_transition(conn, li, to_state, trigger, reason):
    conn.execute(
        """INSERT INTO transitions
           (ts, line_item_id, city, creative_id, creative_name, from_state, to_state,
            trigger, reason)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (now_iso(), li["id"], li["city"], li["creative_id"], li["creative_name"],
         li["state"], to_state, trigger, reason),
    )
    conn.execute("UPDATE line_items SET state = ? WHERE id = ?", (to_state, li["id"]))


def get_cities(conn):
    """Cities and coordinates come FROM the campaign data, not from config.
    Adding a city to the campaign = adding rows to line_items.csv. Nothing else."""
    rows = conn.execute(
        "SELECT city, latitude, longitude FROM line_items GROUP BY city ORDER BY city"
    ).fetchall()
    return {r["city"]: {"lat": r["latitude"], "lon": r["longitude"]} for r in rows}


def latest_snapshot(conn, city):
    return conn.execute(
        "SELECT * FROM weather_snapshots WHERE city = ? ORDER BY id DESC LIMIT 1",
        (city,),
    ).fetchone()
