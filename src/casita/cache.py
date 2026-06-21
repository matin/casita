"""Local cache for detail-page HTML.

Layout: detail_cache/<source>/<source_id>.html.
TTL: 24h by default. Re-runs the same day skip the network entirely.
"""
import time
from pathlib import Path

CACHE_DIR = Path(__file__).parent.parent.parent / "detail_cache"
DEFAULT_TTL_S = 24 * 60 * 60  # 24h


def _path(source: str, source_id: str) -> Path:
    safe_id = "".join(c for c in source_id if c.isalnum() or c in "-_.")
    return CACHE_DIR / source / f"{safe_id}.html"


def get(source: str, source_id: str, ttl_s: int = DEFAULT_TTL_S) -> str | None:
    p = _path(source, source_id)
    if not p.exists():
        return None
    if time.time() - p.stat().st_mtime > ttl_s:
        return None
    return p.read_text()


def put(source: str, source_id: str, html: str) -> None:
    p = _path(source, source_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(html)


def invalidate(source: str, source_id: str) -> None:
    p = _path(source, source_id)
    if p.exists():
        p.unlink()
