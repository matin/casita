"""Craigslist scraper.

No bot wall to speak of. Uses the SF apartment search filtered to
2-3 bedrooms and dog-friendly. Drops the neighborhood-id filter
on purpose: codes drift, and the result set is small enough to filter
on the title/hood text client-side.
"""
import re

from playwright.async_api import BrowserContext

from bs4 import BeautifulSoup

from . import cache, dogs
from .geo import resolve_neighborhood
from .locations import MARIN_SEARCH_TERMS, SF_SEARCH_TERMS
from .models import Listing

SEARCH_URLS = [
    # SF apartments — filter to Richmond/Sunset target hoods below.
    "https://sfbay.craigslist.org/search/sfc/apa"
    "?min_bedrooms=2&max_bedrooms=3"
    "&pets_dog=1",
    # North Bay (Marin) — kept for Mill Valley / Sausalito candidates.
    "https://sfbay.craigslist.org/search/nby/apa"
    "?min_bedrooms=2&max_bedrooms=3"
    "&pets_dog=1",
]

def _term_pattern(term: str) -> str:
    return r"\s+".join(re.escape(part) for part in term.split())


# Hoods we filter SF posts down to (Richmond / Sunset / Presidio cluster).
# Marin posts are kept on a different rule — see scrape() below.
SF_HOODS_RE = re.compile(
    "|".join(_term_pattern(term) for term in SF_SEARCH_TERMS),
    re.IGNORECASE,
)
# Marin: Mill Valley (+ its immediate sub-neighborhoods Tam Valley /
# Homestead Valley / Almonte) and Sausalito. Both are walkable downtowns
# with ferry access plus Headlands / Dipsea trail access. The nby search
# above already covers this geography;
# this regex is the name filter that lets those listings through.
MARIN_HOODS_RE = re.compile(
    "|".join(_term_pattern(term) for term in MARIN_SEARCH_TERMS),
    re.IGNORECASE,
)


async def _extract_card(card) -> Listing | None:
    pid = await card.get_attribute("data-pid")
    if not pid:
        return None
    title = await card.get_attribute("title")
    a = card.locator("a.posting-title").first
    href = await a.get_attribute("href") if await a.count() else None
    if not href:
        a2 = card.locator("a.main").first
        href = await a2.get_attribute("href") if await a2.count() else None
    if not href:
        return None

    async def _text(sel: str) -> str:
        loc = card.locator(sel).first
        return (await loc.inner_text()).strip() if await loc.count() else ""

    price_text = await _text(".priceinfo")
    beds_text = await _text(".post-bedrooms")
    sqft_text = await _text(".post-sqft")
    hood = await _text(".result-location")

    # First gallery image. The page lazy-loads via IntersectionObserver — page
    # scrolling happens once in scrape() before card iteration, not per-card.
    img_loc = card.locator("img").first
    image_url: str | None = None
    if await img_loc.count():
        src = await img_loc.get_attribute("src")
        if src and src.startswith("http"):
            image_url = src

    price = int(re.sub(r"[^\d]", "", price_text)) if price_text else None
    beds_m = re.search(r"(\d+)", beds_text)
    beds = float(beds_m.group(1)) if beds_m else None
    sqft_m = re.search(r"(\d+)", sqft_text)
    sqft = int(sqft_m.group(1)) if sqft_m else None

    # Title often encodes "3BR/1.5BA" or "2bd 2ba" — pull baths from there.
    baths = None
    if title:
        bm = re.search(r"(\d+(?:\.\d+)?)\s*ba", title, re.IGNORECASE)
        if bm:
            baths = float(bm.group(1))

    return Listing(
        source="craigslist",
        source_id=pid,
        url=href,
        title=title,
        price=price,
        beds=beds,
        baths=baths,
        sqft=sqft,
        neighborhood=hood or None,
        # The URL filter is pets_dog=1 — landlord checked "dogs ok". Body enrichment
        # may downgrade this to False if it finds "no large dogs" or similar.
        pets_allowed=True,
        dog_policy="dogs_ok",  # baseline from the search filter; enrichment refines.
        image_url=image_url,
        raw={"hood": hood, "title": title, "price_text": price_text},
    )


async def scrape(ctx: BrowserContext) -> list[Listing]:
    out: list[Listing] = []
    for url in SEARCH_URLS:
        out.extend(await _scrape_one(ctx, url))
    return out


async def _scrape_one(ctx: BrowserContext, url: str) -> list[Listing]:
    page = await ctx.new_page()
    out: list[Listing] = []
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            await page.wait_for_selector(".cl-search-result", timeout=15000)
        except Exception:
            print("  craigslist: no results selector")
            return []
        await page.wait_for_timeout(1500)

        # Trigger lazy-loaded images by scrolling the page to the bottom in steps.
        await page.evaluate(
            """async () => {
                const sleep = (ms) => new Promise(r => setTimeout(r, ms));
                let last = 0;
                for (let i = 0; i < 30; i++) {
                    window.scrollBy(0, 800);
                    await sleep(80);
                    if (window.scrollY === last) break;
                    last = window.scrollY;
                }
                window.scrollTo(0, 0);
            }"""
        )
        await page.wait_for_timeout(500)

        cards = await page.locator(".cl-search-result").all()
        for card in cards:
            try:
                listing = await _extract_card(card)
                if not listing:
                    continue
                # Filter to target neighborhoods. Match either hood field or title.
                # Drop already-leased/rented posts.
                if listing.title and re.search(r"\*?(leased|rented|taken)\*?", listing.title, re.IGNORECASE):
                    continue
                blob = " ".join(filter(None, [listing.neighborhood, listing.title]))
                if not (SF_HOODS_RE.search(blob) or MARIN_HOODS_RE.search(blob)):
                    continue
                out.append(listing)
            except Exception as e:
                print(f"  craigslist card err: {e}")
                continue
        print(f"  craigslist: {len(out)} matched of {len(cards)} total")
        return out
    finally:
        await page.close()


_PHONE_RE = re.compile(
    r"(\(\d{3}\)\s*\d{3}[\s.\-]?\d{4}|\d{3}[\s.\-]\d{3}[\s.\-]\d{4})"
)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _parse_attrs(soup: BeautifulSoup) -> list[str]:
    return [
        el.get_text(" ", strip=True).lower()
        for el in soup.select(".attrgroup span, .attrgroup .attr")
        if el.get_text(strip=True)
    ]


_CL_PHOTO_RE = re.compile(r"https://images\.craigslist\.org/[^\"\s>]+_600x450\.jpg")


def _extract_photos(html: str, max_photos: int = 8) -> list[str]:
    seen: list[str] = []
    for url in _CL_PHOTO_RE.findall(html):
        if url not in seen:
            seen.append(url)
        if len(seen) >= max_photos:
            break
    return seen


def _parse_detail_html(html: str, listing: Listing) -> None:
    """Mutate `listing` with whatever the cached HTML reveals."""
    soup = BeautifulSoup(html, "lxml")
    attrs = _parse_attrs(soup)

    photos = _extract_photos(html)
    if photos:
        listing.photos = photos
        if not listing.image_url or "50x50c" in (listing.image_url or "") or "300x300" in (listing.image_url or ""):
            listing.image_url = photos[0]

    # Parking.
    for a in attrs:
        if "parking" in a or "garage" in a or "carport" in a:
            listing.parking = a[:80]
            break
    # Laundry.
    for a in attrs:
        if a.startswith("w/d ") or "laundry" in a:
            listing.laundry = a[:60]
            break

    # Body text — pets policy + parking fallback + contact phone/email.
    body_el = soup.select_one("#postingbody")
    body = body_el.get_text(" ", strip=True) if body_el else ""
    if body:
        listing.description = body
        if not listing.parking:
            if re.search(r"\bgarage\b", body, re.IGNORECASE):
                listing.parking = "garage (in description)"
            elif re.search(r"no\s+parking", body, re.IGNORECASE):
                listing.parking = "no parking"
            elif re.search(r"street\s+parking", body, re.IGNORECASE):
                listing.parking = "street parking"
        if not listing.laundry:
            if re.search(r"\bw\s*/\s*d\s+in\s+unit\b|\bwasher\s*/?\s*dryer\s+in\s+unit\b|\bin-unit\s+laundry\b",
                          body, re.IGNORECASE):
                listing.laundry = "in-unit"
            elif re.search(r"\blaundry\s+(?:in\s+)?(?:building|bldg)\b|\blaundry\s+on\s+site\b",
                            body, re.IGNORECASE):
                listing.laundry = "shared (in building)"
            elif re.search(r"\bw\s*/\s*d\s+hookups?\b|\blaundry\s+hookups?\b", body, re.IGNORECASE):
                listing.laundry = "hookups only"
            elif re.search(r"\bno\s+laundry\b", body, re.IGNORECASE):
                listing.laundry = "none"
        refined = dogs.classify(body, default=listing.dog_policy)
        if refined:
            listing.dog_policy = refined
        if refined in ("no_dogs", "small_only"):
            listing.pets_allowed = False
        ph = _PHONE_RE.search(body)
        if ph:
            listing.contact_phone = ph.group(0)
        em = _EMAIL_RE.search(body)
        if em:
            listing.contact_email = em.group(0)

    # Address from the map block — skip UI-string + icon-glyph noise.
    addr_el = soup.select_one(".mapaddress")
    if addr_el:
        addr = addr_el.get_text(" ", strip=True)
        addr_clean = "".join(c for c in addr if not (0xE000 <= ord(c) <= 0xF8FF)).strip()
        if addr_clean and addr_clean.lower() not in {"google map", "view map", ""}:
            listing.address = addr_clean

    # Lat/lng from the map div.
    map_el = soup.select_one("#map")
    if map_el:
        lat = map_el.get("data-latitude")
        lng = map_el.get("data-longitude")
        if lat and lng:
            try:
                listing.lat = float(lat)
                listing.lng = float(lng)
                resolved = resolve_neighborhood(listing.lat, listing.lng)
                if resolved:
                    listing.neighborhood_resolved = resolved
            except ValueError:
                pass

    listing.contact_url = listing.url


async def _fetch_detail_html(ctx: BrowserContext, url: str) -> str | None:
    page = await ctx.new_page()
    try:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_selector("#postingbody", timeout=10000)
            return await page.content()
        except Exception:
            return None
    finally:
        await page.close()


async def enrich(ctx: BrowserContext, listing: Listing) -> Listing:
    """Pull body + structured attrs. Uses local HTML cache when fresh."""
    if listing.source != "craigslist":
        return listing
    html = cache.get("craigslist", listing.source_id)
    if html is None:
        html = await _fetch_detail_html(ctx, listing.url)
        if html:
            cache.put("craigslist", listing.source_id, html)
    if not html:
        return listing
    try:
        _parse_detail_html(html, listing)
    except Exception as e:
        print(f"  craigslist parse err [{listing.source_id}]: {e}")
    return listing
