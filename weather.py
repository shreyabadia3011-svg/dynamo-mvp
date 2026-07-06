"""
Sensing layer. Its one job: produce (temp_c, precip_mm, source) per city,
or an honest error. It never decides anything.

Scale/cost choice: Open-Meteo accepts comma-separated coordinates, so all
cities ride in ONE request per cycle. 4 cities or 40, still one call.
"""

import requests

import config


def fetch_live(city_coords):
    """One batched request for every city in the campaign.

    city_coords: {city: {"lat": .., "lon": ..}} — comes from the campaign data.
    Returns (readings, error) where readings is
    {city: {"temp_c": float, "precip_mm": float}} on success,
    and error is a string on total failure (network down, timeout, bad body).
    """
    cities = list(city_coords.keys())
    lats = ",".join(str(city_coords[c]["lat"]) for c in cities)
    lons = ",".join(str(city_coords[c]["lon"]) for c in cities)
    last_err = None
    for attempt in range(1 + config.FETCH_RETRIES):
        try:
            resp = requests.get(
                config.OPEN_METEO_URL,
                params={
                    "latitude": lats,
                    "longitude": lons,
                    "current": "temperature_2m,precipitation",
                },
                timeout=config.REQUEST_TIMEOUT_S,
            )
            resp.raise_for_status()
            payload = resp.json()
            break
        except requests.RequestException as e:
            # Retry transient upstream trouble (429 rate limits, 5xx blips).
            # 4xx other than 429 won't heal on retry, so fail fast on those.
            status = getattr(getattr(e, "response", None), "status_code", None)
            transient = status is None or status == 429 or status >= 500
            last_err = f"Weather API unreachable: {e.__class__.__name__}: {e}"
            if transient and attempt < config.FETCH_RETRIES:
                import time as _t
                _t.sleep(config.FETCH_BACKOFF_S * (attempt + 1))
                continue
            return None, last_err
        except ValueError:
            return None, "Weather API returned a non-JSON body"
    else:
        return None, last_err or "Weather API failed"

    # Single-city responses come back as a dict, multi-city as a list.
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list) or len(payload) != len(cities):
        return None, (
            f"Weather API returned {len(payload) if isinstance(payload, list) else 'malformed'}"
            f" results for {len(cities)} cities"
        )

    readings = {}
    for city, item in zip(cities, payload):
        current = item.get("current") or {}
        temp = current.get("temperature_2m")
        precip = current.get("precipitation")
        if temp is None or precip is None:
            # Null fields for one city must not poison the others:
            # mark this city failed, keep the rest.
            readings[city] = {"error": "API returned null temperature/precipitation"}
        else:
            readings[city] = {"temp_c": float(temp), "precip_mm": float(precip),
                              "source": "open-meteo"}
    return readings, None


def _fetch_met_norway(city_coords):
    """Fallback source: MET Norway, one call per city, keyless.

    Only invoked for cities the primary source failed to serve. Defensive
    parsing: any surprise in the response shape marks that city failed and
    the normal hold/safe-mode policy takes over downstream.
    """
    headers = {"User-Agent": config.MET_NO_USER_AGENT}
    out = {}
    for city, c in city_coords.items():
        try:
            resp = requests.get(
                config.MET_NO_URL,
                params={"lat": round(c["lat"], 4), "lon": round(c["lon"], 4)},
                headers=headers,
                timeout=config.REQUEST_TIMEOUT_S,
            )
            resp.raise_for_status()
            ts = resp.json()["properties"]["timeseries"][0]["data"]
            temp = ts["instant"]["details"]["air_temperature"]
            nxt = ts.get("next_1_hours") or ts.get("next_6_hours") or {}
            precip = (nxt.get("details") or {}).get("precipitation_amount", 0.0)
            out[city] = {"temp_c": float(temp), "precip_mm": float(precip or 0.0),
                         "source": "met-norway"}
        except Exception as e:
            out[city] = {"error": f"Fallback (MET Norway) also failed: "
                                  f"{e.__class__.__name__}"}
    return out


def get_readings(conn, city_coords):
    """Merge live data with any demo-mode simulated weather.

    Simulated values (set from the dashboard's demo panel) take precedence
    per-city and are labelled 'simulated' everywhere they appear, so a demo
    can never be mistaken for reality.
    """
    sims = {
        r["city"]: r
        for r in conn.execute("SELECT * FROM simulated_weather").fetchall()
    }

    live, error = (None, None)
    cities_needing_live = {c: v for c, v in city_coords.items() if c not in sims}
    if cities_needing_live:
        live, error = fetch_live(cities_needing_live)
        live = live or {}
        # Failover: any city the primary source could not serve gets one
        # attempt against the fallback source before the failure policy runs.
        failed = {c: cities_needing_live[c] for c in cities_needing_live
                  if c not in live or "error" in live[c]}
        if failed:
            primary_err = (live.get(next(iter(failed)), {}).get("error")
                           or error or "primary source failed")
            fallback = _fetch_met_norway(failed)
            for c, v in fallback.items():
                if "error" not in v:
                    live[c] = v
                else:
                    live[c] = {"error": f"{primary_err}; {v['error']}"}

    results = {}
    for city in city_coords:
        if city in sims:
            results[city] = {
                "temp_c": sims[city]["temp_c"],
                "precip_mm": sims[city]["precip_mm"],
                "source": "simulated",
            }
        elif live and city in live and "error" not in live[city]:
            results[city] = dict(live[city])
        elif live and city in live:
            results[city] = {"error": live[city]["error"]}
        else:
            results[city] = {"error": error or "No data"}
    return results
