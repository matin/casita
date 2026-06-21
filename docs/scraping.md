---
icon: lucide/search
---

# Scraping

The offline demo needs none of this. `casita demo` renders the fixture.

Live scraping uses Playwright:

- Zillow: `src/casita/zillow.py`
- Craigslist: `src/casita/craigslist.py`
- Zumper: `src/casita/zumper.py`
- Redfin: `src/casita/redfin.py`

Zillow and Redfin can trigger PerimeterX. The human-in-the-loop path is:

```bash
uv run casita solve
uv run casita search --headed
```

`solve` opens a headed browser at Zillow. You clear the captcha manually, then
the session persists in `.chrome-profile/`. Later headed searches reuse that
profile. Craigslist, Zumper, and Redfin have their own source-specific parsing
paths.

## Ways This Could Go Further

Scraping could gain better source isolation, clearer retry reporting, richer
fixtures, or tests around parser behavior. The current code preserves the
human-in-the-loop reality instead of pretending the live scrape is fully
unattended.
