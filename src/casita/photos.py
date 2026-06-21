"""Local mirror of listing photos.

Each listing's photos get downloaded once to tmp/photos/<source>/<source_id>/N.jpg
and the listing.photos URLs are rewritten to relative paths the Firebase host
serves directly. This protects against Zillow CDN expiry and source-page delisting.
"""
import asyncio
import os
from pathlib import Path
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).parent.parent.parent
PHOTOS_DIR = ROOT / "tmp" / "photos"


def _local_path(source: str, source_id: str, idx: int, ext: str) -> Path:
    safe_id = "".join(c for c in source_id if c.isalnum() or c in "-_.")
    return PHOTOS_DIR / source / safe_id / f"{idx}.{ext}"


def _rel_url(p: Path) -> str:
    # The Firebase Hosting `public` dir is `tmp/`, so paths under tmp/photos/...
    # are served as /photos/...
    return "/" + str(p.relative_to(ROOT / "tmp")).replace(os.sep, "/")


async def _download_one(client: httpx.AsyncClient, url: str, dest: Path) -> str | None:
    if dest.exists() and dest.stat().st_size > 0:
        return _rel_url(dest)
    try:
        r = await client.get(url, timeout=20, follow_redirects=True)
        if r.status_code != 200 or len(r.content) < 1024:
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.content)
        return _rel_url(dest)
    except Exception:
        return None


async def mirror_photos(source: str, source_id: str, urls: list[str]) -> list[str]:
    """Download up to 8 photos for one listing; return rewritten local URLs.

    URLs that fail to fetch are skipped — the caller keeps the original list
    intact in case we want to retry later.
    """
    out: list[str] = []
    async with httpx.AsyncClient(http2=False) as client:
        tasks = []
        for i, url in enumerate(urls[:8]):
            ext = "jpg" if ".jpg" in urlparse(url).path.lower() else (
                "webp" if ".webp" in urlparse(url).path.lower() else "jpg"
            )
            dest = _local_path(source, source_id, i, ext)
            tasks.append(_download_one(client, url, dest))
        results = await asyncio.gather(*tasks)
    out = [r for r in results if r]
    return out
