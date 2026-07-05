"""
DynaMo configuration.

Every number a PM might want to change lives here, not buried in logic.
The brief's working definitions are the defaults — but deliberately
adjustable, because the brief invites us to challenge them.
"""

# ------------------------------------------------------------ thresholds
# Brief's working definition: HOT = temp >= 35C.
# We add hysteresis: once HOT, a city stays HOT until temp drops below 34C.
# Why: without it, a city hovering at 34.9/35.1 flips creatives every cycle
# ("flapping"), which erodes exactly the trust the CMO cares about.
HOT_ENTER_C = 35.0
HOT_EXIT_C = 34.0

# Brief's working definition: RAINY = precipitation last hour > 0.
# We use > 0.1 mm instead. Why: sensors report trace amounts (0.01 mm) that
# no human would call "rain". A heat ad swapped out for a rainy-day ad
# under a blazing sun because of a 0.02 mm reading is the embarrassment
# scenario in reverse.
RAIN_ENTER_MM = 0.1
RAIN_EXIT_MM = 0.0  # rain ends only when precipitation is fully zero

# Precedence when both fire: RAINY beats HOT.
# Why: "Beat the heat" during a downpour is the CMO's named nightmare.
# A rainy-day ad on a warm rainy day is merely suboptimal; the reverse is
# embarrassing. When in doubt, avoid the embarrassing failure.

# --------------------------------------------------------------- mapping
# Which creative should be ACTIVE for each condition. Everything else in
# that city is paused. This mapping is the entire "campaign brief" in data.
CONDITION_TO_CREATIVE = {
    "HOT": "CR-HOT",
    "RAINY": "CR-RAIN",
    "NORMAL": "CR-NORM",
}
SAFE_CREATIVE = "CR-NORM"  # what runs when we can't trust our data

# NOTE: cities and their coordinates are NOT configured here — they are read
# from the campaign data itself (line_items.csv → DB). Adding a city to the
# campaign automatically brings it into weather polling and the dashboard.

# ---------------------------------------------------------------- timing
POLL_MINUTES = 10        # fetch cadence; brief's staleness tolerance is 15
STALE_MINUTES = 15       # beyond this, data is untrusted -> safe mode

# Scale note (for the write-up, not enforced here):
# 4 cities x 6 calls/hr x 24 hr = 576 readings/day. Open-Meteo batches all
# 4 cities into ONE request, so actual calls/day = 144. At $0.001/call and
# a $50/day cap, we could poll ~200 cities every ~6 minutes and still spend
# under $50. The binding constraint at 10k line items is not API cost but
# decision fan-out — which is why decisions are computed per-CITY, not
# per-line-item (12 line items = 4 decisions, 10,000 line items across 200
# cities = 200 decisions).

# ----------------------------------------------------------------- misc
DB_PATH = "dynamo.db"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
REQUEST_TIMEOUT_S = 8
