---
icon: lucide/blocks
---

# Architecture

Casita is a small Python package with most behavior exposed through one Click
CLI.

| Area | Files |
| --- | --- |
| CLI orchestration | `src/casita/__init__.py` |
| Listing model and SQLite | `models.py`, `storage.py` |
| Sources | `zillow.py`, `craigslist.py`, `zumper.py`, `redfin.py` |
| Source helpers | `browser.py`, `cache.py`, `dogs.py`, `geo.py`, `locations.py`, `photos.py` |
| Enrichment and ranking | `llm.py`, `rank.py`, `walk.py`, `dedup.py` |
| Static rendering | `html.py`, `listing_page.py` |
| Optional private deploy | `cloud_sync.py`, `publish` command |

## Rough Edges

These are facts about the current codebase, not a ranked task list:

- The CLI currently lives in one large module.
- Some source-specific URL maps still duplicate the canonical location lists.
- The public tests are intentionally smoke-focused; scraper behavior still has
  thin coverage.
- LLM calls are Vertex-only.
- `cloud_sync.py` is optional, and its cloud object names are configured
  through environment variables.
- Rendering is string-based Python rather than templates.

The demo exists so these rough edges can be explored without credentials.
