"""
Watcher
-------
Polls a GraphQL endpoint for records matching a configured filter and posts
an alert (with an illustrative image, a composited location-preview image,
and a reverse-geocoded address) to a Discord webhook for each new match.

SETUP REQUIRED:
Create a `.env` file in this script's own folder (see `.env.example`) with:
    DISCORD_WEBHOOK=<your Discord webhook URL>
    SESSION_COOKIE=<a session cookie value captured from your own logged-in
        browser session against the target site -- see COOKIE_NAME below>
    COOKIE_NAME=<the name of that cookie, as seen in your browser's devtools>
    API_BASE=<the base URL of the target deployment>
    MIN_LAT=... / MAX_LAT=... / MIN_LON=... / MAX_LON=... (your area of interest)

The session cookie WILL expire eventually -- when that happens this script
logs a loud error and alerts once via Discord rather than crashing, so
watch the log/console if alerts stop, and refresh the cookie in .env.

Optional: MAX_RUNTIME_SECONDS has main() exit cleanly after N seconds
instead of running forever (see its definition below for why -- useful for
hosting on a platform with a max job runtime, e.g. GitHub Actions).

Optional: to also mirror match alerts for a specific sub-area to a second
Discord channel, set ZONE_WEBHOOK plus ZONE_MIN_LAT/ZONE_MAX_LAT/
ZONE_MIN_LON/ZONE_MAX_LON (a bbox nested inside MIN_LAT/MAX_LAT/MIN_LON/
MAX_LON above).

LOGS:
Console shows INFO+, full DEBUG log written to watcher.log in this folder.
"""

import os
import io
import math
import time
import json
import logging
import urllib.parse
import requests
from PIL import Image, ImageDraw

# ---------------------------------------------------------------------------
# .ENV FILE LOADER
# ---------------------------------------------------------------------------

def load_dotenv(path=".env"):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, value)

load_dotenv()

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------

logger = logging.getLogger("watcher")
logger.setLevel(logging.DEBUG)

_console_handler = logging.StreamHandler()
_console_handler.setLevel(logging.INFO)
_console_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"
))

_file_handler = logging.FileHandler("watcher.log", encoding="utf-8")
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
))

logger.addHandler(_console_handler)
logger.addHandler(_file_handler)

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK")

# Separate webhook/channel for top-PVP-rank alerts -- optional; that
# feature is simply skipped (logged once, not an error) if unset, same as
# every other optional piece of this script.
PVP_DISCORD_WEBHOOK = os.environ.get("PVP_DISCORD_WEBHOOK")

# The exact cookie name your target deployment uses -- check your browser's
# devtools (Network tab -> any API request -> Cookie header) for the name
# it sends. No hardcoded default here since it varies by deployment.
COOKIE_NAME = os.environ.get("COOKIE_NAME")

def normalize_session_cookie(raw):
    """Accepts either a full 'name=value' Cookie header (copied from the
    Network tab's request headers -- already percent-encoded) or a bare
    cookie value (copied from the Storage/Application panel's cookie value
    column, which shows it decoded/raw, no name prefix) and returns a
    properly formatted, percent-encoded 'name=value' string ready to send
    as-is. Confirmed live this distinction matters: a raw Storage-panel
    value sent unencoded/unprefixed gets silently rejected server-side
    (HTTP 511 'session_expired' -- see query_target_entries) even though
    the underlying session is actually valid."""
    if raw is None or not COOKIE_NAME:
        return None
    raw = raw.strip()
    if not raw:
        return None
    if raw.startswith(f"{COOKIE_NAME}="):
        name, _, value = raw.partition("=")
    else:
        name, value = COOKIE_NAME, raw
    # Header-copied values are already percent-encoded (contain "%3A" etc);
    # Storage-panel copies are raw (contain a literal ":"). Only encode if
    # it looks raw, so an already-encoded value doesn't get double-encoded.
    if "%" not in value and ":" in value:
        value = urllib.parse.quote(value, safe="")
    return f"{name}={value}"

SESSION_COOKIE = normalize_session_cookie(os.environ.get("SESSION_COOKIE"))

# Required -- no hardcoded default, so nothing about the target deployment
# lives in this file.
API_BASE = os.environ.get("API_BASE")
GRAPHQL_URL = f"{API_BASE}/graphql" if API_BASE else None

def _float_env(name):
    """Returns None (not a hardcoded default) if unset/invalid -- the
    bounding box is inherently specific to your area of interest, so
    there's no sensible generic default to fall back to. main() checks
    for None and fails with a clear message rather than silently watching
    the wrong place."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    try:
        return float(raw)
    except ValueError:
        return None

MIN_LAT = _float_env("MIN_LAT")
MAX_LAT = _float_env("MAX_LAT")
MIN_LON = _float_env("MIN_LON")
MAX_LON = _float_env("MAX_LON")

# Optional: mirror a match alert to a second webhook whenever the record's
# coordinates fall inside this smaller "zone" bbox nested within the main
# search area above -- e.g. a specific neighborhood you want pinged to its
# own, more focused channel, without splitting it into a separate watcher.
# Entirely optional: skipped (logged once, not an error) if the webhook
# isn't set; if it IS set, all four bounds must also be set (checked in
# main()) since a webhook with no bbox has nothing to test against.
ZONE_WEBHOOK = os.environ.get("ZONE_WEBHOOK")
ZONE_MIN_LAT = _float_env("ZONE_MIN_LAT")
ZONE_MAX_LAT = _float_env("ZONE_MAX_LAT")
ZONE_MIN_LON = _float_env("ZONE_MIN_LON")
ZONE_MAX_LON = _float_env("ZONE_MAX_LON")

# The target site's own frontend polls every 60s (confirmed via devtools)
# -- matching that cadence rather than polling faster than the underlying
# data updates.
POLL_INTERVAL = 60

# Optional: if set, main()'s loop exits cleanly after roughly this many
# seconds instead of running forever. Added for hosting on GitHub Actions,
# where a single job is capped at 6h -- a workflow schedules a fresh run
# every ~5h and this lets the old one bow out gracefully first, rather than
# getting killed mid-request when GitHub's hard timeout hits. Unset (the
# default) preserves the original run-forever behavior for a normal
# always-on machine.
MAX_RUNTIME_SECONDS = _float_env("MAX_RUNTIME_SECONDS")

# A match is forgotten this long after its expire_timestamp passes, so the
# tracked-id set doesn't grow forever. Small buffer past 0 in case of clock
# skew/late polls.
FORGET_AFTER_EXPIRY = 30

CATALOG_URL = "https://raw.githubusercontent.com/WatWowMap/Masterfile-Generator/master/master-latest-react-map.json"
CATALOG_CACHE_PATH = "catalog_cache.json"
CATALOG_MAX_AGE = 24 * 60 * 60  # re-fetch at most once a day

# Persists tracked_ids across process restarts -- without this, a fresh
# process (e.g. a new GitHub Actions job picking up after the previous
# one's MAX_RUNTIME_SECONDS exit) has no memory of what it already alerted
# on and re-announces every still-active match from scratch. Loaded once
# at startup, saved once per poll cycle. On GitHub Actions this file is
# restored/saved via actions/cache in the workflow, not just local disk.
TRACKED_STATE_PATH = "tracked_state.json"

# Public icon set the target site's own frontend renders from -- no auth
# needed. Filenames follow "{id}[_f{form}][_c{costume}].png"; not every
# form/costume combo has its own asset, so build_icon_url() below checks
# progressively less specific candidates and falls back to the bare id.
ICON_BASE = "https://raw.githubusercontent.com/WatWowMap/wwm-uicons/main/pokemon"

ALERT_COLOR = 0xE74C3C
PVP_ALERT_COLOR = 0xF1C40F

# The API's `weather` field is aliased server-side from a
# weather-boost-condition column -- 0/null means not boosted, any other
# value is the specific weather condition that boosted it. This numeric
# enum is a stable convention across this class of map tooling, not
# something specific to any one deployment.
WEATHER_NAMES = {
    1: "Clear",
    2: "Rainy",
    3: "Partly Cloudy",
    4: "Cloudy",
    5: "Windy",
    6: "Snow",
    7: "Fog",
}
WEATHER_EMOJI = {
    1: "☀️",
    2: "🌧️",
    3: "⛅",
    4: "☁️",
    5: "💨",
    6: "❄️",
    7: "🌫️",
}

# Standard gender enum used across this class of map tooling: 1=male,
# 2=female, 3=genderless. No 0/unset case in practice -- every real entry
# has one of these three.
GENDER_LABELS = {
    1: "♂️ Male",
    2: "♀️ Female",
    3: "⚪ Genderless",
}

# Identifies this script to OpenStreetMap's tile server and Nominatim, per
# both services' usage policies (they require a descriptive User-Agent, not
# a generic library default) -- fine for our volume (a handful of requests
# per rare alert, nowhere near their bulk-usage limits).
OSM_USER_AGENT = "watcher-script/1.0 (low request volume, contact via repo)"

# Composited locally from raw OSM tiles rather than calling a hosted
# "static maps" service -- tried three no-key hosted options and all three
# failed live (a dead domain, a tile provider that blocks third-party
# sites, and a 525 from a community-hosted mirror) -- fetching raw tiles
# directly from tile.openstreetmap.org and drawing the marker ourselves has
# no third-party single point of failure beyond OSM's own tile server.
OSM_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
OSM_TILE_SIZE = 256
MAP_IMAGE_ZOOM = 16
MAP_IMAGE_WIDTH = 500
MAP_IMAGE_HEIGHT = 350
MAP_MARKER_RADIUS = 8

NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"

# ---------------------------------------------------------------------------
# GRAPHQL QUERY
# ---------------------------------------------------------------------------
# Reconstructed from the target site's own frontend network traffic.
# Fragment/operation names below are our own arbitrary labels (renamed for
# this project); the "on Pokemon" type reference and the field names inside
# each selection set are dictated by the server's schema and can't be
# renamed without breaking the query.

RECORDS_QUERY = """
fragment CoreFields on Pokemon {
  id
  lat
  lon
  pokemon_id
  form
  gender
  costume
  first_seen_timestamp
  expire_timestamp
  expire_timestamp_verified
  updated
  display_pokemon_id
  seen_type
}

fragment Stats on Pokemon {
  iv
  cp
  level
  atk_iv
  def_iv
  sta_iv
  weight
  size
  height
  move_1
  move_2
  weather
}

fragment League on Pokemon {
  cleanPvp
  bestPvp
}

query FetchRecords(
  $minLat: Float!
  $minLon: Float!
  $maxLat: Float!
  $maxLon: Float!
  $filters: JSON!
) {
  pokemon(
    minLat: $minLat
    minLon: $minLon
    maxLat: $maxLat
    maxLon: $maxLon
    filters: $filters
  ) {
    ...CoreFields
    ...Stats
    ...League
  }
}
"""

# Deliberately minimal, not a copy of the frontend's full per-species filter
# blob (which mirrors its entire filter-mode UI state and runs to ~300KB).
# Traced through the server's own filter-handling code: any top-level
# boolean field in `filters` becomes a standalone flag, and
# `onlyHundoIv: true` specifically adds the target-match condition to the
# underlying query, independent of the complex per-species filter
# machinery -- exactly what's needed here and nothing more. onlyIv must be
# truthy for the related WHERE clauses to apply at all, hence onlyIv: true
# below. These key names are dictated by the server and can't be renamed.
#
# onlyIvOr is NOT optional despite not being needed for filtering logic --
# confirmed live that omitting it crashes the resolver server-side (an
# unconditional read of a field assumed present). Using the exact shape
# captured from the frontend's own real request rather than guessing a
# minimal one, since a wrong shape here fails server-side, not as a clean
# validation error.
FILTERS_PAYLOAD = {
    "onlyLegacy": False,
    "onlyLinkGlobal": True,
    "onlyAllPvp": False,
    "onlyAreas": [],
    "onlyEasyMode": True,
    "onlyOnlyShowAvailable": True,
    "onlyIv": True,
    "onlyPvp": False,
    "onlyGender": 0,
    "onlyZeroIv": False,
    "onlyHundoIv": True,
    "onlyPvpMega": False,
    "onlyPvp50": False,
    "onlyPvp51": False,
    "onlyIvOr": {
        "enabled": True,
        "size": "md",
        "all": False,
        "adv": "",
        "iv": [100, 100],
        "atk_iv": [0, 15],
        "def_iv": [0, 15],
        "sta_iv": [0, 15],
        "level": [1, 55],
        "cp": [10, 5000],
        "gender": 0,
        "xxs": False,
        "xxl": False,
        "little": [1, 1],
        "great": [1, 1],
        "ultra": [1, 1],
    },
}

# Separate filter for the PVP top-rank alert -- confirmed live this is a
# largely disjoint set from FILTERS_PAYLOAD above (a 100%-match record is
# almost never also a top PVP rank, since PVP ranking favors specific
# lower/mid stat combos under a league's CP cap), so this flips
# onlyHundoIv off and onlyPvp on rather than trying to merge both
# conditions into one filter. onlyIv is deliberately left True (matching
# the confirmed-working baseline) with its range widened to [0, 100] (no
# restriction), and the great/ultra ranges narrowed to
# [1, PVP_RANK_THRESHOLD] as a server-side hint -- still re-verified
# client-side in extract_top_pvp_ranks(), same "never trust the server
# filter alone" rule as FILTERS_PAYLOAD above. Little League is
# deliberately left at [1, 1] (i.e. not hinted/requested) -- per user
# feedback it was the league qualifying most often by far and the results
# were the least useful, so it's dropped from this alert entirely; see
# PVP_LEAGUE_LABELS below, which is what actually excludes it (the
# server-side hint here is just secondary cleanup).
PVP_RANK_THRESHOLD = 3

PVP_FILTERS_PAYLOAD = {
    "onlyLegacy": False,
    "onlyLinkGlobal": True,
    "onlyAllPvp": False,
    "onlyAreas": [],
    "onlyEasyMode": True,
    "onlyOnlyShowAvailable": True,
    "onlyIv": True,
    "onlyPvp": True,
    "onlyGender": 0,
    "onlyZeroIv": False,
    "onlyHundoIv": False,
    "onlyPvpMega": False,
    "onlyPvp50": False,
    "onlyPvp51": False,
    "onlyIvOr": {
        "enabled": True,
        "size": "md",
        "all": False,
        "adv": "",
        "iv": [0, 100],
        "atk_iv": [0, 15],
        "def_iv": [0, 15],
        "sta_iv": [0, 15],
        "level": [1, 55],
        "cp": [10, 5000],
        "gender": 0,
        "xxs": False,
        "xxl": False,
        "little": [1, 1],
        "great": [1, PVP_RANK_THRESHOLD],
        "ultra": [1, PVP_RANK_THRESHOLD],
    },
}

# Little League intentionally excluded here (see PVP_RANK_THRESHOLD comment
# above) -- extract_top_pvp_ranks() only keeps leagues present in this dict,
# so a record ranking top-3 in Little League alone simply won't qualify.
PVP_LEAGUE_LABELS = {
    "great": "Great League",
    "ultra": "Ultra League",
}

# ---------------------------------------------------------------------------
# CATALOG (id -> display name lookup -- public, no auth needed)
# ---------------------------------------------------------------------------

_catalog = None

def load_catalog():
    """Fetches (or reuses a same-day cached copy of) a public catalog
    mapping an id to a display name and form/variant id to a variant name.
    Not authenticated -- this is the same static file the target site's own
    frontend fetches to render names in its own UI."""
    global _catalog
    if _catalog is not None:
        return _catalog

    if os.path.exists(CATALOG_CACHE_PATH):
        age = time.time() - os.path.getmtime(CATALOG_CACHE_PATH)
        if age < CATALOG_MAX_AGE:
            with open(CATALOG_CACHE_PATH, "r", encoding="utf-8") as f:
                _catalog = json.load(f)
            logger.debug("Loaded catalog from local cache.")
            return _catalog

    logger.info("Fetching catalog (display names)...")
    resp = requests.get(CATALOG_URL, timeout=30)
    resp.raise_for_status()
    _catalog = resp.json()
    with open(CATALOG_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(_catalog, f)
    return _catalog

def describe_entry(entry_id, form):
    """Returns a human-readable display name, including the variant name
    if it's anything other than the default variant."""
    catalog = load_catalog()
    entry = catalog.get("pokemon", {}).get(str(entry_id))
    if not entry:
        return f"Unknown #{entry_id}"
    name = entry.get("name", f"#{entry_id}")
    form_entry = entry.get("forms", {}).get(str(form)) if form else None
    if form_entry and form_entry.get("name") and form != entry.get("defaultFormId"):
        return f"{name} ({form_entry['name']})"
    return name

def build_icon_url(record):
    """Returns an illustrative image URL for the record's exact
    form/costume if that specific asset exists in the icon set, otherwise
    falls back to progressively less specific combos and finally the bare
    id. Checked with HEAD requests since not every form/costume has its own
    asset -- an unchecked guess would risk a broken image in the embed."""
    rid = record.get("pokemon_id")
    if not rid:
        return None
    form = record.get("form")
    costume = record.get("costume")

    candidates = []
    if form and costume:
        candidates.append(f"{rid}_f{form}_c{costume}")
    if form:
        candidates.append(f"{rid}_f{form}")
    if costume:
        candidates.append(f"{rid}_c{costume}")
    candidates.append(str(rid))

    for name in candidates:
        url = f"{ICON_BASE}/{name}.png"
        try:
            resp = requests.head(url, timeout=5)
            if resp.status_code == 200:
                return url
        except requests.RequestException:
            continue
    return None

def point_in_bbox(lat, lon, min_lat, max_lat, min_lon, max_lon):
    return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon

def _deg2tile_float(lat, lon, zoom):
    """Slippy-map projection (standard OSM tile math): returns fractional
    tile x/y so callers can find both which tile a point falls in and the
    exact sub-pixel offset within it."""
    lat_rad = math.radians(lat)
    n = 2 ** zoom
    xtile = (lon + 180.0) / 360.0 * n
    ytile = (1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0 * n
    return xtile, ytile

def build_location_map_image(lat, lon):
    """Fetches just enough raw OSM tiles to cover a MAP_IMAGE_WIDTH x
    MAP_IMAGE_HEIGHT window centered on (lat, lon), composites them into one
    image, and draws a marker at the exact center. Returns PNG bytes, or
    None if any tile fetch fails (caller should post the alert without a
    map image rather than not at all)."""
    xf, yf = _deg2tile_float(lat, lon, MAP_IMAGE_ZOOM)
    center_px_x, center_px_y = xf * OSM_TILE_SIZE, yf * OSM_TILE_SIZE
    left = center_px_x - MAP_IMAGE_WIDTH / 2
    top = center_px_y - MAP_IMAGE_HEIGHT / 2

    tile_x_min = int(left // OSM_TILE_SIZE)
    tile_x_max = int((left + MAP_IMAGE_WIDTH - 1) // OSM_TILE_SIZE)
    tile_y_min = int(top // OSM_TILE_SIZE)
    tile_y_max = int((top + MAP_IMAGE_HEIGHT - 1) // OSM_TILE_SIZE)

    canvas = Image.new(
        "RGB",
        ((tile_x_max - tile_x_min + 1) * OSM_TILE_SIZE, (tile_y_max - tile_y_min + 1) * OSM_TILE_SIZE),
    )
    headers = {"User-Agent": OSM_USER_AGENT}
    try:
        for tx in range(tile_x_min, tile_x_max + 1):
            for ty in range(tile_y_min, tile_y_max + 1):
                url = OSM_TILE_URL.format(z=MAP_IMAGE_ZOOM, x=tx, y=ty)
                resp = requests.get(url, headers=headers, timeout=10)
                resp.raise_for_status()
                tile_img = Image.open(io.BytesIO(resp.content)).convert("RGB")
                canvas.paste(tile_img, ((tx - tile_x_min) * OSM_TILE_SIZE, (ty - tile_y_min) * OSM_TILE_SIZE))
    except (requests.RequestException, OSError) as e:
        logger.warning(f"Failed to build location map image (tile fetch): {e}")
        return None

    crop_left = int(left - tile_x_min * OSM_TILE_SIZE)
    crop_top = int(top - tile_y_min * OSM_TILE_SIZE)
    cropped = canvas.crop((crop_left, crop_top, crop_left + MAP_IMAGE_WIDTH, crop_top + MAP_IMAGE_HEIGHT))

    draw = ImageDraw.Draw(cropped)
    cx, cy = MAP_IMAGE_WIDTH // 2, MAP_IMAGE_HEIGHT // 2
    r = MAP_MARKER_RADIUS
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(220, 20, 20), outline=(255, 255, 255), width=2)

    buf = io.BytesIO()
    cropped.save(buf, format="PNG")
    return buf.getvalue()

def reverse_geocode(lat, lon):
    """Looks up a human-readable address for the given coordinates via
    Nominatim (OpenStreetMap's free reverse-geocoding service, no API key).
    Returns None on any failure -- callers fall back to showing raw
    coordinates, since a missing address shouldn't block the alert."""
    try:
        resp = requests.get(
            NOMINATIM_REVERSE_URL,
            params={"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 18, "addressdetails": 0},
            headers={"User-Agent": OSM_USER_AGENT},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("display_name")
    except (requests.RequestException, ValueError) as e:
        logger.debug(f"Reverse geocoding failed for ({lat},{lon}): {e}")
        return None

# ---------------------------------------------------------------------------
# GRAPHQL QUERY EXECUTION
# ---------------------------------------------------------------------------

class SessionExpiredError(Exception):
    pass

def _query_entries(filters):
    """Shared POST + error-handling logic for both queries below -- only
    the `filters` payload differs between the target-match query and the
    PVP top-rank query, everything else (endpoint, headers, error
    handling) is identical."""
    if not SESSION_COOKIE:
        raise RuntimeError(
            "SESSION_COOKIE (and COOKIE_NAME) not set -- see setup instructions at top of file."
        )
    if not GRAPHQL_URL:
        raise RuntimeError("API_BASE not set -- see setup instructions at top of file.")

    body = {
        "operationName": "FetchRecords",
        "variables": {
            "minLat": MIN_LAT,
            "maxLat": MAX_LAT,
            "minLon": MIN_LON,
            "maxLon": MAX_LON,
            "filters": filters,
        },
        "query": RECORDS_QUERY,
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:152.0) Gecko/20100101 Firefox/152.0",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json",
        "apollographql-client-name": "watcher-client",
        "apollographql-client-version": "1.36.0-develop.12",
        "Origin": API_BASE,
        "Referer": f"{API_BASE}/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "DNT": "1",
        "Sec-GPC": "1",
        "Cookie": SESSION_COOKIE,
    }

    resp = requests.post(GRAPHQL_URL, json=body, headers=headers, timeout=30)

    # 511 is the target deployment's actual real-world signal for an
    # expired session (a deliberate app-level GraphQL error, not a generic
    # network failure) -- confirmed live, repeatedly, that THIS is the
    # status code that actually shows up, not 401/403. Missing it here was
    # a real bug: resp.raise_for_status() below throws a plain HTTPError
    # for any unhandled 4xx/5xx BEFORE the GraphQL errors-array check
    # further down ever runs, so a 511 was silently falling into the
    # generic "query failed" handler in main() -- logged, but with no
    # Discord alert, exactly the failure mode this whole
    # SessionExpiredError path exists to prevent.
    if resp.status_code in (401, 403, 511):
        raise SessionExpiredError(f"HTTP {resp.status_code} -- session cookie likely expired.")

    resp.raise_for_status()
    data = resp.json()

    if "errors" in data and data["errors"]:
        messages = "; ".join(e.get("message", str(e)) for e in data["errors"])
        if any(word in messages.lower() for word in ("auth", "login", "unauthorized", "forbidden")):
            raise SessionExpiredError(f"GraphQL auth error -- session cookie likely expired: {messages}")
        raise RuntimeError(f"GraphQL error: {messages}")

    return data.get("data", {}).get("pokemon", []) or []

def query_target_entries():
    """Returns the list of entries in the response, already server-side
    filtered by FILTERS_PAYLOAD's onlyHundoIv flag -- but callers should
    treat that as a hint, not a guarantee, and can re-check the target
    condition themselves if being defensive."""
    return _query_entries(FILTERS_PAYLOAD)

def query_pvp_entries():
    """Returns the list of entries in the response, already server-side
    filtered by PVP_FILTERS_PAYLOAD's per-league rank range -- same
    "hint, not a guarantee" caveat as query_target_entries() above; callers
    should re-verify with extract_top_pvp_ranks()."""
    return _query_entries(PVP_FILTERS_PAYLOAD)

def extract_top_pvp_ranks(record, threshold=PVP_RANK_THRESHOLD):
    """Returns {league_key: entry} for each league where THIS record's own
    species+form (not a hypothetical evolution) ranks <= threshold.

    `cleanPvp[league]` is a list of ranking entries -- confirmed live it can
    include entries for species other than the record itself (e.g. what it
    could evolve into), each with its own `pokemon`/`form`/`rank`. Only the
    entry matching this exact record's `pokemon_id`/`form` is relevant here;
    a hypothetical evolution ranking top-3 isn't what's actually sitting on
    the map right now. `form` is omitted from an entry when it's 0 (per the
    underlying ranking library's convention), so both sides are normalized
    to 0 before comparing."""
    clean_pvp = record.get("cleanPvp") or {}
    rid = record.get("pokemon_id")
    form = record.get("form") or 0

    results = {}
    for league, entries in clean_pvp.items():
        if league not in PVP_LEAGUE_LABELS or not entries:
            continue
        for entry in entries:
            if entry.get("pokemon") != rid:
                continue
            if (entry.get("form") or 0) != form:
                continue
            rank = entry.get("rank")
            if rank is not None and rank <= threshold:
                results[league] = entry
            break  # at most one entry per species+form per league
    return results

# ---------------------------------------------------------------------------
# DISCORD
# ---------------------------------------------------------------------------

def send_discord_text(message):
    if not DISCORD_WEBHOOK:
        logger.error("DISCORD_WEBHOOK not set -- cannot send alert. See setup instructions at top of file.")
        return
    try:
        resp = requests.post(DISCORD_WEBHOOK, json={"content": message}, timeout=15)
        if resp.status_code >= 300:
            logger.warning(f"Discord alert returned status {resp.status_code}: {resp.text[:200]}")
        else:
            logger.info(f"Discord alert sent for: {message.splitlines()[0]}")
    except requests.RequestException as e:
        logger.warning(f"Failed to send Discord alert: {e}")

def send_discord_embed(embed, image_bytes=None, image_filename=None, webhook=None):
    """Posts a single embed to `webhook` (defaults to DISCORD_WEBHOOK, but
    the PVP alert path passes PVP_DISCORD_WEBHOOK to route to a separate
    channel). If image_bytes is given, sends it as a multipart file
    attachment alongside the embed (embed['image']['url'] must already be
    set to 'attachment://{image_filename}' by the caller) rather than a
    plain JSON POST -- Discord webhooks only accept file attachments via
    multipart form data."""
    webhook = webhook or DISCORD_WEBHOOK
    if not webhook:
        logger.error("No Discord webhook set -- cannot send alert. See setup instructions at top of file.")
        return
    try:
        if image_bytes is not None:
            resp = requests.post(
                webhook,
                data={"payload_json": json.dumps({"embeds": [embed]})},
                files={"files[0]": (image_filename, image_bytes, "image/png")},
                timeout=20,
            )
        else:
            resp = requests.post(webhook, json={"embeds": [embed]}, timeout=15)
        if resp.status_code >= 300:
            logger.warning(f"Discord embed alert returned status {resp.status_code}: {resp.text[:200]}")
        else:
            logger.info(f"Discord embed alert sent for: {embed.get('title')}")
    except requests.RequestException as e:
        logger.warning(f"Failed to send Discord embed alert: {e}")

MAP_ATTACHMENT_FILENAME = "location_map.png"

def format_embed(record):
    """Builds a rich embed: an illustrative image as the thumbnail,
    CP/level, weather boost status, expiry time shown both relative and
    absolute via Discord's native <t:...> timestamp markup (self-updating
    client-side, already timezone-correct for every viewer), and a Location
    field with a reverse-geocoded address plus a Google Maps link. Does NOT
    set the embed's main 'image' -- that's the locally-composited OSM map,
    wired up by the caller (send_alert) once it has the attachment bytes,
    since the embed alone doesn't know whether that attachment succeeded."""
    name = describe_entry(record.get("pokemon_id"), record.get("form"))
    iv = record.get("iv")
    cp = record.get("cp")
    level = record.get("level")
    weather = record.get("weather")
    gender = record.get("gender")
    expire_ts = record.get("expire_timestamp")
    lat, lon = record.get("lat"), record.get("lon")

    fields = []
    if cp is not None and level is not None:
        fields.append({"name": "CP / Level", "value": f"{cp} / L{level}", "inline": True})
    if gender in GENDER_LABELS:
        fields.append({"name": "Gender", "value": GENDER_LABELS[gender], "inline": True})
    if weather:
        emoji = WEATHER_EMOJI.get(weather, "✨")
        fields.append({"name": "Weather Boost", "value": f"{emoji} Yes ({WEATHER_NAMES.get(weather, f'Unknown #{weather}')})", "inline": True})
    else:
        fields.append({"name": "Weather Boost", "value": "No", "inline": True})
    if expire_ts:
        ts = int(expire_ts)
        fields.append({"name": "Expires", "value": f"<t:{ts}:R> -- <t:{ts}:T>", "inline": False})
    if lat is not None and lon is not None:
        maps_url = f"https://maps.google.com/?q={lat},{lon}"
        address = reverse_geocode(lat, lon)
        location_lines = [address] if address else []
        location_lines.append(f"`{lat:.5f}, {lon:.5f}`")
        location_lines.append(f"[Open in Google Maps]({maps_url})")
        fields.append({"name": "Location", "value": "\n".join(location_lines), "inline": False})

    embed = {
        "title": f"\U0001F534 {name} -- {iv}% match",
        "color": ALERT_COLOR,
        "fields": fields,
    }
    icon_url = build_icon_url(record)
    if icon_url:
        embed["thumbnail"] = {"url": icon_url}
    return embed

def send_alert(record):
    """Builds the embed and, if coordinates are available, a locally
    composited OSM map preview centered on the record -- sent together as
    one multipart post (embed['image'] pointing at the attached file) so
    the whole alert lands as a single Discord message. If ZONE_WEBHOOK is
    configured and this record's coordinates fall inside the zone bbox, the
    same embed (and map image, reusing the same fetched bytes rather than
    fetching twice) is also mirrored there as a second, independent post."""
    embed = format_embed(record)
    lat, lon = record.get("lat"), record.get("lon")
    map_bytes = None
    if lat is not None and lon is not None:
        map_bytes = build_location_map_image(lat, lon)
        if map_bytes is not None:
            embed["image"] = {"url": f"attachment://{MAP_ATTACHMENT_FILENAME}"}

    if map_bytes is not None:
        send_discord_embed(embed, image_bytes=map_bytes, image_filename=MAP_ATTACHMENT_FILENAME)
    else:
        send_discord_embed(embed)

    zone_bounds = (ZONE_MIN_LAT, ZONE_MAX_LAT, ZONE_MIN_LON, ZONE_MAX_LON)
    if (ZONE_WEBHOOK and lat is not None and lon is not None
            and None not in zone_bounds and point_in_bbox(lat, lon, *zone_bounds)):
        logger.info("Record falls inside the zone bbox -- mirroring alert to ZONE_WEBHOOK.")
        if map_bytes is not None:
            send_discord_embed(embed, image_bytes=map_bytes, image_filename=MAP_ATTACHMENT_FILENAME, webhook=ZONE_WEBHOOK)
        else:
            send_discord_embed(embed, webhook=ZONE_WEBHOOK)

def format_pvp_embed(record, qualifying_leagues):
    """Same overall shape as format_embed() (illustrative thumbnail,
    weather, gender, expiry, location), but for the separate PVP-rank
    alert: shows the record's current/as-caught CP+level and Atk/Def/Sta
    breakdown (not the overall match %, which isn't useful once the
    breakdown is shown) so a player can actually match this alert to the
    entry on their own map, plus a field per qualifying league with its
    rank/percentage and the CP/level to power up TO in order to realize
    that rank (that target CP/level is what the ranking entry itself
    reports, which is generally NOT the same as the record's current
    as-caught CP/level shown above)."""
    name = describe_entry(record.get("pokemon_id"), record.get("form"))
    cp = record.get("cp")
    level = record.get("level")
    atk, defe, sta = record.get("atk_iv"), record.get("def_iv"), record.get("sta_iv")
    weather = record.get("weather")
    gender = record.get("gender")
    expire_ts = record.get("expire_timestamp")
    lat, lon = record.get("lat"), record.get("lon")

    fields = []
    if cp is not None and level is not None:
        fields.append({"name": "Current CP / Level", "value": f"{cp} / L{level}", "inline": True})
    if atk is not None and defe is not None and sta is not None:
        fields.append({"name": "IVs (Atk/Def/Sta)", "value": f"{atk}/{defe}/{sta}", "inline": True})
    if gender in GENDER_LABELS:
        fields.append({"name": "Gender", "value": GENDER_LABELS[gender], "inline": True})
    if weather:
        emoji = WEATHER_EMOJI.get(weather, "✨")
        fields.append({"name": "Weather Boost", "value": f"{emoji} Yes ({WEATHER_NAMES.get(weather, f'Unknown #{weather}')})", "inline": True})
    else:
        fields.append({"name": "Weather Boost", "value": "No", "inline": True})
    for league in ("great", "ultra"):
        entry = qualifying_leagues.get(league)
        if not entry:
            continue
        rank = entry.get("rank")
        pct = entry.get("percentage")
        pct_str = f"{pct * 100:.1f}%" if pct is not None else "?"
        fields.append({
            "name": f"\U0001F3C6 {PVP_LEAGUE_LABELS[league]}",
            "value": f"Rank #{rank} ({pct_str}) -- power up to CP {entry.get('cp')} @ L{entry.get('level')}",
            "inline": False,
        })
    if expire_ts:
        ts = int(expire_ts)
        fields.append({"name": "Despawns", "value": f"<t:{ts}:R> -- <t:{ts}:T>", "inline": False})
    if lat is not None and lon is not None:
        maps_url = f"https://maps.google.com/?q={lat},{lon}"
        address = reverse_geocode(lat, lon)
        location_lines = [address] if address else []
        location_lines.append(f"`{lat:.5f}, {lon:.5f}`")
        location_lines.append(f"[Open in Google Maps]({maps_url})")
        fields.append({"name": "Location", "value": "\n".join(location_lines), "inline": False})

    # Concise "rank + league" title (e.g. "Great League #2", or
    # "Great League #2 / Ultra League #1" if it qualifies in more than
    # one) -- states the actual rank(s) rather than a vaguer "top N" label.
    rank_bits = [
        f"{PVP_LEAGUE_LABELS[league]} #{entry.get('rank')}"
        for league, entry in qualifying_leagues.items()
        if league in PVP_LEAGUE_LABELS
    ]
    embed = {
        "title": f"\U0001F3C6 {name} -- {' / '.join(rank_bits)}",
        "color": PVP_ALERT_COLOR,
        "fields": fields,
    }
    icon_url = build_icon_url(record)
    if icon_url:
        embed["thumbnail"] = {"url": icon_url}
    return embed

def send_pvp_alert(record, qualifying_leagues):
    """Same single-post-with-attached-map-image pattern as send_alert(),
    but posted to PVP_DISCORD_WEBHOOK instead."""
    embed = format_pvp_embed(record, qualifying_leagues)
    lat, lon = record.get("lat"), record.get("lon")
    if lat is not None and lon is not None:
        map_bytes = build_location_map_image(lat, lon)
        if map_bytes is not None:
            embed["image"] = {"url": f"attachment://{MAP_ATTACHMENT_FILENAME}"}
            send_discord_embed(embed, image_bytes=map_bytes, image_filename=MAP_ATTACHMENT_FILENAME, webhook=PVP_DISCORD_WEBHOOK)
            return
    send_discord_embed(embed, webhook=PVP_DISCORD_WEBHOOK)

# ---------------------------------------------------------------------------
# TRACKING (dedup alerts across polls)
# ---------------------------------------------------------------------------
# Each match has a stable unique `id` and a known exact expire_timestamp --
# much simpler than position-based tracking, which is what you'd need for
# screen-scraped data with no stable identity.

tracked_ids = {}  # id -> expire_timestamp

# Separate from tracked_ids above -- a record could be a target match AND
# (in principle) also qualify for the PVP alert, and these get sent to
# different channels, so they're tracked independently rather than sharing
# one dedup set. A single combined post covers however many leagues a
# record qualifies for at once (see extract_top_pvp_ranks), so this only
# needs one entry per record id, not one per (id, league).
tracked_pvp_ids = {}  # id -> expire_timestamp

def prune_tracked_ids(now):
    expired = [pid for pid, exp in tracked_ids.items() if now > exp + FORGET_AFTER_EXPIRY]
    for pid in expired:
        del tracked_ids[pid]
    if expired:
        logger.debug(f"Forgot {len(expired)} expired match(es) from tracking.")

def prune_tracked_pvp_ids(now):
    expired = [pid for pid, exp in tracked_pvp_ids.items() if now > exp + FORGET_AFTER_EXPIRY]
    for pid in expired:
        del tracked_pvp_ids[pid]
    if expired:
        logger.debug(f"Forgot {len(expired)} expired PVP match(es) from tracking.")

def load_tracked_state():
    """Restores tracked_ids/tracked_pvp_ids from disk if a state file from
    a previous run exists -- see TRACKED_STATE_PATH above for why this
    matters. Missing/corrupt file just means starting fresh (not fatal;
    worst case is a handful of re-alerts, same as before this existed)."""
    if not os.path.exists(TRACKED_STATE_PATH):
        return
    try:
        with open(TRACKED_STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
        tracked_ids.update(state.get("tracked_ids", {}))
        tracked_pvp_ids.update(state.get("tracked_pvp_ids", {}))
        logger.info(
            f"Restored tracked state from disk: {len(tracked_ids)} id(s), "
            f"{len(tracked_pvp_ids)} PVP id(s)."
        )
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to load {TRACKED_STATE_PATH}: {e} -- starting with empty tracking.")

def save_tracked_state():
    try:
        with open(TRACKED_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"tracked_ids": tracked_ids, "tracked_pvp_ids": tracked_pvp_ids}, f)
    except OSError as e:
        logger.warning(f"Failed to save {TRACKED_STATE_PATH}: {e}")

# ---------------------------------------------------------------------------
# MAIN LOOP
# ---------------------------------------------------------------------------

def main():
    logger.info("Checking required configuration...")
    if not DISCORD_WEBHOOK:
        logger.error("DISCORD_WEBHOOK not set. See setup instructions at top of file.")
        return
    if not API_BASE:
        logger.error("API_BASE not set. See setup instructions at top of file.")
        return
    if not COOKIE_NAME or not SESSION_COOKIE:
        logger.error("COOKIE_NAME and SESSION_COOKIE must both be set. See setup instructions at top of file.")
        return
    if None in (MIN_LAT, MAX_LAT, MIN_LON, MAX_LON):
        logger.error(
            "MIN_LAT/MAX_LAT/MIN_LON/MAX_LON must all be set to numbers in .env "
            "(your area of interest) -- see setup instructions at top of file."
        )
        return
    if ZONE_WEBHOOK and None in (ZONE_MIN_LAT, ZONE_MAX_LAT, ZONE_MIN_LON, ZONE_MAX_LON):
        logger.error(
            "ZONE_WEBHOOK is set but ZONE_MIN_LAT/MAX_LAT/MIN_LON/MAX_LON "
            "aren't all set -- a zone webhook needs all four bounds to test against."
        )
        return

    load_catalog()
    load_tracked_state()
    logger.info("Starting watcher.")
    logger.info(f"Bounding box: ({MIN_LAT},{MIN_LON}) to ({MAX_LAT},{MAX_LON}), polling every {POLL_INTERVAL}s.")
    if PVP_DISCORD_WEBHOOK:
        logger.info(f"PVP top-{PVP_RANK_THRESHOLD} alerts enabled (separate webhook).")
    else:
        logger.info("PVP_DISCORD_WEBHOOK not set -- PVP top-rank alerts disabled.")
    if ZONE_WEBHOOK:
        logger.info(
            f"Zone mirroring enabled: matches in ({ZONE_MIN_LAT},{ZONE_MIN_LON}) to "
            f"({ZONE_MAX_LAT},{ZONE_MAX_LON}) also post to ZONE_WEBHOOK."
        )
    else:
        logger.info("ZONE_WEBHOOK not set -- zone-mirrored alerts disabled.")
    if MAX_RUNTIME_SECONDS:
        logger.info(f"Will exit cleanly after ~{int(MAX_RUNTIME_SECONDS)}s (MAX_RUNTIME_SECONDS set).")

    start_time = time.time()
    was_session_expired = False
    cycle = 0
    while True:
        if MAX_RUNTIME_SECONDS and (time.time() - start_time) >= MAX_RUNTIME_SECONDS:
            logger.info("MAX_RUNTIME_SECONDS reached -- exiting cleanly for the next scheduled run to take over.")
            break

        cycle += 1
        logger.debug(f"--- Cycle {cycle} ---")
        now = time.time()

        try:
            entries = query_target_entries()
        except SessionExpiredError as e:
            logger.error(f"Session cookie appears to have expired: {e}")
            if not was_session_expired:
                send_discord_text(
                    "⚠️ Watcher: session cookie has expired -- "
                    "grab a fresh one from the browser and update SESSION_COOKIE in .env."
                )
                was_session_expired = True
            time.sleep(POLL_INTERVAL)
            continue
        except (requests.RequestException, RuntimeError) as e:
            logger.error(f"Query failed: {e}")
            time.sleep(POLL_INTERVAL)
            continue

        if was_session_expired:
            logger.info("Session cookie is working again.")
            send_discord_text("✅ Watcher: session cookie is working again.")
            was_session_expired = False

        confirmed_matches = [e for e in entries if e.get("iv") == 100]
        logger.debug(f"{len(confirmed_matches)}/{len(entries)} returned entries confirmed as a match.")

        new_count = 0
        for record in confirmed_matches:
            rid = record.get("id")
            if rid is None:
                continue
            if rid in tracked_ids:
                continue
            tracked_ids[rid] = record.get("expire_timestamp", now + 3600)
            new_count += 1
            logger.info(f"New match: {describe_entry(record.get('pokemon_id'), record.get('form'))} (id={rid})")
            send_alert(record)

        if new_count == 0:
            logger.debug(f"No new matches this cycle ({len(tracked_ids)} tracked).")

        prune_tracked_ids(now)

        # PVP top-rank alerts -- separate query/filter/webhook/tracking
        # from the target-match alert above. Entirely optional: skipped
        # gracefully if PVP_DISCORD_WEBHOOK isn't set.
        if PVP_DISCORD_WEBHOOK:
            try:
                pvp_candidates = query_pvp_entries()
            except SessionExpiredError:
                pvp_candidates = []  # already alerted above for the main query's failure
            except (requests.RequestException, RuntimeError) as e:
                logger.error(f"PVP query failed: {e}")
                pvp_candidates = []

            pvp_new_count = 0
            for record in pvp_candidates:
                rid = record.get("id")
                if rid is None or rid in tracked_pvp_ids:
                    continue
                qualifying_leagues = extract_top_pvp_ranks(record)
                if not qualifying_leagues:
                    continue  # server-side hint didn't hold up client-side -- skip
                tracked_pvp_ids[rid] = record.get("expire_timestamp", now + 3600)
                pvp_new_count += 1
                logger.info(
                    f"New top-{PVP_RANK_THRESHOLD} PVP match: "
                    f"{describe_entry(record.get('pokemon_id'), record.get('form'))} "
                    f"({'/'.join(qualifying_leagues)}) (id={rid})"
                )
                send_pvp_alert(record, qualifying_leagues)

            if pvp_new_count == 0:
                logger.debug(f"No new top-PVP matches this cycle ({len(tracked_pvp_ids)} tracked).")

            prune_tracked_pvp_ids(now)

        save_tracked_state()
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
