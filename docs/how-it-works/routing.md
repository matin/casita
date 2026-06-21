---
icon: lucide/map
---

# Routing

Routing lives in `src/casita/walk.py`.

Casita computes walking or driving minutes from each listing to curated anchors:
beaches, trailheads, bakeries, and the Ferry Building. For SF listings the
times are walking times. For Marin listings the times are drive times, because
walking to SF anchors from Mill Valley or Sausalito would distort the ranking.

The core functions are:

- `populate_for` for walking times to SF anchors
- `populate_drive_for_marin` for drive times from Marin listings
- `_call_routes_api` for Google Routes `computeRouteMatrix`
- `_cache_get` / `_cache_put` for the SQLite route cache

Route rows are cached by rounded coordinates in a `walk_cache` table. The demo
points the route cache at the fixture copy, so it renders from committed cached
rows instead of calling the paid API.

!!! warning "Google Maps cost"

    Live route calculations use the paid Google Maps Routes API when
    `GOOGLE_MAPS_API_KEY` is set. The demo path is free because it renders from
    cached `walk_cache` rows in the fixture. Set `CASITA_ROUTES_OFFLINE=1` to
    force cached or haversine fallback behavior even when a Maps key exists.

## Ways This Could Go Further

Routing is one of the richest parts of the system. A future version could make
anchor sets easier to inspect, explain why a route matters on the card, or make
cache behavior more testable without changing the personal assumptions.
