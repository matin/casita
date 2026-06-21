"""LLM-driven extraction + ranking via Vertex AI (Gemini 3+).

Default backend: Gemini 3 Flash (extraction) + 3.1 Pro (ranking) on Vertex AI.

Calls google-genai directly while still using Pydantic models for structured
output schemas via `response_schema`.

Swap to a different backend later by replacing _call_structured with another
implementation; the schemas stay.
"""
import json
import os
import re
import sqlite3
import textwrap
from typing import Literal

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google import genai
from google.genai import types as gtypes
from markdownify import markdownify
from pydantic import BaseModel, Field

from . import cache
from .models import Listing

load_dotenv()

PROJECT = os.environ.get("CASITA_GCP_PROJECT")
LOCATION = os.environ.get("CASITA_VERTEX_LOCATION", "global")

# Strip a "google-vertex:" prefix if an env var includes one.
def _model_name(env_var: str, default: str) -> str:
    raw = os.environ.get(env_var, default)
    return raw.split(":", 1)[1] if ":" in raw else raw

EXTRACT_MODEL = _model_name("CASITA_EXTRACT_MODEL", "gemini-3.1-pro-preview")
RANK_MODEL = _model_name("CASITA_RANK_MODEL", "gemini-3.1-pro-preview")

_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if not PROJECT:
        raise RuntimeError("Set CASITA_GCP_PROJECT to use Vertex-backed LLM commands.")
    if _client is None:
        _client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
    return _client


# ---------- schemas ----------


class ExtractedFacts(BaseModel):
    has_yard: bool | None = Field(
        None,
        description="Private outdoor space the tenant controls. Communal courtyard or rooftop is NOT a yard.",
    )
    yard_note: str | None = Field(None, description='One short line, e.g. "fenced backyard with grass".')
    dog_policy: Literal["large_ok", "dogs_ok", "small_only", "no_dogs"] | None = None
    parking: str | None = None
    laundry: str | None = None
    vibe: str | None = Field(None, description="One short sentence on what makes this place distinctive.")
    warnings: str | None = Field(None, description="One line on red flags.")


class RankEntry(BaseModel):
    key: str = Field(description="Listing key in 'source:source_id' format.")
    reason: str = Field(
        description="One short sentence on the ranking — name the load-bearing facts. "
        "For filtered listings, name the specific reason it was dropped."
    )
    severity: Literal["ok", "concerns", "filtered"] = Field(
        description=(
            "Severity flag. 'ok' = clean match, render green. "
            "'concerns' = rankable but has notable red flags (missing data, "
            "no parking, no yard in SF, dated finishes, weight-cap with negotiation "
            "needed, etc.) — render yellow. "
            "'filtered' = hard-gated out and should be dragged to the bottom — "
            "render red. INCLUDE filtered listings in the output so the user can "
            "see what got dropped and why."
        )
    )


class RankList(BaseModel):
    results: list[RankEntry]


class PrefContradiction(BaseModel):
    policy_quote: str = Field(
        description="The exact line or phrase in the current ranking policy that the votes contradict."
    )
    revealed_behavior: str = Field(
        description="What the votes actually show, with counts — e.g. '7 of 19 passes cite no trail/beach access'."
    )
    proposed_resolution: str = Field(
        description="A concrete proposed edit to that policy line reconciling it with revealed behavior. Specific text, not a vague direction."
    )


class PrefNewRule(BaseModel):
    rule: str = Field(description="A proposed new policy rule capturing a consistent preference the policy doesn't encode yet.")
    evidence: str = Field(description="The votes supporting it, with counts.")


class PrefAnalysis(BaseModel):
    summary: str = Field(description="One-paragraph read of revealed preference vs the current policy.")
    contradictions: list[PrefContradiction]
    new_rules: list[PrefNewRule]


class PhotoReview(BaseModel):
    """Gemini-vision read of a listing's photos.

    The goal is to surface what the listing copy never mentions: light quality,
    views, layout flow, condition, and outdoor features (driveways, side yards,
    terraces) that don't show up in the facts grid.
    """
    light_quality: Literal["abundant", "moderate", "dim", "unknown"] = Field(
        description=(
            "Natural-light read across the interior shots. "
            "'abundant' = many large windows / bright across multiple rooms; "
            "'moderate' = ok but not a selling point; "
            "'dim' = noticeably dark, north-facing, or window-poor."
        )
    )
    view_quality: Literal["panoramic", "open", "blocked", "ground-level", "unknown"] = Field(
        description=(
            "From the windows in the photos. 'panoramic' = bridge/ocean/skyline; "
            "'open' = clearly outward-facing with sky; "
            "'blocked' = right up against another building / wall; "
            "'ground-level' = street-level windows."
        )
    )
    condition_quality: Literal["high-end", "well-kept", "dated", "needs-work", "unknown"] = Field(
        description=(
            "Honest assessment of finishes and upkeep. 'high-end' = recent quality remodel; "
            "'well-kept' = clean and presentable; "
            "'dated' = original 80s/90s finishes still in place; "
            "'needs-work' = visible damage / very tired finishes."
        )
    )
    outdoor_visible: str | None = Field(
        None,
        description=(
            "Outdoor space visible in the photos that the listing might not "
            "fully describe. e.g. 'private fenced backyard with grass', "
            "'large driveway + side yard', 'small balcony only', 'roof deck'. "
            "Null if no outdoor space is visible."
        ),
    )
    other_visible: str | None = Field(
        None,
        description=(
            "Other notable things visible in the photos but not necessarily "
            "in the listing copy: hardwood floors, fireplace, period detail, "
            "open floor plan, eat-in kitchen, washer/dryer hookup, etc. "
            "One short sentence."
        ),
    )
    visual_summary: str = Field(
        description=(
            "One short, designer-eye sentence summarizing the overall feel of "
            "the place from the photos. Don't repeat the listing copy — focus "
            "on what the photos show that the words don't."
        )
    )
    best_photo_index: int = Field(
        description=(
            "0-based index of the BEST photo to use as the card cover. Pick the "
            "single shot that's most useful when scanning a grid of cards. "
            "Heuristics, in order: (1) main living space / kitchen with "
            "natural light, (2) the most distinctive room or outdoor space "
            "showing what makes this place stand out, (3) exterior of the "
            "building. AVOID: bedrooms, bathrooms, satellite/map images, "
            "headshots/portraits of people, agent profile photos, floor plans, "
            "logos, empty rooms with bad framing, blurry/low-light shots. "
            "If unsure, pick 0. NEVER pick an index that you flag in drop_indices."
        )
    )
    drop_indices: list[int] = Field(
        default_factory=list,
        description=(
            "0-based indices of photos that should be REMOVED from the listing "
            "entirely because they aren't unit photos. Include any index whose "
            "image is: a headshot or portrait of a person, an agent profile "
            "photo, a floor plan diagram, a brokerage logo, a satellite/map "
            "image, a screenshot of text, or an exterior shot of a completely "
            "different building. Real listing photos of the unit (rooms, "
            "exterior, outdoor space, common amenities) MUST NOT be in this "
            "list. If all photos look legitimate, return an empty list."
        ),
    )


class InteractionUpdate(BaseModel):
    """Structured updates extracted from a free-form landlord/agent message."""

    address: str | None = None
    price: int | None = None
    beds: float | None = None
    baths: float | None = None
    parking: str | None = None
    laundry: str | None = None
    dog_policy: Literal["large_ok", "dogs_ok", "small_only", "no_dogs"] | None = None
    has_yard: bool | None = None
    yard_note: str | None = None
    availability: str | None = None
    status: Literal[
        "new", "contacted", "viewing_scheduled", "viewing_done",
        "shortlist", "declined_by_us", "declined_by_landlord", "applied", "accepted",
    ] | None = None
    viewing_at: str | None = None
    summary: str


# ---------- prompts ----------


_EXTRACT_SYSTEM = (
    "You're reading a real-estate listing for a rental in the SF Bay Area. "
    "Extract the requested fields. Be conservative — return null when unclear."
)

_RANK_SYSTEM = textwrap.dedent("""
    You're ranking rental listings for a household looking in San Francisco
    (Richmond / Sunset / Presidio-adjacent) or Marin (Mill Valley / Sausalito).
    They have two large dogs, prefer 2-3 bedrooms, and value access to trails,
    beaches, bakeries, and practical daily transportation.

    ── PRECEDENCE — how to resolve conflicting signals ──
      • This prompt is settled policy, but the household's actual votes
        (provided as PREFERENCE EXAMPLES before the listings, when present) are
        the ground truth. Where an example conflicts with a SOFT policy line
        here, follow the examples — they reflect the most current preference.
      • HARD REQUIREMENTS below always win, even over examples (dog policy, etc.).
      • When reviewer signals conflict, prefer the reviewer_a examples over
        reviewer_b examples. reviewer_a is the primary preference signal.

    ── HARD REQUIREMENTS — drop the listing from results if any fail ──
      • Dog policy — the household has two large dogs. Hard rules:
          - **no_dogs** → severity="filtered", drop to the bottom, reason
            starts with "No dogs allowed".
          - **small_only** → severity="concerns" ALWAYS, never "ok". Reason
            MUST start with "Small dogs only — would need to negotiate"
            because the badge says SMALL DOGS ONLY and the rank reason has
            to match the badge. Never describe these as "dog-friendly".
            They should not outrank a comparable dogs_ok / large_ok listing.
          - **dogs_ok / large_ok** → eligible for severity="ok".
          - **null/unknown** → severity="concerns", flag the verification need.
      • For Marin listings: a private yard is strongly preferred
        (fenced backyard, side yard, or private patio with grass). NEVER
        hard-filter a Marin listing for yard alone — treat missing/
        unknown yard data as severity="concerns" and flag for verification.
        Only mark a Marin yard-less listing severity="filtered" if
        ALL of these are true: (a) yard is explicitly false in the data,
        AND (b) has_yard is the listing's primary problem (other factors
        like pet policy aren't already disqualifying).
      • Listings missing critical data like price or beds (showing as ? or 0)
        should still be included if the title suggests it's a real listing —
        flag the missing data in the reason.

    ── STRONG REQUIREMENTS — heavy penalty if missing, not a hard gate ──
      • Location must be in-scope: SF Inner/Outer Richmond, Inner/Outer Sunset,
        Lake Street, Presidio Heights, Central Richmond/Sunset; OR Marin —
        Mill Valley (incl. Tam Valley, Homestead Valley, Almonte) or Sausalito.
      • Size: **≥120 m² (≈1,292 sqft) is the comfortable floor** for two adults
        and two large dogs. Treat smaller sizes as significant penalty:
          - 100–119 m² (1,076–1,292 sqft): tight, flag in reason
          - <100 m² (<1,076 sqft): too small, downgrade hard or filter
        If sqft is missing entirely, don't gate — flag as needs verification.
      • In-unit laundry strongly preferred. "Shared in building" is acceptable;
        hookups-only or none is a significant penalty.
      • Parking on-site (garage, attached, off-street). Street-only is
        workable in SF, but is a soft penalty.
      • Trail OR beach access — REVEALED AS NEAR-MANDATORY. ~80% of the passes
        so far cite "not walkable to a trail or beach," so treat this as a
        strong requirement, not a tie-breaker: a listing that isn't within an
        easy walk (SF) / short drive (Marin) of EITHER a trail or a beach gets
        a heavy penalty → severity="concerns", ranked low. NOT a hard gate —
        never "filtered"-drop on trail/beach distance alone. (Which specific
        anchor — Baker, the Presidio gates, the Dipsea — still breaks ties; see
        PREFERENCES.)
      • Aesthetics is a SOFT tie-breaker, NOT a heavy penalty. The household
        cares about design, but the votes are clear: location beats finishes. "It's
        ugly but we can take a look" was an UP vote. Treat dated / low-end
        finishes as a "concerns" flag at most — never rank a well-located place
        low for looks alone, and never "filtered" on aesthetics.

    ── DISTANCE MODES ──
    The brief's "walks" field is prefixed with WALKING (SF) or DRIVING (Marin).
    For SF listings, all times are WALKING — apply the bakery preference, etc.
    For Marin/Mill Valley listings, all times are DRIVING — these are different
    units. Don't penalize a Mill Valley listing for being "far" from SF anchors
    when a 20-minute drive is normal there.

    ── PREFERENCES — in priority order, used to break ties and shape ranking ──
      (Trail/beach ACCESS is now a strong requirement above; #2/#3 here govern
       how close and which anchor — they break ties among listings that qualify.)
      1. Close to SF. For SF listings this means walking distance to Muni /
         downtown. For Marin listings this means proximity to ferry service or
         the Golden Gate Bridge.
      2. Close to trail access. SF = Presidio gates (Arguello, Lyon, West
         Pacific). Marin = Dipsea / Tennessee Valley / Headlands access.
      3. Close to a beach. **Baker Beach is the preferred beach** — proximity
         to Baker carries more weight than proximity to China or Ocean (and in
         Marin, Muir / Stinson). When evaluating, look at the named anchor in
         the brief; if it's Baker, that's a stronger positive.
      4. Close to a bakery or cafe-with-pastries.
         For SF listings: the bar is 4.7★ + 1,500+ reviews. Qualifying set:
         Arsicault, Cinderella, b. patisserie, Arizmendi — all in SF.
         **Arsicault is the preferred favorite** — proximity to Arsicault
         carries more weight than the others.
         For Marin listings: the bar is lower (4.5★+ / 100+ reviews) because
         the market is smaller. Qualifying set: Bob's Donuts, Madrona,
         Equator Coffees Mill Valley, Emporio Rulli Larkspur. Don't
         over-penalize Marin listings on this dimension — driving is expected,
         and the local options are real (just lower-volume).

    ── ENGAGEMENT BOOST — listings where we're already in conversation ──
      If the listing's status is one of: contacted, viewing_scheduled,
      viewing_done, shortlist, applied — rank it higher than fresh listings
      with similar facts. Conversations have momentum; protect that.
      Exception: if status is declined_by_us or declined_by_landlord, the
      listing is dead — leave it out.

    ── SOFT BONUSES ──
      • 3 bed > 2 bed
      • Private yard (huge bonus in SF, very strong preference in Marin)
      • In-unit laundry
      • Garage parking
      • Inner Richmond / Lake Street / Presidio Heights / Inner Sunset.
      • Downtown Mill Valley walkability.

    ── OUTPUT FORMAT ──
    Return EVERY listing in the input — none dropped silently. Order best
    first. Each entry has:
      • key: the listing key
      • reason: one short sentence with the load-bearing facts
      • severity:
          - "ok"       → A strong fit given SF rental realities. Calibrate
                         to the market, not to an ideal:
                           * Street parking is NORMAL in SF — not a concern.
                           * Shared laundry-in-building is fine — not a concern.
                           * No private yard in SF is the default — not a concern.
                           * 1 bath in a 2-bed, or 1.5 bath in a 3-bed, is
                             normal — not a concern.
                         A 3BR/1.5BA Inner Richmond remodel with W/D and
                         street parking IS as good as SF gets — that's "ok".
          - "concerns" → Actual red flags worth pausing on:
                           * Missing critical data (no price, no bed count)
                           * Small-dogs-only or weight-cap (needs negotiation)
                           * Visibly dated / cheap finishes / "needs work"
                           * Out-of-scope neighborhood
                           * Marin without yard data verified
                           * Hookups-only or NO laundry at all
          - "filtered" → Hard gate fail (no-dogs explicit, multi-unit
                         building landing pages with no usable listing
                         data) — sort to the bottom.
""").strip()


# ---------- helpers ----------


def _html_to_markdown(html: str, *, max_chars: int = 12000) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "iframe"]):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.body
    if not main:
        return ""

    # Zillow buries the structured "Facts and features" list outside <main>
    # in some layouts. Surface it explicitly so the model can read it.
    facts_section = ""
    fact_lis = soup.select("ul[class*=Fact] li")
    if fact_lis:
        facts_lines = [
            "## Facts and features",
            *[f"- {li.get_text(' ', strip=True)}" for li in fact_lis if li.get_text(strip=True)],
            "",
        ]
        facts_section = "\n".join(facts_lines) + "\n"

    md = markdownify(str(main), heading_style="ATX", strip=["a", "img"])
    md = re.sub(r"\n{3,}", "\n\n", md).strip()
    return (facts_section + md)[:max_chars]


def _call_structured(
    model: str, system: str, prompt: str, schema: type[BaseModel],
    *, max_output_tokens: int | None = None,
) -> BaseModel | None:
    try:
        client = _get_client()
    except RuntimeError as e:
        print(f"  llm config err: {e}")
        return None
    # Don't cap output unless the caller insists. Gemini 2.5 supports 65k+ output
    # tokens; capping mid-response truncates JSON mid-string and the parser fails.
    config = gtypes.GenerateContentConfig(
        temperature=0,
        max_output_tokens=max_output_tokens,
        response_mime_type="application/json",
        response_schema=schema,
        system_instruction=system,
    )
    try:
        resp = client.models.generate_content(model=model, contents=prompt, config=config)
    except Exception as e:
        print(f"  llm call err [{model}]: {e}")
        return None
    text = (resp.text or "").strip()
    if not text:
        return None
    try:
        return schema.model_validate_json(text)
    except Exception as e:
        # Sometimes the model returns JSON wrapped in extra prose — strip.
        m = re.search(r"\{.*\}|\[.*\]", text, re.DOTALL)
        if m:
            try:
                return schema.model_validate_json(m.group(0))
            except Exception:
                pass
        print(f"  llm parse err: {e}")
        return None


# ---------- public ----------


def extract_facts(listing: Listing, conn: sqlite3.Connection) -> ExtractedFacts | None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS llm_facts (
            key TEXT PRIMARY KEY,
            facts_json TEXT NOT NULL,
            ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    row = conn.execute("SELECT facts_json FROM llm_facts WHERE key=?", (listing.key,)).fetchone()
    if row:
        return ExtractedFacts.model_validate_json(row[0])

    html = cache.get(listing.source, listing.source_id)
    if not html:
        return None
    md = _html_to_markdown(html)
    if not md:
        return None
    facts = _call_structured(EXTRACT_MODEL, _EXTRACT_SYSTEM, md, ExtractedFacts)
    if facts is None:
        return None

    conn.execute(
        "INSERT OR REPLACE INTO llm_facts (key, facts_json) VALUES (?, ?)",
        (listing.key, facts.model_dump_json()),
    )
    conn.commit()
    return facts


def apply_facts(listing: Listing, facts: ExtractedFacts) -> None:
    if facts.has_yard is not None:
        listing.has_yard = facts.has_yard
    if facts.yard_note:
        listing.yard_note = facts.yard_note
    if not listing.parking and facts.parking:
        listing.parking = facts.parking
    if not listing.laundry and facts.laundry:
        listing.laundry = facts.laundry
    if facts.dog_policy and not listing.dog_policy:
        listing.dog_policy = facts.dog_policy
    if not listing.description:
        parts = []
        if facts.vibe:
            parts.append(facts.vibe)
        if facts.warnings:
            parts.append(f"Note: {facts.warnings}")
        if parts:
            listing.description = " · ".join(parts)


def _listing_brief(L: Listing, walk_summary: str, feedback: str = "") -> str:
    bits = [
        f"key: {L.key}",
        f"price: ${L.price}/mo" if L.price else "price: ?",
        f"beds: {L.beds}, baths: {L.baths}",
        f"sqft: {L.sqft}" if L.sqft else "",
        f"hood: {L.hood}",
        f"dog: {L.dog_policy}",
        f"parking: {L.parking}",
        f"laundry: {L.laundry}",
        f"yard: {L.has_yard} ({L.yard_note})" if L.has_yard is not None else "yard: ?",
        # walk_summary is always populated on the live ranking path (it starts
        # with "WALKING (SF)" / "DRIVING (Marin)"); only the historical
        # example briefs pass "" — drop the empty bit there.
        f"walks: {walk_summary}" if walk_summary else "",
    ]
    if L.description:
        bits.append(f"vibe: {L.description[:140]}")
    # Verbatim human feedback for THIS listing, welded onto its own line so the
    # ranker sees up/pass reasons inline in the reviewers' own words.
    if feedback:
        bits.append(f"feedback: {feedback}")
    return " | ".join(b for b in bits if b)


# Voters whose signal leads the few-shot block, in priority order. reviewer_a
# is the primary signal and is never crowded out by the N cap. Unknown voter last.
_VOTER_PRIORITY = {"reviewer_a": 0, "reviewer_b": 1}
_EXAMPLES_CAP = 40


def _current_feedback(conn: sqlite3.Connection, keys: list[str]) -> dict[str, str]:
    """Verbatim votes/notes for listings in the CURRENT batch, keyed by listing.

    Each current-batch line carries its own human feedback inline (their own
    words). Returns {} when there's none — keeping the prompt unchanged.
    """
    if not keys:
        return {}
    ph = ",".join("?" * len(keys))
    arrow = {"up": "↑", "down": "↓"}
    out: dict[str, list[str]] = {}
    for r in conn.execute(
        f"SELECT listing_key, voter, direction, reason FROM votes "
        f"WHERE listing_key IN ({ph}) AND reason IS NOT NULL AND reason != '' "
        f"ORDER BY ts, id",
        keys,
    ):
        out.setdefault(r["listing_key"], []).append(
            f'{r["voter"]} {arrow.get(r["direction"], r["direction"])} "{r["reason"]}"'
        )
    for r in conn.execute(
        f"SELECT listing_key, status_note FROM listing_status "
        f"WHERE listing_key IN ({ph}) AND status='passed_on' "
        f"AND status_note IS NOT NULL AND status_note != ''",
        keys,
    ):
        out.setdefault(r["listing_key"], []).append(f'passed_on "{r["status_note"]}"')
    return {k: "; ".join(v) for k, v in out.items()}


def _preference_examples(conn: sqlite3.Connection, *, cap: int = _EXAMPLES_CAP) -> str:
    """Few-shot block of recent up/pass votes, fresh signal the prompt hasn't
    yet absorbed. Returns "" when there are no votes (prompt stays byte-identical
    to the voteless baseline). Deterministic: stable ORDER BY + temperature=0.

    UP signal = `votes` (direction='up'). DOWN signal = `passed_on` notes in
    `listing_status` (voter via the eliminate `actions` row) UNION
    `votes` (direction='down'); passed_on wins on overlap. reviewer_a examples
    lead and survive the cap; promoted contradictions age out as new votes land.
    """
    from . import storage

    def _recent_per(direction: str) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT v.listing_key AS key, v.voter AS voter, v.reason AS reason, "
            "v.ts AS ts FROM votes v JOIN (SELECT listing_key, voter, MAX(id) AS mid "
            "FROM votes WHERE direction=? GROUP BY listing_key, voter) m "
            "ON v.id = m.mid",
            (direction,),
        ).fetchall()

    ups = [
        {"key": r["key"], "voter": r["voter"], "reason": r["reason"], "ts": r["ts"] or "", "kind": "UP"}
        for r in _recent_per("up")
    ]

    # Down signal, merged by listing — the funnel-level `passed_on` note wins
    # over a raw down-vote when a listing has both.
    down: dict[str, dict] = {}
    for r in _recent_per("down"):
        if r["reason"]:
            down[r["key"]] = {"key": r["key"], "voter": r["voter"], "reason": r["reason"],
                              "ts": r["ts"] or "", "kind": "PASS"}
    for r in conn.execute(
        "SELECT s.listing_key AS key, s.status_note AS reason, s.updated_at AS ts, "
        "(SELECT a.voter FROM actions a WHERE a.listing_key = s.listing_key "
        " AND a.kind='eliminate' AND json_extract(a.payload_json,'$.new')='passed_on' "
        " ORDER BY a.id DESC LIMIT 1) AS voter "
        "FROM listing_status s WHERE s.status='passed_on' "
        "AND s.status_note IS NOT NULL AND s.status_note != ''"
    ):
        down[r["key"]] = {"key": r["key"], "voter": r["voter"], "reason": r["reason"],
                          "ts": r["ts"] or "", "kind": "PASS"}

    # Collapse identical pass reasons into one example with a (×N) count,
    # keeping the most-recent occurrence's voter/ts for ordering + labeling.
    passes: dict[str, dict] = {}
    for e in sorted(down.values(), key=lambda x: x["ts"]):
        norm = (e["reason"] or "").strip().lower()
        cur = passes.get(norm)
        if cur:
            cur["count"] += 1
            cur.update(key=e["key"], voter=e["voter"], ts=e["ts"])  # latest wins
        else:
            passes[norm] = {**e, "count": 1}

    entries = ups + list(passes.values())
    if not entries:
        return ""

    def _prio(e: dict) -> int:
        return _VOTER_PRIORITY.get(e["voter"], 2)

    # ts desc within each voter (string ISO sorts lexically), primary reviewer first.
    entries.sort(key=lambda e: e["ts"], reverse=True)
    entries.sort(key=_prio)
    entries = entries[:cap]

    # Hydrate facts for the listings still in the DB; gone listings fall back to
    # a reason-only example (the reason is the durable signal).
    rows = {
        r["key"]: r for r in conn.execute(
            f"SELECT * FROM listings WHERE key IN ({','.join('?' * len(entries))})",
            [e["key"] for e in entries],
        )
    }
    lines = []
    for e in entries:
        row = rows.get(e["key"])
        brief = _listing_brief(storage._row_to_listing(row), "") if row else "(listing no longer in DB)"
        reason = f' → "{e["reason"]}"' if e.get("reason") else ""
        count = f' (×{e["count"]})' if e.get("count", 1) > 1 else ""
        lines.append(f'[{e["kind"]} · {e["voter"] or "?"}] {brief}{reason}{count}')

    header = (
        "PREFERENCE EXAMPLES — recent up/pass votes from the household, most "
        "current first. These reflect their LIVE preference; weigh them per the "
        "PRECEDENCE rules (reviewer_a signal leads; examples beat soft policy; "
        "hard requirements still win)."
    )
    return header + "\n" + "\n".join(lines)


def rank_listings(
    listings: list[Listing], walk_map: dict, conn: sqlite3.Connection
) -> dict[str, tuple[int, str, str]]:
    """Return {key: (rank, reason, severity)}."""
    if not listings:
        return {}

    from .walk import BAKERIES, BEACHES, SF_CENTER, TRAILS, minutes_to, nearest, is_marin, populate_drive_for_marin

    drive_map = populate_drive_for_marin(listings)

    def _walk_summary(L: Listing) -> str:
        if is_marin(L) and drive_map:
            # Marin: drive-time mode so the LLM doesn't see giant walking numbers
            # for SF anchors and penalize Mill Valley unfairly.
            def _best(anchors):
                best = None
                for a in anchors:
                    m = drive_map.get((L.key, a.name))
                    if m is None: continue
                    if best is None or m < best[1]:
                        best = (a, m)
                return best
            bits = ["DRIVING (Marin)"]
            sf = drive_map.get((L.key, SF_CENTER[0].name))
            if sf is not None: bits.append(f"{sf}m drive to SF")
            t = _best(TRAILS)
            if t: bits.append(f"{t[1]}m drive trail({t[0].short})")
            b = _best(BEACHES)
            if b: bits.append(f"{b[1]}m drive beach({b[0].short})")
            ba = _best(BAKERIES)
            if ba: bits.append(f"{ba[1]}m drive bakery({ba[0].short})")
            return ", ".join(bits)
        # SF: walking.
        sf = minutes_to(walk_map, L.key, SF_CENTER[0])
        np = nearest(walk_map, L.key, TRAILS)
        nb = nearest(walk_map, L.key, BEACHES)
        nba = nearest(walk_map, L.key, BAKERIES)
        bits = ["WALKING (SF)"]
        if sf is not None: bits.append(f"{sf}m walk SF")
        if np: bits.append(f"{np[1]}m walk trail({np[0].short})")
        if nb: bits.append(f"{nb[1]}m walk beach({nb[0].short})")
        if nba: bits.append(f"{nba[1]}m walk bakery({nba[0].short})")
        return ", ".join(bits)

    feedback_map = _current_feedback(conn, [L.key for L in listings])
    body = "\n".join(
        _listing_brief(L, _walk_summary(L), feedback_map.get(L.key, "")) for L in listings
    )
    # Fresh human signal the static policy hasn't absorbed yet. Empty on cold
    # start → content is byte-identical to the voteless baseline.
    examples = _preference_examples(conn)
    content = (examples + "\n\n" if examples else "") + "Listings:\n" + body
    result = _call_structured(RANK_MODEL, _RANK_SYSTEM, content, RankList)
    if result is None:
        return {}
    assert isinstance(result, RankList)
    return {
        entry.key: (i + 1, entry.reason, entry.severity)
        for i, entry in enumerate(result.results)
    }


_PHOTO_REVIEW_SYSTEM = textwrap.dedent("""
    You're reviewing rental listing photos.

    Call out what photos reveal that words usually skip:
      - Natural light, window count, sun direction hints
      - Outward views vs blocked-by-building
      - Outdoor space (driveways, side yards, fenced backyards, terraces,
        roof decks) — often visible in photos but absent from listing copy
      - Floor type, period detail, layout flow
      - Condition: high-end recent remodel vs dated 80s/90s vs tired

    Be honest and specific. Don't repeat the listing description. Focus on
    things photos show that copy hides.
""").strip()


def review_photos(
    listing: Listing, conn: sqlite3.Connection, *, max_photos: int = 6,
) -> PhotoReview | None:
    """Fetch the listing's photos, hand them to Gemini Vision, cache the result.

    Photos come from listing.photos. We fetch the bytes inline (Vertex's
    multimodal API takes either GCS URIs or inline bytes; inline is simpler
    and the photos are small).
    """
    import hashlib
    conn.execute(
        """CREATE TABLE IF NOT EXISTS llm_photo_reviews (
            key TEXT PRIMARY KEY,
            photos_hash TEXT,
            review_json TEXT NOT NULL,
            ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    # ALTER table for older DBs that don't have photos_hash yet.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(llm_photo_reviews)").fetchall()}
    if "photos_hash" not in cols:
        conn.execute("ALTER TABLE llm_photo_reviews ADD COLUMN photos_hash TEXT")

    photos = (listing.photos or [])[:max_photos]
    if not photos:
        return None
    # Fingerprint the photo set so the cache auto-invalidates when photos change.
    photos_hash = hashlib.md5("|".join(photos).encode()).hexdigest()[:16]

    row = conn.execute(
        "SELECT review_json, photos_hash FROM llm_photo_reviews WHERE key=?",
        (listing.key,),
    ).fetchone()
    if row and row[1] == photos_hash:
        return PhotoReview.model_validate_json(row[0])

    import httpx
    image_parts = []
    for url in photos:
        try:
            r = httpx.get(url, timeout=15, follow_redirects=True)
            if r.status_code != 200:
                continue
            mime = "image/jpeg" if url.lower().endswith(".jpg") else r.headers.get("content-type", "image/jpeg")
            image_parts.append(gtypes.Part.from_bytes(data=r.content, mime_type=mime))
        except Exception:
            continue

    if not image_parts:
        return None

    client = _get_client()
    addr = listing.address or "(no address)"
    text_part = gtypes.Part.from_text(
        text=f"Listing: {addr}\nPrice: ${listing.price}/mo · "
        f"{listing.beds}bd/{listing.baths}ba · {listing.sqft} sqft.\n\n"
        "Please review these listing photos and return the structured JSON."
    )
    try:
        resp = client.models.generate_content(
            model=RANK_MODEL,  # 3.1 Pro — vision-capable
            contents=[gtypes.Content(role="user", parts=[text_part, *image_parts])],
            config=gtypes.GenerateContentConfig(
                temperature=0,
                response_mime_type="application/json",
                response_schema=PhotoReview,
                system_instruction=_PHOTO_REVIEW_SYSTEM,
            ),
        )
        text = (resp.text or "").strip()
        review = PhotoReview.model_validate_json(text)
    except Exception as e:
        print(f"  photo review err [{listing.key}]: {str(e)[:120]}")
        return None

    conn.execute(
        "INSERT OR REPLACE INTO llm_photo_reviews (key, photos_hash, review_json) "
        "VALUES (?, ?, ?)",
        (listing.key, photos_hash, review.model_dump_json()),
    )
    conn.commit()
    return review


def apply_photo_review(listing: Listing, review: PhotoReview) -> None:
    listing.light_quality = review.light_quality if review.light_quality != "unknown" else None
    listing.view_quality = review.view_quality if review.view_quality != "unknown" else None
    listing.condition_quality = review.condition_quality if review.condition_quality != "unknown" else None
    listing.outdoor_visible = review.outdoor_visible
    listing.other_visible = review.other_visible
    listing.visual_summary = review.visual_summary
    # 1. Drop any photos Gemini flagged as non-unit (headshots, floor plans,
    #    logos, satellite maps, etc.). Apply BEFORE the best_photo promotion
    #    so indices don't shift.
    drops = sorted(set(review.drop_indices or []), reverse=True)
    if listing.photos:
        for i in drops:
            if 0 <= i < len(listing.photos):
                listing.photos.pop(i)
    # 2. Promote Gemini's chosen best photo to position 0. The drop step may
    #    have shifted indices — recompute against the post-drop list.
    idx = review.best_photo_index or 0
    # If the chosen index was dropped, treat 0 as the new best.
    if idx in drops:
        idx = 0
    else:
        # Adjust idx down by the count of dropped indices below it.
        idx -= sum(1 for d in drops if d < idx)
    if listing.photos and 0 < idx < len(listing.photos):
        chosen = listing.photos.pop(idx)
        listing.photos.insert(0, chosen)
    if listing.photos:
        listing.image_url = listing.photos[0]
    else:
        listing.image_url = None
    # If the photos show outdoor space and we hadn't detected a yard, upgrade.
    if review.outdoor_visible and listing.has_yard is None:
        # Only commit yard=True when the phrasing is unambiguous.
        outdoor_lc = review.outdoor_visible.lower()
        if any(k in outdoor_lc for k in ("backyard", "back yard", "side yard", "private", "fenced")):
            listing.has_yard = True
            if not listing.yard_note:
                listing.yard_note = review.outdoor_visible


_SHARE_BLURB_SYSTEM = textwrap.dedent("""
    You're writing a SHORT preview blurb for sharing a rental listing in a
    chat between household reviewers. The blurb appears below the listing
    photo in the share card.

    Constraints:
      - 100–180 characters total. WhatsApp truncates around 200.
      - One sentence, plain text, no emoji unless it's load-bearing.
      - Lead with what makes the place interesting OR what the red flag is.
      - Name the neighborhood explicitly.
      - Don't repeat the price (it's in the title).
      - For listings with concerns (small-dog-only, no parking, dated finishes,
        missing data) — be honest, name the issue.
      - For strong matches — name the most distinctive feature (yard, walk to
        Arsicault, in-unit laundry + Inner Richmond, etc.).

    Examples:
      "Spacious 3BR Inner Richmond flat with in-unit laundry and a fenced
       backyard, 8 min walk to Arsicault."
      "Stunning Mill Valley remodel near the Dipsea trail — but small dogs
       only, will need landlord negotiation."
      "Outer Sunset 2BR with no parking and dated finishes — passable
       value at this price but better options exist."
""").strip()


class _ShareBlurb(BaseModel):
    blurb: str


def _listing_to_md(listing: Listing) -> str:
    """Render a listing as a markdown brief for Gemini prose calls.

    Photo-review fields (visual_summary, light/view/condition, outdoor_visible)
    are already merged onto the Listing during enrichment, so they're included
    automatically — no separate fetch from llm_photo_reviews needed.
    """
    payload = listing.model_dump(exclude_none=True, exclude={"raw", "photos", "scraped_at"})
    lines = ["# Listing", ""]
    # Group fields by section for readability.
    sections = {
        "Where": ["address", "neighborhood", "neighborhood_resolved", "hood", "lat", "lng"],
        "Size & price": ["price", "beds", "baths", "sqft"],
        "Pets": ["dog_policy", "pets_allowed"],
        "Amenities": ["parking", "laundry", "has_yard", "yard_note", "outdoor_visible", "other_visible"],
        "Photos read": ["light_quality", "view_quality", "condition_quality", "visual_summary"],
        "CRM": ["llm_rank", "llm_severity", "llm_reason"],
        "Contact": ["contact_name", "contact_phone", "contact_email"],
        "Source": ["source", "url", "title", "description"],
    }
    used: set[str] = set()
    for section, fields in sections.items():
        rows = [(f, payload[f]) for f in fields if f in payload]
        used.update(f for f, _ in rows)
        if not rows:
            continue
        lines.append(f"## {section}")
        for k, v in rows:
            lines.append(f"- **{k}**: {v}")
        lines.append("")
    # Catch-all for fields not in the section map.
    leftover = [(k, v) for k, v in payload.items() if k not in used]
    if leftover:
        lines.append("## Other")
        for k, v in leftover:
            lines.append(f"- **{k}**: {v}")
    return "\n".join(lines)


def generate_share_blurb(listing: Listing) -> str | None:
    """One-shot Gemini call → a tight share-friendly description."""
    res = _call_structured(
        EXTRACT_MODEL, _SHARE_BLURB_SYSTEM, _listing_to_md(listing), _ShareBlurb,
    )
    return res.blurb if res else None


def extract_interaction(
    body: str, *, direction: str = "in", sender: str | None = None,
) -> InteractionUpdate | None:
    """Pull structured updates from a free-form message.

    `direction`: 'in' = from the landlord/agent, 'out' = from the household.
    Used to disambiguate ambiguous prose like "my phone number is …" — only
    the landlord saying it should populate the listing's contact_phone.
    """
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    who = (
        f"This message was sent by the landlord/agent ({sender or 'unknown'})."
        if direction == "in"
        else "This message was sent by the household to the landlord/agent. "
        "Any phone number, address, or 'my X' references here belong to the household, "
        "not the landlord's — do NOT populate landlord-contact fields from this."
    )
    return _call_structured(
        EXTRACT_MODEL,
        f"Extract structured listing-update info from a message. {who} "
        f"Today's date is {today}. When the sender mentions a day/date without a "
        "year (e.g. 'Saturday 5/16'), resolve it to the NEAREST UPCOMING date in "
        f"the same calendar year — DO NOT default to a past year. "
        "If the landlord replies to an inquiry, infer status='contacted'. If they "
        "confirm a viewing time, infer status='viewing_scheduled' and set "
        "viewing_at as ISO. If the message only exchanges contact info or "
        "addresses (no scheduling change), leave status as null — don't downgrade "
        "an existing viewing. The summary should clearly attribute the action to "
        "the correct sender (landlord vs us).",
        body,
        InteractionUpdate,
    )


_ANALYZE_PREFS_SYSTEM = textwrap.dedent("""
    You're auditing a rental-ranking POLICY against the humans' actual VOTES.
    You'll get (1) the current ranking policy verbatim, and (2) every up/pass
    vote with its written reason. Find where the policy and the votes DISAGREE,
    and propose concrete, reviewable edits.

    Rules:
      • Quote the exact policy line you're flagging (policy_quote).
      • Back every claim with counts — "N of M passes cite X". No vibes.
      • Propose specific replacement text, not a vague direction. A human will
        hand-apply it and commit — you NEVER rewrite the policy yourself.
      • Only flag a pattern with real support (≥2 votes), not a one-off.
      • reviewer_a votes outweigh reviewer_b votes when they diverge.
      • Also surface consistent preferences the policy doesn't encode yet
        (new_rules) — but hold the same ≥2-votes bar.
      • If policy and votes already agree on a dimension, say nothing about it.
""").strip()


def analyze_preferences(conn: sqlite3.Connection) -> PrefAnalysis | None:
    """Compare revealed preference (all up/pass votes + reasons) against the
    static `_RANK_SYSTEM` policy. Returns flagged contradictions + proposed new
    rules for a human to review and hand-apply. Read-only; proposes, never writes.
    """
    rows: list[str] = []
    for r in conn.execute(
        "SELECT voter, direction, reason FROM votes "
        "WHERE reason IS NOT NULL AND reason != '' ORDER BY ts, id"
    ):
        rows.append(f'[{(r["direction"] or "?").upper()} · {r["voter"]}] "{r["reason"]}"')
    for r in conn.execute(
        "SELECT s.status_note AS reason, "
        "(SELECT a.voter FROM actions a WHERE a.listing_key = s.listing_key "
        " AND a.kind='eliminate' AND json_extract(a.payload_json,'$.new')='passed_on' "
        " ORDER BY a.id DESC LIMIT 1) AS voter "
        "FROM listing_status s WHERE s.status='passed_on' "
        "AND s.status_note IS NOT NULL AND s.status_note != '' ORDER BY s.updated_at"
    ):
        rows.append(f'[PASS · {r["voter"] or "?"}] "{r["reason"]}"')

    if not rows:
        return None

    prompt = (
        "CURRENT RANKING POLICY:\n" + _RANK_SYSTEM + "\n\n"
        f"VOTES ({len(rows)} total, oldest first):\n" + "\n".join(rows)
    )
    result = _call_structured(RANK_MODEL, _ANALYZE_PREFS_SYSTEM, prompt, PrefAnalysis)
    return result if isinstance(result, PrefAnalysis) else None
