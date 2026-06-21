"""Zillow scraper.

Search results: parse `__NEXT_DATA__` JSON from the search page (clean,
structured, doesn't need a real browser render).

Detail enrichment: Zillow embeds the facts as a flat list of "Key: Value"
items inside `<ul class*=Fact> <li>`. We pull the list raw and key off it
instead of regex-grepping the rendered body — that's how we get reliable
'Pets allowed: No', 'Laundry: Shared', 'Parking features: Attached'.

Cache: every detail-page HTML lands in detail_cache/zillow/<zpid>.html.
Re-runs the same day skip the network entirely.
"""
import asyncio
import json
import os
import random
import re
from typing import Any

from bs4 import BeautifulSoup
from playwright.async_api import BrowserContext

# ---------- pacing (anti-PerimeterX) ----------
# Zillow trips a PerimeterX captcha when detail pages are loaded too fast or
# in parallel (issue #8). We serialize ALL Zillow page loads through one lock
# regardless of the caller's --concurrency, and space them out with a
# randomized delay. Other sources are unaffected — this lives entirely here.
_ZILLOW_DELAY = float(os.environ.get("CASITA_ZILLOW_DELAY", "5.0"))   # base seconds between detail loads
_ZILLOW_JITTER = float(os.environ.get("CASITA_ZILLOW_JITTER", "4.0")) # added random 0..jitter
_NEIGHBORHOOD_DELAY = 3.0   # base gap between the 7 search-result pages

_fetch_lock: asyncio.Lock | None = None
_captcha_hits = 0


def _get_lock() -> asyncio.Lock:
    # Lazily created so it binds to the running loop, not import-time.
    global _fetch_lock
    if _fetch_lock is None:
        _fetch_lock = asyncio.Lock()
    return _fetch_lock


async def _pace(base: float, spread: float) -> None:
    await asyncio.sleep(base + random.uniform(0.0, spread))


def _looks_blocked(html: str) -> bool:
    """A real Zillow page carries __NEXT_DATA__; a PerimeterX challenge is
    small and lacks it. Distinguish by content, not the px script reference
    (which is embedded on every normal page too)."""
    if "__NEXT_DATA__" in html:
        return False
    low = html[:4000].lower()
    return (
        len(html) < 50_000
        or "px-captcha" in low
        or "press & hold" in low
        or "access to this page has been denied" in low
    )


def captcha_hits() -> int:
    return _captcha_hits

from . import cache, dogs
from .geo import resolve_neighborhood
from .locations import MARIN_CITY_SLUGS, SF_NEIGHBORHOOD_SLUGS
from .models import Listing

NEIGHBORHOODS = {
    # San Francisco — Richmond / Sunset / Presidio-adjacent.
    "inner-richmond": "https://www.zillow.com/inner-richmond-san-francisco-ca/rentals/",
    "outer-richmond": "https://www.zillow.com/outer-richmond-san-francisco-ca/rentals/",
    "inner-sunset": "https://www.zillow.com/inner-sunset-san-francisco-ca/rentals/",
    "outer-sunset": "https://www.zillow.com/outer-sunset-san-francisco-ca/rentals/",
    "lake-street": "https://www.zillow.com/lake-st-san-francisco-ca/rentals/",
    "presidio-heights": "https://www.zillow.com/presidio-heights-san-francisco-ca/rentals/",
    # Mill Valley — Marin-side listings need strong trail access and enough
    # downtown density to work without constant driving.
    "mill-valley":   "https://www.zillow.com/mill-valley-ca/rentals/",
    # Sausalito — same Marin rationale: walkable downtown + ferry to SF and
    # Headlands / Tennessee Valley trail access.
    "sausalito":     "https://www.zillow.com/sausalito-ca/rentals/",
}


def _validate_neighborhoods() -> None:
    expected = set(SF_NEIGHBORHOOD_SLUGS) | set(MARIN_CITY_SLUGS)
    actual = set(NEIGHBORHOODS)
    if actual != expected:
        missing = ", ".join(sorted(expected - actual)) or "none"
        extra = ", ".join(sorted(actual - expected)) or "none"
        raise RuntimeError(f"zillow neighborhood config drift: missing={missing}; extra={extra}")

_PHONE_RE = re.compile(
    r"(\(\d{3}\)\s*\d{3}[\s.\-]?\d{4}|\d{3}[\s.\-]\d{3}[\s.\-]\d{4})"
)


def _parse_price(raw: Any) -> int | None:
    if isinstance(raw, (int, float)):
        return int(raw)
    if not raw:
        return None
    digits = re.sub(r"[^\d]", "", str(raw))
    return int(digits) if digits else None


def _record_to_listing(rec: dict, neighborhood: str) -> Listing | None:
    zpid = rec.get("zpid") or rec.get("id")
    if not zpid:
        return None
    info = rec.get("hdpData", {}).get("homeInfo", {}) if rec.get("hdpData") else {}
    url = rec.get("detailUrl") or info.get("hdpUrl") or ""
    if url and not url.startswith("http"):
        url = "https://www.zillow.com" + url
    price = _parse_price(rec.get("unformattedPrice") or info.get("price") or rec.get("price"))
    beds = rec.get("beds") or info.get("bedrooms")
    baths = rec.get("baths") or info.get("bathrooms")
    sqft = rec.get("area") or info.get("livingArea")
    latlng = rec.get("latLong") or {}
    lat = latlng.get("latitude")
    lng = latlng.get("longitude")
    resolved = resolve_neighborhood(lat, lng) if lat and lng else None
    return Listing(
        source="zillow",
        source_id=str(zpid),
        url=url,
        title=rec.get("statusText"),
        address=rec.get("address") or info.get("streetAddress"),
        neighborhood=neighborhood,
        neighborhood_resolved=resolved,
        price=price,
        beds=float(beds) if beds is not None else None,
        baths=float(baths) if baths is not None else None,
        sqft=int(sqft) if sqft else None,
        image_url=rec.get("imgSrc"),
        lat=lat,
        lng=lng,
        raw=rec,
    )


def _extract_results(next_data: dict) -> list[dict]:
    """Union of listResults (current page) + mapResults (everything on the map).

    Zillow caps listResults at ~22/page but mapResults has every listing the
    search would surface across all pages. Deduped on zpid.
    """
    try:
        sr = next_data["props"]["pageProps"]["searchPageState"]["cat1"]["searchResults"]
    except (KeyError, TypeError):
        return []
    out: dict[str, dict] = {}
    for r in (sr.get("listResults") or []) + (sr.get("mapResults") or []):
        zpid = r.get("zpid") or r.get("id")
        if zpid and str(zpid) not in out:
            out[str(zpid)] = r
    return list(out.values())


async def scrape(ctx: BrowserContext, neighborhood: str, url: str) -> list[Listing]:
    page = await ctx.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            await page.wait_for_selector("script#__NEXT_DATA__", state="attached", timeout=10000)
        except Exception:
            return []
        raw = await page.locator("script#__NEXT_DATA__").inner_text()
        data = json.loads(raw)
        results = _extract_results(data)
        listings: list[Listing] = []
        for rec in results:
            listing = _record_to_listing(rec, neighborhood)
            if listing:
                listings.append(listing)
        return listings
    finally:
        await page.close()


async def scrape_all(ctx: BrowserContext) -> list[Listing]:
    _validate_neighborhoods()
    out: list[Listing] = []
    for i, (neighborhood, url) in enumerate(NEIGHBORHOODS.items()):
        if i:
            # Space out the search-result pages too — back-to-back loads
            # are part of what trips PerimeterX (#8).
            await _pace(_NEIGHBORHOOD_DELAY, 3.0)
        try:
            results = await scrape(ctx, neighborhood, url)
            print(f"  zillow/{neighborhood}: {len(results)} listings")
            out.extend(results)
        except Exception as e:
            print(f"  zillow/{neighborhood}: ERROR {e}")
    return out


# ---------- detail-page enrichment ----------


def _fact_pairs(soup: BeautifulSoup) -> dict[str, str]:
    """Collect 'Key: Value' pairs from Zillow's facts-and-features list."""
    out: dict[str, str] = {}
    # Cast a wide net — the list lives inside multiple ul[class*=Fact] siblings.
    for li in soup.select("ul[class*=Fact] li"):
        text = li.get_text(" ", strip=True)
        if ":" not in text:
            continue
        k, _, v = text.partition(":")
        out[k.strip().lower()] = v.strip()
    return out


def _classify_parking(value: str) -> str:
    """Normalize Zillow's parking string for display."""
    v = value.lower()
    if "no parking" in v or v == "none":
        return "no parking"
    if "garage" in v:
        return "garage" + (" + off-street" if "off street" in v or "off-street" in v else "")
    if "attached" in v:
        return "attached garage"
    if "carport" in v:
        return "carport"
    if "off-street" in v or "off street" in v:
        return "off-street"
    if "street" in v:
        return "street"
    return value[:60]


def _classify_laundry(value: str) -> str:
    v = value.lower()
    if "in unit" in v or "in-unit" in v:
        return "in-unit"
    if "shared" in v or "common" in v or "building" in v or "on site" in v:
        return "shared (in building)"
    if "hookup" in v:
        return "hookups only"
    if v in {"no", "none"}:
        return "none"
    return value[:60]


def _classify_pets(value: str) -> tuple[str | None, bool | None]:
    """Returns (dog_policy, pets_allowed_bool)."""
    v = value.lower()
    # No / disallowed.
    if v in {"no", "none"} or "not allowed" in v or "no pets" in v:
        return "no_dogs", False
    # Large explicit.
    if re.search(r"large\s+dogs?\s+(ok|welcome|allowed)", v) or "no breed restriction" in v:
        return "large_ok", True
    # Size restricted.
    if "small dogs only" in v or re.search(r"under\s+\d+\s*(lb|lbs|pounds)", v):
        return "small_only", False
    # Generic dogs allowed.
    if "dogs" in v or v == "yes" or "pets" in v or "allowed" in v or "ok" in v:
        return "dogs_ok", True
    return None, None


# Match listing-photo URLs. Zillow's photo URL suffixes encode purpose:
#   -cc_ft_<size>.jpg  → real unit listing photos (cover-crop fixed-thumb)
#   -p_e.webp / -p_i.jpg → building photos on /apartments/ multi-unit pages
#   -h_e.jpg            → headshots / agent avatars (always drop)
# For /homedetails/ pages we strict-filter to -cc_ft_ only.
# For /apartments/ pages there are NO -cc_ft_ photos (no unit-specific shots)
# so we accept the -p_e / -p_i building photos as the best signal available.
_PHOTO_URL_HOMEDETAILS_RE = re.compile(
    r"https://photos\.zillowstatic\.com/fp/[a-z0-9]+-cc_ft_\d+\.(?:jpg|webp)"
)
_PHOTO_URL_APARTMENTS_RE = re.compile(
    r"https://photos\.zillowstatic\.com/fp/[a-z0-9]+-p_[ei]\.(?:jpg|webp)"
)
# Backwards-compat name still imported in places.
_PHOTO_URL_RE = _PHOTO_URL_HOMEDETAILS_RE

_YOUTUBE_RE = re.compile(
    r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)([a-zA-Z0-9_-]{11})"
)


def _extract_photos(html: str, max_photos: int = 8, *, url: str = "") -> list[str]:
    """Pull unique full-size photo URLs from the detail page HTML.

    For /homedetails/ pages we strict-filter to the -cc_ft_ unit photos.
    For /apartments/ multi-unit pages there are no -cc_ft_ photos — building
    -p_e / -p_i shots are the best signal we have, so use those.
    """
    # /apartments/ and /b/ are both multi-unit building landing pages — they
    # share the -p_e / -p_i photo convention. /homedetails/ pages have
    # unit-specific -cc_ft_ photos.
    is_building = "/apartments/" in url or "/b/" in url
    if is_building:
        urls = _PHOTO_URL_APARTMENTS_RE.findall(html)
    else:
        urls = _PHOTO_URL_HOMEDETAILS_RE.findall(html)
    # Group by photo-hash (the part before `-cc_ft_<size>`).
    by_hash: dict[str, list[str]] = {}
    for u in urls:
        m = re.match(r"(https://photos\.zillowstatic\.com/fp/[a-z0-9]+)-", u)
        if not m:
            continue
        key = m.group(1)
        by_hash.setdefault(key, []).append(u)
    out: list[str] = []
    for key, variants in by_hash.items():
        # Prefer 1536 jpg, else the largest webp.
        preferred = next((v for v in variants if v.endswith("1536.jpg")), None)
        if not preferred:
            preferred = sorted(variants, key=lambda v: (".jpg" not in v, len(v)))[-1]
        out.append(preferred)
        if len(out) >= max_photos:
            break
    return out


def _parse_detail_html(html: str, listing: Listing) -> None:
    """Mutate `listing` in place with what we can extract from the detail page."""
    soup = BeautifulSoup(html, "lxml")
    facts = _fact_pairs(soup)

    # Photos for the carousel.
    photos = _extract_photos(html)
    if photos:
        listing.photos = photos
        if not listing.image_url:
            listing.image_url = photos[0]

    # Parking.
    parking_val = (
        facts.get("parking features")
        or facts.get("parking")
        or facts.get("garage")
    )
    if parking_val:
        listing.parking = _classify_parking(parking_val)
    elif facts.get("has attached garage", "").lower() == "yes":
        listing.parking = "attached garage"

    # Laundry.
    laundry_val = facts.get("laundry") or facts.get("laundry features")
    if laundry_val:
        listing.laundry = _classify_laundry(laundry_val)

    # Pets / dogs.
    pets_val = facts.get("pets allowed") or facts.get("pets")
    if pets_val:
        policy, allowed = _classify_pets(pets_val)
        if policy:
            listing.dog_policy = policy
        if allowed is not None:
            listing.pets_allowed = allowed

    # Body text as fallback for things that aren't in the facts grid.
    body_text = soup.get_text(" ", strip=True)

    # If the facts grid had nothing on pets, regex-search the body.
    if not listing.dog_policy:
        policy = dogs.classify(body_text)
        if policy:
            listing.dog_policy = policy
            listing.pets_allowed = policy in ("large_ok", "dogs_ok")

    # Listing-agent name lives in body text as "Listed by …". Possibilities:
    #   "Listed by property owner" (no name)  — show "property owner"
    #   "Listed by Jane Doe" → "Jane Doe"
    #   "Listed by John Smith Avatar/MD …" → "John Smith"
    m = re.search(r"Listed by ([A-Z][^\n]+?)(?:\s+Avatar|\s+Ask a question|\s{3,}|$)", body_text)
    if m:
        listing.contact_name = m.group(1).strip()[:60]
    elif re.search(r"Listed by property owner", body_text):
        listing.contact_name = "property owner"

    # Phone rarely leaks; capture it when present.
    m = _PHONE_RE.search(body_text)
    if m:
        listing.contact_phone = m.group(0)

    listing.contact_url = listing.url


async def _fetch_detail_html(ctx: BrowserContext, url: str) -> str | None:
    # Serialize ALL Zillow detail loads regardless of the caller's
    # --concurrency, and wait a randomized beat before each one. This turns a
    # 4-wide burst into a paced single-file stream (#8).
    async with _get_lock():
        await _pace(_ZILLOW_DELAY, _ZILLOW_JITTER)
        page = await ctx.new_page()
        try:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                # Wait for the facts list to mount.
                try:
                    await page.wait_for_selector("ul[class*=Fact] li", timeout=8000)
                except Exception:
                    pass
                # Longer, randomized settle so we read like a human, not a bot.
                await page.wait_for_timeout(random.randint(1500, 2500))
                return await page.content()
            except Exception:
                return None
        finally:
            await page.close()


async def enrich(ctx: BrowserContext, listing: Listing) -> Listing:
    """Pull parking/laundry/pets/contact for a single Zillow listing.

    Uses local HTML cache when fresh — only hits the network on first see or
    when the cache is >24h old.
    """
    if listing.source != "zillow" or not listing.url:
        return listing
    html = cache.get("zillow", listing.source_id)
    if html is None:
        html = await _fetch_detail_html(ctx, listing.url)
        if html and _looks_blocked(html):
            # PerimeterX challenge — never cache a captcha page (it would
            # poison the 24h cache and starve enrich of facts). Count it so a
            # run that silently lost pages is visible (#8).
            global _captcha_hits
            _captcha_hits += 1
            print(f"  zillow captcha/block on {listing.source_id} — not caching")
            html = None
        elif html:
            cache.put("zillow", listing.source_id, html)
    if not html:
        return listing
    try:
        _parse_detail_html(html, listing)
    except Exception as e:
        print(f"  zillow parse err [{listing.source_id}]: {e}")
    return listing
