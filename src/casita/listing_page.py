"""Per-listing detail pages.

One HTML file per active listing under tmp/listing/<slug>.html, deployed by
Firebase Hosting so each card on the index page links to a casita-owned
detail page instead of out to Zillow/Craigslist.

The detail page shows everything we know about the listing:
  - Full-size photo carousel (large, scroll-snap)
  - Gemini visual summary + light/view/condition pills
  - All structured facts (parking, laundry, yard, walks, drives, contact)
  - Full conversation thread (CRM)
  - Attached screenshots
  - "View on $source" footer with link back to the canonical listing
"""
from __future__ import annotations

import re
import sqlite3
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

from . import dogs
from .html import (
    CAROUSEL_JS,
    CSS,
    PACIFIC,
    SHARE_JS,
    THEME_PREPAINT,
    THEME_SWITCH_HTML,
    THEME_SWITCH_JS,
    VOTE_JS,
    _anchor_link_html,
    _clean_address_for_maps,
    _esc,
    _gcal_link,
    public_url,
    _walk_class,
)

_DEFAULT_REDACT_PATTERNS = [
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
    re.compile(
        r"(?<![\d.-])(?:\+?1[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]\d{3}[\s.-]\d{4}(?![\d.-])"
    ),
]


def _redact_patterns() -> list[re.Pattern]:
    raw = os.environ.get("CASITA_REDACT_PATTERNS", "")
    extra = [re.compile(p, re.IGNORECASE) for p in raw.splitlines() if p.strip()]
    return [*_DEFAULT_REDACT_PATTERNS, *extra]


def _scrub(s: str | None) -> str | None:
    """Apply optional deployment-specific redaction patterns."""
    if not s:
        return s
    for pat in _redact_patterns():
        s = pat.sub("[redacted]", s)
    return s
from .models import Listing
from .walk import BAKERIES, BEACHES, PRESIDIO_GATES, TRAILS, minutes_to, nearest

ROOT = Path(__file__).parent.parent.parent


def _slug(L: Listing) -> str:
    """URL-safe slug — neighborhood + stable source_id + random token.

    Format: `<hood>-<source_id>-<token>.html`
    Hood is human-readable so the URL itself signals the location. Source
    name (zillow/craigslist) is omitted — the reader doesn't care which
    site it came from.
    """
    hood = (L.hood or "sf").lower()
    hood = re.sub(r"[^a-z0-9-]+", "-", hood).strip("-") or "sf"
    sid = re.sub(r"[^a-zA-Z0-9-]+", "-", L.source_id).strip("-").lower()
    parts = [hood, sid]
    if L.share_token:
        parts.append(L.share_token)
    return "-".join(parts)


def listing_url(L: Listing) -> str:
    # No .html suffix — Firebase Hosting's cleanUrls strips it automatically.
    return f"/listing/{_slug(L)}"


_DETAIL_CSS = """
.detail-wrap { max-width: 880px; margin: 0 auto; padding: 32px 24px 100px; }
@media (max-width: 700px) { .detail-wrap { padding: 18px 16px 64px; } }

.back-link {
  display: inline-flex; align-items: center; gap: 6px;
  color: var(--ink-3); text-decoration: none;
  font-size: 12px; font-weight: 600; letter-spacing: 0.04em;
  margin-bottom: 14px;
}
.back-link:hover { color: var(--accent); }

.hero {
  width: 100%;
  aspect-ratio: 16 / 10;
  background: var(--img-bg);
  border-radius: 18px;
  overflow: hidden;
  margin-bottom: 26px;
  position: relative;
  box-shadow: 0 8px 30px var(--shadow);
}
.hero-track {
  display: flex;
  width: 100%; height: 100%;
  overflow-x: auto;
  scroll-snap-type: x mandatory;
  scrollbar-width: none;
  -webkit-overflow-scrolling: touch;
}
.hero-track::-webkit-scrollbar { display: none; }
.hero-slide {
  flex: 0 0 100%;
  width: 100%; height: 100%;
  scroll-snap-align: start;
}
.hero-slide img { width: 100%; height: 100%; object-fit: cover; display: block; }

.detail-hood-prominent {
  font-size: 11px; color: var(--ink-3); text-transform: uppercase;
  letter-spacing: 0.13em; font-weight: 700; margin-bottom: 8px;
}
.detail-title {
  font-family: var(--serif); font-optical-sizing: auto;
  font-size: 30px; font-weight: 420; margin: 0 0 14px; line-height: 1.12;
  letter-spacing: -0.012em; color: var(--ink);
}
@media (max-width: 700px) { .detail-title { font-size: 24px; } }
.detail-head { display: flex; flex-wrap: wrap; gap: 6px 18px; align-items: baseline; margin-bottom: 4px; }
.detail-price {
  font-family: var(--serif); font-optical-sizing: auto;
  font-size: 34px; font-weight: 460; letter-spacing: -0.01em; line-height: 1;
}
.detail-stats { color: var(--ink-2); font-size: 15px; font-weight: 500; }
.detail-stats .stat { margin-right: 14px; }
.detail-hood { font-size: 12px; color: var(--ink-3); text-transform: uppercase; letter-spacing: 0.08em; font-weight: 600; }

.detail-section { margin-top: 30px; padding-top: 24px; border-top: 1px solid var(--line); }
.detail-section h2 {
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.13em;
  color: var(--ink-3); font-weight: 700; margin: 0 0 14px;
}

.detail-kv .row { display: flex; gap: 14px; padding: 9px 0; font-size: 14.5px; border-bottom: 1px solid var(--line); }
.detail-kv .row:last-child { border-bottom: 0; }
.detail-kv .k { color: var(--ink-3); text-transform: uppercase; font-size: 11px; letter-spacing: 0.07em; min-width: 110px; padding-top: 3px; font-weight: 600; }
.detail-kv .v { color: var(--ink); flex: 1; font-weight: 500; }
.detail-kv .v.warn { color: var(--warn); }
.detail-kv .v.caution { color: var(--caution); }
.detail-kv a { color: var(--accent); text-decoration: none; font-weight: 500; }
.detail-kv a:hover { text-decoration: underline; }
.detail-kv .contact-note { color: var(--ink-3); font-size: 12px; font-style: italic; margin-left: 4px; }

.detail-eliminated {
  margin: 0 0 22px;
  padding: 14px 18px;
  background: var(--warn-soft);
  color: var(--warn);
  border-radius: 12px;
  font-size: 13.5px;
}
.detail-eliminated strong {
  display: block; font-size: 11px; font-weight: 700;
  text-transform: uppercase; letter-spacing: 0.1em;
  margin-bottom: 4px;
}
.detail-eliminated .note { color: var(--ink-2); font-weight: 500; }

.vote-bar {
  display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
  margin: 18px 0 6px;
}
.vote-btn {
  display: inline-flex; align-items: center; gap: 7px;
  background: var(--card); color: var(--ink-2);
  border: 1px solid var(--line);
  border-radius: 999px; padding: 9px 18px;
  font: inherit; font-size: 13px; font-weight: 600;
  cursor: pointer; transition: all 0.16s ease;
  box-shadow: 0 1px 2px var(--shadow);
}
.vote-btn:hover { color: var(--ink); border-color: var(--ink-3); }
.vote-btn .material-symbols-outlined { font-size: 18px; }
.vote-btn[data-vote="up"][aria-pressed="true"] {
  color: #fff; border-color: var(--accent); background: var(--accent);
}
.vote-btn[data-vote="pass"][aria-pressed="true"] {
  color: #fff; border-color: var(--warn); background: var(--warn);
}
#vote-reason {
  font-size: 12.5px; color: var(--ink-3); font-style: italic;
  margin: 0 0 0 4px;
}

.thread { display: flex; flex-direction: column; gap: 12px; }
.msg { padding: 11px 15px; border-radius: 14px; max-width: 80%; font-size: 14px; line-height: 1.45; }
.msg .who { font-size: 11px; color: var(--ink-3); margin-bottom: 4px; font-weight: 600; }
.msg-in  { background: var(--card-2); border: 1px solid var(--line); align-self: flex-start; border-bottom-left-radius: 4px; }
.msg-out { background: var(--accent); color: #fff; align-self: flex-end; border-bottom-right-radius: 4px; }
.msg-out .who { color: rgba(255,255,255,0.75); }
.msg-out .msg-ts { color: rgba(255,255,255,0.7); }
.msg-note { background: var(--caution-soft); align-self: center; font-size: 13px; font-style: italic; }
.msg-ts { font-size: 10px; color: var(--ink-3); margin-top: 4px; }

.shots { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px; }
.shots a { display: block; border: 1px solid var(--line); border-radius: 12px; overflow: hidden; }
.shots img { width: 100%; display: block; }
.shots .caption { font-size: 11px; color: var(--ink-3); padding: 6px 10px; }

.detail-source-link {
  display: inline-flex; align-items: center; gap: 6px; margin-top: 8px; margin-right: 18px;
  font-size: 13px; color: var(--accent); text-decoration: none; font-weight: 600;
}
.detail-source-link:hover { text-decoration: underline; }

/* Street View — full-width and tall so it's actually useful */
.streetview-wrap {
  position: relative;
  width: 100%;
  aspect-ratio: 16 / 10;
  border-radius: 16px;
  overflow: hidden;
  background: var(--img-bg);
}
.streetview-wrap iframe {
  width: 100%; height: 100%;
  border: 0;
  display: block;
}
@media (max-width: 700px) {
  .streetview-wrap { aspect-ratio: 4 / 3; }
}
"""


def _render_kv(L: Listing, walk_map, drive_map, drive_bakery) -> str:
    rows: list[str] = []

    def row(k: str, v: str, klass: str = "v") -> str:
        return f'<div class="row"><span class="k">{_esc(k)}</span><span class="{klass}">{v}</span></div>'

    if L.price:
        rows.append(row("price", f"${L.price:,}/mo"))
    bb = []
    if L.beds: bb.append(f"{L.beds:g} bd")
    if L.baths: bb.append(f"{L.baths:g} ba")
    if L.sqft: bb.append(f"{L.sqft:,} sqft ({round(L.sqft / 10.7639)} m²)")
    if bb:
        rows.append(row("size", " · ".join(_esc(b) for b in bb)))

    if L.dog_policy:
        label = dogs.LABELS.get(L.dog_policy, L.dog_policy)
        cls = "v warn" if L.dog_policy == "no_dogs" else (
            "v caution" if L.dog_policy == "small_only" else "v"
        )
        rows.append(row("dogs", _esc(label), cls))
    if L.parking:
        parking_warn = (L.parking or "").lower() in {"no parking", "none"}
        rows.append(row("parking", _esc(_scrub(L.parking)), "v warn" if parking_warn else "v"))
    if L.laundry:
        laundry_warn = (L.laundry or "").lower() in {"none", "no laundry", "hookups only"}
        rows.append(row("laundry", _esc(_scrub(L.laundry)), "v warn" if laundry_warn else "v"))
    if L.has_yard:
        rows.append(row("yard", _esc(_scrub(L.yard_note or "yes")), "v"))
    elif L.has_yard is False:
        rows.append(row("yard", "no private outdoor", "v"))
    if L.outdoor_visible and not L.has_yard:
        rows.append(row("outdoor", _esc(_scrub(L.outdoor_visible)), "v"))
    if L.other_visible:
        rows.append(row("features", _esc(_scrub(L.other_visible)), "v"))

    # Walk / drive times — same logic as the card.
    is_marin = L.lat is not None and L.lat > 37.84
    # Plain word suffixes — clearer than emoji and don't shift baseline.
    WALK = ""
    DRIVE = ""
    WALK_SUFFIX = ' <span class="mode-word">walk</span>'
    DRIVE_SUFFIX = ' <span class="mode-word">drive</span>'

    # Listing origin for Directions URL — None when we lack coords.
    origin = (L.lat, L.lng) if (L.lat is not None and L.lng is not None) else None

    if is_marin and drive_map:
        from .walk import SF_CENTER as _SFC
        def _best(anchors):
            best = None
            for a in anchors:
                m = drive_map.get((L.key, a.name))
                if m is None: continue
                if best is None or m < best[1]: best = (a, m)
            return best
        for label, anchors in [("trail", TRAILS), ("beach", BEACHES), ("bakery", BAKERIES)]:
            b = _best(anchors)
            if b:
                a, m = b
                link = _anchor_link_html(a, origin=origin, mode="driving")
                rows.append(row(label, f'{m} min{DRIVE_SUFFIX} · {link}'))
        sf = drive_map.get((L.key, _SFC[0].name))
        if sf is not None:
            link = _anchor_link_html(_SFC[0], origin=origin, mode="driving")
            rows.append(row("to sf", f'{sf} min{DRIVE_SUFFIX} · {link}'))
    elif walk_map is not None:
        np = nearest(walk_map, L.key, TRAILS)
        if np:
            a, m = np
            link = _anchor_link_html(a, origin=origin, mode="walking")
            rows.append(row("trail", f'{m} min{WALK_SUFFIX} · {link}', _walk_class(m)))
        nb = nearest(walk_map, L.key, BEACHES)
        if nb:
            a, m = nb
            link = _anchor_link_html(a, origin=origin, mode="walking")
            rows.append(row("beach", f'{m} min{WALK_SUFFIX} · {link}', _walk_class(m)))
        nba = nearest(walk_map, L.key, BAKERIES)
        if nba:
            a, m = nba
            if m > 45 and drive_bakery:
                d_a, d_m = drive_bakery
                link = _anchor_link_html(d_a, origin=origin, mode="driving")
                rows.append(row("bakery", f'{d_m} min{DRIVE_SUFFIX} · {link}'))
            else:
                link = _anchor_link_html(a, origin=origin, mode="walking")
                rows.append(row("bakery", f'{m} min{WALK_SUFFIX} · {link}', _walk_class(m)))

    if L.contact_name or L.contact_phone or L.contact_email:
        bits = []
        if L.contact_name:
            name_html = _esc(_scrub(L.contact_name))
            if L.contact_url:
                name_html = f'<a href="{_esc(L.contact_url)}" target="_blank" rel="noopener">{name_html}</a>'
            bits.append(name_html)
        if L.contact_phone:
            ph = _esc(_scrub(L.contact_phone))
            tel = re.sub(r"[^\d+]", "", ph)
            bits.append(f'<a href="tel:{tel}">{ph}</a>' if tel else ph)
        if L.contact_email:
            em = _esc(_scrub(L.contact_email))
            bits.append(f'<a href="mailto:{em}">{em}</a>' if "@" in em else em)
        contact_html = " · ".join(bits)
        if L.contact_note:
            contact_html += f' <span class="contact-note">{_esc(_scrub(L.contact_note))}</span>'
        rows.append(row("contact", contact_html))

    # Map row — address link, place_id-free since we don't have one for arbitrary addresses.
    cleaned = _clean_address_for_maps(L.address or "")
    if cleaned:
        q = cleaned if "san francisco" in cleaned.lower() or "mill valley" in cleaned.lower() else f"{cleaned}, San Francisco, CA"
        rows.append(row("map", f'<a href="https://www.google.com/maps/search/?api=1&query={quote_plus(q)}" target="_blank" rel="noopener">{_esc(_scrub(cleaned))} <span class="material-symbols-outlined link-icon">arrow_outward</span></a>'))

    return f'<div class="detail-kv">{"".join(rows)}</div>'


def _render_thread(conn: sqlite3.Connection, listing_key: str) -> str:
    rows = conn.execute(
        "SELECT direction, sender, body, ts, extracted_json FROM interactions WHERE listing_key=? ORDER BY ts",
        (listing_key,),
    ).fetchall()
    if not rows:
        return ""
    import json as _json
    parts: list[str] = []
    for r in rows:
        d = r[0]
        sender = r[1] or ("Landlord" if d == "in" else "You" if d == "out" else "Note")
        body = r[2] or ""
        try:
            dt = datetime.fromisoformat(str(r[3])).replace(tzinfo=timezone.utc).astimezone(PACIFIC)
            when = dt.strftime("%b %-d · %-I:%M %p")
        except Exception:
            when = str(r[3])
        klass = {"in": "msg msg-in", "out": "msg msg-out"}.get(d, "msg msg-note")
        # Use the LLM summary if available; full body collapses below it.
        summary = ""
        if r[4]:
            try:
                summary = _json.loads(r[4]).get("summary") or ""
            except Exception:
                pass
        body_html = _esc(_scrub(summary or body))
        parts.append(
            f'<div class="{klass}">'
            f'<div class="who">{_esc(_scrub(sender))}</div>'
            f'<div>{body_html}</div>'
            f'<div class="msg-ts">{_esc(when)}</div>'
            f"</div>"
        )
    return f'<div class="thread">{"".join(parts)}</div>'


def _render_attachments(conn: sqlite3.Connection, listing_key: str) -> str:
    rows = conn.execute(
        "SELECT filename, kind, caption FROM attachments WHERE listing_key=? ORDER BY ts",
        (listing_key,),
    ).fetchall()
    if not rows:
        return ""
    items = []
    for fn, kind, caption in rows:
        cap = caption or kind or ""
        items.append(
            f'<a href="/shots/{_esc(fn)}" target="_blank" rel="noopener">'
            f'<img src="/shots/{_esc(fn)}" alt="" loading="lazy">'
            f'<div class="caption">{_esc(cap)}</div>'
            f"</a>"
        )
    return f'<div class="shots">{"".join(items)}</div>'


def _compose_share_blurb(L: Listing) -> str:
    """Build a WhatsApp/iMessage preview line from listing facts."""
    if L.share_blurb:
        return L.share_blurb
    bits = []
    if L.price:
        bits.append(f"${L.price:,}/mo")
    bb = []
    if L.beds: bb.append(f"{L.beds:g}bd")
    if L.baths: bb.append(f"{L.baths:g}ba")
    if bb: bits.append("/".join(bb))
    if L.hood:
        bits.append(L.hood)
    head = " · ".join(bits)
    if L.visual_summary:
        return f"{head}. {L.visual_summary}"
    if L.llm_reason:
        return f"{head}. {L.llm_reason}"
    return head or "Casita listing"


def render_detail(L: Listing, conn: sqlite3.Connection, walk_map=None,
                  drive_map=None, drive_bakery_map=None) -> str:
    listing_path = listing_url(L)
    share_blurb = _scrub(_compose_share_blurb(L)) or "Casita listing"

    # Eliminated banner — soft-delete via listing_status. The status is the
    # outcome ("declined_by_landlord"); the note is the human reason.
    status_row = conn.execute(
        "SELECT status, status_note FROM listing_status WHERE listing_key=?", (L.key,),
    ).fetchone()
    elim_html = ""
    is_eliminated = bool(status_row and status_row[0] in {"declined_by_landlord", "declined_by_us", "passed_on"})
    if is_eliminated:
        label = status_row[0].replace("_", " ")
        note = status_row[1] or ""
        note_html = f'<span class="note">{_esc(_scrub(note))}</span>' if note else ""
        elim_html = f'<div class="detail-eliminated"><strong>Eliminated · {_esc(label)}</strong>{note_html}</div>'

    # Vote bar — up / pass, with a "why?" prompt on click. Reasons are the
    # signal we share back to Claude to improve future Gemini ranking.
    vote_bar_html = (
        f'<div class="vote-bar">'
        f'<button class="vote-btn" type="button" data-key="{_esc(L.key)}" data-vote="up" aria-pressed="false" title="Mark as preferred — adds your reason to the learning loop">'
        f'<span class="material-symbols-outlined">arrow_upward</span><span>like</span>'
        f'</button>'
        f'<button class="vote-btn" type="button" data-key="{_esc(L.key)}" data-vote="pass" aria-pressed="false" title="Mark as not a fit — adds your reason to the learning loop">'
        f'<span class="material-symbols-outlined">arrow_downward</span><span>pass</span>'
        f'</button>'
        f'<span id="vote-reason"></span>'
        f'</div>'
    )
    og_image = public_url(f"/og/listing/{_slug(L)}.png")
    photos = L.photos or ([L.image_url] if L.image_url else [])
    photos = [p for p in photos if p]
    if photos:
        slides = "".join(
            f'<div class="carousel-slide" data-idx="{i}">'
            f'<img src="{_esc(p)}" alt="" loading="lazy"></div>'
            for i, p in enumerate(photos)
        )
        dots = "".join(
            f'<span class="carousel-dot"{" data-active=\"true\"" if i == 0 else ""}></span>'
            for i, _ in enumerate(photos)
        )
        hero = (
            f'<div class="hero carousel">'
            f'<div class="carousel-track">{slides}</div>'
            f'<div class="carousel-dots">{dots}</div>'
            f'</div>'
        )
    else:
        hero = '<div class="hero"></div>'

    addr = _scrub(L.address or L.title or "Listing") or "Listing"
    sub = []
    if L.price: sub.append(f"${L.price:,}/mo")
    if L.beds: sub.append(f"{L.beds:g} bd")
    if L.baths: sub.append(f"{L.baths:g} ba")
    if L.sqft: sub.append(f"{L.sqft:,} sqft")

    vibe_html = ""
    if L.visual_summary:
        vibe_html = (
            f'<div class="detail-section">'
            f'<h2>Gemini\'s eye</h2>'
            f'<div style="font-size:14px;line-height:1.5;color:var(--ink-2);font-style:italic">'
            f'{_esc(_scrub(L.visual_summary))}'
            f'</div></div>'
        )

    reason_html = ""
    if L.llm_reason:
        sev = L.llm_severity or "ok"
        cls = {"filtered": "why why-filtered", "concerns": "why why-concerns"}.get(sev, "why")
        # No rank number — it churns on every re-rank with no stable meaning.
        # Severity color is the signal. Only label "filtered" listings so the
        # red border isn't ambiguous.
        prefix = '<span class="why-rank">FILTERED</span>' if sev == "filtered" else ""
        reason_html = (
            f'<div class="{cls}" style="margin-bottom:16px">'
            f'{prefix}'
            f'<span class="why-text">{_esc(_scrub(L.llm_reason))}</span>'
            f'</div>'
        )

    thread = _render_thread(conn, L.key)
    thread_html = (
        f'<div class="detail-section"><h2>Conversation</h2>{thread}</div>'
        if thread else ""
    )
    shots = _render_attachments(conn, L.key)
    shots_html = (
        f'<div class="detail-section"><h2>Attached screenshots</h2>{shots}</div>'
        if shots else ""
    )

    facts_html = (
        f'<div class="detail-section">'
        f'<h2>Facts</h2>'
        f'{_render_kv(L, walk_map, drive_map, drive_bakery_map.get(L.key) if drive_bakery_map else None)}'
        f'</div>'
    )

    source_link = ""
    # Primary source first, then every duplicate found via cross-source dedup.
    # `also_on` is populated by dedup.deduplicate_db when this listing absorbed
    # a secondary from another site.
    sources: list[tuple[str, str]] = []
    if L.url:
        sources.append((L.source, L.url))
    for alt in (L.raw.get("also_on") or []):
        u = alt.get("url")
        s = alt.get("source") or "other"
        if u and u != L.url:
            sources.append((s, u))
    if sources:
        heading = "Source" if len(sources) == 1 else "Sources"
        links = "".join(
            f'<a class="detail-source-link" href="{_esc(u)}" target="_blank" rel="noopener">'
            f'View on {_esc(s)} <span class="material-symbols-outlined link-icon">arrow_outward</span></a>'
            for s, u in sources
        )
        source_link = f'<div class="detail-section"><h2>{heading}</h2>{links}</div>'

    # Google Street View embed — uses the Maps Embed API. The view defaults
    # to looking at the listing address. Only show when we have either an
    # address or lat/lng.
    import os as _os
    streetview_html = ""
    sv_key = _os.environ.get("GOOGLE_MAPS_API_KEY")
    if sv_key:
        if L.lat is not None and L.lng is not None:
            sv_param = f"location={L.lat},{L.lng}"
        elif L.address:
            from urllib.parse import quote_plus as _qp
            sv_param = f"location={_qp(L.address)}"
        else:
            sv_param = None
        if sv_param:
            sv_src = (
                f"https://www.google.com/maps/embed/v1/streetview?key={sv_key}"
                f"&{sv_param}&heading=0&pitch=0&fov=90"
            )
            streetview_html = (
                f'<div class="detail-section">'
                f'<h2>Street view</h2>'
                f'<div class="streetview-wrap">'
                f'<iframe src="{sv_src}" loading="lazy" allowfullscreen '
                f'referrerpolicy="no-referrer-when-downgrade"></iframe>'
                f'</div></div>'
            )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" media="(prefers-color-scheme: light)" content="#f1eee5">
<meta name="theme-color" media="(prefers-color-scheme: dark)" content="#15130d">
<meta name="color-scheme" content="light dark">
<meta name="robots" content="noindex, nofollow">
<link rel="icon" type="image/svg+xml" href="/assets/favicon.svg">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,300..600;1,9..144,300..500&display=swap">
<link rel="stylesheet" href="https://fonts.googleapis.com/icon?family=Material+Symbols+Outlined&icon_names=arrow_upward,arrow_downward,arrow_outward,arrow_back,ios_share,cancel&display=block">
<meta property="og:type" content="website">
<meta property="og:title" content="{_esc(addr)}">
<meta property="og:description" content="{_esc(share_blurb)}">
<meta property="og:image" content="{_esc(og_image)}">
<meta property="og:image:type" content="image/png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:url" content="{_esc(public_url(listing_path))}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:image" content="{_esc(og_image)}">
<title>{_esc(addr)} · casita</title>
<style>{CSS}{_DETAIL_CSS}</style>
{THEME_PREPAINT}
</head>
<body>
<div class="detail-wrap">
  <div class="head-row" style="margin-bottom:14px">
    <a class="back-link" href="/" style="margin-bottom:0"><span class="material-symbols-outlined link-icon">arrow_back</span> Back to all listings</a>
    <div class="header-actions">
      <button class="share-btn" aria-label="Share" title="Share"
              data-url="{_esc(public_url(listing_path))}"
              data-title="{_esc(addr)} · casita">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8"/><polyline points="16 6 12 2 8 6"/><line x1="12" y1="2" x2="12" y2="15"/></svg>
      </button>
      {THEME_SWITCH_HTML}
    </div>
  </div>
  {elim_html}
  {hero}
  <div class="detail-hood-prominent">{_esc((L.hood or "Unknown neighborhood").replace("-"," "))}</div>
  <h1 class="detail-title">{_esc(addr)}</h1>
  <div class="detail-head">
    <div class="detail-price">{_esc(sub[0]) if sub else 'price on request'}</div>
    <div class="detail-stats">{' · '.join(_esc(s) for s in sub[1:])}</div>
  </div>
  {reason_html}
  {vote_bar_html}
  {vibe_html}
  {facts_html}
  {thread_html}
  {shots_html}
  {streetview_html}
  {source_link}
</div>
{CAROUSEL_JS}
{SHARE_JS}
{VOTE_JS}
{THEME_SWITCH_JS}
</body>
</html>
"""
