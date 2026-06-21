"""Canonical rental-search locations.

The source modules keep their own URL dictionaries on purpose: each site has
different URL shapes, and that duplication is useful candidate surface area.
This module only names the location set the personal tool cares about.
"""

SF_NEIGHBORHOOD_SLUGS = (
    "inner-richmond",
    "outer-richmond",
    "inner-sunset",
    "outer-sunset",
    "lake-street",
    "presidio-heights",
)

SF_NEIGHBORHOOD_NAMES = (
    "Inner Richmond",
    "Outer Richmond",
    "Inner Sunset",
    "Outer Sunset",
    "Lake Street",
    "Presidio Heights",
)

MARIN_CITY_SLUGS = (
    "mill-valley",
    "sausalito",
)

MARIN_CITY_NAMES = (
    "Mill Valley",
    "Sausalito",
)

SF_SEARCH_TERMS = (
    "inner richmond",
    "outer richmond",
    "inner sunset",
    "outer sunset",
    "richmond",
    "sunset",
    "lake st",
    "lake street",
    "presidio",
    "seacliff",
    "sea cliff",
    "laurel",
    "jordan park",
)

MARIN_SEARCH_TERMS = (
    "mill valley",
    "tam valley",
    "homestead valley",
    "almonte",
    "sausalito",
)
