"""Redfin scraper.

Redfin sits behind PerimeterX (same wall as Zillow), so we drive the shared
persistent Playwright profile (`.chrome-profile`) — a captcha cleared once via
`casita solve`-style flow sticks across runs. The rental search server-renders
the listing cards into the page (the data is NOT in an XHR), so we parse the
`bp-Homecard` DOM directly, the way craigslist/zillow do.

Search is city/zip-scoped rather than neighborhood-scoped (neighborhood URLs
need region ids that drift), so we run a city-wide SF search plus a Mill Valley
zip search. Cards expose the street address + zip but not lat/lng, so the hood
stays coarse (`san-francisco` / `mill-valley`); walk.py geocodes the address
downstream for the fine-grained location.
"""
import json
import re

from playwright.async_api import BrowserContext

from .geo import resolve_neighborhood
from .models import Listing

# Redfin rental search URLs. Filters live in the `/filter/` path segment:
# 2-3 beds, dog-friendly. City id 17151 = San Francisco.
SEARCHES = [
    (
        "san-francisco",
        "https://www.redfin.com/city/17151/CA/San-Francisco/apartments-for-rent/"
        "filter/min-beds=2,max-beds=3,pets-allowed=dogs",
    ),
    (
        "mill-valley",
        "https://www.redfin.com/zipcode/94941/apartments-for-rent/"
        "filter/min-beds=2,max-beds=3,pets-allowed=dogs",
    ),
    (
        "sausalito",
        "https://www.redfin.com/zipcode/94965/apartments-for-rent/"
        "filter/min-beds=2,max-beds=3,pets-allowed=dogs",
    ),
]

# Card DOM extractor — one JS pass returns the rental cards plus the page's
# schema.org JSON-LD blocks. The search renders both map-pane and list-pane
# cards (`.bp-Homecard`); we dedupe by listing url in python. The DOM has
# baths + image; the JSON-LD `Accommodation` blocks carry lat/lng + a clean
# structured street address, joined back to each card by url.
_EXTRACT_JS = """() => {
    const cards = Array.from(document.querySelectorAll('.bp-Homecard'));
    const txt = (el, sel) => {
        const n = el.querySelector(sel);
        return n ? n.textContent.trim() : null;
    };
    const out = cards.map(el => {
        const a = el.querySelector('a.bp-Homecard__Address');
        const href = a ? a.getAttribute('href') : null;
        const addrFull = a ? a.textContent.replace(/\\u00a0/g, ' ').trim() : null;
        const img = el.querySelector('img.bp-Homecard__Photo--image');
        return {
            href,
            address: addrFull,
            price: txt(el, '.bp-Homecard__Price--value'),
            beds: txt(el, '.bp-Homecard__Stats--beds'),
            baths: txt(el, '.bp-Homecard__Stats--baths'),
            sqft: txt(el, '.bp-Homecard__Stats--sqft'),
            image_url: img ? img.getAttribute('src') : null,
        };
    }).filter(c => c.href);
    const ld = Array.from(
        document.querySelectorAll('script[type="application/ld+json"]')
    ).map(s => s.textContent);
    return {cards: out, ld};
}"""


def _url_path(url: str) -> str:
    """Path portion of a redfin url, for joining relative hrefs to absolute
    JSON-LD urls."""
    u = url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    return re.sub(r"^https?://[^/]+", "", u)


def _geo_index(ld_blocks: list[str]) -> dict[str, dict]:
    """Build {url_path: {lat, lng, street}} from schema.org Accommodation
    JSON-LD blocks. These carry the authoritative coordinates + a clean
    structured street address."""
    def _f(v) -> float | None:
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    idx: dict[str, dict] = {}
    for block in ld_blocks:
        if not block or "GeoCoordinates" not in block:
            continue
        try:
            data = json.loads(block)
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        # Flatten any @graph nesting schema.org sometimes uses.
        flat: list = []
        for it in items:
            if isinstance(it, dict) and isinstance(it.get("@graph"), list):
                flat.extend(it["@graph"])
            else:
                flat.append(it)
        for it in flat:
            geo = it.get("geo") if isinstance(it, dict) else None
            url = it.get("url") if isinstance(it, dict) else None
            if not (isinstance(geo, dict) and url):
                continue
            lat, lng = _f(geo.get("latitude")), _f(geo.get("longitude"))
            if lat is None or lng is None:
                continue
            addr = it.get("address") or {}
            street = addr.get("streetAddress") if isinstance(addr, dict) else None
            locality = addr.get("addressLocality") if isinstance(addr, dict) else None
            full = ", ".join(p for p in (street, locality) if p) or None
            idx[_url_path(url)] = {"lat": lat, "lng": lng, "street": full}
    return idx


def _source_id_from_url(url: str) -> str:
    """Redfin listing URLs end in `/home/<id>`, `/apartment/<id>`, or
    `/rental/<guid>`. The trailing id is the stable handle."""
    if not url:
        return ""
    clean = url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    m = re.search(r"(\d{5,})$", clean)
    if m:
        return m.group(1)
    parts = [p for p in clean.split("/") if p]
    return parts[-1] if parts else url


def _first_num(s: str | None) -> float | None:
    """First number in a string, tolerating commas / ranges ('874-1,483')."""
    if not s:
        return None
    m = re.search(r"\d[\d,]*(?:\.\d+)?", s)
    return float(m.group(0).replace(",", "")) if m else None


def _record_to_listing(rec: dict, area: str, geo_idx: dict[str, dict]) -> Listing | None:
    href = rec.get("href") or ""
    if not href:
        return None
    url = href if href.startswith("http") else "https://www.redfin.com" + href
    source_id = _source_id_from_url(url)
    if not source_id:
        return None

    price = _first_num(rec.get("price"))
    # A real rental card always shows a $/mo. Price-less cards are the
    # "nearby" sale/off-market homes Redfin pads zip searches with — drop them.
    if not price:
        return None
    beds = _first_num(rec.get("beds"))  # "2 beds" / "Studio" -> None
    baths = _first_num(rec.get("baths"))
    sqft_v = _first_num(rec.get("sqft"))  # "874-1,483 sq ft" -> low end
    image_url = rec.get("image_url")

    geo = geo_idx.get(_url_path(url), {})
    lat, lng = geo.get("lat"), geo.get("lng")
    # Prefer the JSON-LD structured street address (clean "39 Bruton St, San
    # Francisco"); fall back to the DOM card text, stripping the "Building |"
    # prefix buildings carry.
    addr = geo.get("street")
    if not addr:
        addr = rec.get("address")
        if addr and "|" in addr:
            addr = addr.split("|", 1)[1].strip()
    resolved = resolve_neighborhood(lat, lng) if lat and lng else None

    return Listing(
        source="redfin",
        source_id=source_id,
        url=url,
        title=rec.get("address"),  # full "Building | street" reads well as a title
        address=addr,
        neighborhood=area,
        neighborhood_resolved=resolved,
        price=int(price) if price else None,
        beds=beds,
        baths=baths,
        sqft=int(sqft_v) if sqft_v else None,
        image_url=image_url,
        photos=[image_url] if image_url else [],
        pets_allowed=True,  # we filtered for pets-allowed=dogs
        lat=lat,
        lng=lng,
        raw=rec,
    )


async def scrape(ctx: BrowserContext, area: str, url: str) -> list[Listing]:
    page = await ctx.new_page()
    try:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            return []
        # Wait for the server-rendered cards. If PerimeterX intercepts, this
        # times out and we return [] — `casita solve`-cleared cookies prevent
        # that on warm runs.
        try:
            await page.wait_for_selector(".bp-Homecard", timeout=20000)
        except Exception:
            return []
        await page.wait_for_timeout(800)
        try:
            payload = await page.evaluate(_EXTRACT_JS)
        except Exception:
            return []
        records = payload.get("cards", [])
        geo_idx = _geo_index(payload.get("ld", []))
        listings: list[Listing] = []
        seen: set[str] = set()
        for rec in records:
            try:
                L = _record_to_listing(rec, area, geo_idx)
            except Exception as e:
                print(f"  redfin card err: {e}")
                continue
            if not L or L.source_id in seen:
                continue
            seen.add(L.source_id)
            listings.append(L)
        return listings
    finally:
        await page.close()


async def scrape_all(ctx: BrowserContext) -> list[Listing]:
    seen: dict[str, Listing] = {}
    for area, url in SEARCHES:
        try:
            results = await scrape(ctx, area, url)
            print(f"  redfin/{area}: {len(results)} listings")
            for L in results:
                seen.setdefault(L.key, L)
        except Exception as e:
            print(f"  redfin/{area}: ERROR {e}")
    return list(seen.values())
