---
name: casita-update
description: Kick off the casita rental pipeline — search, enrich, rank, publish — or spin up the offline demo. Use when asked to refresh listings, run a search, re-rank, or show the site.
---

# Run casita

This is how the tool is meant to be driven: by asking Claude to run the
pipeline, not by typing each command by hand. The author's normal workflow is
"Claude, run a casita update" — this skill encodes that so you can do it end to
end, including the judgment calls (what order, when to pause, what costs money).

Two modes. Pick based on what credentials are available.

## Demo — no credentials (default)

The point of this repo. No scraping, no cloud, no API keys.

```bash
uv sync
uv run playwright install chromium
uv run casita demo            # renders the fixture, serves http://127.0.0.1:8765/
```

Use this when there are no credentials, or when the ask is just "show me the
site." Everything below requires a `.env` (copy from `.env.example`).

## Live update — credentials + human-in-the-loop

The real pipeline. Run the steps **in order** — each depends on the last.

### 1. search — scrape sources, dedupe, upsert

```bash
uv run casita search --headed --local
```

**Pause for the captcha — do not push through.** Zillow and Redfin trigger
PerimeterX. When a captcha appears in the headed browser, stop and ask the
human to clear it by hand. `uv run casita solve` opens a headed Zillow tab for
exactly this; the session then persists in `.chrome-profile/` and later headed
runs reuse it. Craigslist and Zumper don't have this wall.

### 2. enrich — Gemini extraction, photo review, ranking

```bash
uv run casita enrich --local
```

Separate step, easy to forget. `search` only does heuristic ordering, so
skipping `enrich` leaves new listings unranked on the page. Requires
`CASITA_GCP_PROJECT` (Vertex); without it the LLM calls are skipped and nothing
gets re-ranked.

### 3. publish — render the static site + deploy

```bash
CASITA_FIREBASE_PROJECT=your-project uv run casita publish --local
```

To render against the live DB without deploying, use `uv run casita demo` or
`uv run casita show` instead.

## Votes feed the ranker (don't hand-edit the prompt)

The author tunes ranking by voting, then letting the model fold votes back into
the policy:

```bash
uv run casita vote --listing <slug-or-key> --dir up --reason "easy walk to the trail"
uv run casita analyze-prefs    # proposes policy edits from accumulated votes
```

Votes become few-shot examples on the next `enrich`. See
[`docs/how-it-works/learning.md`](../../../docs/how-it-works/learning.md).

## Cost + safety

- `search`/`enrich` can call the **paid** Google Maps Routes API when
  `GOOGLE_MAPS_API_KEY` is set. Without a key, route times fall back to
  haversine estimates. The demo is free (cached rows in the fixture).
- After touching fixtures, prompts, docs, or rendered strings, run the leak
  check: `uv run python scripts/validate_public.py`.
- Never commit private data — names, emails, phone numbers, project IDs, API
  keys, or the chosen home.

## Handy reads

- `uv run casita ls` — quick read-only listing lookup.
- `uv run casita --help` — every verb.
- [`AGENTS.md`](../../../AGENTS.md) — repo map and the public-repo contract.
