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
            readings[city] = {"temp_c": float(temp), "precip_mm": float(precip)}
    return readings, None


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

    results = {}
    for city in city_coords:
        if city in sims:
            results[city] = {
                "temp_c": sims[city]["temp_c"],
                "precip_mm": sims[city]["precip_mm"],
                "source": "simulated",
            }
        elif live and city in live and "error" not in live[city]:
            results[city] = {**live[city], "source": "open-meteo"}
        elif live and city in live:
            results[city] = {"error": live[city]["error"]}
        else:
            results[city] = {"error": error or "No data"}
    return results
