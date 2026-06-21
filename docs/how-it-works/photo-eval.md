---
icon: lucide/image
---

# Photo Evaluation

Photo evaluation lives in `src/casita/llm.py`:

- `review_photos`
- `PhotoReview`
- `apply_photo_review`

Listing copy often skips the things people notice first in photos: natural
light, blocked windows, tired finishes, useful outdoor space, awkward layouts,
or whether the cover image is actually useful. Casita asks Gemini to review a
small photo set and return structured fields:

- `light_quality`
- `view_quality`
- `condition_quality`
- `outdoor_visible`
- `other_visible`
- `visual_summary`
- `best_photo_index`
- `drop_indices`

Those fields feed the card, the detail page, and the later share blurb.

## Ways This Could Go Further

Photo review could be easier to replay from fixtures, easier to compare before
and after prompt changes, or more graceful when listing photos are missing,
duplicated, or misleading.
