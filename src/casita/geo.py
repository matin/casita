"""SF neighborhood lookup by lat/lng.

Bounding-box matching, ordered by priority (most-specific first). Rough
boundaries — accurate enough to disambiguate "Laurel Heights" from
"Presidio" when a Craigslist post has the hood wrong, which was the
motivating case.
"""

# (name, lat_min, lat_max, lng_min, lng_max). Order matters — first match wins.
SF_NEIGHBORHOODS: list[tuple[str, float, float, float, float]] = [
    ("Presidio",         37.786,  37.810, -122.4895, -122.4485),
    ("Sea Cliff",        37.7855, 37.792, -122.495,  -122.487),
    ("Lake Street",      37.7855, 37.788, -122.487,  -122.455),
    ("Inner Richmond",   37.776,  37.7855, -122.475, -122.455),
    ("Central Richmond", 37.770,  37.7855, -122.485, -122.475),
    ("Outer Richmond",   37.770,  37.7855, -122.512, -122.485),
    ("Presidio Heights", 37.786,  37.792, -122.455,  -122.443),
    ("Laurel Heights",   37.784,  37.792, -122.450,  -122.443),
    ("Pacific Heights",  37.788,  37.795, -122.443,  -122.428),
    ("Inner Sunset",     37.756,  37.770, -122.475,  -122.450),
    ("Golden Gate Heights", 37.755, 37.764, -122.481, -122.470),
    ("Central Sunset",   37.755,  37.770, -122.490,  -122.475),
    ("Outer Sunset",     37.747,  37.770, -122.512,  -122.490),
    ("Parkside",         37.738,  37.755, -122.508,  -122.475),
]


def resolve_neighborhood(lat: float | None, lng: float | None) -> str | None:
    if lat is None or lng is None:
        return None
    for name, lat_min, lat_max, lng_min, lng_max in SF_NEIGHBORHOODS:
        if lat_min <= lat <= lat_max and lng_min <= lng <= lng_max:
            return name
    return None
