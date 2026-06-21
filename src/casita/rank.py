"""Rank listings by fit.

Heuristic baseline. Higher score = better.

Inputs are weighted in line with stated priorities, in priority order:
  - Dogs OK (large or any-size) — gate; no-dogs heavily penalized
  - Walk-to-Presidio (trail access) — primary
  - Walk-to-beach — secondary
  - 3 bedrooms preferred, ≥ 1.5 baths preferred
  - In-unit laundry > shared > hookups
  - Garage parking > street > none

Walking times come from the `walk_map` populated by walk.populate_for().
When None, score is computed without those terms.
"""
from .models import Listing


def _hood_fallback_bonus(listing: Listing) -> int:
    """Small extra credit for target SF neighborhoods."""
    hood = (listing.hood or "").lower()
    if any(h in hood for h in ["inner richmond", "lake street", "presidio heights"]):
        return 6
    if "inner sunset" in hood:
        return 5
    if "presidio" in hood:
        return 4
    if any(h in hood for h in ["central richmond", "central sunset", "outer richmond"]):
        return 2
    if "outer sunset" in hood or "parkside" in hood:
        return 1
    return 0


def _walk_bonus(minutes: int | None, *, sweet_spot: int) -> int:
    """Sigmoid-ish: a 5-min walk should clearly beat 20-min, but 20 vs 25 is noise."""
    if minutes is None:
        return 0
    if minutes <= sweet_spot:
        return 15
    if minutes <= sweet_spot + 5:
        return 10
    if minutes <= sweet_spot + 10:
        return 5
    if minutes <= sweet_spot + 20:
        return 1
    return -3


def score(listing: Listing, walk_map: dict | None = None) -> int:
    s = 0

    # Dog policy — gate.
    if listing.dog_policy == "no_dogs" or listing.pets_allowed is False:
        return -1000
    if listing.dog_policy == "small_only":
        s -= 30  # not a hard gate, but large dogs need negotiation.
    if listing.dog_policy == "large_ok":
        s += 12
    elif listing.dog_policy == "dogs_ok":
        s += 6

    # Walk times — Presidio is primary.
    if walk_map is not None:
        # Use minimum of presidio gates / beaches as the listing's value.
        from .walk import BEACHES, PRESIDIO_GATES, nearest
        np = nearest(walk_map, listing.key, PRESIDIO_GATES)
        nb = nearest(walk_map, listing.key, BEACHES)
        if np:
            s += _walk_bonus(np[1], sweet_spot=10) * 2  # weighted ×2 — stated priority
        if nb:
            s += _walk_bonus(nb[1], sweet_spot=10)

    s += _hood_fallback_bonus(listing)

    # Size / config.
    if listing.beds and listing.beds >= 3:
        s += 4
    if listing.baths and listing.baths >= 1.5:
        s += 5

    # Laundry.
    if listing.laundry == "in-unit":
        s += 3
    elif listing.laundry == "shared (in building)":
        s += 1
    elif listing.laundry in ("hookups only", "none"):
        s -= 2

    # Parking.
    if listing.parking and "no parking" not in (listing.parking or "").lower() and listing.parking != "none":
        s += 2
    if listing.parking and ("garage" in listing.parking.lower()):
        s += 2

    return s


ELIMINATED_STATUSES = frozenset({"declined_by_landlord", "declined_by_us", "passed_on"})

# Active CRM pipeline — the listings we're actually pursuing. Higher strength =
# further along; orders within the pipeline bucket after vote weight.
PIPELINE_STRENGTH = {
    "applied": 5,
    "viewing_done": 4,
    "viewing_scheduled": 3,
    "shortlist": 2,
    "contacted": 1,
}


def rank(
    listings: list[Listing],
    walk_map: dict | None = None,
    status_map: dict[str, str] | None = None,
    vote_scores: dict[str, int] | None = None,
) -> list[Listing]:
    """Sort order — six buckets:
     -2. Active pipeline — a live CRM status (contacted → viewing → applied):
         the real to-do list, above everything. Within: more up-voters first,
         then further-along status, then llm_rank.
     -1. Favorites — net-upvoted (and not in pipeline/eliminated). An explicit
         human "yes" beats the ranker. Within: more up-voters first, then rank.
      0. Ranked + not filtered (severity ok / concerns), by llm_rank ascending
      1. New listings without an llm_rank yet (don't punish for being unranked)
      2. Filtered listings (severity=filtered)
      3. Eliminated — landlord-declined / we-passed / out-of-area, at the bottom

    Eliminated is soft-delete: we keep them visible at the end so we don't lose
    track of past leads. An eliminated listing stays down even if it was once
    up-voted or in the pipeline — the explicit pass is the newer, stronger
    signal. Within each bucket, ties break on heuristic score.
    """
    status_map = status_map or {}
    vote_scores = vote_scores or {}
    def sort_key(L: Listing) -> tuple:
        net = vote_scores.get(L.key, 0)
        status = status_map.get(L.key)
        strength = PIPELINE_STRENGTH.get(status, 0)
        if status in ELIMINATED_STATUSES:
            bucket = 3
        elif strength:
            bucket = -2
        elif net > 0:
            bucket = -1
        elif L.llm_severity == "filtered" or (L.llm_rank or 0) >= 9000:
            bucket = 2
        elif L.llm_rank is None:
            bucket = 1
        else:
            bucket = 0
        return (bucket, -net, -strength, L.llm_rank or 0, -score(L, walk_map))
    return sorted(listings, key=sort_key)
