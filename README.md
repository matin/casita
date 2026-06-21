# Casita

Casita is a personal rental-search tool published as a public repo.

It started as a small script for a time-boxed San Francisco rental search with
two large dogs: scrape Zillow, Craigslist, Zumper, and Redfin; enrich the
listings; rank them; and render a static page that was easier to review than
four open browser tabs.

This is not a product or service. It is published as-is, under MIT, as a
personal-use codebase for an interview loop. The interesting part is what a
candidate chooses to improve.

## Demo

The demo is credentials-free and uses a sanitized SQLite fixture with cached
route times and precomputed LLM enrichment.

```bash
uv sync
uv run playwright install chromium
uv run casita demo
```

Then open <http://127.0.0.1:8765/>.

The demo does not scrape, call Vertex, deploy to Firebase, read GCS, or call the
Google Maps Routes API. It does use Playwright's local Chromium browser to
render Open Graph preview images from listing photos and facts. Live `search` /
`enrich` / `publish` paths still exist for private use and are controlled by
environment variables; see `.env.example`.

## What It Does

- Scrapes active rental listings from Zillow, Craigslist, Zumper, and Redfin.
- Normalizes listing facts into SQLite.
- Classifies dog policy and enriches details from listing pages.
- Uses Gemini for fact extraction, photo review, share blurbs, and ranking.
- Computes walking and driving times to curated SF / Marin anchors.
- Renders a static, mobile-friendly site with index and detail pages.
- Records votes and passes so future ranking can learn from reviewer feedback.

The domain assumptions are intentionally personal: large dogs, San Francisco
walkability, Marin driving context, trails, beaches, and good bakeries nearby.
That is the point of a personal tool.

## Docs

The documentation site explains the systems without turning them into assigned
tasks:

```bash
uv run zensical serve
```

Start at `docs/index.md`, or run `uv run zensical build` to generate the site.

## Checks

```bash
make check
```

This compiles the Python modules, runs the pytest suite, runs the public leak
validator, builds the docs, builds the Python package artifacts, and checks
that the CLI imports.

## Contributing

Read `CONTRIBUTING.md`. The short version: pick something you think makes
Casita better, and explain why you chose it in the PR description.
