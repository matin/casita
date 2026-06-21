"""Cross-source dedup.

Two listings are the same property if:
- both have lat/lng and they're within ~80m, AND
- bed counts match (or one is None), AND
- prices are within 15%.

Fallback when lat/lng is missing on one side: normalized street-address match.

When merging a cluster, fields are coalesced in source priority order.
Zillow has the cleanest structured fields; Craigslist often has better
dog/laundry text.
"""
import math
import re
from dataclasses import dataclass

from .models import Listing

# Lower number = higher priority for "primary listing of cluster".
SOURCE_PRIORITY = {
    "zillow": 0,
    "redfin": 1,
    "zumper": 2,
    "craigslist": 3,
    "manual": 4,
}

EARTH_M_PER_DEG_LAT = 111_320.0


def _meters_between(a_lat, a_lng, b_lat, b_lng) -> float:
    """Equirectangular approximation — fine at the ~100m scale we care about."""
    lat_avg = math.radians((a_lat + b_lat) / 2.0)
    dx = (a_lng - b_lng) * math.cos(lat_avg) * EARTH_M_PER_DEG_LAT
    dy = (a_lat - b_lat) * EARTH_M_PER_DEG_LAT
    return math.hypot(dx, dy)


_ADDR_NOISE = re.compile(
    r"\b(apt|apartment|unit|#|suite|ste|fl|floor)\b\.?\s*\S*",
    re.IGNORECASE,
)
_ABBREV = {
    "street": "st", "avenue": "ave", "av": "ave", "boulevard": "blvd",
    "road": "rd", "drive": "dr", "court": "ct", "place": "pl", "lane": "ln",
    "highway": "hwy", "terrace": "ter",
}


def _norm_addr(addr: str | None) -> str | None:
    if not addr:
        return None
    s = addr.lower().strip()
    # Strip "near X" suffixes — Craigslist cross-street noise.
    s = re.sub(r"\s+near\s+.*$", "", s)
    # Strip "San Francisco, CA …" suffix.
    s = re.sub(r",?\s*san\s+francisco.*$", "", s)
    # Strip unit / apartment markers.
    s = _ADDR_NOISE.sub("", s)
    # Collapse punctuation and whitespace.
    s = re.sub(r"[.,;]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # Normalize street-type abbreviations.
    tokens = s.split()
    tokens = [_ABBREV.get(t, t) for t in tokens]
    return " ".join(tokens) or None


@dataclass
class _Node:
    listing: Listing
    parent: int  # union-find


def _same_property(a: Listing, b: Listing) -> bool:
    # Identical key on same source — defensive, should be deduped earlier.
    if a.key == b.key:
        return True
    # Price plausibility: if both have prices and differ by >20%, treat as different.
    if a.price and b.price and abs(a.price - b.price) / max(a.price, b.price) > 0.20:
        return False
    # Bed counts: if both given and don't match, different.
    if a.beds and b.beds and abs(a.beds - b.beds) >= 1:
        return False
    # Coord-based: within 80m → same building.
    if a.lat and a.lng and b.lat and b.lng:
        if _meters_between(a.lat, a.lng, b.lat, b.lng) <= 80:
            return True
    # Address-based fallback.
    na, nb = _norm_addr(a.address), _norm_addr(b.address)
    if na and nb and na == nb:
        return True
    return False


def _merge(primary: Listing, secondary: Listing) -> Listing:
    """Field-by-field coalesce — keep primary's value if set, else fill from secondary."""
    out = primary.model_copy(deep=True)
    for field in [
        "title", "address", "neighborhood", "neighborhood_resolved",
        "price", "beds", "baths", "sqft", "pets_allowed", "dog_policy",
        "parking", "laundry", "contact_name", "contact_phone", "contact_email",
        "contact_url", "description", "image_url", "lat", "lng",
    ]:
        v_pri = getattr(out, field)
        v_sec = getattr(secondary, field)
        if v_pri in (None, "", "?"):
            setattr(out, field, v_sec)
    # Stash the alternate URL so the page can show "also on $source".
    others = out.raw.setdefault("also_on", [])
    others.append({"source": secondary.source, "url": secondary.url})
    return out


def deduplicate_db(conn) -> int:
    """Cross-source dedup across the whole active set in the DB.

    Unlike `dedupe()`, which merges in-memory listings, this finds clusters
    across active listings already persisted and marks secondaries INACTIVE.
    Primary picked by SOURCE_PRIORITY. Returns the number of listings
    deactivated. Conversations and listing_status pointers are migrated to
    the primary so we don't lose CRM context.
    """
    from . import storage
    listings = storage.active_listings(conn)
    clusters_in = listings
    n = len(clusters_in)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj: parent[ri] = rj

    for i in range(n):
        for j in range(i + 1, n):
            if _same_property(clusters_in[i], clusters_in[j]):
                union(i, j)

    clusters: dict[int, list[Listing]] = {}
    for i, L in enumerate(clusters_in):
        clusters.setdefault(find(i), []).append(L)

    import json as _json
    deactivated = 0
    for cluster in clusters.values():
        if len(cluster) < 2:
            continue
        cluster.sort(key=lambda L: SOURCE_PRIORITY.get(L.source, 99))
        primary = cluster[0]
        # Pull primary's raw blob so we can merge `also_on` into it. The DB
        # is the source of truth, not `primary.raw` (which was loaded before
        # we may have run dedup in a prior publish — `also_on` is cumulative).
        prim_row = conn.execute(
            "SELECT raw_json FROM listings WHERE key=?", (primary.key,)
        ).fetchone()
        prim_raw = _json.loads(prim_row[0]) if (prim_row and prim_row[0]) else {}
        also_on = prim_raw.get("also_on", [])
        seen_urls = {a.get("url") for a in also_on if a.get("url")}
        for sec in cluster[1:]:
            if sec.url and sec.url not in seen_urls:
                also_on.append({"source": sec.source, "url": sec.url})
                seen_urls.add(sec.url)
            conn.execute(
                "UPDATE interactions SET listing_key=? WHERE listing_key=?",
                (primary.key, sec.key),
            )
            conn.execute(
                "UPDATE OR REPLACE listing_status SET listing_key=? WHERE listing_key=?",
                (primary.key, sec.key),
            )
            conn.execute(
                "UPDATE attachments SET listing_key=? WHERE listing_key=?",
                (primary.key, sec.key),
            )
            conn.execute("UPDATE listings SET active=0 WHERE key=?", (sec.key,))
            deactivated += 1
        prim_raw["also_on"] = also_on
        conn.execute(
            "UPDATE listings SET raw_json=? WHERE key=?",
            (_json.dumps(prim_raw, default=str), primary.key),
        )
    conn.commit()
    return deactivated


def dedupe(listings: list[Listing]) -> list[Listing]:
    """Cluster listings, return one merged listing per cluster.

    O(n²) — fine at n<500. If we ever scrape thousands, swap in a spatial index.
    """
    n = len(listings)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    for i in range(n):
        for j in range(i + 1, n):
            if _same_property(listings[i], listings[j]):
                union(i, j)

    clusters: dict[int, list[Listing]] = {}
    for i, L in enumerate(listings):
        clusters.setdefault(find(i), []).append(L)

    merged: list[Listing] = []
    for cluster in clusters.values():
        # Sort by source priority — primary first.
        cluster.sort(key=lambda L: SOURCE_PRIORITY.get(L.source, 99))
        primary = cluster[0]
        for sec in cluster[1:]:
            primary = _merge(primary, sec)
        merged.append(primary)
    return merged
