"""Walking-time estimates from a listing to named anchors.

Two anchor groups:
  - BEACHES: any of Baker / China / Ocean — used as "nearest beach".
  - BAKERIES: Arsicault (Inner Richmond) and Arizmendi (Inner Sunset).

Real walking times via Google Routes API (computeRouteMatrix) when
GOOGLE_MAPS_API_KEY is set; falls back to haversine × 1.3 ÷ 4.5 km/h
when the key is missing or the call fails. Results cached in a dedicated
local DB (~/.casita/routes_cache.sqlite, table `walk_cache`) keyed
by rounded lat/lng — re-runs are free, including the publisher daemon's
read-only renders (issue #4).
"""
import math
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent.parent / ".env")


@dataclass(frozen=True)
class Anchor:
    name: str
    short: str
    lat: float
    lng: float
    # Google Maps signals — surfaced on the card so the user can judge the
    # anchor at a glance. None for non-business anchors (trailheads, beaches).
    rating: float | None = None
    rating_count: int | None = None
    # Google Places place_id — drives the maps deep-link to the actual place
    # panel (reviews/hours/photos) rather than a generic search.
    place_id: str | None = None


BEACHES: list[Anchor] = [
    Anchor("Baker Beach",   "Baker Beach",   37.7935, -122.4836, place_id="ChIJp_hAi_yGhYARGdXAfU1rPYs"),
    Anchor("China Beach",   "China Beach",   37.7895, -122.4910, place_id="ChIJDSFWGgCHhYAR3kcTq2vIdVQ"),
    Anchor("Ocean Beach",   "Ocean Beach",   37.7693, -122.5117, place_id="ChIJY8btaZSHhYAR_GctC494Hoo"),
    Anchor("Muir Beach",    "Muir Beach",    37.8607, -122.5760, place_id="ChIJ7zeudX6FhYARq7-NVf7dhgk"),
    Anchor("Stinson Beach", "Stinson Beach", 37.9006, -122.6446, place_id="ChIJLZ_NrZKThYARujlCxy63w78"),
]

BAKERIES: list[Anchor] = [
    # Curated via Places API. SF threshold: ≥4.7★ AND ≥1,500 reviews. Marin
    # threshold: ≥4.5★ AND ≥100 reviews. Bakeries + cafes-with-pastries only.
    # place_id values drive the maps deep-link to the actual place panel.
    Anchor("Arsicault Bakery (Inner Richmond)",  "Arsicault",     37.7834, -122.4593, 4.8, 3148, "ChIJJ09d9DmHhYARkrToDt9JCy8"),
    # Cinderella dropped — too close to Arsicault and a notch below.
    Anchor("b. patisserie (Lower Pac Heights)",  "b. patisserie", 37.7878, -122.4408, 4.7, 2870, "ChIJ59WgYcmAhYARu5gLD0FVpw4"),
    Anchor("Arizmendi Bakery (Inner Sunset)",    "Arizmendi",     37.7634, -122.4664, 4.7, 1972, "ChIJMdUfNVyHhYAR7pvCohDZC70"),
    # Marin — bakeries with real pastries (croissants) or quality coffee shops
    # that serve them. Bob's Donuts dropped — donuts, not croissants.
    Anchor("Le Marais Bakery (Mill Valley)",     "Le Marais",     37.9039, -122.5419, 4.5,  325, "ChIJM1n1oviRhYARrY83h6TQtSc"),
    Anchor("Madrona Bakery (Mill Valley)",       "Madrona",       37.9059, -122.5495, 4.7,  119, "ChIJt3Lx4LCRhYARWGAHgm0sQJU"),
    Anchor("Equator Coffees (Mill Valley)",      "Equator MV",    37.8817, -122.5244, 4.6,  581, "ChIJ3Qt5gHKQhYAR1lasgUpcLIY"),
    Anchor("Emporio Rulli (Larkspur)",           "Emporio Rulli", 37.9354, -122.5352, 4.6,  409, "ChIJ77I5OniahYAR5ZpjxedUINQ"),
]

# Trail access — using named places (not street corners) so map links land on
# something useful: park entrances and trailheads with reviews/photos.
TRAILS: list[Anchor] = [
    Anchor("Mountain Lake Park (Inner Richmond entry)", "Mountain Lake Park",
           37.7873, -122.4697, place_id="ChIJqfCTeBiHhYARt_0t7D65SWU"),
    Anchor("Inspiration Point (Presidio)",   "Inspiration Pt",
           37.7922, -122.4584, place_id="ChIJweecq2aHhYARQBcuiGtSqNY"),
    Anchor("Presidio Tunnel Tops",           "Tunnel Tops",
           37.8029, -122.4563, place_id="ChIJXfbP6YmAhYARX1lrFe54-tY"),
    Anchor("Lover's Lane (Presidio entry)",  "Lover's Lane",
           37.7873, -122.4530, place_id="ChIJ0YqOXS2HhYARxXrmOhmY7-Y"),
    Anchor("Dipsea Trailhead (Mill Valley)", "Dipsea Trailhead",
           37.9050, -122.5481, place_id="ChIJcaTo5GuQhYAR2IyBgkDEPPM"),
    Anchor("Tennessee Valley Trailhead",     "Tennessee Valley",
           37.8666, -122.5364, place_id="ChIJgdjluGKFhYARdFab_W53d1I"),
]
PRESIDIO_GATES = TRAILS

# Downtown SF — for Marin listings this is the "how far am I from the city?"
# lever. For SF listings, it's how close to downtown / ferry / BART.
SF_CENTER: list[Anchor] = [
    Anchor("Ferry Building (Embarcadero)", "Ferry Bldg",
           37.7956, -122.3933, place_id="ChIJWTGPjmaAhYARxz6l1hOj92w"),
]

_WALK_SPEED_KMH = 4.5
_GRID_FACTOR = 1.30
_DRIVE_SPEED_KMH = 45.0
_DRIVE_GRID_FACTOR = 1.20
_ROUTES_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"


def _haversine_km(a_lat: float, a_lng: float, b_lat: float, b_lng: float) -> float:
    R = 6371.0
    dlat = math.radians(b_lat - a_lat)
    dlng = math.radians(b_lng - a_lng)
    s = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(a_lat))
        * math.cos(math.radians(b_lat))
        * math.sin(dlng / 2) ** 2
    )
    return 2 * R * math.asin(math.sqrt(s))


def _haversine_minutes(lat: float, lng: float, anchor: Anchor) -> int:
    km = _haversine_km(lat, lng, anchor.lat, anchor.lng) * _GRID_FACTOR
    return max(1, round((km / _WALK_SPEED_KMH) * 60))


def _haversine_drive_minutes(lat: float, lng: float, anchor: Anchor) -> int:
    km = _haversine_km(lat, lng, anchor.lat, anchor.lng) * _DRIVE_GRID_FACTOR
    return max(1, round((km / _DRIVE_SPEED_KMH) * 60))


# ---------- cache (rounded coords as key) ----------
#
# Lives in a dedicated local DB, NOT the canonical GCS DB. The
# publisher daemon renders via with_db(read_only=True) — anything written
# to the pulled temp copy is discarded, so an in-DB cache re-paid the full
# Routes matrix on every publish (issue #4, ~$200 in 3 days). Route times
# are derived, location-keyed data; local runs are the only Routes consumer.

_DEFAULT_CACHE_DB = Path.home() / ".casita" / "routes_cache.sqlite"
_cache_connection: sqlite3.Connection | None = None
_cache_connection_path: Path | None = None


def _cache_db_path() -> Path:
    return Path(os.environ.get("CASITA_ROUTE_CACHE_DB", str(_DEFAULT_CACHE_DB)))


def _cache_conn() -> sqlite3.Connection:
    global _cache_connection, _cache_connection_path
    path = _cache_db_path()
    if _cache_connection is None or _cache_connection_path != path:
        if _cache_connection is not None:
            _cache_connection.close()
        path.parent.mkdir(parents=True, exist_ok=True)
        _cache_connection = sqlite3.connect(path)
        _cache_connection_path = path
        _ensure_cache(_cache_connection)
    return _cache_connection


def _ensure_cache(conn: sqlite3.Connection) -> None:
    create_sql = """CREATE TABLE IF NOT EXISTS walk_cache (
            from_lat REAL, from_lng REAL,
            to_lat REAL, to_lng REAL,
            mode TEXT NOT NULL DEFAULT 'walk',  -- 'walk' or 'drive'
            minutes INTEGER NOT NULL,
            source TEXT NOT NULL,   -- 'api' or 'haversine'
            ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (from_lat, from_lng, to_lat, to_lng, mode)
        )"""
    conn.execute(create_sql)
    # If table was created before the mode column existed, add it.
    info = conn.execute("PRAGMA table_info(walk_cache)").fetchall()
    cols = [r[1] for r in info]
    if "mode" not in cols:
        conn.execute("ALTER TABLE walk_cache ADD COLUMN mode TEXT NOT NULL DEFAULT 'walk'")
        info = conn.execute("PRAGMA table_info(walk_cache)").fetchall()

    # Older caches may have `mode` as a column but not as part of the primary
    # key, which lets walk and drive rows overwrite each other for the same
    # coordinate pair. Rebuild once into the current schema.
    pk_cols = {r[1] for r in info if r[5]}
    if "mode" not in pk_cols:
        conn.execute("ALTER TABLE walk_cache RENAME TO walk_cache_old")
        conn.execute(create_sql)
        conn.execute(
            """INSERT OR REPLACE INTO walk_cache
               (from_lat, from_lng, to_lat, to_lng, mode, minutes, source, ts)
               SELECT from_lat, from_lng, to_lat, to_lng,
                      COALESCE(mode, 'walk'), minutes, source, ts
               FROM walk_cache_old"""
        )
        conn.execute("DROP TABLE walk_cache_old")
    conn.commit()


def _rnd(x: float) -> float:
    # ~10m grid — good enough to dedupe API calls without losing accuracy.
    return round(x, 4)


def _cache_get(
    fl: float, fn: float, tl: float, tn: float,
    mode: str = "walk",
) -> int | None:
    row = _cache_conn().execute(
        "SELECT minutes FROM walk_cache "
        "WHERE from_lat=? AND from_lng=? AND to_lat=? AND to_lng=? AND mode=?",
        (_rnd(fl), _rnd(fn), _rnd(tl), _rnd(tn), mode),
    ).fetchone()
    return row[0] if row else None


def _cache_put(fl, fn, tl, tn, minutes: int, source: str, mode: str = "walk") -> None:
    _cache_conn().execute(
        "INSERT OR REPLACE INTO walk_cache "
        "(from_lat,from_lng,to_lat,to_lng,mode,minutes,source) VALUES (?,?,?,?,?,?,?)",
        (_rnd(fl), _rnd(fn), _rnd(tl), _rnd(tn), mode, minutes, source),
    )
    _cache_conn().commit()


# ---------- Routes API ----------


def _routes_api_enabled() -> bool:
    return os.environ.get("CASITA_ROUTES_OFFLINE") != "1" and bool(os.environ.get("GOOGLE_MAPS_API_KEY"))


def _call_routes_api(
    origins: list[tuple[float, float]], destinations: list[tuple[float, float]],
    *, mode: str = "walk",
) -> list[list[int | None]]:
    """Returns matrix[i][j] = travel minutes from origins[i] to destinations[j]."""
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not _routes_api_enabled() or not api_key:
        return [[None] * len(destinations) for _ in origins]
    travel_mode = "DRIVE" if mode == "drive" else "WALK"
    body = {
        "origins": [{"waypoint": {"location": {"latLng": {"latitude": la, "longitude": ln}}}} for la, ln in origins],
        "destinations": [{"waypoint": {"location": {"latLng": {"latitude": la, "longitude": ln}}}} for la, ln in destinations],
        "travelMode": travel_mode,
    }
    # No routingPreference: omitting it bills the Essentials SKU instead of
    # traffic-aware Pro — static drive context doesn't need live traffic (#4).
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "originIndex,destinationIndex,duration,condition",
    }
    try:
        r = httpx.post(_ROUTES_URL, json=body, headers=headers, timeout=15)
        r.raise_for_status()
        rows = r.json()
    except Exception as e:
        print(f"  routes api err: {e}")
        return [[None] * len(destinations) for _ in origins]

    out: list[list[int | None]] = [[None] * len(destinations) for _ in origins]
    for entry in rows:
        i = entry.get("originIndex")
        j = entry.get("destinationIndex")
        if i is None or j is None or entry.get("condition") != "ROUTE_EXISTS":
            continue
        dur = entry.get("duration", "")  # e.g. "1234s"
        if dur.endswith("s"):
            try:
                seconds = int(dur[:-1])
                out[i][j] = max(1, round(seconds / 60))
            except ValueError:
                pass
    return out


# ---------- public ----------


def is_marin(L) -> bool:
    """Mill Valley / Marin side of the bridge — north of 37.84."""
    return L.lat is not None and L.lng is not None and L.lat > 37.84


def populate_drive_for_marin(listings) -> dict[tuple[str, str], int]:
    """For Marin listings, compute DRIVE time to every SF anchor (trails,
    beaches, bakeries, SF center). Returns {(listing.key, anchor.name): minutes}.

    Walking 25+ minutes to Baker Beach is nonsensical from Mill Valley — they'd
    drive. This populates the matrix so the renderer can show drive-time rows.
    """
    marin = [L for L in listings if is_marin(L)]
    if not marin:
        return {}

    anchors = BEACHES + BAKERIES + TRAILS + SF_CENTER
    result: dict[tuple[str, str], int] = {}
    pending_origins: list[tuple[float, float]] = []
    seen_origin: set[tuple[float, float]] = set()
    for L in marin:
        for a in anchors:
            cached = _cache_get(L.lat, L.lng, a.lat, a.lng, mode="drive")
            if cached is not None:
                result[(L.key, a.name)] = cached
        key = (_rnd(L.lat), _rnd(L.lng))
        if key not in seen_origin:
            seen_origin.add(key)
            # Only enqueue if anything is missing for this origin.
            if any(_cache_get(L.lat, L.lng, a.lat, a.lng, mode="drive") is None for a in anchors):
                pending_origins.append((L.lat, L.lng))

    if pending_origins:
        destinations = [(a.lat, a.lng) for a in anchors]
        label = "routes api (drive)" if _routes_api_enabled() else "routes fallback (drive)"
        print(f"  {label}: {len(pending_origins)} origins × {len(destinations)} anchors")
        for chunk_start in range(0, len(pending_origins), 25):
            chunk = pending_origins[chunk_start:chunk_start + 25]
            matrix = _call_routes_api(chunk, destinations, mode="drive")
            for i, (fl, fn) in enumerate(chunk):
                for j, (tl, tn) in enumerate(destinations):
                    mins = matrix[i][j]
                    if mins is None:
                        mins = _haversine_drive_minutes(fl, fn, anchors[j])
                        _cache_put(fl, fn, tl, tn, mins, "haversine", mode="drive")
                        continue
                    _cache_put(fl, fn, tl, tn, mins, "api", mode="drive")

    # Re-read into result.
    for L in marin:
        for a in anchors:
            m = _cache_get(L.lat, L.lng, a.lat, a.lng, mode="drive")
            if m is not None:
                result[(L.key, a.name)] = m
    return result


def populate_drive_for_bakeries(listings):
    """Kept for backwards compatibility — calls populate_drive_for_marin and
    returns the nearest-bakery summary. New callers should use
    populate_drive_for_marin directly so they get all anchors.
    """
    drive_map = populate_drive_for_marin(listings)
    out: dict[str, tuple[Anchor, int]] = {}
    for L in listings:
        if not is_marin(L):
            continue
        best: tuple[Anchor, int] | None = None
        for a in BAKERIES:
            m = drive_map.get((L.key, a.name))
            if m is None:
                continue
            if best is None or m < best[1]:
                best = (a, m)
        if best:
            out[L.key] = best
    return out


def populate_for(listings) -> dict[tuple[str, str], int]:
    """Ensure every (listing, anchor) pair has a cached minutes value.

    Returns a dict keyed by (listing.key, anchor.name) → minutes.
    """
    anchors = BEACHES + BAKERIES + PRESIDIO_GATES
    result: dict[tuple[str, str], int] = {}
    pending_origins: list[tuple[float, float]] = []
    pending_pairs: list[tuple[str, str, float, float, float, float]] = []
    # ↑ (listing_key, anchor_name, fl, fn, tl, tn)

    for L in listings:
        if L.lat is None or L.lng is None:
            continue
        for a in anchors:
            cached = _cache_get(L.lat, L.lng, a.lat, a.lng)
            if cached is not None:
                result[(L.key, a.name)] = cached
            else:
                pending_pairs.append((L.key, a.name, L.lat, L.lng, a.lat, a.lng))

    if not pending_pairs:
        return result

    # Batch by unique origin. Routes API allows up to 25 origins × 25 destinations per call,
    # and elements ≤ 625. Our destination set is fixed at 5, so we can batch 25 origins at once.
    unique_origins: list[tuple[float, float]] = []
    seen = set()
    for _, _, fl, fn, _, _ in pending_pairs:
        key = (_rnd(fl), _rnd(fn))
        if key not in seen:
            seen.add(key)
            unique_origins.append((fl, fn))

    destinations = [(a.lat, a.lng) for a in anchors]
    label = "routes api" if _routes_api_enabled() else "routes fallback"
    print(f"  {label}: {len(unique_origins)} origins × {len(destinations)} anchors")
    for chunk_start in range(0, len(unique_origins), 25):
        chunk = unique_origins[chunk_start:chunk_start + 25]
        matrix = _call_routes_api(chunk, destinations)
        for i, (fl, fn) in enumerate(chunk):
            for j, (tl, tn) in enumerate(destinations):
                mins = matrix[i][j]
                if mins is None:
                    # Fall back to haversine for this pair.
                    mins = _haversine_minutes(fl, fn, anchors[j])
                    _cache_put(fl, fn, tl, tn, mins, "haversine")
                else:
                    _cache_put(fl, fn, tl, tn, mins, "api")

    # Re-lookup to fill `result` for the now-cached pairs.
    for lk, an, fl, fn, tl, tn in pending_pairs:
        m = _cache_get(fl, fn, tl, tn)
        if m is not None:
            result[(lk, an)] = m
    return result


def minutes_to(walk_map: dict[tuple[str, str], int], listing_key: str, anchor: Anchor) -> int | None:
    return walk_map.get((listing_key, anchor.name))


def nearest(walk_map: dict[tuple[str, str], int], listing_key: str, anchors: list[Anchor]) -> tuple[Anchor, int] | None:
    best: tuple[Anchor, int] | None = None
    for a in anchors:
        m = walk_map.get((listing_key, a.name))
        if m is None:
            continue
        if best is None or m < best[1]:
            best = (a, m)
    return best
