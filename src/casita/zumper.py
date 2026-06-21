"""Zumper scraper.

Zumper hydrates the search results into `window.__PRELOADED_STATE__`
under `currentSearch.listables.listables` — same shape-trick as Zillow's
`__NEXT_DATA__` but stashed on the window instead of a script tag. We
pull the array directly via `page.evaluate` and skip DOM parsing.

URL filters (`bed=2,3&dogs=true`) get partially stripped
on render — Zumper drops the pet param from the URL bar but the API
still returns broader results than the filters imply (studios, 1BRs). We
re-filter client-side from `min_bedrooms` and the returned structured fields.
"""
import re
from typing import Any

from playwright.async_api import BrowserContext

from .browser import UA
from .geo import resolve_neighborhood
from .locations import MARIN_CITY_SLUGS, SF_NEIGHBORHOOD_SLUGS
from .models import Listing

# Zumper stamps "BR/BA" titles for unnamed buildings — those add no signal
# over the address, so we swap them out below.
_TITLE_BR_RE = re.compile(r"^\d+\s*BR\s*/\s*\d+(?:\.\d+)?\s*BA", re.IGNORECASE)

NEIGHBORHOODS = {
    "inner-richmond":   "https://www.zumper.com/apartments-for-rent/san-francisco-ca/inner-richmond?bed=2,3&dogs=true",
    "outer-richmond":   "https://www.zumper.com/apartments-for-rent/san-francisco-ca/outer-richmond?bed=2,3&dogs=true",
    "inner-sunset":     "https://www.zumper.com/apartments-for-rent/san-francisco-ca/inner-sunset?bed=2,3&dogs=true",
    "outer-sunset":     "https://www.zumper.com/apartments-for-rent/san-francisco-ca/outer-sunset?bed=2,3&dogs=true",
    "lake-street":      "https://www.zumper.com/apartments-for-rent/san-francisco-ca/lake-street?bed=2,3&dogs=true",
    "presidio-heights": "https://www.zumper.com/apartments-for-rent/san-francisco-ca/presidio-heights?bed=2,3&dogs=true",
    "mill-valley":      "https://www.zumper.com/apartments-for-rent/mill-valley-ca?bed=2,3&dogs=true",
    "sausalito":        "https://www.zumper.com/apartments-for-rent/sausalito-ca?bed=2,3&dogs=true",
}


def _validate_neighborhoods() -> None:
    expected = set(SF_NEIGHBORHOOD_SLUGS) | set(MARIN_CITY_SLUGS)
    actual = set(NEIGHBORHOODS)
    if actual != expected:
        missing = ", ".join(sorted(expected - actual)) or "none"
        extra = ", ".join(sorted(actual - expected)) or "none"
        raise RuntimeError(f"zumper neighborhood config drift: missing={missing}; extra={extra}")

def _img_url(image_id: Any) -> str | None:
    if not image_id:
        return None
    # 1280x960 source, same shape the page itself emits via srcset.
    return f"https://img.zumpercdn.com/{image_id}/1280x960"


def _record_to_listing(rec: dict, neighborhood: str) -> Listing | None:
    lid = rec.get("listing_id")
    if not lid:
        return None
    rel_url = rec.get("url") or ""
    if not rel_url:
        return None
    url = rel_url if rel_url.startswith("http") else "https://www.zumper.com" + rel_url

    price = rec.get("min_price") or rec.get("max_price")
    beds = rec.get("min_bedrooms")
    baths = rec.get("min_bathrooms") or rec.get("min_all_bathrooms")
    sqft_raw = rec.get("min_square_feet")
    sqft = (
        int(sqft_raw)
        if isinstance(sqft_raw, (int, float)) and 0 < sqft_raw < 100000
        else None
    )
    lat = rec.get("lat")
    lng = rec.get("lng")
    resolved = resolve_neighborhood(lat, lng) if lat and lng else None

    # Title: prefer the human one, fall back to address.
    title = rec.get("title") or rec.get("building_name") or rec.get("address")
    # Strip leading "2BR/2.5BA" stubs that Zumper generates for unnamed buildings.
    if title and _TITLE_BR_RE.match(title):
        title = rec.get("building_name") or rec.get("address") or title

    address = rec.get("address")
    if address and rec.get("city"):
        address = f"{address}, {rec['city']}, {rec.get('state') or ''}".strip(", ")

    image_ids = rec.get("image_ids") or []
    photos = [u for u in (_img_url(i) for i in image_ids[:8]) if u]
    image_url = photos[0] if photos else None

    return Listing(
        source="zumper",
        source_id=str(lid),
        url=url,
        title=title,
        address=address,
        neighborhood=neighborhood,
        neighborhood_resolved=resolved,
        price=int(price) if price else None,
        beds=float(beds) if beds is not None else None,
        baths=float(baths) if baths is not None else None,
        sqft=sqft,
        image_url=image_url,
        photos=photos,
        pets_allowed=True,  # we searched dogs=true; enrich resolves dog_policy from detail page
        lat=lat,
        lng=lng,
        raw=rec,
    )


async def _extract_listables(page) -> list[dict]:
    """Pull primary + nearby-match listables off window.__PRELOADED_STATE__.

    Each entry is wrapped as `{_data: {...}}` in the live page (some kind of
    MobX/lazy unwrap that flattens during JSON.stringify in dev probes but
    survives a direct return). We unwrap here so callers see the flat shape.
    """
    return await page.evaluate(
        """() => {
            const s = window.__PRELOADED_STATE__;
            if (!s || !s.currentSearch || !s.currentSearch.listables) return [];
            const cs = s.currentSearch.listables;
            const primary = cs.listables || [];
            const nearby = (cs.nearby && cs.nearby.matches && cs.nearby.matches.listables) || [];
            return primary.concat(nearby).map(x => (x && x._data) ? x._data : x);
        }"""
    )


async def scrape(ctx: BrowserContext, neighborhood: str, url: str) -> list[Listing]:
    page = await ctx.new_page()
    try:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            return []
        # Give React a beat to hydrate __PRELOADED_STATE__.
        await page.wait_for_timeout(1500)
        try:
            records = await _extract_listables(page)
        except Exception:
            return []
        listings: list[Listing] = []
        seen: set[str] = set()
        for rec in records:
            try:
                L = _record_to_listing(rec, neighborhood)
            except Exception as e:
                print(f"  zumper card err: {e}")
                continue
            if not L or L.source_id in seen:
                continue
            seen.add(L.source_id)
            listings.append(L)
        return listings
    finally:
        await page.close()


async def scrape_all(ctx: BrowserContext) -> list[Listing]:
    _validate_neighborhoods()
    seen: dict[str, Listing] = {}
    for neighborhood, url in NEIGHBORHOODS.items():
        try:
            results = await scrape(ctx, neighborhood, url)
            print(f"  zumper/{neighborhood}: {len(results)} listings")
            for L in results:
                # Earlier neighborhood wins — the first hood we found it in is
                # the canonical search context; nearby-match overlap is noise.
                seen.setdefault(L.key, L)
        except Exception as e:
            print(f"  zumper/{neighborhood}: ERROR {e}")
    return list(seen.values())


_PETS_RE = re.compile(r'"pets"\s*:\s*\[([^\]]*)\]')
_DOG_POLICY_RE = re.compile(r'Dog Policy[\s\S]{0,80}?(Not allowed|Allowed|Cats only|Small dogs|Large dogs)', re.IGNORECASE)
_CAT_POLICY_RE = re.compile(r'Cat Policy[\s\S]{0,80}?(Not allowed|Allowed)', re.IGNORECASE)


def _parse_detail_html(html: str, listing: Listing) -> None:
    """Pull dog_policy + pets_allowed from Zumper's detail page.

    Zumper's structured 'Pets' section uses Dog Policy / Cat Policy with
    canonical strings. The preloaded `pets: []` array also indicates
    no-pets when empty. Prefer the visible text — it's authoritative.
    """
    m = _DOG_POLICY_RE.search(html)
    if m:
        verdict = m.group(1).lower()
        if "not allowed" in verdict:
            listing.dog_policy = "no_dogs"
            listing.pets_allowed = False
        elif "small dogs" in verdict:
            listing.dog_policy = "small_only"
            listing.pets_allowed = False
        elif "large dogs" in verdict:
            listing.dog_policy = "large_ok"
            listing.pets_allowed = True
        elif verdict == "allowed":
            listing.dog_policy = "dogs_ok"
            listing.pets_allowed = True
        return
    # Fallback to the preloaded state — empty array = no pets.
    m2 = _PETS_RE.search(html)
    if m2 and not m2.group(1).strip():
        listing.dog_policy = "no_dogs"
        listing.pets_allowed = False


def fetch_and_parse(listing: Listing) -> Listing:
    """Sync version using httpx — Zumper isn't bot-blocked so no browser needed.

    Used by `casita enrich`. Caches the detail HTML for re-runs.
    """
    if listing.source != "zumper" or not listing.url:
        return listing
    from . import cache as _cache
    import httpx
    html = _cache.get("zumper", listing.source_id)
    if html is None:
        try:
            r = httpx.get(
                listing.url, timeout=20, follow_redirects=True,
                headers={"User-Agent": UA},
            )
            if r.status_code != 200 or "Access to this page has been denied" in r.text[:2000]:
                return listing
            html = r.text
            _cache.put("zumper", listing.source_id, html)
        except Exception as e:
            print(f"  zumper fetch err [{listing.key}]: {e}")
            return listing
    try:
        _parse_detail_html(html, listing)
    except Exception as e:
        print(f"  zumper parse err [{listing.key}]: {e}")
    return listing
