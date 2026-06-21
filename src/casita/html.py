"""Static HTML renderer.

iPhone-first responsive layout. Helvetica Neue. Designed to be uploaded
to a public GCS bucket and shared by URL.
"""
import re
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

from . import dogs
from .models import Listing
from .rank import score
from .walk import BAKERIES, BEACHES, PRESIDIO_GATES, TRAILS, minutes_to, nearest


def site_url() -> str:
    return os.environ.get("CASITA_SITE_URL", "http://localhost:8765").rstrip("/")


def public_url(path: str) -> str:
    return f"{site_url()}{path if path.startswith('/') else '/' + path}"


def _gcal_link(L: Listing, viewing_at: str, duration_minutes: int = 60) -> str | None:
    """Build a Google Calendar event-create URL for a scheduled viewing.

    viewing_at is interpreted as wall-clock time in America/Los_Angeles.
    End = start + duration_minutes.
    """
    try:
        start = datetime.fromisoformat(viewing_at).replace(tzinfo=PACIFIC)
    except Exception:
        return None
    end = start + timedelta(minutes=duration_minutes)
    fmt = "%Y%m%dT%H%M%S"
    # Google Calendar accepts local times when ctz is set.
    dates = f"{start.strftime(fmt)}/{end.strftime(fmt)}"
    address = L.address or L.title or "TBD"
    title = f"Viewing · {address.split(',')[0]}"

    details_parts = []
    if L.url:
        details_parts.append(f"Listing: {L.url}")
    if L.price:
        details_parts.append(f"Price: ${L.price:,}/mo")
    bb = []
    if L.beds: bb.append(f"{L.beds:g} bd")
    if L.baths: bb.append(f"{L.baths:g} ba")
    if L.sqft: bb.append(f"{L.sqft:,} sqft")
    if bb: details_parts.append(" · ".join(bb))
    if L.contact_name or L.contact_phone:
        c = " ".join(filter(None, [L.contact_name, L.contact_phone]))
        details_parts.append(f"Landlord: {c}")
    if L.parking: details_parts.append(f"Parking: {L.parking}")
    if L.laundry: details_parts.append(f"Laundry: {L.laundry}")
    if L.has_yard: details_parts.append(f"Yard: {L.yard_note or 'yes'}")
    details = "\n".join(details_parts)

    params = {
        "action": "TEMPLATE",
        "text": title,
        "dates": dates,
        "details": details,
        "location": address,
        "ctz": "America/Los_Angeles",
    }
    qs = "&".join(f"{k}={quote_plus(v)}" for k, v in params.items())
    return f"https://calendar.google.com/calendar/render?{qs}"


def _clean_address_for_maps(addr: str) -> str | None:
    """Turn a Craigslist-ish address into something Google Maps can geocode.

    Cases:
    - "1527-A Pershing Drive near Lincoln Blvd." (street # + cross): drop the
      cross — the specific address is enough.
    - "9th Ave near Moraga" (cross-streets only): rewrite as
      "9th Ave & Moraga" — Google parses this as an intersection.
    """
    if not addr:
        return None
    addr = addr.strip().rstrip(",.;")
    m = re.match(r"(.+?)\s+near\s+(.+)$", addr, re.IGNORECASE)
    if not m:
        return addr
    primary = m.group(1).strip().rstrip(",.;")
    cross = m.group(2).strip().rstrip(",.;")
    # Numeric prefix → specific address; cross-street is redundant.
    if re.match(r"^\d", primary):
        return primary
    return f"{primary} & {cross}"

PACIFIC = ZoneInfo("America/Los_Angeles")


# Set the data-theme attribute BEFORE any styles paint, to avoid a flash of
# the wrong theme on load. Stored value: 'light' | 'dark' | 'system' (default).
THEME_PREPAINT = """<script>
(function(){
  try {
    var t = localStorage.getItem('casita-theme');
    if (t === 'light' || t === 'dark') {
      document.documentElement.setAttribute('data-theme', t);
    }
  } catch (e) {}
})();
</script>"""


# Three-state toggle: system / light / dark. Style mirrors common docs-site
# theme switchers — compact pill with icon buttons.
THEME_SWITCH_HTML = """<div class="theme-switch" role="radiogroup" aria-label="Theme">
  <button type="button" data-theme-value="system" aria-pressed="true" title="Match system" aria-label="System theme">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="14" rx="2"/><path d="M8 21h8"/><path d="M12 18v3"/></svg>
  </button>
  <button type="button" data-theme-value="light" aria-pressed="false" title="Light" aria-label="Light theme">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>
  </button>
  <button type="button" data-theme-value="dark" aria-pressed="false" title="Dark" aria-label="Dark theme">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
  </button>
</div>"""


SEARCH_JS = """<script>
(function() {
  var input = document.getElementById('q');
  var countEl = document.getElementById('search-count');
  var chips = Array.from(document.querySelectorAll('.since-chip'));
  var dateInput = document.getElementById('since-date');
  if (!input) return;
  var cards = Array.from(document.querySelectorAll('.card'));

  // sinceCutoff is a YYYY-MM-DD string ('' = any time). A card shows when it
  // matches the text query AND its data-added date is >= the cutoff. Date
  // strings compare lexically, so a plain string >= works for the cutoff.
  // activeDays records which control owns the current cutoff: '' = the
  // "Any time" chip, a preset string ('1'/'3'/'7') = that chip, or null = a
  // custom date. Tracking it explicitly (vs re-deriving from the date) keeps
  // the chip highlight stable across repaints / midnight rollover.
  var sinceCutoff = '';
  var activeDays = '';
  var DATE_RE = /^\\d{4}-\\d{2}-\\d{2}$/;

  function daysAgoISO(n) {
    var d = new Date();
    d.setDate(d.getDate() - n);
    return d.toISOString().slice(0, 10);
  }

  function syncHash(q) {
    var parts = [];
    if (q) parts.push('q=' + encodeURIComponent(q));
    if (sinceCutoff) parts.push('since=' + sinceCutoff);
    var hash = parts.length ? '#' + parts.join('&') : '';
    if (location.hash !== hash) {
      history.replaceState(null, '', location.pathname + location.search + hash);
    }
  }

  function apply() {
    var q = (input.value || '').trim().toLowerCase();
    var tokens = q ? q.split(/\\s+/).filter(Boolean) : [];
    var shown = 0;
    cards.forEach(function(c) {
      var hay = c.dataset.search || '';
      var textMatch = tokens.every(function(t) { return hay.indexOf(t) !== -1; });
      var added = c.dataset.added || '';
      // No cutoff → show all; with a cutoff, an unknown date is treated as old.
      var dateMatch = !sinceCutoff || (added && added >= sinceCutoff);
      var match = textMatch && dateMatch;
      c.style.display = match ? '' : 'none';
      if (match) shown++;
    });
    if (countEl) countEl.textContent = shown;
    syncHash(q);
  }

  // Reflect the active cutoff in the chip / date-input pressed states, driven
  // by activeDays (not by re-deriving the date — see note above).
  function paintControls() {
    chips.forEach(function(ch) {
      ch.setAttribute('aria-pressed', (activeDays !== null && ch.dataset.days === activeDays) ? 'true' : 'false');
    });
    if (dateInput) {
      var custom = activeDays === null && !!sinceCutoff;
      dateInput.setAttribute('data-active', custom ? 'true' : 'false');
      if (custom) dateInput.value = sinceCutoff;
      else if (!sinceCutoff) dateInput.value = '';
    }
  }

  chips.forEach(function(ch) {
    ch.addEventListener('click', function() {
      activeDays = ch.dataset.days;  // '' for Any, or '1'/'3'/'7'
      sinceCutoff = activeDays === '' ? '' : daysAgoISO(parseInt(activeDays, 10));
      paintControls();
      apply();
    });
  });
  if (dateInput) {
    dateInput.addEventListener('change', function() {
      var v = dateInput.value || '';
      sinceCutoff = DATE_RE.test(v) ? v : '';
      activeDays = sinceCutoff ? null : '';  // custom date owns it, or fall back to Any
      paintControls();
      apply();
    });
  }

  input.addEventListener('input', apply);

  // Restore both filters from the URL hash on load (#q=...&since=YYYY-MM-DD).
  var raw = location.hash.replace(/^#/, '');
  raw.split('&').forEach(function(pair) {
    var kv = pair.split('=');
    if (kv[0] === 'q' && kv[1]) input.value = decodeURIComponent(kv[1].replace(/\\+/g, '%20'));
    if (kv[0] === 'since' && kv[1]) {
      var v = decodeURIComponent(kv[1]);
      if (DATE_RE.test(v)) sinceCutoff = v;  // ignore malformed cutoffs
    }
  });
  // A restored cutoff is treated as a custom date unless it matches a preset.
  if (sinceCutoff) {
    activeDays = null;
    chips.forEach(function(ch) {
      if (ch.dataset.days && sinceCutoff === daysAgoISO(parseInt(ch.dataset.days, 10))) activeDays = ch.dataset.days;
    });
  }
  paintControls();
  apply();
})();
</script>"""


SHARE_JS = """<script>

(function() {
  document.querySelectorAll('.share-btn').forEach(function(btn) {
    btn.addEventListener('click', async function() {
      var url = btn.dataset.url || location.href;
      var title = btn.dataset.title || document.title;
      var orig = btn.innerHTML;
      try {
        if (navigator.share) {
          await navigator.share({ title: title, url: url });
        } else {
          await navigator.clipboard.writeText(url);
          btn.innerHTML = 'Copied';
          setTimeout(function() { btn.innerHTML = orig; }, 1500);
        }
      } catch (e) {
        // user cancelled native share sheet — no-op
      }
    });
  });
})();
</script>"""


# Votes are browser-local: stored in localStorage keyed by listing key.
# Each vote captures {vote: 'up'|'pass', reason: '...', ts: ISO}. The reason is
# the load-bearing part — it's what we feed back to Gemini as multi-shot
# examples to improve future ranking.
# Elimination is server-side (via listing_status), not done from the browser.
# Export is a shareable URL (#v=base64-JSON) sent via the native share sheet,
# so it works from a phone — clipboard is the fallback for desktop.
VOTE_JS = """<script>
(function() {
  const KEY = 'casita_votes_v1';
  // Migrate old "string vote" entries → {vote, reason, ts} shape.
  const raw = JSON.parse(localStorage.getItem(KEY) || '{}');
  const votes = {};
  for (const [k, v] of Object.entries(raw)) {
    votes[k] = typeof v === 'string' ? { vote: v, reason: '', ts: '' } : v;
  }
  const save = () => localStorage.setItem(KEY, JSON.stringify(votes));

  // Encode votes into a shareable URL fragment. base64(JSON), stripped padding.
  function shareUrl() {
    const payload = JSON.stringify({ v: 1, votes: votes, ts: new Date().toISOString() });
    const b64 = btoa(unescape(encodeURIComponent(payload)))
      .replace(/\\+/g, '-').replace(/\\//g, '_').replace(/=+$/, '');
    return location.origin + '/#v=' + b64;
  }

  // Apply persisted votes on load: dim pass'd cards, highlight up'd cards,
  // and move pass'd cards to the bottom of the grid (above eliminated).
  const grid = document.querySelector('main.grid');
  if (grid) {
    const cards = Array.from(grid.querySelectorAll('.card[data-key]'));
    cards.forEach(card => {
      const entry = votes[card.dataset.key];
      if (!entry) return;
      card.classList.add('voted-' + entry.vote);
      const badge = document.createElement('div');
      badge.className = 'card-vote-badge';
      badge.dataset.vote = entry.vote;
      badge.textContent = entry.vote === 'up' ? 'liked' : 'pass';
      if (entry.reason) badge.title = entry.reason;
      card.appendChild(badge);
    });
    cards.filter(c => votes[c.dataset.key] && votes[c.dataset.key].vote === 'pass'
                       && !c.classList.contains('eliminated'))
         .forEach(c => grid.insertBefore(c, grid.querySelector('.card.eliminated') || null));
  }

  // Export button on the index header — share a URL via native share sheet,
  // or fall back to clipboard on desktop.
  const exportBtn = document.getElementById('export-votes');
  if (exportBtn) {
    const label = exportBtn.querySelector('.export-btn-label');
    const setLabel = (s) => { if (label) label.textContent = s; };
    exportBtn.addEventListener('click', async () => {
      const count = Object.keys(votes).length;
      if (!count) {
        setLabel('no votes yet');
        setTimeout(() => setLabel('export votes'), 1500);
        return;
      }
      const url = shareUrl();
      const flash = (s) => {
        exportBtn.dataset.state = 'copied';
        setLabel(s);
        setTimeout(() => { delete exportBtn.dataset.state; setLabel('export votes'); }, 2000);
      };
      if (navigator.share) {
        try { await navigator.share({ title: 'casita votes', url: url }); flash(`shared ${count}`); }
        catch (e) { /* user cancelled */ }
        return;
      }
      try { await navigator.clipboard.writeText(url); flash(`copied ${count}`); }
      catch (e) { prompt('share this link:', url); }
    });
  }

  // Vote buttons on the detail page. Clicking 'up' or 'pass' prompts for a
  // short "why?" — the reason is the signal we want to share back to Claude.
  document.querySelectorAll('.vote-btn[data-vote]').forEach(btn => {
    const key = btn.dataset.key;
    const vote = btn.dataset.vote;
    const sync = () => {
      const current = votes[key] && votes[key].vote;
      const reasonText = (votes[key] && votes[key].reason) || '';
      document.querySelectorAll(`.vote-btn[data-key="${key}"]`).forEach(b => {
        b.setAttribute('aria-pressed', b.dataset.vote === current ? 'true' : 'false');
      });
      const note = document.getElementById('vote-reason');
      if (note) note.textContent = reasonText ? '“' + reasonText + '”' : '';
    };
    sync();
    btn.addEventListener('click', () => {
      const existing = votes[key];
      if (existing && existing.vote === vote) {
        delete votes[key];
        save(); sync(); return;
      }
      const placeholder = vote === 'up'
        ? 'what makes this a fit? (optional — helps Claude learn)'
        : 'why is this not a fit? (optional — helps Claude learn)';
      const prior = (existing && existing.reason) || '';
      const reason = (prompt(placeholder, prior) || '').trim();
      votes[key] = { vote: vote, reason: reason, ts: new Date().toISOString() };
      save(); sync();
    });
  });
})();
</script>"""


CAROUSEL_JS = """<script>

(function() {
  // Highlight the dot that matches the most-visible carousel slide.
  document.querySelectorAll('.carousel').forEach(function(car) {
    var dots = car.querySelectorAll('.carousel-dot');
    var slides = car.querySelectorAll('.carousel-slide');
    if (!dots.length || !slides.length) return;
    var track = car.querySelector('.carousel-track');
    function setActive(idx) {
      dots.forEach(function(d, i) {
        if (i === idx) d.setAttribute('data-active', 'true');
        else d.removeAttribute('data-active');
      });
    }
    var obs = new IntersectionObserver(function(entries) {
      var best = null, bestRatio = 0;
      entries.forEach(function(e) {
        if (e.isIntersecting && e.intersectionRatio > bestRatio) {
          bestRatio = e.intersectionRatio;
          best = parseInt(e.target.dataset.idx, 10);
        }
      });
      if (best !== null) setActive(best);
    }, { root: track, threshold: [0.5, 0.75, 1.0] });
    slides.forEach(function(s) { obs.observe(s); });
  });
})();
</script>"""


THEME_SWITCH_JS = """<script>

(function(){
  var KEY = 'casita-theme';
  var stored = (function(){ try { return localStorage.getItem(KEY); } catch(e) { return null; } })() || 'system';
  var buttons = document.querySelectorAll('.theme-switch button');
  function apply(value) {
    if (value === 'system') {
      document.documentElement.removeAttribute('data-theme');
    } else {
      document.documentElement.setAttribute('data-theme', value);
    }
    buttons.forEach(function(b){
      b.setAttribute('aria-pressed', b.dataset.themeValue === value ? 'true' : 'false');
    });
    try { localStorage.setItem(KEY, value); } catch(e) {}
  }
  buttons.forEach(function(b){ b.addEventListener('click', function(){ apply(b.dataset.themeValue); }); });
  apply(stored);
})();
</script>"""

CSS = """
:root {
  /* Light — warm paper, forest green, clay warmth. Editorial. */
  --paper: #f1eee5;
  --bg: #f1eee5;
  --card: #fbfaf4;
  --card-2: #f6f3ea;
  --ink: #211d16;
  --ink-2: #5c554a;
  --ink-3: #9a9282;
  --line: #e3dfd2;
  --line-2: #d6d1c1;
  --img-bg: #e7e2d5;
  --accent: #355c43;
  --accent-soft: #e6ede5;
  --clay: #b06440;
  --clay-soft: #f3e6dd;
  --gold: #9c7b2e;
  --gold-soft: #f0e8d2;
  --warn: #a8504a;
  --warn-soft: #f2dfdb;
  --caution: #9c7321;
  --caution-soft: #f0e6cf;
  --info: #2f5c80;
  --info-soft: #e0eaf0;
  --shadow: rgba(40,34,22,0.07);
  --shadow-2: rgba(40,34,22,0.13);
  --backdrop: rgba(28,24,16,0.62);
  --serif: "Fraunces", "Iowan Old Style", "Hoefler Text", Georgia, serif;
  --sans: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
}
:root[data-theme="light"] {
  --paper: #f1eee5; --bg: #f1eee5; --card: #fbfaf4; --card-2: #f6f3ea;
  --ink: #211d16; --ink-2: #5c554a; --ink-3: #9a9282;
  --line: #e3dfd2; --line-2: #d6d1c1; --img-bg: #e7e2d5;
  --accent: #355c43; --accent-soft: #e6ede5;
  --clay: #b06440; --clay-soft: #f3e6dd;
  --gold: #9c7b2e; --gold-soft: #f0e8d2;
  --warn: #a8504a; --warn-soft: #f2dfdb;
  --caution: #9c7321; --caution-soft: #f0e6cf;
  --info: #2f5c80; --info-soft: #e0eaf0;
  --shadow: rgba(40,34,22,0.07); --shadow-2: rgba(40,34,22,0.13);
  --backdrop: rgba(28,24,16,0.62);
}
:root[data-theme="dark"] {
  --paper: #15130d; --bg: #15130d; --card: #201d15; --card-2: #1a170f;
  --ink: #f1ece0; --ink-2: #aaa294; --ink-3: #756d5e;
  --line: #2e2a20; --line-2: #3a352a; --img-bg: #272217;
  --accent: #82bd97; --accent-soft: #1d271f;
  --clay: #e0966b; --clay-soft: #2f2218;
  --gold: #d6b25f; --gold-soft: #2c2614;
  --warn: #e09089; --warn-soft: #321f1c;
  --caution: #d4ad55; --caution-soft: #2c2613;
  --info: #7fb0d6; --info-soft: #18242e;
  --shadow: rgba(0,0,0,0.45); --shadow-2: rgba(0,0,0,0.6);
  --backdrop: rgba(0,0,0,0.66);
}
@media (prefers-color-scheme: dark) {
  :root {
    --paper: #15130d; --bg: #15130d; --card: #201d15; --card-2: #1a170f;
    --ink: #f1ece0; --ink-2: #aaa294; --ink-3: #756d5e;
    --line: #2e2a20; --line-2: #3a352a; --img-bg: #272217;
    --accent: #82bd97; --accent-soft: #1d271f;
    --clay: #e0966b; --clay-soft: #2f2218;
    --gold: #d6b25f; --gold-soft: #2c2614;
    --warn: #e09089; --warn-soft: #321f1c;
    --caution: #d4ad55; --caution-soft: #2c2613;
    --info: #7fb0d6; --info-soft: #18242e;
    --shadow: rgba(0,0,0,0.45); --shadow-2: rgba(0,0,0,0.6);
    --backdrop: rgba(0,0,0,0.66);
  }
}
* { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
html, body { margin: 0; padding: 0; }
body {
  font-family: var(--sans);
  font-weight: 400;
  background: var(--bg);
  color: var(--ink);
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  text-rendering: optimizeLegibility;
  font-feature-settings: "ss01";
}

/* —— layout —— */
.wrap { max-width: 1200px; margin: 0 auto; padding: 40px 24px 100px; }
@media (max-width: 600px) { .wrap { padding: 22px 16px 64px; } }

/* —— masthead —— */
header { margin: 0 0 36px; }
@media (max-width: 600px) { header { margin-bottom: 26px; } }

.brandline { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.wordmark { display: flex; align-items: baseline; gap: 12px; }
h1 {
  margin: 0;
  font-family: var(--serif);
  font-optical-sizing: auto;
  font-weight: 420;
  font-size: 40px;
  letter-spacing: -0.015em;
  line-height: 1;
  color: var(--ink);
}
@media (max-width: 600px) { h1 { font-size: 30px; } }
.wordmark .tagline {
  font-size: 11px; font-weight: 600; letter-spacing: 0.16em; text-transform: uppercase;
  color: var(--accent); white-space: nowrap;
}
@media (max-width: 600px) { .wordmark .tagline { display: none; } }

.lede {
  margin: 16px 0 0;
  max-width: 620px;
  font-family: var(--serif);
  font-optical-sizing: auto;
  font-weight: 340;
  font-size: 19px;
  line-height: 1.5;
  letter-spacing: -0.005em;
  color: var(--ink-2);
}
.lede em { font-style: italic; color: var(--ink); }
@media (max-width: 600px) { .lede { font-size: 16.5px; margin-top: 12px; } }

/* editorial stat strip */
.stats {
  display: flex; flex-wrap: wrap; align-items: baseline;
  gap: 8px 22px;
  margin-top: 20px;
}
.stat-item { display: inline-flex; align-items: baseline; gap: 7px; }
.stat-num {
  font-family: var(--serif); font-optical-sizing: auto;
  font-weight: 460; font-size: 20px; line-height: 1; color: var(--ink);
}
.stat-num.is-accent { color: var(--accent); }
.stat-num.is-clay { color: var(--clay); }
.stat-label {
  font-size: 11px; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase;
  color: var(--ink-3);
}
.stat-sep { width: 1px; align-self: stretch; background: var(--line-2); margin: 2px 0; }
@media (max-width: 600px) { .stats { gap: 6px 16px; } .stat-num { font-size: 17px; } }

.meta {
  color: var(--ink-3);
  font-size: 12px;
  font-weight: 500;
  letter-spacing: 0.02em;
  line-height: 1.5;
  margin-top: 14px;
}

/* —— theme switcher (3-state: system / light / dark) —— */
.theme-switch {
  display: inline-flex;
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 3px;
  gap: 2px;
  box-shadow: 0 1px 2px var(--shadow);
}
.theme-switch button {
  width: 30px; height: 30px;
  border: 0; background: transparent;
  color: var(--ink-3);
  cursor: pointer; border-radius: 999px;
  display: inline-flex; align-items: center; justify-content: center;
  font-size: 14px; padding: 0;
  transition: background 0.18s ease, color 0.18s ease;
}
.theme-switch button:hover { color: var(--ink); }
.theme-switch button[aria-pressed="true"] {
  background: var(--accent);
  color: var(--card);
}
.theme-switch svg { width: 15px; height: 15px; }

/* toolbar: search + result count */
.toolbar { margin-top: 26px; }
@media (max-width: 600px) { .toolbar { margin-top: 20px; } }

.search-box {
  display: flex; align-items: center; gap: 10px;
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 11px 16px;
  width: 100%; max-width: 520px;
  box-shadow: 0 1px 2px var(--shadow);
  transition: border-color 0.18s ease, box-shadow 0.18s ease;
}
.search-box:focus-within { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
.search-box svg { flex-shrink: 0; color: var(--ink-3); width: 17px; height: 17px; }
.search-box input {
  border: 0; outline: 0; background: transparent;
  color: var(--ink); font-family: inherit; font-size: 15px;
  width: 100%; padding: 0;
}
.search-box input::placeholder { color: var(--ink-3); }
/* "Added since" filter — chip row + custom date, matches the search bar */
.since-filter {
  display: flex; align-items: center; flex-wrap: wrap; gap: 8px;
  margin-top: 12px;
}
.since-label {
  color: var(--ink-3); font-size: 11px; font-weight: 600;
  letter-spacing: 0.08em; text-transform: uppercase; margin-right: 2px;
}
.since-chip {
  font-family: inherit; font-size: 13px; font-weight: 500;
  color: var(--ink-2); background: var(--card); cursor: pointer;
  border: 1px solid var(--line); border-radius: 999px; padding: 5px 13px;
  transition: color .12s, background .12s, border-color .12s;
}
.since-chip:hover { color: var(--ink); border-color: var(--ink-3); }
.since-chip[aria-pressed="true"] {
  color: #fff; background: var(--accent); border-color: transparent;
}
.since-date {
  font-family: inherit; font-size: 13px; color: var(--ink-2);
  background: var(--card); border: 1px solid var(--line);
  border-radius: 999px; padding: 4px 11px; cursor: pointer;
  color-scheme: light dark;
}
.since-date:focus-within, .since-date:hover { border-color: var(--ink-3); }
.since-date[data-active="true"] {
  color: #fff; background: var(--accent); border-color: transparent;
}
.search-meta {
  color: var(--ink-3); font-size: 11px; margin-top: 10px;
  font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase;
}

.share-btn {
  display: inline-flex; align-items: center; justify-content: center;
  width: 38px; height: 38px;
  background: var(--card);
  color: var(--ink-2);
  border: 1px solid var(--line);
  border-radius: 999px;
  cursor: pointer;
  padding: 0;
  font-size: 14px;
  font-family: inherit;
  box-shadow: 0 1px 2px var(--shadow);
  transition: background 0.18s ease, color 0.18s ease, border-color 0.18s ease;
}
.share-btn:hover { color: var(--accent); border-color: var(--accent); }
.share-btn svg { width: 16px; height: 16px; }
.header-actions {
  display: inline-flex; gap: 10px; align-items: center;
  flex-shrink: 0;
}

/* —— grid —— */
.grid {
  display: grid;
  gap: 30px 26px;
  grid-template-columns: 1fr;
}
@media (min-width: 640px) {
  .grid { grid-template-columns: repeat(2, 1fr); gap: 32px 24px; }
}
@media (min-width: 1000px) {
  .grid { grid-template-columns: repeat(3, 1fr); }
}

/* —— card —— */
.card {
  position: relative;
  background: var(--card);
  border-radius: 16px;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  border: 1px solid var(--line);
  box-shadow: 0 1px 2px var(--shadow);
  transition: transform 0.22s cubic-bezier(.2,.7,.3,1), box-shadow 0.22s ease, border-color 0.22s ease;
  text-decoration: none;
  color: inherit;
}
.card.sev-filtered { opacity: 0.7; }
.card.eliminated   { opacity: 0.42; }
.card.voted-up     { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent), 0 6px 18px var(--shadow); }
.card.voted-pass   { opacity: 0.5; }
@media (hover: hover) {
  .card:hover {
    transform: translateY(-3px);
    box-shadow: 0 16px 36px var(--shadow-2);
    border-color: var(--line-2);
  }
  .card.sev-filtered:hover { opacity: 1; }
  .card.eliminated:hover   { opacity: 0.85; }
  .card.voted-pass:hover   { opacity: 0.85; }
}
.card-eliminated {
  padding: 8px 16px;
  font-size: 10px; font-weight: 700;
  text-transform: uppercase; letter-spacing: 0.1em;
  color: var(--warn); background: var(--warn-soft);
}
.card-eliminated .card-eliminated-note {
  display: block; font-weight: 500; text-transform: none; letter-spacing: 0;
  color: var(--ink-2); font-size: 12px; margin-top: 2px;
}
.card-vote-badge {
  position: absolute; top: 12px; right: 12px;
  font-size: 9px; font-weight: 700; letter-spacing: 0.1em;
  padding: 4px 9px; border-radius: 999px; pointer-events: none;
  text-transform: uppercase; z-index: 3;
  backdrop-filter: blur(8px);
}
.card-vote-badge[data-vote="up"]   { background: var(--accent); color: #fff; }
.card-vote-badge[data-vote="pass"] { background: var(--backdrop); color: #fff; }
.export-btn {
  background: var(--card); border: 1px solid var(--line); color: var(--ink-2);
  font: inherit; font-size: 12px; font-weight: 600;
  padding: 0 14px; height: 38px; border-radius: 999px; cursor: pointer;
  display: inline-flex; align-items: center; gap: 7px;
  box-shadow: 0 1px 2px var(--shadow);
  transition: color 0.18s ease, border-color 0.18s ease;
}
.export-btn:hover { color: var(--accent); border-color: var(--accent); }
.export-btn[data-state="copied"] { color: var(--accent); border-color: var(--accent); }
.export-btn .material-symbols-outlined { font-size: 16px; vertical-align: -3px; }
@media (max-width: 480px) { .export-btn .export-btn-label { display: none; } .export-btn { padding: 0; width: 38px; justify-content: center; } }
.material-symbols-outlined {
  font-family: 'Material Symbols Outlined';
  font-weight: 400; font-style: normal;
  font-size: 18px; line-height: 1;
  letter-spacing: normal; text-transform: none;
  display: inline-block; white-space: nowrap;
  direction: ltr; -webkit-font-smoothing: antialiased;
  font-feature-settings: 'liga';
  font-variation-settings: 'opsz' 20;
  vertical-align: -3px;
}
.material-symbols-outlined.link-icon {
  font-size: 14px; vertical-align: -2px;
  color: inherit; opacity: 0.7;
}

.card-body-link {
  display: block;
  color: inherit;
  text-decoration: none;
}
.card-body-link:hover { text-decoration: none; }
.card-body-link * { text-decoration: none; }

/* eyebrow row: neighborhood + fit verdict */
.eyebrow-row {
  display: flex; align-items: center; justify-content: space-between; gap: 10px;
}
.card-hood {
  color: var(--ink-3);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.11em;
  text-transform: uppercase;
}
.fit {
  display: inline-flex; align-items: center; gap: 5px;
  font-size: 10px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase;
  white-space: nowrap; color: var(--ink-3);
}
.fit::before {
  content: ""; width: 7px; height: 7px; border-radius: 50%;
  background: currentColor; flex-shrink: 0;
}
.fit-ok       { color: var(--accent); }
.fit-concerns { color: var(--caution); }
.fit-filtered { color: var(--warn); }

/* —— Gemini reason on card —— */
.card-reason {
  font-size: 13.5px;
  line-height: 1.5;
  color: var(--ink-2);
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.card-price-row {
  display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap;
}
.card-price {
  font-family: var(--serif); font-optical-sizing: auto;
  font-size: 25px; font-weight: 460; letter-spacing: -0.01em; color: var(--ink);
  line-height: 1;
}
.card-stats { color: var(--ink-2); font-size: 13px; font-weight: 500; letter-spacing: 0.01em; }
.card-address {
  font-size: 14px; color: var(--ink); line-height: 1.4; font-weight: 500;
  display: -webkit-box; -webkit-line-clamp: 1; -webkit-box-orient: vertical; overflow: hidden;
}

/* chip row in card body */
.card-tags { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.chip-tag {
  display: inline-flex; align-items: center; gap: 5px;
  font-size: 10.5px; font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase;
  padding: 4px 9px; border-radius: 999px;
  background: var(--card-2); color: var(--ink-2); border: 1px solid var(--line);
}
.chip-dog-large_ok, .chip-dog-dogs_ok { color: var(--accent); background: var(--accent-soft); border-color: transparent; }
.chip-dog-small_only { color: var(--caution); background: var(--caution-soft); border-color: transparent; }
.chip-dog-no_dogs    { color: var(--warn); background: var(--warn-soft); border-color: transparent; }

/* —— conversation pill on card (compact) —— */
.card-convo {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 10.5px;
  font-weight: 700;
  color: var(--clay);
  background: var(--clay-soft);
  padding: 4px 9px;
  border-radius: 999px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.img-wrap {
  position: relative;
  width: 100%;
  aspect-ratio: 3 / 2;
  background: var(--img-bg);
  overflow: hidden;
  display: block;
}
.img-wrap img {
  width: 100%; height: 100%; object-fit: cover; display: block;
  transition: transform 0.4s cubic-bezier(.2,.7,.3,1);
}
@media (hover: hover) { .card:hover .img-wrap img { transform: scale(1.03); } }

/* Photo carousel — horizontal scroll-snap, no JS. Works on iPhone touch. */
.carousel {
  position: relative;
  width: 100%;
  aspect-ratio: 3 / 2;
  background: var(--img-bg);
  overflow: hidden;
}
.carousel-track {
  display: flex;
  width: 100%; height: 100%;
  overflow-x: auto;
  scroll-snap-type: x mandatory;
  scrollbar-width: none;
  -ms-overflow-style: none;
  -webkit-overflow-scrolling: touch;
}
.carousel-track::-webkit-scrollbar { display: none; }
.carousel-slide {
  flex: 0 0 100%;
  width: 100%; height: 100%;
  scroll-snap-align: start;
}
.carousel-slide img {
  width: 100%; height: 100%; object-fit: cover; display: block;
}
.carousel-dots {
  position: absolute; bottom: 10px; left: 0; right: 0;
  display: flex; justify-content: center; gap: 5px;
  pointer-events: none;
}
.carousel-dot {
  width: 6px; height: 6px; border-radius: 50%;
  background: rgba(255,255,255,0.55);
  box-shadow: 0 0 4px rgba(0,0,0,0.35);
  transition: background 0.15s ease, transform 0.15s ease, width 0.15s ease;
}
.carousel-dot[data-active="true"] {
  background: white;
  width: 17px;
  border-radius: 4px;
}
.carousel-count {
  position: absolute; bottom: 12px; right: 12px;
  background: var(--backdrop); color: white;
  padding: 3px 8px; border-radius: 999px;
  font-size: 11px; font-weight: 500;
  pointer-events: none;
}
.no-img {
  width: 100%; height: 100%;
  display: flex; align-items: center; justify-content: center;
  color: var(--ink-3); font-size: 12px; letter-spacing: 0.1em;
  text-transform: uppercase; font-weight: 600;
}
.badge {
  position: absolute;
  top: 13px; left: 13px;
  background: var(--backdrop);
  color: rgba(255,255,255,0.92);
  padding: 5px 10px;
  border-radius: 999px;
  font-size: 9.5px;
  font-weight: 700;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  backdrop-filter: blur(10px);
  pointer-events: none; z-index: 2;
}
.dog-badge {
  position: absolute;
  top: 13px; right: 13px;
  padding: 5px 10px;
  border-radius: 999px;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.04em;
  pointer-events: none; z-index: 2;
  backdrop-filter: blur(10px);
}
.dog-badge-large_ok { color: #fff;  background: rgba(53,92,67,0.82); }
.dog-badge-dogs_ok  { color: #fff;  background: rgba(53,92,67,0.82); }
.dog-badge-small_only { color: #fff; background: rgba(156,115,33,0.85); }
.dog-badge-no_dogs    { color: #fff; background: rgba(168,80,74,0.85); }

/* —— body —— */
.body {
  padding: 18px 18px 20px;
  display: flex; flex-direction: column;
  gap: 11px; flex: 1;
}
.price {
  font-family: var(--serif); font-optical-sizing: auto;
  font-size: 24px;
  font-weight: 460;
  letter-spacing: -0.01em;
  line-height: 1;
}
.stats {
  display: flex; gap: 14px; flex-wrap: wrap;
  font-size: 14px; font-weight: 400;
  color: var(--ink-2);
}
.stat { display: inline-flex; align-items: center; gap: 5px; }
.dot { color: var(--ink-3); }

.why {
  display: flex;
  flex-direction: column;
  gap: 4px;
  background: var(--accent-soft);
  border-left: 3px solid var(--accent);
  padding: 8px 12px;
  border-radius: 6px;
  font-size: 13px;
  line-height: 1.4;
  color: var(--ink);
}
.why-rank {
  font-weight: 700;
  color: var(--accent);
  font-size: 10px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.why-text { line-height: 1.45; }

/* conversation state block — shown for listings we've contacted */
.convo {
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 10px 12px;
  font-size: 13px;
  line-height: 1.45;
  display: flex; flex-direction: column; gap: 6px;
}
.convo-head {
  display: flex; align-items: center; justify-content: space-between;
  gap: 8px;
}
.convo-status {
  font-size: 11px; font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.06em;
  padding: 3px 8px; border-radius: 999px;
}
.convo-when { font-size: 11px; color: var(--ink-3); }
.convo-line { color: var(--ink); }
.convo-next {
  color: var(--accent); font-size: 12px; font-weight: 500;
  padding-top: 2px; border-top: 1px dashed var(--line); margin-top: 2px; padding-top: 6px;
}
.convo-sub {
  font-size: 12px; color: var(--ink-3); font-style: italic;
}
.convo-gcal {
  display: inline-block;
  align-self: flex-start;
  margin-top: 4px;
  padding: 6px 10px;
  background: var(--card);
  color: var(--accent);
  border: 1px solid var(--accent);
  border-radius: 6px;
  font-size: 12px; font-weight: 500;
  text-decoration: none;
}
.convo-gcal:active, .convo-gcal:hover {
  background: var(--accent); color: var(--card);
}

/* concerns: rankable but flagged */
.why.why-concerns {
  background: var(--caution-soft);
  border-left-color: var(--caution);
  color: var(--ink);
}
.why.why-concerns .why-rank { color: var(--caution); }

/* filtered: hard-gated out */
.why.why-filtered {
  background: var(--warn-soft);
  border-left-color: var(--warn);
  color: var(--ink);
}
.why.why-filtered .why-rank { color: var(--warn); }

/* status pills inside the convo block */
.convo-status-contacted           { color: var(--info);    background: var(--info-soft); }
.convo-status-viewing_scheduled,
.convo-status-viewing_done,
.convo-status-accepted            { color: var(--accent);  background: var(--accent-soft); }
.convo-status-shortlist,
.convo-status-applied             { color: var(--caution); background: var(--caution-soft); }
.convo-status-declined_by_us      { color: var(--ink-3);   background: var(--line); }
.convo-status-declined_by_landlord{ color: var(--warn);    background: var(--warn-soft); }
.convo-status-neutral             { color: var(--ink);     background: var(--line); }


.title {
  font-size: 15px;
  font-weight: 500;
  line-height: 1.35;
  color: var(--ink);
  text-decoration: none;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.hood {
  font-size: 12px;
  color: var(--ink-3);
  text-transform: uppercase;
  letter-spacing: 0.08em;
  font-weight: 500;
}

.kv {
  border-top: 1px solid var(--line);
  padding-top: 12px;
  display: flex; flex-direction: column; gap: 6px;
  font-size: 13px;
  color: var(--ink-2);
}
.kv .row { display: flex; gap: 8px; }
.kv .k {
  color: var(--ink-3);
  min-width: 62px;
  flex-shrink: 0;
  text-transform: uppercase;
  font-size: 11px;
  letter-spacing: 0.06em;
  padding-top: 2px;
  font-weight: 500;
}
.kv .v { color: var(--ink); flex: 1; }
.kv a { color: var(--accent); text-decoration: none; }
.kv .v.warn { color: var(--warn); }
.kv .v.caution { color: var(--caution); }  /* yellow/amber for 30–45 min walks */

/* —— Gemini photo-review surfaces —— */
.visual-summary {
  font-size: 12px;
  font-style: italic;
  color: var(--ink-2);
  line-height: 1.45;
  padding: 2px 0 4px;
}
.quality-pills {
  display: flex; flex-wrap: wrap; gap: 4px;
  font-size: 10px; font-weight: 600;
  letter-spacing: 0.06em;
  padding-top: 2px;
}
.quality-pill {
  padding: 3px 7px; border-radius: 999px;
  background: var(--line); color: var(--ink-2);
  text-transform: uppercase;
  border: 1px solid transparent;
}
.quality-pill.good    { color: var(--accent);  background: var(--accent-soft); }
.quality-pill.neutral { color: var(--ink-2);   background: var(--line); }
.quality-pill.caution { color: var(--caution); background: var(--caution-soft); }
.quality-pill.warn    { color: var(--warn);    background: var(--warn-soft); }
.mode {
  /* Desaturate the walk/drive emoji so it reads as iconography, not decoration */
  filter: saturate(0) brightness(0.85);
  display: inline-block;
  margin-right: 3px;
  font-size: 13px;
  vertical-align: -1px;
}
.mode-word {
  /* Small subtle label after the time — "12 min walk", "23 min drive" */
  color: var(--ink-3);
  font-size: 11px;
  font-weight: 500;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  padding-left: 2px;
}
.rating {
  font-size: 11px;
  color: var(--ink-3);
  font-weight: 500;
  white-space: nowrap;
}
@media (prefers-color-scheme: dark) {
  .mode { filter: saturate(0) brightness(1.15); }
}

.cta {
  margin-top: 4px;
  display: block;
  text-align: center;
  background: var(--ink);
  color: var(--bg);
  text-decoration: none;
  padding: 14px 16px;
  border-radius: 10px;
  font-size: 14px;
  font-weight: 500;
  letter-spacing: 0.01em;
  min-height: 44px;
}
.cta:active { background: var(--accent); }
@media (hover: hover) {
  .cta:hover { background: var(--accent); }
}
.cta-secondary {
  margin-top: 6px;
  display: block;
  width: 100%;
  text-align: center;
  background: transparent;
  color: var(--ink-2);
  border: 1px solid var(--line);
  padding: 8px 12px;
  border-radius: 8px;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  font-family: inherit;
}
.cta-secondary:active { background: var(--line); }
@media (hover: hover) {
  .cta-secondary:hover { background: var(--line); color: var(--ink); }
}

/* —— filters —— */
.head-row {
  display: flex; align-items: flex-start; justify-content: space-between;
  gap: 16px; flex-wrap: wrap;
}
.title-block { flex: 1; min-width: 260px; }
.filters {
  display: flex; flex-direction: column; gap: 12px;
  align-items: flex-end;
}
@media (max-width: 600px) {
  .filters { width: 100%; align-items: stretch; }
}
.filter-group {
  display: flex; flex-direction: column; gap: 6px;
  min-width: 240px;
}
.filter-label {
  font-size: 11px; color: var(--ink-3); letter-spacing: 0.08em;
  text-transform: uppercase; font-weight: 500;
}
.chip-row {
  display: flex; flex-wrap: wrap; gap: 6px;
}
.chip {
  background: var(--card); color: var(--ink-2);
  border: 1px solid var(--line);
  padding: 6px 10px; border-radius: 999px;
  font-size: 12px; font-weight: 500; cursor: pointer;
  user-select: none; -webkit-user-select: none;
}
.chip[aria-pressed="true"] {
  background: var(--ink); color: var(--card); border-color: var(--ink);
}
.count-pill {
  font-size: 12px; color: var(--ink-3); font-weight: 400;
  margin-left: 6px;
}
.hood-select {
  background: var(--card); color: var(--ink);
  border: 1px solid var(--line);
  padding: 8px 12px; border-radius: 8px;
  font-size: 13px; font-weight: 500;
  font-family: inherit;
  min-width: 240px;
  -webkit-appearance: none; appearance: none;
  background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'><path d='M3 4l3 3 3-3' stroke='%23888' stroke-width='1.5' fill='none' stroke-linecap='round' stroke-linejoin='round'/></svg>");
  background-repeat: no-repeat; background-position: right 12px center;
  padding-right: 30px;
}

.empty {
  grid-column: 1 / -1;
  text-align: center; padding: 60px 20px;
  color: var(--ink-3); font-size: 14px;
}

/* —— footer —— */
footer {
  margin-top: 64px;
  padding-top: 28px;
  border-top: 1px solid var(--line);
  color: var(--ink-3);
  font-size: 12px;
  letter-spacing: 0.04em;
  text-align: center;
}
footer .footer-mark {
  font-family: var(--serif); font-optical-sizing: auto;
  font-style: italic; font-size: 16px; color: var(--ink-2);
}

/* feature card — the top pick, full-width editorial spread */
.card.feature { grid-column: 1 / -1; }
@media (min-width: 760px) {
  .card.feature { flex-direction: row; align-items: stretch; }
  .card.feature .feature-media { flex: 0 0 56%; }
  .card.feature .img-wrap, .card.feature .carousel { height: 100%; aspect-ratio: auto; min-height: 340px; }
  .card.feature .card-body-link { flex: 1; display: flex; }
  .card.feature .body { justify-content: center; padding: 36px 40px; gap: 14px; }
  .card.feature .card-price { font-size: 36px; }
  .card.feature .card-reason { -webkit-line-clamp: 5; font-size: 15px; }
  .card.feature .card-address { font-size: 16px; -webkit-line-clamp: 2; }
}
.feature-flag {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 10px; font-weight: 700; letter-spacing: 0.14em; text-transform: uppercase;
  color: var(--gold);
}
.feature-flag .material-symbols-outlined { font-size: 15px; vertical-align: -3px; }
"""


def _walk_class(mins: int) -> str:
    # ≤29 min = OK (default), 30–45 = caution (yellow), >45 = warn (red).
    if mins > 45:
        return "v warn"
    if mins >= 30:
        return "v caution"
    return "v"


def _anchor_link_html(anchor, *, origin: tuple[float, float] | None = None,
                       mode: str = "walking") -> str:
    """Anchor link → Google Maps.

    When `origin` is provided, builds a Directions URL (FROM origin TO anchor)
    in the given travel mode (walking | driving). Otherwise falls back to
    a Place panel.
    """
    name_q = re.sub(r"\s*\([^)]*\)", "", anchor.name).strip()
    if origin is not None:
        lat, lng = origin
        params = [
            f"origin={lat},{lng}",
            f"travelmode={mode}",
        ]
        if anchor.place_id:
            params.append(f"destination_place_id={anchor.place_id}")
            params.append(f"destination={quote_plus(name_q)}")
        else:
            params.append(f"destination={quote_plus(name_q)}@{anchor.lat},{anchor.lng}")
        url = f"https://www.google.com/maps/dir/?api=1&{'&'.join(params)}"
    elif anchor.place_id:
        url = (
            f"https://www.google.com/maps/search/?api=1"
            f"&query={quote_plus(name_q)}"
            f"&query_place_id={anchor.place_id}"
        )
    else:
        query = quote_plus(f"{name_q}, San Francisco Bay Area")
        url = (
            f"https://www.google.com/maps/search/?api=1&query={query}"
            f"&center={anchor.lat},{anchor.lng}"
        )
    rating_html = ""
    if anchor.rating and anchor.rating_count:
        count = (
            f"{anchor.rating_count / 1000:.1f}k".rstrip("0").rstrip(".")
            if anchor.rating_count >= 1000 else str(anchor.rating_count)
        )
        rating_html = f' <span class="rating">{anchor.rating} · {count}</span>'
    return (
        f'<a href="{url}" target="_blank" rel="noopener">'
        f'{_esc(anchor.short)}{rating_html} '
        f'<span class="material-symbols-outlined link-icon">arrow_outward</span></a>'
    )


def _esc(s: str | None) -> str:
    if not s:
        return ""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _img_url(L: Listing) -> str | None:
    if L.image_url:
        return L.image_url
    if L.source == "zillow":
        return L.raw.get("imgSrc")
    return None


def _stat(label: str, val) -> str:
    if val in (None, "", "?"):
        return ""
    return f'<span class="stat"><span class="dot">{label}</span> {val}</span>'


FIT_LABELS = {"ok": "Strong fit", "concerns": "Worth a look", "filtered": "Long shot"}


def _amenity_chips(L: Listing) -> list[str]:
    """A few scannable amenity tags drawn straight from our priorities:
    private outdoor for the dogs, in-unit laundry, off-street parking."""
    chips: list[str] = []
    if L.has_yard:
        chips.append("Yard")
    laundry = (L.laundry or "").lower()
    if any(w in laundry for w in ("in-unit", "in unit", "in_unit", "washer")):
        chips.append("In-unit laundry")
    parking = (L.parking or "").lower()
    if any(w in parking for w in ("garage", "attached", "carport", "deeded", "covered")):
        chips.append("Garage")
    return chips[:3]


def _card(L: Listing, walk_map: dict | None = None, convo: dict | None = None,
          drive_bakery: tuple | None = None, drive_map: dict | None = None,
          feature: bool = False) -> str:
    """Card surface — editorial listing card:
       Photo (carousel) · source + dog overlays · neighborhood + fit verdict ·
       price + size · address · Gemini take · amenity + conversation chips.
       Parking, laundry, walks, etc. all live on the detail page.
    """
    from .listing_page import listing_url
    detail_href = listing_url(L)

    # ISO date this listing first entered the DB — drives the "Added since"
    # filter (data-added) and is compared lexically against a YYYY-MM-DD cutoff.
    added_date = L.first_seen.date().isoformat() if L.first_seen else ""

    badge = f'<span class="badge">{_esc(L.source)}</span>'
    dog_label = dogs.LABELS.get(L.dog_policy) if L.dog_policy else None
    dog_badge = (
        f'<span class="dog-badge dog-badge-{_esc(L.dog_policy)}">{_esc(dog_label)}</span>'
        if dog_label else ""
    )

    photos = L.photos or ([L.image_url] if L.image_url else [])
    photos = [p for p in photos if p]
    if len(photos) > 1:
        # Each slide is an <a> so tapping the image navigates to the detail
        # page. Horizontal scroll-snap still works because the link doesn't
        # absorb horizontal drag gestures — only taps that resolve to a click.
        slides = "".join(
            f'<a class="carousel-slide" data-idx="{i}" href="{detail_href}">'
            f'<img src="{_esc(p)}" alt="" loading="lazy"></a>'
            for i, p in enumerate(photos)
        )
        dots = "".join(
            f'<span class="carousel-dot"{" data-active=\"true\"" if i == 0 else ""}></span>'
            for i, _ in enumerate(photos)
        )
        img_html = (
            f'<div class="carousel">'
            f'<div class="carousel-track">{slides}</div>'
            f'<div class="carousel-dots">{dots}</div>'
            f'{badge}{dog_badge}</div>'
        )
    elif photos:
        img_html = (
            f'<a class="img-wrap" href="{detail_href}">'
            f'<img src="{_esc(photos[0])}" alt="" loading="lazy">'
            f'{badge}{dog_badge}</a>'
        )
    else:
        img_html = (
            f'<a class="img-wrap" href="{detail_href}">'
            f'<div class="no-img">no photo</div>{badge}{dog_badge}</a>'
        )

    price = f"${L.price:,}/mo" if L.price else "price on request"
    stats = []
    if L.beds: stats.append(f"{L.beds:g} bd")
    if L.baths: stats.append(f"{L.baths:g} ba")
    if L.sqft: stats.append(f"{round(L.sqft / 10.7639)} m²")
    stat_line = " · ".join(stats)

    # Severity → card-level border class. Color carries the meaning; no badge.
    sev = L.llm_severity or ("filtered" if (L.llm_rank or 0) >= 9000 else "ok")
    sev_class = f"sev-{sev}"

    # Eliminated overlay — soft-delete via listing_status. Pushed to the bottom
    # by rank(); here we just dim the card and surface the reason inline.
    elim_statuses = {"declined_by_landlord", "declined_by_us", "passed_on"}
    is_eliminated = bool(convo and convo.get("status") in elim_statuses)
    elim_class = " eliminated" if is_eliminated else ""
    elim_banner_html = ""
    if is_eliminated:
        elim_label = convo["status"].replace("_", " ")
        note = convo.get("status_note") or ""
        note_html = f'<span class="card-eliminated-note">{_esc(note)}</span>' if note else ""
        elim_banner_html = f'<div class="card-eliminated">{_esc(elim_label)}{note_html}</div>'

    # The reason from Gemini IS the card's main message — share_blurb wins
    # when present (designer-friendly), else fall back to llm_reason, else
    # visual_summary, else nothing.
    reason_text = L.share_blurb or L.llm_reason or L.visual_summary or ""
    reason_html = (
        f'<div class="card-reason">{_esc(reason_text)}</div>' if reason_text else ""
    )

    # Eyebrow row: neighborhood (left) + fit verdict (right). The fit dot color
    # carries the Gemini severity; the reason text below gives the detail.
    hood_label = _esc((L.hood or "").replace("-", " "))
    hood_html = f'<span class="card-hood">{hood_label}</span>' if L.hood else "<span></span>"
    fit_html = ""
    if not is_eliminated and sev in FIT_LABELS:
        fit_html = f'<span class="fit fit-{sev}">{FIT_LABELS[sev]}</span>'
    eyebrow_html = f'<div class="eyebrow-row">{hood_html}{fit_html}</div>'

    # Conversation pill — compact, only the status. The full thread is on detail.
    convo_pill = ""
    if convo and not is_eliminated:
        status = convo.get("status") or "neutral"
        label = status.replace("_", " ").upper()
        if convo.get("viewing_at"):
            try:
                vdt = datetime.fromisoformat(convo["viewing_at"])
                label += " · " + vdt.strftime("%a %-I:%M %p")
            except Exception:
                pass
        convo_pill = f'<span class="card-convo">{_esc(label)}</span>'

    # Amenity chips alongside the convo pill — yard / laundry / parking.
    amenity_html = "".join(
        f'<span class="chip-tag">{_esc(a)}</span>' for a in _amenity_chips(L)
    )
    tags_html = ""
    if convo_pill or amenity_html:
        tags_html = f'<div class="card-tags">{convo_pill}{amenity_html}</div>'

    address_line = ""
    if L.address:
        address_line = f'<div class="card-address">{_esc(L.address)}</div>'

    feature_class = " feature" if feature else ""
    feature_flag = (
        '<span class="feature-flag"><span class="material-symbols-outlined">'
        'auto_awesome</span>Top pick</span>'
        if feature else ""
    )

    # Searchable haystack — used by the main-page search box. Lowercased and
    # joined so a single substring scan handles "$7000 inner richmond" or
    # "734 37th" or "yard" or "1758 9th ave".
    haystack_bits = [
        L.source or "",
        L.title or "", L.address or "", L.hood or "",
        f"${L.price}" if L.price else "", f"{L.price}" if L.price else "",
        f"{L.beds:g}bd" if L.beds else "", f"{L.baths:g}ba" if L.baths else "",
        L.share_blurb or "", L.llm_reason or "", L.visual_summary or "",
        L.parking or "", L.laundry or "", L.yard_note or "",
        L.outdoor_visible or "", L.other_visible or "",
        L.dog_policy or "",
        L.contact_name or "", L.contact_phone or "",
    ]
    haystack = _esc(" | ".join(b for b in haystack_bits if b).lower())

    # Fallback so cards with thin data still render something useful.
    fallback_title = ""
    if not reason_text:
        fallback_title = (
            f'<div class="card-fallback-title">{_esc(L.title or L.address or "View listing")}</div>'
        )

    return f"""
<article class="card {sev_class}{elim_class}{feature_class}" data-search="{haystack}" data-added="{added_date}" data-key="{_esc(L.key)}">
  {elim_banner_html}
  <div class="feature-media">{img_html}</div>
  <a class="card-body-link" href="{detail_href}">
    <div class="body">
      {feature_flag}
      {eyebrow_html}
      <div class="card-price-row">
        <span class="card-price">{price}</span>
        <span class="card-stats">{_esc(stat_line)}</span>
      </div>
      {address_line}
      {reason_html}
      {fallback_title}
      {tags_html}
    </div>
  </a>
</article>
"""


_FILTER_JS = """
(function() {
  const grid = document.querySelector('main.grid');
  const cards = Array.from(grid.querySelectorAll('.card'));
  const countEl = document.getElementById('count');
  const dogChips = Array.from(document.querySelectorAll('.chip[data-filter="dog"]'));
  const walkAnchor = document.getElementById('walk-anchor');
  const walkChips = Array.from(document.querySelectorAll('.chip[data-filter="walk"]'));

  function setDogState(value) {
    dogChips.forEach(c => c.setAttribute('aria-pressed', c.dataset.value === value ? 'true' : 'false'));
    applyFilters();
  }
  function setWalkState(value) {
    walkChips.forEach(c => c.setAttribute('aria-pressed', c.dataset.value === value ? 'true' : 'false'));
    applyFilters();
  }
  dogChips.forEach(c => c.addEventListener('click', () => setDogState(c.dataset.value)));
  walkChips.forEach(c => c.addEventListener('click', () => setWalkState(c.dataset.value)));
  walkAnchor.addEventListener('change', applyFilters);

  function activeDog() {
    const p = dogChips.find(c => c.getAttribute('aria-pressed') === 'true');
    return p ? p.dataset.value : 'any';
  }
  function activeWalkMax() {
    const p = walkChips.find(c => c.getAttribute('aria-pressed') === 'true');
    return p ? parseInt(p.dataset.value, 10) : Infinity;
  }

  function applyFilters() {
    const dog = activeDog();
    const anchor = walkAnchor.value;     // 'beach' | 'arsicault' | 'arizmendi'
    const maxMin = activeWalkMax();
    let shown = 0;
    cards.forEach(card => {
      const dogOk = dog === 'any' || card.dataset.dog === dog;
      const mins = parseInt(card.dataset[anchor] || '999', 10);
      const walkOk = !isFinite(maxMin) || mins <= maxMin;
      const visible = dogOk && walkOk;
      card.style.display = visible ? '' : 'none';
      if (visible) shown++;
    });
    countEl.textContent = shown;
    let empty = grid.querySelector('.empty');
    if (shown === 0) {
      if (!empty) {
        empty = document.createElement('div');
        empty.className = 'empty';
        empty.textContent = 'No listings match these filters.';
        grid.appendChild(empty);
      }
    } else if (empty) {
      empty.remove();
    }
  }
})();
"""


def render(
    listings: list[Listing], run=None, walk_map: dict | None = None,
    convo_map: dict[str, dict] | None = None,
    drive_bakery_map: dict | None = None,
    drive_map: dict | None = None,
    title: str = "Casita",
) -> str:
    convo_map = convo_map or {}
    drive_bakery_map = drive_bakery_map or {}
    drive_map = drive_map or {}

    # Pick the feature card — the top-ranked strong fit that isn't eliminated.
    # rank() already sorts best-first, so the first qualifying listing wins.
    elim_set = {"declined_by_landlord", "declined_by_us", "passed_on"}
    feature_key = None
    for L in listings:
        status = (convo_map.get(L.key) or {}).get("status")
        if L.llm_severity == "ok" and status not in elim_set:
            feature_key = L.key
            break

    cards = "\n".join(
        _card(L, walk_map=walk_map, convo=convo_map.get(L.key),
              drive_bakery=drive_bakery_map.get(L.key),
              drive_map=drive_map, feature=(L.key == feature_key))
        for L in listings
    )
    ts_raw = (run["finished_at"] or run["started_at"]) if run else datetime.utcnow().isoformat()
    try:
        dt = datetime.fromisoformat(str(ts_raw)).replace(tzinfo=timezone.utc)
        ts = dt.astimezone(PACIFIC).strftime("%b %-d, %Y · %-I:%M %p %Z")
    except Exception:
        ts = str(ts_raw)

    count = len(listings)

    # Editorial stat strip — the search at a glance.
    strong = sum(1 for L in listings if L.llm_severity == "ok")
    active_convo = {"contacted", "viewing_scheduled", "viewing_done", "applied",
                    "accepted", "shortlist"}
    in_convo = sum(
        1 for L in listings
        if (convo_map.get(L.key) or {}).get("status") in active_convo
    )
    viewings = sum(
        1 for L in listings
        if (convo_map.get(L.key) or {}).get("status") == "viewing_scheduled"
        or (convo_map.get(L.key) or {}).get("viewing_at")
    )

    def stat_item(num, label, cls=""):
        return (f'<span class="stat-item"><span class="stat-num {cls}">{num}</span>'
                f'<span class="stat-label">{label}</span></span>')

    stat_parts = [stat_item(count, "places")]
    if strong:
        stat_parts.append(stat_item(strong, "strong fits", "is-accent"))
    if in_convo:
        stat_parts.append(stat_item(in_convo, "in conversation", "is-clay"))
    if viewings:
        stat_parts.append(stat_item(viewings, "viewing" + ("s" if viewings != 1 else "")))
    stats_html = '<div class="stats">' + '<span class="stat-sep"></span>'.join(stat_parts) + '</div>'

    sub = f"{count} places · ranked for large dogs, trails, beaches, and a good loaf"
    og_image = public_url("/og/index.png")
    og_url = public_url("/")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" media="(prefers-color-scheme: light)" content="#f1eee5">
<meta name="theme-color" media="(prefers-color-scheme: dark)" content="#15130d">
<meta name="color-scheme" content="light dark">
<meta name="format-detection" content="telephone=no">
<meta name="robots" content="noindex, nofollow">
<link rel="icon" type="image/svg+xml" href="/assets/favicon.svg">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,300..600;1,9..144,300..500&display=swap">
<link rel="stylesheet" href="https://fonts.googleapis.com/icon?family=Material+Symbols+Outlined&icon_names=arrow_upward,arrow_downward,arrow_outward,arrow_back,ios_share,cancel,auto_awesome&display=block">
<meta property="og:type" content="website">
<meta property="og:title" content="{title} — rental search demo">
<meta property="og:description" content="{sub}">
<meta property="og:image" content="{_esc(og_image)}">
<meta property="og:image:type" content="image/png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:url" content="{_esc(og_url)}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:image" content="{_esc(og_image)}">
<title>{title} — rental search demo</title>
<style>{CSS}</style>
{THEME_PREPAINT}
</head>
<body>
<div class="wrap">
<header>
  <div class="brandline">
    <div class="wordmark">
      <h1>{title}</h1>
      <span class="tagline">rental search demo</span>
    </div>
    <div class="header-actions">
      <button id="export-votes" class="export-btn" type="button" title="share all up/pass votes as a link — paste to Claude to incorporate"><span class="material-symbols-outlined">ios_share</span><span class="export-btn-label">export votes</span></button>
      {THEME_SWITCH_HTML}
    </div>
  </div>
  <p class="lede">A rental-search snapshot for a household with two large dogs: SF walkability, Marin drive times, trail access, and good bread nearby.</p>
  {stats_html}
  <div class="toolbar">
    <div class="search-box">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
      <input id="q" type="search" placeholder="Search hood, price, beds, “garage”, “Inner Richmond”…" autocomplete="off">
    </div>
    <div class="since-filter" role="group" aria-label="Filter by date added">
      <span class="since-label">Added</span>
      <button type="button" class="since-chip" data-days="" aria-pressed="true">Any time</button>
      <button type="button" class="since-chip" data-days="1" aria-pressed="false">24h</button>
      <button type="button" class="since-chip" data-days="3" aria-pressed="false">3 days</button>
      <button type="button" class="since-chip" data-days="7" aria-pressed="false">7 days</button>
      <input type="date" id="since-date" class="since-date" aria-label="Added on or after this date">
    </div>
    <div class="search-meta"><span id="search-count">{count}</span> of {count} shown · refreshed {ts}</div>
  </div>
</header>
<main class="grid">
{cards}
</main>
<footer><span class="footer-mark">Casita</span> · personal-use rental search</footer>
</div>
{CAROUSEL_JS}
{SEARCH_JS}
{SHARE_JS}
{VOTE_JS}
{THEME_SWITCH_JS}
</body>
</html>
"""
