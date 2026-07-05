# DynaMo MVP — CoolSip Weather-Triggered Campaign

Automatically activates/pauses CoolSip's ad creatives across Mumbai, Delhi,
Bangalore, and Chennai based on live weather, with a full audit trail and
manual override — built for the CMO's two non-negotiables: trust and visibility.

## Run locally

```bash
pip install -r requirements.txt
python app.py            # http://localhost:5000
```

On startup it seeds the database from `line_items.csv`, runs one
sense→decide→act cycle, and then re-runs every 10 minutes in the background.
Press **Check weather now** on the dashboard to force a cycle.

Uses YOptima's official `line_items.csv` schema (line_item_id, creative_id,
creative_name, city, latitude, longitude, state, bid_inr, daily_budget_inr).
Cities and coordinates are read from the campaign data itself — adding a city
to the campaign automatically brings it into weather polling and the dashboard.

## Deploy (Render)

1. Push this folder to a GitHub repo.
2. On render.com → New → Web Service → connect the repo. `render.yaml` does the rest.
3. Known free-tier caveat: the SQLite file resets on redeploy/sleep. Fine for
   a demo; production would use Postgres (schema is standard SQL).

## Architecture (one line each)

```
line_items.csv ──seed──▶ SQLite ◀──state writes──┐
                                                  │
Open-Meteo (1 batched call, all cities) ──▶ classify (HOT/RAINY/NORMAL)
                                                  │
                     desired states per CITY ──▶ diff vs current ──▶ log + update
                                                  │
                       dashboard: states, weather, audit trail, overrides
```

- `config.py` — every threshold and mapping, as data. The campaign rules live here.
- `weather.py` — sensing only; returns readings or honest errors. Never decides.
- `decision.py` — pure functions; the only place that knows what "hot" means.
- `engine.py` — the loop: fetch → classify → decide → apply diffs → log.
- `app.py` + `templates/dashboard.html` — visibility layer + control endpoints.

## Deliberate decisions (defend these on the call)

| Decision | Why |
|---|---|
| **Rain beats heat** when both fire | "Beat the heat" in a downpour is the CMO's named nightmare. Ties break away from the embarrassing failure. |
| **Rain threshold 0.1 mm**, not >0 | Sensors report trace values no human calls rain. The brief invites challenging its definitions. |
| **Hysteresis on heat** (enter 35°C, exit <34°C) | A city hovering at 35.0 would otherwise flip creatives every 10 min. Flapping erodes trust. |
| **Safe mode on stale data** (>15 min + failed refresh) | Run the generic creative — the ad that can't be wrong — instead of trusting a stale reading. Brief blips (<15 min) just hold state. |
| **Overrides always win** | The CMO can pin any line item; automation logs that it yielded and never touches it until released. |
| **Decisions per city, not per line item** | 12 line items = 4 decisions. 10,000 line items across 200 cities = 200 decisions. This is the scale story. |
| **One batched API call** for all cities | Open-Meteo takes comma-separated coordinates. 144 calls/day for 4 cities; ~200 cities at 6-min polling still fits the $50/day cap. |
| **Every transition logged with readings** | The audit trail is the trust feature, not a debugging convenience. |
| **SQLite / Flask / no framework magic** | Boring choices, fast to working loop, trivially inspectable. Postgres when there's more than one writer. |

## Demo script (for the 10-min walkthrough demo)

1. Open dashboard — show live weather per city, one active creative each.
2. Demo panel → set Mumbai to `28°C / 5 mm` → rainy creative activates; read
   the log line out loud (it contains the readings and the rule).
3. Set Delhi to `35.2°C` → HOT. Then `34.5°C` → still HOT (hysteresis). Then
   `33°C` → NORMAL. Explain flapping.
4. Set Chennai to `38°C / 3 mm` → RAINY wins. Explain precedence.
5. Pin a line item, run a cycle, show automation yielding in the events feed.
6. "Breaking gracefully": point at the safe-mode logic and the stale banner;
   explain the <15 min hold vs ≥15 min safe-mode split.

## JSON API

- `GET /api/line-items` — current campaign state
- `GET /api/transitions` — audit trail
- `GET /api/weather` — latest snapshot per city
