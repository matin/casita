import asyncio
import contextlib
import os
import sqlite3
import subprocess
import urllib.parse
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import craigslist, dedup, html, llm, redfin, storage, walk, zillow, zumper
from .browser import context
from .models import Listing
from .rank import rank, score

console = Console()
PACKAGE_ROOT = Path(__file__).resolve().parent
ROOT = PACKAGE_ROOT.parent.parent if PACKAGE_ROOT.parent.name == "src" else Path.cwd()
DEMO_FIXTURE = PACKAGE_ROOT / "fixtures" / "demo.sqlite"


async def _enrich_one(sem, ctx, L: Listing, enrich_zillow: bool) -> None:
    async with sem:
        try:
            if L.source == "zillow" and enrich_zillow:
                await zillow.enrich(ctx, L)
            elif L.source == "craigslist":
                await craigslist.enrich(ctx, L)
        except Exception as e:
            print(f"  enrich err [{L.key}]: {e}")


async def _scrape(
    headless: bool, concurrency: int, enrich_zillow: bool
) -> tuple[list[Listing], list[str]]:
    """Return (ranked listings, succeeded_sources)."""
    listings: list[Listing] = []
    succeeded: list[str] = []
    async with context(headless=headless) as ctx:
        console.print("[bold]scraping zillow…[/bold]")
        z = await zillow.scrape_all(ctx)
        if z:
            succeeded.append("zillow")
        listings.extend(z)

        console.print("[bold]scraping craigslist…[/bold]")
        c = await craigslist.scrape(ctx)
        if c:
            succeeded.append("craigslist")
        listings.extend(c)

        console.print("[bold]scraping zumper…[/bold]")
        try:
            zu = await zumper.scrape_all(ctx)
            if zu:
                succeeded.append("zumper")
            listings.extend(zu)
        except Exception as e:
            console.print(f"[yellow]zumper failed: {e}[/yellow]")

        console.print("[bold]scraping redfin…[/bold]")
        try:
            rf = await redfin.scrape_all(ctx)
            if rf:
                succeeded.append("redfin")
            listings.extend(rf)
        except Exception as e:
            console.print(f"[yellow]redfin failed: {e}[/yellow]")

        # Dedupe by (source, source_id).
        seen: dict[str, Listing] = {}
        for L in listings:
            seen[L.key] = L
        listings = list(seen.values())

        console.print(f"[bold]enriching {len(listings)} listings (parallel={concurrency})…[/bold]")
        sem = asyncio.Semaphore(concurrency)
        await asyncio.gather(*(_enrich_one(sem, ctx, L, enrich_zillow) for L in listings))
        if zillow.captcha_hits():
            console.print(
                f"[yellow]zillow: {zillow.captcha_hits()} detail page(s) hit a captcha "
                f"and were skipped — re-run `casita search --headed` to backfill[/yellow]"
            )

    # Cross-source dedup: the same property often appears on Zillow + Craigslist.
    before = len(listings)
    listings = dedup.dedupe(listings)
    if before != len(listings):
        console.print(f"[bold]dedup:[/bold] {before} → {len(listings)} listings")
    return rank(listings), succeeded


def _print_table(listings: list[Listing], limit: int = 25):
    table = Table(title=f"top {min(limit, len(listings))} of {len(listings)}")
    for col in ["score", "source", "hood", "beds", "ba", "price", "parking", "title"]:
        table.add_column(col)
    for L in listings[:limit]:
        table.add_row(
            str(score(L)),
            L.source,
            (L.neighborhood or "?")[:16],
            str(L.beds or "?"),
            str(L.baths or "?"),
            f"${L.price:,}" if L.price else "?",
            (L.parking or "?")[:20],
            (L.title or L.address or "")[:50],
        )
    console.print(table)


@click.group()
def cli():
    """casita — SF rental search."""


@cli.command()
@click.option("--headless/--headed", default=True, help="Run browser headless.")
@click.option("--concurrency", default=4, help="Parallel enrichment workers.")
@click.option(
    "--enrich-zillow/--no-enrich-zillow",
    default=True,
    help="Visit Zillow detail pages to pull parking/laundry/pets. Cached locally.",
)
@click.option(
    "--refresh-cache",
    is_flag=True,
    help="Force-refetch all detail pages (default uses 24h cache).",
)
@click.option("--local", is_flag=True, help="Skip GCS sync; operate on the local DB only.")
def search(headless: bool, concurrency: int, enrich_zillow: bool, refresh_cache: bool, local: bool):
    """Scrape configured rental sources, enrich, upsert to the canonical DB.

    The scrape runs entirely outside the GCS pull→push window — only the
    upsert holds the canonical DB, so the OCC window is sub-second and a
    conflicting cloud write just triggers a cheap re-pull + re-apply.
    """
    if refresh_cache:
        import shutil
        from .cache import CACHE_DIR
        if CACHE_DIR.exists():
            shutil.rmtree(CACHE_DIR)
            console.print("[yellow]cache cleared[/yellow]")
    listings, succeeded = asyncio.run(
        _scrape(headless=headless, concurrency=concurrency, enrich_zillow=enrich_zillow)
    )

    def _apply():
        with storage.connect() as conn:
            return storage.upsert_run(conn, listings, succeeded_sources=succeeded)

    run_id, new_count = _retry_canonical(local, _apply)
    console.print(
        f"\n[green]run #{run_id}[/green]: {len(listings)} listings this run "
        f"(succeeded: {succeeded or 'NONE'}), {new_count} new · "
        f"db: {storage.db_path() if local else 'GCS canonical'}"
    )
    _print_table(listings)


@cli.command()
@click.option("--address", help="Address to match the listing (or create one if not found).")
@click.option("--key", help="Existing listing key (source:source_id) — overrides --address.")
@click.option("--direction", default="in", type=click.Choice(["in", "out", "note"]))
@click.option("--channel", default="text", help="text / email / phone / zillow / in_person")
@click.option("--sender", help="Name of the sender if known.")
@click.option("--body-file", type=click.Path(exists=True), help="Read body from file (avoids quoting hell).")
@click.argument("body", required=False)
def ingest(address: str | None, key: str | None, direction: str, channel: str,
           sender: str | None, body_file: str | None, body: str | None):
    """Ingest a message from a landlord/agent into the CRM.

    Examples:
      casita ingest --address "1758 9th Ave" --sender "Vivian Kennedy" "Thank you for your interest..."
      casita ingest --body-file ./msg.txt --address "1614 Balboa" --channel text
    """
    if body_file:
        body = Path(body_file).read_text()
    if not body:
        console.print("[red]no body — pass argument or --body-file[/red]")
        return

    with storage.connect() as conn:
        listing_key = key
        if listing_key is None and address:
            # Try fuzzy match on existing listings.
            normalized = address.lower().split(",")[0]
            row = conn.execute(
                "SELECT key FROM listings WHERE LOWER(address) LIKE ? LIMIT 1",
                (f"%{normalized}%",),
            ).fetchone()
            if row:
                listing_key = row["key"]
                console.print(f"[green]matched existing listing:[/green] {listing_key}")
            else:
                # Create a manual listing.
                from datetime import datetime as _dt
                listing_key = f"manual:{address.lower().replace(' ', '-').replace(',', '')[:40]}"
                now = _dt.utcnow()
                conn.execute(
                    "INSERT OR IGNORE INTO listings (key, source, source_id, url, address, "
                    "first_seen, last_seen, active) VALUES (?, 'manual', ?, '', ?, ?, ?, 1)",
                    (listing_key, listing_key.split(":", 1)[1], address, now, now),
                )
                console.print(f"[yellow]created manual listing:[/yellow] {listing_key}")

        # Extract structured updates with the LLM.
        console.print("[bold]gemini: extracting update from message…[/bold]")
        update = llm.extract_interaction(body, direction=direction, sender=sender)
        if update:
            console.print(f"  summary: {update.summary}")

        # Record the interaction.
        conn.execute(
            "INSERT INTO interactions (listing_key, direction, channel, sender, body, extracted_json, applied) "
            "VALUES (?, ?, ?, ?, ?, ?, 0)",
            (
                listing_key, direction, channel, sender, body,
                update.model_dump_json() if update else None,
            ),
        )

        # Apply updates onto the listing.
        if update and listing_key:
            fields = {}
            if update.price: fields["price"] = update.price
            if update.beds: fields["beds"] = update.beds
            if update.baths: fields["baths"] = update.baths
            if update.parking and not _get_field(conn, listing_key, "parking"):
                fields["parking"] = update.parking
            if update.laundry and not _get_field(conn, listing_key, "laundry"):
                fields["laundry"] = update.laundry
            if update.dog_policy:
                fields["dog_policy"] = update.dog_policy
            if update.has_yard is not None:
                fields["has_yard"] = int(update.has_yard)
            if update.yard_note:
                fields["yard_note"] = update.yard_note
            # If the landlord shared their address/phone in this message, pull
            # those onto the listing (only when not already set).
            if update.address:
                # Trust the landlord-supplied address over a Craigslist
                # cross-street description. Mark it verified so subsequent
                # scrapes can't overwrite it.
                fields["address"] = update.address
                fields["address_verified"] = 1
            # Regex-scan inbound messages for a phone number — the structured
            # extractor often misses it because it shows up as plain prose.
            if direction == "in":
                import re as _re
                phone = _re.search(r"\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}", body)
                if phone and not _get_field(conn, listing_key, "contact_phone"):
                    fields["contact_phone"] = phone.group(0)
                # If we know the sender, save it as contact_name if missing.
                if sender and not _get_field(conn, listing_key, "contact_name"):
                    fields["contact_name"] = sender

            if fields:
                sets = ", ".join(f"{k}=?" for k in fields)
                conn.execute(
                    f"UPDATE listings SET {sets} WHERE key=?",
                    (*fields.values(), listing_key),
                )
                console.print(f"  applied to listing: {list(fields.keys())}")

            # Set funnel status.
            if update.status:
                storage.set_status(conn, listing_key, update.status, viewing_at=update.viewing_at)
                console.print(f"  status: {update.status}" + (
                    f" · viewing {update.viewing_at}" if update.viewing_at else ""
                ))

        conn.commit()
        console.print(f"[green]interaction recorded for {listing_key}[/green]")


def _get_field(conn: sqlite3.Connection, key: str, field: str):
    row = conn.execute(f"SELECT {field} FROM listings WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


@cli.command()
@click.option("--limit", default=50, help="Max listings to backfill in one run.")
@click.option("--delay", default=3.0, help="Seconds to pause between page loads (anti-bot).")
@click.option("--only-mill-valley", is_flag=True, help="Restrict to Mill Valley listings.")
def backfill(limit: int, delay: float, only_mill_valley: bool):
    """Open a HEADED browser and walk through Zillow listings whose detail
    pages we never cached. Each page is cached locally; LLM extraction
    re-runs over the cache on next `casita enrich`.

    Watch the window — if a captcha appears, solve it and the script
    continues. Close the window to stop early.
    """
    import asyncio as _a
    from .cache import get as cache_get, put as cache_put

    async def _go():
        with storage.connect() as conn:
            q = """
                SELECT key, source_id, url, address, neighborhood_resolved
                FROM listings
                WHERE active=1 AND source='zillow'
                  AND (url LIKE 'https://www.zillow.com/homedetails/%'
                       OR url LIKE 'https://www.zillow.com/apartments/%'
                       OR url LIKE 'https://www.zillow.com/b/%')
                  AND (parking IS NULL OR laundry IS NULL OR dog_policy IS NULL OR has_yard IS NULL)
            """
            if only_mill_valley:
                q += " AND (neighborhood_resolved LIKE '%Mill%' OR address LIKE '%Mill Valley%' OR neighborhood LIKE 'mill-valley')"
            rows = conn.execute(q).fetchall()

        # Filter out the ones we already cached.
        targets = []
        for r in rows:
            if cache_get("zillow", r["source_id"]) is None:
                targets.append(r)
        targets = targets[:limit]
        if not targets:
            console.print("[green]nothing to backfill — all detail pages cached[/green]")
            return
        console.print(f"[bold]backfilling {len(targets)} Zillow detail pages…[/bold]")

        from playwright.async_api import async_playwright
        from .browser import PROFILE_DIR, UA, STEALTH_JS
        PROFILE_DIR.mkdir(exist_ok=True)

        async with async_playwright() as p:
            ctx = await p.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=False,
                user_agent=UA,
                viewport={"width": 1280, "height": 900},
                locale="en-US",
                timezone_id="America/Los_Angeles",
            )
            await ctx.add_init_script(STEALTH_JS)
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            saved = 0
            for i, r in enumerate(targets, 1):
                addr = r["address"] or r["url"][:60]
                console.print(f"  [{i}/{len(targets)}] {addr}")
                try:
                    await page.goto(r["url"], wait_until="domcontentloaded", timeout=30000)
                except Exception as e:
                    console.print(f"    [yellow]nav err: {e}[/yellow]")
                    continue
                # Wait briefly for either: (a) /homedetails/ facts grid, (b)
                # /apartments/ photo carousel, or (c) the captcha denial page.
                # Different page types have different DOM — race them.
                try:
                    await page.wait_for_function(
                        """() => {
                            return document.querySelector('ul[class*=Fact] li')
                                || document.querySelector('img[src*="photos.zillowstatic.com"]')
                                || document.title.toLowerCase().includes('denied');
                        }""",
                        timeout=20000,
                    )
                except Exception:
                    console.print("    [yellow]page didn't settle — moving on[/yellow]")
                    continue
                html = await page.content()
                if "Access to this page has been denied" in html[:2000]:
                    console.print("    [red]blocked — pausing 30s for captcha solve[/red]")
                    await _a.sleep(30)
                    continue
                cache_put("zillow", r["source_id"], html)
                saved += 1
                await _a.sleep(delay)
            console.print(f"[green]saved {saved}/{len(targets)} detail pages.[/green] "
                          f"run `casita enrich` to extract.")
            await ctx.close()

    _a.run(_go())


@cli.command()
def solve():
    """Open a headed browser at Zillow so you can clear the PerimeterX captcha.

    Uses a persistent profile — once the captcha is cleared and listings load,
    close the browser window. The session cookies survive in .chrome-profile/
    and subsequent `casita search` runs reuse them.
    """
    import asyncio as _a

    async def _go():
        async with context(headless=False, persistent=True) as ctx:
            page = await ctx.new_page()
            await page.goto(
                "https://www.zillow.com/inner-richmond-san-francisco-ca/rentals/",
                wait_until="domcontentloaded",
                timeout=60000,
            )
            console.print(
                "\n[bold yellow]Solve the captcha in the open browser window.[/bold yellow]"
            )
            console.print(
                "Listings should appear when the captcha is cleared. "
                "[bold]Close the browser window when done[/bold] — session will be saved automatically."
            )
            # Keep alive until the user closes the browser. The context closes
            # when the last page does.
            while True:
                try:
                    if not ctx.pages:
                        break
                    await _a.sleep(2)
                except Exception:
                    break
            console.print("[green]session saved.[/green] now run `casita search`.")

    _a.run(_go())


@cli.command()
@click.option("--force", is_flag=True, help="Re-run extraction even for already-enriched listings.")
@click.option("--local", is_flag=True, help="Skip GCS sync; operate on the local DB only.")
def enrich(force: bool, local: bool):
    """Run Gemini extraction + ranking against the canonical DB.

    Idempotent: on a GCS generation conflict the whole pass re-runs against
    a fresh pull. Conflicts are rare (sporadic cloud writes); a retry
    re-pays the LLM calls because llm_facts caches live in the discarded
    temp copy — acceptable at this tool's scale.
    """
    _retry_canonical(local, lambda: _enrich_impl(force))


def _enrich_impl(force: bool):
    with storage.connect() as conn:
        listings = storage.active_listings(conn)
        if not listings:
            console.print("[red]no listings in DB — run `casita search` first[/red]")
            return

        # Decorate listings with their current status — the LLM uses this for engagement boost.
        for L in listings:
            srow = storage.status_for(conn, L.key)
            if srow:
                L.description = (L.description or "")
                stat = f"[STATUS: {srow['status']}"
                if srow["viewing_at"]:
                    stat += f" · viewing {srow['viewing_at']}"
                stat += "]"
                L.description = f"{stat} {L.description}".strip()

        # 1) Per-listing extraction.
        #
        # For Zillow listings, prefer the structured DOM-parsed facts over the
        # LLM extraction — the facts grid has authoritative "Pets allowed: No"
        # / "Laundry: Shared" / "Parking features: Attached" entries that are
        # less prone to hallucination than the LLM's free-form read of the
        # marketing copy. Run DOM parse first, then layer LLM on top to fill
        # what the structured grid didn't have (yard, vibe, warnings).
        from . import cache as _cache
        from . import craigslist as _cl
        from . import zillow as _zillow
        console.print(f"[bold]extraction (DOM + Gemini):[/bold] {len(listings)} listings")
        for L in listings:
            # 1a) DOM-based parse — runs over cached HTML to pull photos +
            # structured facts. Source-specific.
            if L.source == "zillow":
                html = _cache.get("zillow", L.source_id)
                if html:
                    try:
                        _zillow._parse_detail_html(html, L)
                    except Exception as e:
                        print(f"  zillow parse err [{L.key}]: {e}")
            elif L.source == "craigslist":
                html = _cache.get("craigslist", L.source_id)
                if html:
                    try:
                        _cl._parse_detail_html(html, L)
                    except Exception as e:
                        print(f"  craigslist parse err [{L.key}]: {e}")
            elif L.source == "zumper":
                # Zumper isn't bot-blocked — fetch detail page synchronously
                # via httpx and parse the Pet section for dog_policy.
                try:
                    zumper.fetch_and_parse(L)
                except Exception as e:
                    print(f"  zumper enrich err [{L.key}]: {e}")
            # 1b) LLM extraction for everything (yard, vibe, fallback fields).
            #
            # Photos extracted by 1a are persisted in EVERY branch — they're
            # cheap, deterministic, and the carousel depends on them.
            import json as _json
            # If the DB already has LOCAL mirrored photos, don't clobber them
            # with the just-re-extracted REMOTE photo URLs. Run the photo
            # mirror step explicitly to get them local instead.
            existing = conn.execute("SELECT photos_json FROM listings WHERE key=?", (L.key,)).fetchone()
            existing_photos = _json.loads(existing[0]) if (existing and existing[0]) else []
            existing_are_local = existing_photos and all(p.startswith("/") for p in existing_photos)
            if existing_are_local:
                # Keep the local paths; ignore the re-extracted remote URLs.
                photos_json = existing[0]
                L.photos = existing_photos
                L.image_url = existing_photos[0]
            else:
                photos_json = _json.dumps(L.photos) if L.photos else None
            if not force and L.has_yard is not None:
                conn.execute(
                    "UPDATE listings SET parking=COALESCE(?, parking), "
                    "laundry=COALESCE(?, laundry), dog_policy=COALESCE(?, dog_policy), "
                    "pets_allowed=COALESCE(?, pets_allowed), "
                    "image_url=COALESCE(?, image_url), "
                    "photos_json=COALESCE(?, photos_json) WHERE key=?",
                    (L.parking, L.laundry, L.dog_policy,
                     None if L.pets_allowed is None else int(L.pets_allowed),
                     L.image_url, photos_json, L.key),
                )
                conn.commit()
                continue
            if force:
                conn.execute("DELETE FROM llm_facts WHERE key=?", (L.key,))
                conn.commit()
            facts = llm.extract_facts(L, conn)
            if facts:
                llm.apply_facts(L, facts)
            conn.execute(
                "UPDATE listings SET has_yard=?, yard_note=?, "
                "parking=COALESCE(parking,?), laundry=COALESCE(laundry,?), "
                "dog_policy=COALESCE(dog_policy,?), description=COALESCE(description,?), "
                "image_url=COALESCE(?, image_url), "
                "photos_json=COALESCE(?, photos_json) WHERE key=?",
                (
                    None if L.has_yard is None else int(L.has_yard),
                    L.yard_note, L.parking, L.laundry, L.dog_policy, L.description,
                    L.image_url, photos_json, L.key,
                ),
            )
            conn.commit()

        # 1c) Gemini photo review — vision pass over listing photos.
        review_targets = [
            L for L in listings
            if L.photos and (force or L.visual_summary is None)
        ]
        if review_targets:
            console.print(f"[bold]gemini photo review:[/bold] {len(review_targets)} listings")
            for i, L in enumerate(review_targets, 1):
                try:
                    review = llm.review_photos(L, conn)
                    if not review:
                        continue
                    llm.apply_photo_review(L, review)
                    conn.execute(
                        "UPDATE listings SET light_quality=?, view_quality=?, "
                        "condition_quality=?, outdoor_visible=?, other_visible=?, "
                        "visual_summary=?, has_yard=COALESCE(?, has_yard), "
                        "yard_note=COALESCE(?, yard_note) WHERE key=?",
                        (
                            L.light_quality, L.view_quality, L.condition_quality,
                            L.outdoor_visible, L.other_visible, L.visual_summary,
                            None if L.has_yard is None else int(L.has_yard),
                            L.yard_note, L.key,
                        ),
                    )
                    conn.commit()
                    console.print(
                        f"  [{i}/{len(review_targets)}] {L.key}: "
                        f"light={L.light_quality} view={L.view_quality} "
                        f"cond={L.condition_quality}"
                    )
                except Exception as e:
                    print(f"  photo review err [{L.key}]: {e}")
                    continue

        # 2) Walking-time matrix (Routes API, cached).
        walk_map = walk.populate_for(listings)

        # 3) Cross-listing ranking with household priorities.
        console.print("[bold]gemini ranking…[/bold]")
        ranks = llm.rank_listings(listings, walk_map, conn)

        # 3a) Share-card blurbs — one per listing. Uses the post-rank state
        # (so the blurb can reference llm_reason / severity) and persists.
        # Skipped for listings that already have a blurb unless --force.
        blurb_targets = [
            L for L in listings if (force or not L.share_blurb) and L.llm_reason
        ]
        if blurb_targets:
            console.print(f"[bold]gemini share blurbs:[/bold] {len(blurb_targets)} listings")
            for L in blurb_targets:
                # Refresh from ranks dict in case we just ranked.
                if L.key in ranks:
                    L.llm_reason = ranks[L.key][1]
                    L.llm_severity = ranks[L.key][2]
                try:
                    blurb = llm.generate_share_blurb(L)
                    if blurb:
                        L.share_blurb = blurb
                        conn.execute(
                            "UPDATE listings SET share_blurb=? WHERE key=?",
                            (blurb, L.key),
                        )
                        conn.commit()
                except Exception as e:
                    print(f"  share blurb err [{L.key}]: {e}")

        for key, (rk, reason, severity) in ranks.items():
            conn.execute(
                "UPDATE listings SET llm_rank=?, llm_reason=?, llm_severity=? WHERE key=?",
                (rk, reason, severity, key),
            )
        # Listings the LLM didn't return (rare — the prompt asks for ALL).
        for L in listings:
            if L.key not in ranks:
                conn.execute(
                    "UPDATE listings SET llm_rank=?, llm_reason=?, llm_severity=? WHERE key=?",
                    (9999, "not returned by ranker", "filtered", L.key),
                )
        conn.commit()
        counts = {"ok": 0, "concerns": 0, "filtered": 0}
        for _, _, sev in ranks.values():
            counts[sev] = counts.get(sev, 0) + 1
        console.print(f"[green]ranked:[/green] ok={counts['ok']} concerns={counts['concerns']} filtered={counts['filtered']}")


def _vote_scores(conn) -> dict[str, int]:
    """Net distinct-voter score per listing: latest vote per voter, up=+1/down=-1.
    Drives the favorites (net>0) top bucket in rank()."""
    rows = conn.execute(
        """WITH latest AS (
             SELECT listing_key, voter, direction,
                    ROW_NUMBER() OVER (PARTITION BY listing_key, voter
                                       ORDER BY ts DESC, id DESC) AS rn
             FROM votes
           )
           SELECT listing_key,
                  SUM(CASE WHEN direction='up' THEN 1
                           WHEN direction='down' THEN -1 ELSE 0 END)
           FROM latest WHERE rn = 1 GROUP BY listing_key"""
    ).fetchall()
    return {r[0]: r[1] for r in rows}


@cli.command()
def show():
    """Show current active listings from the DB."""
    with storage.connect() as conn:
        status_map = {r[0]: r[1] for r in conn.execute("SELECT listing_key, status FROM listing_status")}
        listings = rank(storage.active_listings(conn), status_map=status_map,
                        vote_scores=_vote_scores(conn))
        run = storage.latest_run(conn)
    if run:
        console.print(f"last run #{run['id']} at {run['finished_at']}")
    _print_table(listings, limit=60)


@cli.command()
@click.option("--project", default=None, help="Firebase project. Defaults to CASITA_FIREBASE_PROJECT.")
@click.option("--filename", default="index.html", help="Generated filename in tmp/.")
@click.option("--local", is_flag=True, help="Skip GCS sync; render from the local DB only.")
def publish(project: str | None, filename: str, local: bool):
    """Render the page from the canonical DB + deploy to Firebase Hosting.

    Reads GCS canonical (read-only pull) so a manual publish can never
    deploy a stale local file — same semantics as the publisher daemon.
    """
    with _cloud_or_local(local, read_only=True):
        _publish_impl(project, filename)


def _copy_static_assets(output_dir: Path) -> None:
    import shutil

    src = PACKAGE_ROOT / "static"
    dst = output_dir / "assets"
    if dst.exists():
        shutil.rmtree(dst)
    if not src.exists():
        return
    shutil.copytree(src, dst)


def _missing_local_asset(url: str | None, output_dir: Path) -> bool:
    if not url or not url.startswith("/") or url.startswith("//"):
        return False
    return not (output_dir / url.lstrip("/")).exists()


def _drop_missing_local_media(listings: list[Listing], output_dir: Path) -> None:
    """Keep the rendered demo from emitting local photo URLs that are not present."""
    for L in listings:
        if L.photos:
            L.photos = [u for u in L.photos if not _missing_local_asset(u, output_dir)]
        if _missing_local_asset(L.image_url, output_dir):
            L.image_url = L.photos[0] if L.photos else None


def _demo_clean_url_path(path: str, translated: str | Path) -> str:
    translated_path = Path(translated)
    if translated_path.exists() or translated_path.suffix:
        return str(translated_path)
    parsed_path = urllib.parse.urlsplit(path).path
    if parsed_path.endswith("/"):
        return str(translated_path)
    html_path = Path(f"{translated_path}.html")
    if html_path.exists():
        return str(html_path)
    return str(translated_path)


def _rendered_site_handler(output_dir: Path):
    import functools
    import http.server

    class RenderedSiteHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
        def translate_path(self, path: str) -> str:
            return _demo_clean_url_path(path, super().translate_path(path))

        def log_message(self, format: str, *args) -> None:
            if os.environ.get("CASITA_HTTP_LOGS"):
                super().log_message(format, *args)

    return functools.partial(RenderedSiteHTTPRequestHandler, directory=str(output_dir))


def _rendered_site_server(output_dir: Path, host: str, port: int):
    import socketserver

    class ReusableTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        allow_reuse_address = True
        daemon_threads = True

    return ReusableTCPServer((host, port), _rendered_site_handler(output_dir))


@contextlib.contextmanager
def _serve_rendered_site(output_dir: Path, host: str = "127.0.0.1", port: int = 0):
    import threading

    with _rendered_site_server(output_dir, host, port) as httpd:
        bound_host, bound_port = httpd.server_address[:2]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://{bound_host}:{bound_port}"
        finally:
            httpd.shutdown()
            thread.join(timeout=2)


def _fmt_og_number(value: float | int | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return f"{value:g}"


def _og_photo_url(L: Listing, output_dir: Path, base_url: str) -> str | None:
    candidates = [*(L.photos or [])]
    if L.image_url:
        candidates.append(L.image_url)
    seen: set[str] = set()
    for url in candidates:
        if not url or url in seen:
            continue
        seen.add(url)
        if url.startswith(("http://", "https://")):
            return url
        if url.startswith("/"):
            if (output_dir / url.lstrip("/")).exists():
                return f"{base_url}{urllib.parse.quote(url, safe='/%')}"
            continue
        local_path = output_dir / url
        if local_path.exists():
            return f"{base_url}/{urllib.parse.quote(url, safe='/%')}"
    return None


def _og_listing_card_html(L: Listing, output_dir: Path, base_url: str) -> str:
    from .listing_page import _scrub

    esc = html._esc
    photo = _og_photo_url(L, output_dir, base_url)
    addr = _scrub(L.address or L.title or "Listing") or "Listing"
    hood = (L.hood or "San Francisco").replace("-", " ").title()
    price = f"${L.price:,}/mo" if L.price else "Price on request"
    specs = []
    if beds := _fmt_og_number(L.beds):
        specs.append(f"{beds} bd")
    if baths := _fmt_og_number(L.baths):
        specs.append(f"{baths} ba")
    if L.sqft:
        specs.append(f"{L.sqft:,} sqft")

    facts = []
    if L.dog_policy:
        from . import dogs

        facts.append(dogs.LABELS.get(L.dog_policy, L.dog_policy.replace("_", " ")))
    if L.parking:
        facts.append(_scrub(L.parking) or "")
    if L.laundry:
        facts.append(_scrub(L.laundry) or "")
    if L.has_yard:
        facts.append(_scrub(L.yard_note or "yard") or "")
    if not facts and L.visual_summary:
        facts.append(_scrub(L.visual_summary) or "")
    facts = [f for f in facts if f][:3]

    photo_html = (
        f'<img class="bg" src="{esc(photo)}" alt="">'
        if photo else
        '<div class="fallback-bg"><div>Casita</div></div>'
    )
    specs_html = " · ".join(esc(s) for s in specs)
    facts_html = "".join(f"<span>{esc(f)}</span>" for f in facts)
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>{_OG_CARD_CSS}</style>
</head>
<body>
<main class="og-card">
  {photo_html}
  <div class="shade"></div>
  <div class="brand">Casita</div>
  <div class="hood">{esc(hood)}</div>
  <section class="copy">
    <div class="price">{esc(price)}</div>
    <h1>{esc(addr)}</h1>
    <p>{specs_html or esc(L.source.title())}</p>
    <div class="chips">{facts_html}</div>
  </section>
</main>
</body>
</html>"""


def _og_index_card_html(listings: list[Listing], run, output_dir: Path, base_url: str) -> str:
    esc = html._esc
    feature = next((L for L in listings if _og_photo_url(L, output_dir, base_url)), listings[0])
    photo = _og_photo_url(feature, output_dir, base_url)
    strong = sum(1 for L in listings if L.llm_severity == "ok")
    ts_raw = (run["finished_at"] or run["started_at"]) if run else ""
    refreshed = ""
    if ts_raw:
        try:
            from datetime import datetime as _dt, timezone as _timezone

            dt = _dt.fromisoformat(str(ts_raw)).replace(tzinfo=_timezone.utc)
            refreshed = dt.astimezone(html.PACIFIC).strftime("Refreshed %b %-d, %-I:%M %p")
        except Exception:
            refreshed = str(ts_raw)
    photo_html = (
        f'<img class="bg" src="{esc(photo)}" alt="">'
        if photo else
        '<div class="fallback-bg"><div>Casita</div></div>'
    )
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>{_OG_CARD_CSS}</style>
</head>
<body>
<main class="og-card index-card">
  {photo_html}
  <div class="shade"></div>
  <div class="brand">Casita</div>
  <section class="copy">
    <div class="price">{len(listings)} places</div>
    <h1>Rental search demo</h1>
    <p>{strong} strong fits · large dogs · trails · beaches · bakeries</p>
    <div class="chips"><span>{esc(refreshed or "Static demo")}</span></div>
  </section>
</main>
</body>
</html>"""


_OG_CARD_CSS = """
* { box-sizing: border-box; }
html, body { width: 1200px; height: 630px; margin: 0; overflow: hidden; }
body {
  font-family: "Helvetica Neue", Arial, sans-serif;
  background: #ece8dd;
  color: #fff;
}
.og-card {
  position: relative;
  width: 1200px;
  height: 630px;
  overflow: hidden;
  background: #2f3f34;
}
.bg {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.fallback-bg {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  background:
    linear-gradient(135deg, #2f5e48 0%, #795f45 55%, #d6c5a0 100%);
  color: rgba(255,255,255,0.3);
  font-family: Georgia, serif;
  font-size: 144px;
}
.shade {
  position: absolute;
  inset: 0;
  background:
    linear-gradient(90deg, rgba(18,20,16,0.88) 0%, rgba(18,20,16,0.58) 42%, rgba(18,20,16,0.14) 100%),
    linear-gradient(0deg, rgba(18,20,16,0.82) 0%, rgba(18,20,16,0.12) 58%);
}
.brand {
  position: absolute;
  top: 42px;
  left: 54px;
  font-family: Georgia, serif;
  font-size: 44px;
  letter-spacing: 0;
  text-shadow: 0 2px 14px rgba(0,0,0,0.4);
}
.hood {
  position: absolute;
  top: 48px;
  right: 54px;
  max-width: 420px;
  padding: 12px 18px;
  border-radius: 999px;
  background: rgba(255,255,255,0.9);
  color: #274831;
  font-size: 20px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.copy {
  position: absolute;
  left: 54px;
  bottom: 48px;
  width: 780px;
  text-shadow: 0 3px 18px rgba(0,0,0,0.55);
}
.price {
  margin-bottom: 12px;
  font-family: Georgia, serif;
  font-size: 72px;
  line-height: 0.96;
}
h1 {
  margin: 0;
  font-family: Georgia, serif;
  font-size: 46px;
  line-height: 1.05;
  font-weight: 500;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
p {
  margin: 16px 0 0;
  font-size: 28px;
  line-height: 1.18;
  font-weight: 650;
}
.chips {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  margin-top: 22px;
}
.chips span {
  max-width: 360px;
  padding: 11px 16px;
  border-radius: 999px;
  background: rgba(255,255,255,0.9);
  color: #213d2b;
  font-size: 19px;
  font-weight: 700;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  text-shadow: none;
}
.index-card .copy { width: 850px; }
"""


def _generate_og_images(output_dir: Path, listings: list[Listing], run=None) -> int:
    """Render social preview cards from listing photos and facts."""
    import shutil

    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    og_dir = output_dir / "og"
    if og_dir.exists():
        shutil.rmtree(og_dir)
    og_dir.mkdir(parents=True, exist_ok=True)

    count = 0
    with _serve_rendered_site(output_dir) as base_url:
        from . import listing_page

        targets = [(_og_index_card_html(listings, run, output_dir, base_url), og_dir / "index.png")]
        targets.extend(
            (_og_listing_card_html(L, output_dir, base_url), og_dir / "listing" / f"{listing_page._slug(L)}.png")
            for L in listings
        )
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                try:
                    context = browser.new_context(
                        viewport={"width": 1200, "height": 630},
                        device_scale_factor=1,
                        color_scheme="light",
                    )
                    try:
                        page = context.new_page()
                        page.set_default_timeout(15_000)
                        for card_html, out_file in targets:
                            out_file.parent.mkdir(parents=True, exist_ok=True)
                            page.set_content(card_html, wait_until="domcontentloaded")
                            page.wait_for_timeout(250)
                            page.screenshot(path=str(out_file), type="png")
                            count += 1
                    finally:
                        context.close()
                finally:
                    browser.close()
        except PlaywrightError as e:
            raise RuntimeError(
                "Open Graph image generation needs a Playwright Chromium browser. "
                "Run `uv run playwright install chromium` and retry."
            ) from e

    return count


def _render_site(filename: str, output_dir: Path) -> dict[str, int | Path]:
    with storage.connect() as conn:
        # Cross-source dedup across the whole active set BEFORE we render —
        # catches duplicates that landed in separate scrape runs.
        deactivated = dedup.deduplicate_db(conn)
        if deactivated:
            console.print(f"[bold]dedup:[/bold] deactivated {deactivated} duplicate listings")
        status_rows = conn.execute("SELECT listing_key, status FROM listing_status").fetchall()
        status_map = {r[0]: r[1] for r in status_rows}
        listings = rank(storage.active_listings(conn), status_map=status_map,
                        vote_scores=_vote_scores(conn))
        run = storage.latest_run(conn)
        walk_map = walk.populate_for(listings)
        drive_map = walk.populate_drive_for_marin(listings)
        drive_bakery_map = walk.populate_drive_for_bakeries(listings)
        convo_map = {
            L.key: storage.conversation_state(conn, L.key)
            for L in listings
            if storage.conversation_state(conn, L.key) is not None
        }
    if not listings:
        console.print("[red]no listings in DB — run `casita search` first[/red]")
        out_html = output_dir / filename
        out_html.parent.mkdir(parents=True, exist_ok=True)
        _copy_static_assets(output_dir)
        out_html.write_text("""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Casita</title></head>
<body><p>No listings in DB. Run <code>casita search</code> first.</p></body>
</html>
""")
        return {
            "out_html": out_html,
            "listings": 0,
            "conversations": 0,
            "bakery_drives": 0,
            "details": 0,
            "og_images": 0,
        }

    _drop_missing_local_media(listings, output_dir)
    out_html = output_dir / filename
    out_html.parent.mkdir(exist_ok=True)
    out_html.write_text(html.render(
        listings, run=run, walk_map=walk_map, convo_map=convo_map,
        drive_bakery_map=drive_bakery_map, drive_map=drive_map,
    ))

    # Per-listing detail pages — one file per active listing under tmp/listing/.
    from . import listing_page
    listing_dir = output_dir / "listing"
    listing_dir.mkdir(exist_ok=True)
    detail_count = 0
    with storage.connect() as conn:
        for L in listings:
            slug = listing_page._slug(L)
            page_html = listing_page.render_detail(
                L, conn, walk_map=walk_map, drive_map=drive_map,
                drive_bakery_map=drive_bakery_map,
            )
            (listing_dir / f"{slug}.html").write_text(page_html)
            detail_count += 1

    _copy_static_assets(output_dir)

    # Mirror screenshots into the served tree at tmp/shots/.
    import shutil
    src_shots = ROOT / "screenshots"
    if src_shots.exists():
        dst_shots = output_dir / "shots"
        dst_shots.mkdir(exist_ok=True)
        for f in src_shots.iterdir():
            if f.is_file():
                shutil.copy2(f, dst_shots / f.name)

    og_count = _generate_og_images(output_dir, listings, run=run)
    console.print(
        f"wrote {out_html} ({len(listings)} listings, "
        f"{len(convo_map)} with conversations, "
        f"{len(drive_bakery_map)} with bakery drive-times, "
        f"{detail_count} detail pages, "
        f"{og_count} og images)"
    )
    return {
        "out_html": out_html,
        "listings": len(listings),
        "conversations": len(convo_map),
        "bakery_drives": len(drive_bakery_map),
        "details": detail_count,
        "og_images": og_count,
    }


def _publish_impl(project: str | None, filename: str):
    project = project or os.environ.get("CASITA_FIREBASE_PROJECT")
    if not project:
        console.print("[red]set CASITA_FIREBASE_PROJECT or pass --project[/red]")
        raise SystemExit(1)

    _render_site(filename, ROOT / "tmp")

    result = subprocess.run(
        ["firebase", "deploy", "--only", "hosting", f"--project={project}"],
        cwd=ROOT, capture_output=True, text=True,
    )
    if result.returncode != 0:
        console.print(f"[red]firebase deploy failed:[/red]\n{result.stderr or result.stdout}")
        raise SystemExit(1)
    site_url = os.environ.get("CASITA_SITE_URL")
    if site_url:
        console.print(f"\n[green]live:[/green] {site_url.rstrip('/')}")


@cli.command()
@click.option(
    "--fixture",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=DEMO_FIXTURE,
    help="SQLite fixture to render.",
)
@click.option("--host", default="127.0.0.1", help="HTTP bind host.")
@click.option("--port", default=8765, help="HTTP port; intentionally not 8000.")
def demo(fixture: Path, host: str, port: int):
    """Render the committed fixture and serve it locally without credentials."""
    import shutil

    output_dir = ROOT / "tmp" / "demo-site"
    demo_db = ROOT / "tmp" / "demo.sqlite"
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    demo_db.parent.mkdir(exist_ok=True)
    shutil.copy2(fixture, demo_db)

    url = f"http://{host}:{port}"
    env_updates = {
        "CASITA_DB_PATH": str(demo_db),
        "CASITA_ROUTE_CACHE_DB": str(demo_db),
        "CASITA_ROUTES_OFFLINE": "1",
        "CASITA_SITE_URL": url,
    }
    previous = {k: os.environ.get(k) for k in env_updates}
    try:
        os.environ.update(env_updates)
        _render_site("index.html", output_dir)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    console.print(f"[green]demo:[/green] {url}/")
    with _rendered_site_server(output_dir, host, port) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            console.print("\n[yellow]demo server stopped[/yellow]")


# ---------------------------------------------------------------------------
# Cloud-synced verbs — optional private deployment helpers.
#
# As of issue #2, `search`/`enrich`/`publish` also operate on the canonical
# GCS DB (via _retry_canonical / _cloud_or_local), so a local casita.db
# does not have to be a second writer — GCS is the single source of truth.
#
# Every mutating verb wraps the work in cloud_sync.with_db(): pulls the
# canonical DB from GCS, mutates the temp copy, pushes back with OCC, and
# flips the publish-pending flag for the auto-publisher daemon.
#
# Pass --local to skip GCS sync when you just want to inspect or fix a row
# without a roundtrip.
# ---------------------------------------------------------------------------

VOTERS = ["reviewer_a", "reviewer_b"]
ELIMINATE_KINDS = ["declined_by_us", "declined_by_landlord", "passed_on"]


def _lookup_listing_key(conn: sqlite3.Connection, ident: str) -> str | None:
    """Resolve a slug ("lake-street-63095237") or key ("zillow:63095237")
    to a canonical listings.key. Returns None if no match.
    """
    if ":" in ident:
        row = conn.execute(
            "SELECT key FROM listings WHERE key=?", (ident,)
        ).fetchone()
        return row[0] if row else None
    from . import listing_page
    rows = conn.execute("SELECT * FROM listings WHERE active=1").fetchall()
    for row in rows:
        L = storage._row_to_listing(row)
        if listing_page._slug(L) == ident:
            return L.key
    # Fallback: source_id suffix match. People often paste the trailing
    # digits without the hood prefix.
    suffix = ident.rsplit("-", 1)[-1]
    row = conn.execute(
        "SELECT key FROM listings WHERE source_id=? AND active=1 LIMIT 1",
        (suffix,),
    ).fetchone()
    return row[0] if row else None


def _record_action(
    conn: sqlite3.Connection,
    *,
    listing_key: str | None,
    voter: str,
    kind: str,
    payload: dict,
) -> int:
    import json as _json
    cur = conn.execute(
        "INSERT INTO actions (listing_key, voter, kind, payload_json) VALUES (?, ?, ?, ?)",
        (listing_key, voter, kind, _json.dumps(payload)),
    )
    return cur.lastrowid


def _cloud_or_local(local: bool, *, read_only: bool = False):
    from contextlib import nullcontext
    if local:
        return nullcontext()
    from . import cloud_sync
    return cloud_sync.with_db(read_only=read_only)


def _retry_canonical(local: bool, thunk, *, attempts: int = 3):
    """Run `thunk()` against the canonical DB, retrying on OCC conflict.

    `thunk` must be replayable: each attempt re-pulls a fresh canonical DB
    (via with_db) and re-runs the mutation against it. with_db deliberately
    doesn't auto-retry because verbs aren't replayable from inside it —
    these callers are, so the loop lives here.
    """
    from . import cloud_sync
    for attempt in range(1, attempts + 1):
        try:
            with _cloud_or_local(local):
                return thunk()
        except cloud_sync.ConflictError:
            if attempt == attempts:
                console.print(
                    f"[red]GCS generation conflict persisted after {attempts} "
                    f"attempts — re-run the command.[/red]"
                )
                raise
            console.print(
                f"[yellow]GCS generation conflict (attempt {attempt}/{attempts}) "
                f"— re-pulling and re-applying[/yellow]"
            )


@cli.command()
@click.option("--listing", required=True, help="Slug (lake-street-63095237) or key (zillow:63095237).")
@click.option("--dir", "direction", required=True, type=click.Choice(["up", "down"]))
@click.option("--reason")
@click.option("--voter", default="reviewer_b", type=click.Choice(VOTERS))
@click.option("--local", is_flag=True, help="Skip GCS sync; operate on local DB only.")
def vote(listing: str, direction: str, reason: str | None, voter: str, local: bool):
    """Record an up/down vote on a listing."""
    with _cloud_or_local(local):
        with storage.connect() as conn:
            key = _lookup_listing_key(conn, listing)
            if not key:
                console.print(f"[red]no listing matches:[/red] {listing}")
                raise SystemExit(1)
            conn.execute(
                "INSERT INTO votes (listing_key, voter, direction, reason) VALUES (?, ?, ?, ?)",
                (key, voter, direction, reason),
            )
            _record_action(conn, listing_key=key, voter=voter, kind="vote",
                           payload={"direction": direction, "reason": reason})
            conn.commit()
    console.print(f"[green]voted[/green] {direction} on {key} by {voter}")


@cli.command(name="analyze-prefs")
@click.option("--local", is_flag=True, help="Skip GCS sync; operate on the local DB only.")
def analyze_prefs(local: bool):
    """Audit the static ranking policy against actual votes — propose edits.

    Reads every up/pass vote + reason, compares revealed preference against the
    current `_RANK_SYSTEM`, and prints flagged contradictions + proposed new
    rules. Proposes only — hand-edit `src/casita/llm.py` and commit.
    """
    with _cloud_or_local(local, read_only=True):
        with storage.connect() as conn:
            analysis = llm.analyze_preferences(conn)

    if not analysis:
        console.print("[yellow]no votes with reasons yet — nothing to analyze.[/yellow]")
        return

    console.print()
    console.print(Panel(
        analysis.summary, title="[bold]preference audit[/bold]",
        border_style="cyan", padding=(1, 2),
    ))

    if analysis.contradictions:
        console.print(
            f"\n[bold yellow]contradictions[/bold yellow] "
            f"({len(analysis.contradictions)}) — policy vs revealed behavior\n"
        )
        for i, c in enumerate(analysis.contradictions, 1):
            body = (
                f"[dim]policy says[/dim]\n  {c.policy_quote}\n\n"
                f"[dim]votes reveal[/dim]\n  {c.revealed_behavior}\n\n"
                f"[green]proposed edit[/green]\n  {c.proposed_resolution}"
            )
            console.print(Panel(body, title=f"[bold]#{i}[/bold]",
                                border_style="yellow", padding=(1, 2)))

    if analysis.new_rules:
        console.print(f"\n[bold cyan]proposed new rules[/bold cyan] ({len(analysis.new_rules)})\n")
        for r in analysis.new_rules:
            console.print(f"  [green]•[/green] {r.rule}")
            console.print(f"    [dim]{r.evidence}[/dim]")

    console.print(
        "\n[dim]proposes only — hand-edit _RANK_SYSTEM in src/casita/llm.py "
        "and commit the reconciled policy.[/dim]"
    )


@cli.command()
@click.option("--listing", required=True)
@click.option("--kind", required=True, type=click.Choice(ELIMINATE_KINDS))
@click.option("--note")
@click.option("--voter", default="reviewer_b", type=click.Choice(VOTERS))
@click.option("--local", is_flag=True)
def eliminate(listing: str, kind: str, note: str | None, voter: str, local: bool):
    """Mark a listing eliminated and move it to the end of the page."""
    with _cloud_or_local(local):
        with storage.connect() as conn:
            key = _lookup_listing_key(conn, listing)
            if not key:
                console.print(f"[red]no listing matches:[/red] {listing}")
                raise SystemExit(1)
            prev = storage.status_for(conn, key)
            prev_status = prev["status"] if prev else None
            prev_note = prev["status_note"] if prev else None
            storage.set_status(conn, key, kind, note=note)
            _record_action(
                conn, listing_key=key, voter=voter, kind="eliminate",
                payload={"new": kind, "note": note,
                         "prev_status": prev_status, "prev_note": prev_note},
            )
            conn.commit()
    console.print(f"[green]eliminated[/green] {key} as {kind}")


@cli.command(name="note")
@click.option("--listing", required=True)
@click.option("--body", required=True)
@click.option("--visibility", default="internal", type=click.Choice(["internal", "public"]))
@click.option("--voter", default="reviewer_b", type=click.Choice(VOTERS))
@click.option("--local", is_flag=True)
def note_cmd(listing: str, body: str, visibility: str, voter: str, local: bool):
    """Append a free-form note on a listing (recorded as a 'note' interaction)."""
    with _cloud_or_local(local):
        with storage.connect() as conn:
            key = _lookup_listing_key(conn, listing)
            if not key:
                console.print(f"[red]no listing matches:[/red] {listing}")
                raise SystemExit(1)
            cur = conn.execute(
                "INSERT INTO interactions (listing_key, direction, channel, sender, body) "
                "VALUES (?, 'note', ?, ?, ?)",
                (key, visibility, voter, body),
            )
            _record_action(conn, listing_key=key, voter=voter, kind="note",
                           payload={"interaction_id": cur.lastrowid,
                                    "visibility": visibility, "body": body})
            conn.commit()
    console.print(f"[green]noted[/green] on {key}")


@cli.command()
@click.option("--url", required=True, help="Full listing URL from Zillow / Craigslist / etc.")
@click.option("--voter", default="reviewer_b", type=click.Choice(VOTERS))
@click.option("--note")
@click.option("--local", is_flag=True)
def add(url: str, voter: str, note: str | None, local: bool):
    """Queue an external URL for the local scraper to ingest on its next run."""
    with _cloud_or_local(local):
        with storage.connect() as conn:
            cur = conn.execute(
                "INSERT INTO pending_urls (url, added_by, note) VALUES (?, ?, ?)",
                (url, voter, note),
            )
            _record_action(conn, listing_key=None, voter=voter, kind="add",
                           payload={"pending_id": cur.lastrowid, "url": url})
            conn.commit()
    console.print(f"[green]queued[/green] {url} (will appear after next scrape)")


@cli.command()
@click.option("--listing", required=True)
@click.option("--voter", default="reviewer_b", type=click.Choice(VOTERS))
@click.option("--local", is_flag=True)
def undo(listing: str, voter: str, local: bool):
    """Reverse the most recent action on a listing."""
    import json as _json
    with _cloud_or_local(local):
        with storage.connect() as conn:
            key = _lookup_listing_key(conn, listing) or listing
            row = conn.execute(
                "SELECT id, kind, payload_json FROM actions "
                "WHERE listing_key=? AND undone=0 ORDER BY id DESC LIMIT 1",
                (key,),
            ).fetchone()
            if not row:
                console.print(f"[yellow]nothing to undo on {listing}[/yellow]")
                raise SystemExit(1)
            payload = _json.loads(row["payload_json"])
            kind = row["kind"]
            if kind == "vote":
                conn.execute(
                    "DELETE FROM votes WHERE listing_key=? AND id="
                    "(SELECT MAX(id) FROM votes WHERE listing_key=?)",
                    (key, key),
                )
            elif kind == "eliminate":
                prev_status = payload.get("prev_status")
                if prev_status:
                    storage.set_status(conn, key, prev_status, note=payload.get("prev_note"))
                else:
                    conn.execute("DELETE FROM listing_status WHERE listing_key=?", (key,))
            elif kind == "note":
                conn.execute("DELETE FROM interactions WHERE id=?",
                             (payload["interaction_id"],))
            else:
                console.print(f"[red]can't undo kind={kind}[/red]")
                raise SystemExit(1)
            conn.execute("UPDATE actions SET undone=1 WHERE id=?", (row["id"],))
            conn.commit()
    console.print(f"[green]undid[/green] {kind} on {key}")


@cli.command(name="ls")
@click.option("--status", default="active",
              type=click.Choice(["active", "eliminated", "all"]))
@click.option("--neighborhood")
@click.option("--limit", default=20)
@click.option("--local", is_flag=True)
def ls_cmd(status: str, neighborhood: str | None, limit: int, local: bool):
    """List listings — read-only, fast lookup for listing questions."""
    eliminated_set = "('declined_by_us','declined_by_landlord','passed_on')"
    with _cloud_or_local(local, read_only=True):
        with storage.connect() as conn:
            base = (
                "SELECT l.key, l.address, l.price, l.neighborhood_resolved, "
                "       s.status "
                "FROM listings l "
                "LEFT JOIN listing_status s ON s.listing_key = l.key "
                "WHERE l.active=1"
            )
            args: list = []
            if status == "active":
                base += f" AND (s.status IS NULL OR s.status NOT IN {eliminated_set})"
            elif status == "eliminated":
                base += f" AND s.status IN {eliminated_set}"
            if neighborhood:
                base += " AND (LOWER(l.neighborhood_resolved) LIKE ? OR LOWER(l.address) LIKE ?)"
                pat = f"%{neighborhood.lower()}%"
                args.extend([pat, pat])
            base += " ORDER BY l.price ASC LIMIT ?"
            args.append(limit)
            rows = conn.execute(base, args).fetchall()
    if not rows:
        console.print("[yellow]no matches[/yellow]")
        return
    table = Table(show_lines=False)
    for col in ["key", "addr", "price", "hood", "status"]:
        table.add_column(col)
    for r in rows:
        table.add_row(
            r["key"],
            (r["address"] or "")[:40],
            f"${r['price']:,}" if r["price"] else "?",
            (r["neighborhood_resolved"] or "")[:14],
            r["status"] or "",
        )
    console.print(table)


@cli.group()
def publisher():
    """Auto-publisher daemon — watches the publish-pending flag in GCS."""


@publisher.command(name="tick")
@click.option("--debounce", default=30, help="Seconds the flag must be settled before deploying.")
def publisher_tick(debounce: int):
    """Run one publisher iteration; intended for cron or a loop wrapper."""
    import datetime as _dt
    from . import cloud_sync
    flag = cloud_sync.read_publish_pending()
    if not flag:
        return
    body, gen = flag
    try:
        ts = _dt.datetime.fromisoformat(
            __import__("json").loads(body)["ts"].replace("Z", "+00:00")
        )
    except Exception:
        ts = _dt.datetime.now(_dt.timezone.utc)
    now = _dt.datetime.now(_dt.timezone.utc)
    age = (now - ts).total_seconds()
    if age < debounce:
        console.print(f"[dim]publish pending, age {age:.0f}s < debounce {debounce}s — waiting[/dim]")
        return
    if not os.environ.get("CASITA_FIREBASE_PROJECT"):
        console.print("[red]set CASITA_FIREBASE_PROJECT before running the publisher[/red]")
        raise SystemExit(1)
    console.print(f"[bold]publishing[/bold] (flag age {age:.0f}s)")
    try:
        with cloud_sync.with_db(read_only=True, mark_publish=False):
            ctx = click.Context(cli.commands["publish"])
            ctx.invoke(cli.commands["publish"], project=None, filename="index.html", local=True)
    except SystemExit as e:
        if e.code != 0:
            # Leave the flag set so the next tick retries. Don't crash the run loop.
            console.print("[yellow]publish failed; leaving publish_pending set for retry[/yellow]")
            return
    cloud_sync.clear_publish_pending(gen)


@publisher.command(name="run")
@click.option("--interval", default=10, help="Seconds between polls.")
@click.option("--debounce", default=30)
def publisher_run(interval: int, debounce: int):
    """Long-running poller; run via nohup or systemd."""
    import time
    console.print(f"[bold]publisher running[/bold] interval={interval}s debounce={debounce}s")
    while True:
        try:
            ctx = click.Context(publisher.commands["tick"])
            ctx.invoke(publisher.commands["tick"], debounce=debounce)
        except Exception as e:
            console.print(f"[red]publisher error:[/red] {e}")
        time.sleep(interval)


def main() -> None:
    cli()
