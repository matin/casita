---
icon: lucide/panel-top
---

# Static Site

Static rendering lives in:

- `src/casita/html.py`
- `src/casita/listing_page.py`
- `_render_site` in `src/casita/__init__.py`

Casita does not run a dynamic web server for the rental UI. It reads SQLite,
ranks the active listings, renders `index.html`, renders one detail page per
listing, copies static assets, captures Open Graph preview PNGs, and serves or
deploys the result.

That makes the sharing surface simple: a static page can be hosted anywhere,
and the local demo can use Python's `http.server`. The preview images are 1200 x
630 listing cards: the main listing photo with a compact overlay of price,
address, neighborhood, size, and key fit signals, captured with Playwright's
local Chromium browser.

## Ways This Could Go Further

Static rendering could be split into smaller template units, given focused
snapshot tests, or made easier to theme. The current shape is intentionally
plain Python string rendering.
