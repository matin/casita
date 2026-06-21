from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Source = Literal["zillow", "craigslist", "zumper", "redfin", "manual"]

# Dog policy granularity, in order of preference for two large dogs:
#   "large_ok"  — large dogs explicitly welcome (best)
#   "dogs_ok"   — dogs allowed, no size given (needs a conversation)
#   "small_only"— only small/under-N-lb dogs (excludes us, but visible to user)
#   "no_dogs"   — no dogs (gate)
DogPolicy = Literal["large_ok", "dogs_ok", "small_only", "no_dogs"]


class Listing(BaseModel):
    source: Source
    source_id: str
    url: str
    title: str | None = None
    address: str | None = None
    neighborhood: str | None = None
    price: int | None = None
    beds: float | None = None
    baths: float | None = None
    sqft: int | None = None
    pets_allowed: bool | None = None
    dog_policy: DogPolicy | None = None
    parking: str | None = None
    laundry: str | None = None
    has_yard: bool | None = None  # LLM-detected: private outdoor space, backyard, patio, garden
    yard_note: str | None = None  # one-line description of the outdoor space if any

    # Gemini-vision review of the listing photos.
    light_quality: str | None = None       # "abundant" | "moderate" | "dim"
    view_quality: str | None = None        # "panoramic" | "open" | "blocked" | "ground-level"
    condition_quality: str | None = None   # "high-end" | "well-kept" | "dated" | "needs work"
    outdoor_visible: str | None = None     # e.g. "private fenced backyard with grass", "balcony only"
    other_visible: str | None = None       # other notable details from photos (driveway, side yard, etc.)
    visual_summary: str | None = None      # one short sentence Gemini's overall read of the photos
    share_blurb: str | None = None         # WhatsApp/iMessage preview text — 1-2 sentences
    share_token: str | None = None         # random suffix on the public URL so it's unguessable
    llm_rank: int | None = None    # 1-based; lower = better
    llm_reason: str | None = None  # one-line fit explanation from the ranker
    llm_severity: str | None = None  # "ok" | "concerns" | "filtered"
    contact_name: str | None = None
    contact_phone: str | None = None
    contact_email: str | None = None
    contact_url: str | None = None
    contact_note: str | None = None  # short hint like "prefers calls" or office hours
    description: str | None = None
    image_url: str | None = None
    # Up to 8 photo URLs for the on-card carousel. First entry is the cover
    # (mirrors image_url). Stored as a JSON-serialized list in the DB.
    photos: list[str] = Field(default_factory=list)
    # YouTube embed URL (https://www.youtube.com/embed/<id>) if the listing
    # has a video tour. Not yet rendered anywhere — reserved for future use.
    video_url: str | None = None
    lat: float | None = None
    lng: float | None = None
    neighborhood_resolved: str | None = None  # computed from lat/lng, overrides `neighborhood`
    first_seen: datetime | None = None  # when this listing first entered the DB (set by storage)
    raw: dict = Field(default_factory=dict)
    scraped_at: datetime = Field(default_factory=datetime.utcnow)

    @property
    def hood(self) -> str | None:
        return self.neighborhood_resolved or self.neighborhood

    @property
    def key(self) -> str:
        return f"{self.source}:{self.source_id}"


class Manifest(BaseModel):
    run_at: datetime = Field(default_factory=datetime.utcnow)
    query: dict
    listings: list[Listing]
