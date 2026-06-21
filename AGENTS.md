# AGENTS.md

Casita is a personal rental-search assistant that scrapes rental sources,
normalizes listings into SQLite, enriches and ranks them with Gemini, and
renders a static review site. It is published as an MIT-licensed interview
project, not as a hosted product or supported service.

This file is the always-loaded entry point for coding agents. Keep it terse:
cross-cutting context lives in [`docs/`](docs/), command-level entry points
live in the Click CLI, and subsystem detail should stay near the owning module
or in the relevant docs page.

## Commands

```bash
uv sync                  # Install dependencies
uv run casita demo       # Render and serve the sanitized offline fixture
uv run zensical serve    # Serve the documentation site locally
uv run zensical build    # Build the docs without starting a server
make check               # Compile, test, validate public data, build docs/package

uv run pytest tests/test_file.py::test_name  # Run a single test
```

The default workflow is credentials-free. `uv run casita demo` copies
`fixtures/demo.sqlite` into `tmp/`, points route caching at that copy, forces
offline route behavior, renders the static site, and serves it at
`http://127.0.0.1:8765/`.

## Repository Structure

- `src/casita/__init__.py` - Click CLI orchestration and static render entry points.
- `src/casita/storage.py`, `src/casita/models.py` - SQLite schema, migrations, and listing model.
- `src/casita/zillow.py`, `craigslist.py`, `zumper.py`, `redfin.py` - live source scrapers.
- `src/casita/locations.py` - canonical SF and Marin search terms shared by scrapers.
- `src/casita/llm.py`, `rank.py`, `walk.py`, `dedup.py` - enrichment, scoring, routing, and deduplication.
- `src/casita/html.py`, `listing_page.py`, `static/` - static site rendering and assets.
- `src/casita/cloud_sync.py` - optional private GCS/Firebase deployment plumbing.
- `fixtures/demo.sqlite` - sanitized offline fixture used by the demo and tests.
- `scripts/validate_public.py` - public-release leak validator.
- `tests/` - fast credentials-free smoke and regression tests.

When you need the shape of a subsystem, read the file header and the matching
docs page before changing code.

## Where The WHY Lives

| Topic | Authoritative source |
| --- | --- |
| Backstory, contributor invitation, and product assumptions | [`docs/index.md`](docs/index.md) |
| First-run demo and live-run setup | [`docs/getting-started.md`](docs/getting-started.md) |
| Module map and rough edges | [`docs/architecture.md`](docs/architecture.md) |
| SQLite schema and fixture contract | [`docs/data-model.md`](docs/data-model.md) |
| Source scraping behavior | [`docs/scraping.md`](docs/scraping.md) |
| Route matrix cache, Maps costs, offline fallback | [`docs/how-it-works/routing.md`](docs/how-it-works/routing.md) |
| Gemini photo review | [`docs/how-it-works/photo-eval.md`](docs/how-it-works/photo-eval.md) |
| Deterministic and LLM ranking | [`docs/how-it-works/ranking.md`](docs/how-it-works/ranking.md) |
| Vote feedback loop | [`docs/how-it-works/learning.md`](docs/how-it-works/learning.md) |
| Static rendering | [`docs/how-it-works/static-site.md`](docs/how-it-works/static-site.md) |

## Public-Repo Contract

- Keep `uv run casita demo` credentials-free.
- Do not require GCS, Firebase, Vertex, browser login, or paid API calls for
  the demo path or tests.
- Keep private names, emails, phone numbers, project IDs, API keys, one-off
  operational details, and the chosen home out of the public tree.
- Run `uv run python scripts/validate_public.py` after touching fixtures,
  prompts, docs, rendered copy, or source strings.
- Live `search`, `enrich`, and `publish` paths may use credentials from
  `.env`, but they must stay optional and environment-driven.

## Code Style

- Prefer the existing small-module, plain-Python style.
- Keep helper functions readable without forcing abstractions too early.
- Use relative imports inside `src/casita`.
- Keep network calls and paid APIs behind explicit commands or env vars.
- Avoid logging or printing secrets, raw private messages, or full sensitive URLs.
- For configuration, prefer the existing environment-variable pattern in the
  owning module unless you are deliberately centralizing a broader config surface.

## Testing

Tests follow the pattern:

```text
test_<action>_<condition>_<expected>
```

Focus public tests on behavior that can run without credentials: fixture
rendering, public validation, source configuration, route-cache behavior, and
small pure helpers. Mock external systems rather than calling live rental
sites, GCS, Firebase, Vertex, or Google Maps.

## Philosophy

> "Programs must be written for people to read, and only incidentally for
> machines to execute." -- Harold Abelson

The goal is a codebase that:

- Reads naturally, almost like prose
- Surprises no one
- Lets a candidate understand the system quickly enough to make a good choice

Principles:

- A new contributor should understand the project shape in 10 minutes.
- The demo should work before the live credentials do.
- Personal-tool assumptions are allowed; private personal data is not.
- Leave the repo better, but keep the reason for your choice visible.
