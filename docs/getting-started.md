---
icon: lucide/play
---

# Getting Started

The default path is the offline demo. It renders a sanitized SQLite fixture and
serves the static site locally.

```bash
uv sync
uv run playwright install chromium
uv run casita demo
```

Open <http://127.0.0.1:8765/>.

The demo path does not need credentials. It does not scrape, call Vertex, read
GCS, deploy to Firebase, or call the Google Maps Routes API. It does use the
local Playwright Chromium browser to capture Open Graph preview cards from
listing photos and facts.

## Live Runs

Live search uses browser automation and network calls:

```bash
uv run casita solve --help
uv run casita search --headed --local
uv run casita enrich --local
CASITA_FIREBASE_PROJECT=your-project uv run casita publish --local
```

Copy `.env.example` to `.env` for live/private runs. `publish --local` renders
from the local SQLite file, but it still deploys to Firebase; set
`CASITA_FIREBASE_PROJECT` or pass `--project`.

!!! warning "Google Maps cost"

    `search` and `enrich` can eventually call `walk.py`, which uses the paid
    Google Maps Routes API when `GOOGLE_MAPS_API_KEY` is set. The demo is free:
    it reads cached route rows from `fixtures/demo.sqlite`. Without a Maps key,
    live route calculations fall back to haversine estimates.
