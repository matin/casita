---
icon: lucide/git-merge
---

# Deduplication

Deduplication lives in `src/casita/dedup.py`.

Casita pulls the same rental from multiple sources, so it needs one card per
property instead of one card per website. The dedup layer clusters likely
matches, keeps a primary listing, and preserves links back to the secondary
sources.

## Match Rules

Two listings are treated as different when both have prices and differ by more
than 20%, or when both have bed counts and the counts disagree.

After those guards, Casita considers listings to be the same property when:

- the source key is identical
- both listings have coordinates within 80 meters
- the normalized street addresses match

Address normalization is intentionally small and rental-site-specific. It strips
Craigslist cross-street suffixes such as `near ...`, removes San Francisco
location suffixes, drops unit and floor markers, collapses punctuation, and
normalizes common street-type words like `avenue` to `ave`.

## Merge Order

Clusters are sorted by source priority:

1. Zillow
2. Redfin
3. Zumper
4. Craigslist
5. Manual entries

The primary listing keeps its existing values. Missing fields are filled from
secondary listings, so structured sources can provide clean facts while
Craigslist can still contribute useful text such as dog policy or laundry
details.

## Where It Runs

There are two dedup paths:

- `dedupe()` runs in memory during search, before ranking newly scraped results.
- `deduplicate_db()` runs before rendering the static site, so duplicates that
  arrived in separate scrape runs still collapse into one active listing.

The database pass marks secondary listings inactive, moves conversations,
status rows, and attachments to the primary listing, and records secondary
source URLs in `raw_json.also_on`. Listing pages render those alternate URLs as
additional source links.

## Ways This Could Go Further

The current approach is O(n²), which is fine for the small personal search
space. A larger crawler would probably want a spatial index, better address
parsing, and snapshot tests for difficult cross-source matches.
