"""SQLite-backed storage for listings.

One row per (source, source_id). Each search upserts; `last_seen` tracks
freshness, `active` marks whether the most recent run saw the listing.
"""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .models import Listing

_DEFAULT_DB_PATH = Path(__file__).parent.parent.parent / "casita.db"


def db_path() -> Path:
    """Resolve the SQLite path, allowing CASITA_DB_PATH to redirect.

    cloud_sync uses this to point each verb at a GCS-pulled temp file
    without rewriting every storage.connect() call site.
    """
    return Path(os.environ.get("CASITA_DB_PATH", str(_DEFAULT_DB_PATH)))


# Module-level alias kept for callers that print the path; for sqlite
# connections always go through db_path() so env overrides are honored.
DB_PATH = _DEFAULT_DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
  key TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  source_id TEXT NOT NULL,
  url TEXT,
  title TEXT,
  address TEXT,
  neighborhood TEXT,
  neighborhood_resolved TEXT,
  price INTEGER,
  beds REAL,
  baths REAL,
  sqft INTEGER,
  pets_allowed INTEGER,
  dog_policy TEXT,
  parking TEXT,
  laundry TEXT,
  has_yard INTEGER,
  yard_note TEXT,
  llm_rank INTEGER,
  llm_reason TEXT,
  llm_severity TEXT,
  contact_name TEXT,
  contact_phone TEXT,
  contact_email TEXT,
  contact_url TEXT,
  description TEXT,
  image_url TEXT,
  photos_json TEXT,
  lat REAL,
  lng REAL,
  raw_json TEXT,
  first_seen TIMESTAMP NOT NULL,
  last_seen TIMESTAMP NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_listings_active_price
  ON listings (active, price);

CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TIMESTAMP NOT NULL,
  finished_at TIMESTAMP,
  total_seen INTEGER,
  new_count INTEGER
);

-- Inbound / outbound communications about a listing. Free-form body — the
-- LLM extracts structured updates and writes them back to `listings`.
CREATE TABLE IF NOT EXISTS interactions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  listing_key TEXT,           -- nullable: we may add the interaction before resolving the listing
  ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  direction TEXT NOT NULL,    -- 'in' (from landlord) | 'out' (from us) | 'note' (our own)
  channel TEXT,               -- 'text' | 'email' | 'phone' | 'in_person' | null
  sender TEXT,                -- name of the person if known
  body TEXT NOT NULL,
  extracted_json TEXT,        -- LLM-extracted structured updates
  applied INTEGER NOT NULL DEFAULT 0  -- whether the extracted updates have been merged onto the listing
);

CREATE INDEX IF NOT EXISTS idx_interactions_listing ON interactions (listing_key);

-- Per-listing status in the search funnel.
CREATE TABLE IF NOT EXISTS listing_status (
  listing_key TEXT PRIMARY KEY,
  status TEXT NOT NULL,       -- 'new' | 'contacted' | 'viewing_scheduled' | 'viewing_done'
                              -- | 'shortlist' | 'declined_by_us' | 'declined_by_landlord' | 'applied' | 'accepted'
  status_note TEXT,
  viewing_at TIMESTAMP,       -- when a viewing is scheduled, if any
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Manually-entered listings — for when we hear about a place that isn't on
-- Zillow / Craigslist (landlord contacted directly, found via flyer, etc.).
-- Stored alongside scraped listings via source='manual'.

-- Local artifacts attached to a listing or interaction. We store screenshots
-- of landlord chats + listing pages here so the data survives even if the
-- source is later taken down / rate-limited.
CREATE TABLE IF NOT EXISTS attachments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  listing_key TEXT,
  interaction_id INTEGER,
  filename TEXT NOT NULL,    -- relative path inside screenshots/
  kind TEXT NOT NULL,        -- 'chat' | 'listing' | 'page' | 'photo' | 'other'
  caption TEXT,
  ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_attachments_listing ON attachments (listing_key);

-- Lightweight upvote / downvote on a listing. Distinct from listing_status
-- (which records the funnel state — declined, applied, etc.). Votes capture
-- preference signal from chat: "I like this one", "not this one". Multiple
-- votes per (listing, voter) allowed — most recent wins for display, but
-- the history is kept for learning the ranker.
CREATE TABLE IF NOT EXISTS votes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  listing_key TEXT NOT NULL,
  voter TEXT NOT NULL,           -- reviewer label
  direction TEXT NOT NULL,       -- 'up' | 'down'
  reason TEXT,
  ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_votes_listing ON votes (listing_key, ts);

-- URLs pasted into another workflow that the local scraper should pick up.
-- If the cloud side cannot fetch a source itself, the 'casita add' verb queues
-- the URL for the next local scrape.
CREATE TABLE IF NOT EXISTS pending_urls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  url TEXT NOT NULL,
  added_by TEXT NOT NULL,        -- reviewer label
  note TEXT,
  ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  processed_at TIMESTAMP         -- null until the scraper ingests it
);

-- Append-only log of every mutating verb. Enables 'casita undo': pop the
-- most recent un-undone action on a listing and reverse it.
CREATE TABLE IF NOT EXISTS actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  listing_key TEXT,              -- null for cross-listing actions (e.g. add)
  voter TEXT NOT NULL,
  kind TEXT NOT NULL,            -- 'vote' | 'eliminate' | 'note' | 'add'
  payload_json TEXT NOT NULL,    -- args + the prior-state snapshot for undo
  ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  undone INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_actions_listing ON actions (listing_key, ts);
"""

COLUMNS = [
    "key", "source", "source_id", "url", "title", "address",
    "neighborhood", "neighborhood_resolved",
    "price", "beds", "baths", "sqft", "pets_allowed", "dog_policy",
    "parking", "laundry", "has_yard", "yard_note", "llm_rank", "llm_reason", "llm_severity",
    "contact_name", "contact_phone", "contact_email", "contact_url", "contact_note",
    "description", "image_url", "photos_json", "lat", "lng", "raw_json",
    "light_quality", "view_quality", "condition_quality",
    "outdoor_visible", "other_visible", "visual_summary", "share_blurb", "share_token",
]


_NEW_COLUMNS: list[tuple[str, str, str]] = [
    # (table, column, type) — applied via ALTER TABLE if missing.
    ("listings", "photos_json", "TEXT"),
    ("listings", "has_yard", "INTEGER"),
    ("listings", "yard_note", "TEXT"),
    ("listings", "neighborhood_resolved", "TEXT"),
    ("listings", "llm_rank", "INTEGER"),
    ("listings", "llm_reason", "TEXT"),
    ("listings", "llm_severity", "TEXT"),
    ("listings", "dog_policy", "TEXT"),
    ("listings", "laundry", "TEXT"),
    ("listings", "lat", "REAL"),
    ("listings", "lng", "REAL"),
    ("listings", "light_quality", "TEXT"),
    ("listings", "view_quality", "TEXT"),
    ("listings", "condition_quality", "TEXT"),
    ("listings", "outdoor_visible", "TEXT"),
    ("listings", "other_visible", "TEXT"),
    ("listings", "visual_summary", "TEXT"),
    ("listings", "share_blurb", "TEXT"),
    ("listings", "share_token", "TEXT"),
    ("listings", "address_verified", "INTEGER NOT NULL DEFAULT 0"),
    ("listings", "contact_note", "TEXT"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, col, typ in _NEW_COLUMNS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")


@contextmanager
def connect():
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _listing_to_row(L: Listing) -> dict:
    return {
        "key": L.key,
        "source": L.source,
        "source_id": L.source_id,
        "url": L.url,
        "title": L.title,
        "address": L.address,
        "neighborhood": L.neighborhood,
        "neighborhood_resolved": L.neighborhood_resolved,
        "price": L.price,
        "beds": L.beds,
        "baths": L.baths,
        "sqft": L.sqft,
        "pets_allowed": None if L.pets_allowed is None else int(L.pets_allowed),
        "dog_policy": L.dog_policy,
        "parking": L.parking,
        "laundry": L.laundry,
        "has_yard": None if L.has_yard is None else int(L.has_yard),
        "yard_note": L.yard_note,
        "llm_rank": L.llm_rank,
        "llm_reason": L.llm_reason,
        "llm_severity": L.llm_severity,
        "contact_name": L.contact_name,
        "contact_phone": L.contact_phone,
        "contact_email": L.contact_email,
        "contact_url": L.contact_url,
        "contact_note": L.contact_note,
        "description": L.description,
        "image_url": L.image_url,
        "photos_json": json.dumps(L.photos) if L.photos else None,
        "lat": L.lat,
        "lng": L.lng,
        "raw_json": json.dumps(L.raw, default=str),
        "light_quality": L.light_quality,
        "view_quality": L.view_quality,
        "condition_quality": L.condition_quality,
        "outdoor_visible": L.outdoor_visible,
        "other_visible": L.other_visible,
        "visual_summary": L.visual_summary,
        "share_blurb": L.share_blurb,
        "share_token": L.share_token,
    }


def _row_to_listing(row: sqlite3.Row) -> Listing:
    return Listing(
        source=row["source"],
        source_id=row["source_id"],
        url=row["url"] or "",
        title=row["title"],
        address=row["address"],
        neighborhood=row["neighborhood"],
        neighborhood_resolved=row["neighborhood_resolved"],
        price=row["price"],
        beds=row["beds"],
        baths=row["baths"],
        sqft=row["sqft"],
        pets_allowed=None if row["pets_allowed"] is None else bool(row["pets_allowed"]),
        dog_policy=row["dog_policy"],
        parking=row["parking"],
        laundry=row["laundry"],
        has_yard=None if row["has_yard"] is None else bool(row["has_yard"]),
        yard_note=row["yard_note"],
        llm_rank=row["llm_rank"],
        llm_reason=row["llm_reason"],
        llm_severity=row["llm_severity"],
        contact_name=row["contact_name"],
        contact_phone=row["contact_phone"],
        contact_email=row["contact_email"],
        contact_url=row["contact_url"],
        contact_note=row["contact_note"] if "contact_note" in row.keys() else None,
        description=row["description"],
        image_url=row["image_url"],
        photos=json.loads(row["photos_json"]) if row["photos_json"] else [],
        lat=row["lat"],
        lng=row["lng"],
        raw=json.loads(row["raw_json"]) if row["raw_json"] else {},
        light_quality=row["light_quality"],
        view_quality=row["view_quality"],
        condition_quality=row["condition_quality"],
        outdoor_visible=row["outdoor_visible"],
        other_visible=row["other_visible"],
        visual_summary=row["visual_summary"],
        share_blurb=row["share_blurb"],
        share_token=row["share_token"],
        first_seen=row["first_seen"] if "first_seen" in row.keys() else None,
    )


def upsert_run(
    conn, listings: list[Listing], succeeded_sources: list[str] | None = None
) -> tuple[int, int]:
    """Upsert listings; return (run_id, new_count).

    `succeeded_sources` is the set of sources that returned results this run.
    Only listings from those sources will be marked inactive if not seen — a
    captcha-blocked source returning 0 shouldn't wipe its prior inventory.
    """
    now = datetime.utcnow()
    cur = conn.execute(
        "INSERT INTO runs (started_at) VALUES (?)", (now,)
    )
    run_id = cur.lastrowid
    seen_keys: list[str] = []
    new_count = 0
    for L in listings:
        row = _listing_to_row(L)
        existing = conn.execute(
            "SELECT key FROM listings WHERE key = ?", (L.key,)
        ).fetchone()
        if existing:
            # Coalesce: only overwrite each column when the new scrape has a
            # non-null value. Protects manually-corrected fields (e.g. an
            # ingested landlord text that filled in beds/baths) from being
            # nulled-out by a search-result re-scrape that doesn't carry
            # per-unit detail.
            non_key_cols = [c for c in COLUMNS if c != "key"]
            # Address is special — if user manually verified an address via
            # the CRM ingest path (address_verified=1), never overwrite it
            # with the next scrape's less-precise data.
            sets = []
            for c in non_key_cols:
                if c == "address":
                    sets.append(
                        f"address = CASE WHEN address_verified = 1 THEN address "
                        f"ELSE COALESCE(:address, address) END"
                    )
                else:
                    sets.append(f"{c} = COALESCE(:{c}, {c})")
            conn.execute(
                f"UPDATE listings SET {', '.join(sets)}, last_seen = :last_seen, active = 1 WHERE key = :key",
                {**row, "last_seen": now},
            )
        else:
            new_count += 1
            insert_cols = COLUMNS + ["first_seen", "last_seen", "active"]
            placeholders = ", ".join(f":{c}" for c in insert_cols)
            conn.execute(
                f"INSERT INTO listings ({', '.join(insert_cols)}) "
                f"VALUES ({placeholders})",
                {**row, "first_seen": now, "last_seen": now, "active": 1},
            )
        seen_keys.append(L.key)
    # Only deactivate within sources that actually returned data this run.
    # Never deactivate listings with active conversations or a non-empty
    # funnel status — those represent real, ongoing work that matters even
    # if the search result has rotated off page 1.
    if seen_keys and succeeded_sources:
        placeholders = ",".join("?" * len(seen_keys))
        src_placeholders = ",".join("?" * len(succeeded_sources))
        conn.execute(
            f"UPDATE listings SET active = 0 "
            f"WHERE source IN ({src_placeholders}) "
            f"  AND key NOT IN ({placeholders}) "
            f"  AND key NOT IN (SELECT DISTINCT listing_key FROM interactions WHERE listing_key IS NOT NULL) "
            f"  AND key NOT IN (SELECT listing_key FROM listing_status)",
            (*succeeded_sources, *seen_keys),
        )
    conn.execute(
        "UPDATE runs SET finished_at = ?, total_seen = ?, new_count = ? WHERE id = ?",
        (datetime.utcnow(), len(listings), new_count, run_id),
    )
    return run_id, new_count


def active_listings(conn) -> list[Listing]:
    rows = conn.execute(
        "SELECT * FROM listings WHERE active = 1 ORDER BY price"
    ).fetchall()
    return [_row_to_listing(r) for r in rows]


def latest_run(conn) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM runs ORDER BY id DESC LIMIT 1"
    ).fetchone()


def interactions_for(conn: sqlite3.Connection, listing_key: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM interactions WHERE listing_key=? ORDER BY ts",
        (listing_key,),
    ).fetchall()


def attachments_for(conn: sqlite3.Connection, listing_key: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM attachments WHERE listing_key=? ORDER BY ts",
        (listing_key,),
    ).fetchall()


def attach(
    conn: sqlite3.Connection, filename: str, *, listing_key: str | None = None,
    interaction_id: int | None = None, kind: str = "other", caption: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO attachments (listing_key, interaction_id, filename, kind, caption) "
        "VALUES (?, ?, ?, ?, ?)",
        (listing_key, interaction_id, filename, kind, caption),
    )
    conn.commit()
    return cur.lastrowid


def conversation_state(conn: sqlite3.Connection, listing_key: str) -> dict | None:
    """Summarize the active-conversation state for a listing.

    Returns None if there are no interactions. Otherwise a dict with:
      status, status_note, viewing_at, last_ts, last_direction, last_sender,
      last_summary, interaction_count, next_action (heuristic).
    """
    s = status_for(conn, listing_key)
    rows = interactions_for(conn, listing_key)
    if not rows and not s:
        return None
    # `last` = chronologically latest (for timestamp, ball-in-court arrow).
    last = rows[-1] if rows else None
    # `headline` = latest INBOUND if any; that's what the user needs to act on.
    # We already know what we said.
    inbound = [r for r in rows if r["direction"] == "in"]
    headline = inbound[-1] if inbound else last
    headline_summary = None
    if headline and headline["extracted_json"]:
        import json as _json
        try:
            headline_summary = _json.loads(headline["extracted_json"]).get("summary")
        except Exception:
            pass
    # Heuristic next-action: status wins; otherwise who's holding the ball.
    next_action = None
    status_val = s["status"] if s else None
    if status_val == "viewing_scheduled" and s and s["viewing_at"]:
        from datetime import datetime as _dt
        try:
            dt = _dt.fromisoformat(s["viewing_at"])
            next_action = f"Attend viewing · {dt.strftime('%a %b %-d · %-I:%M %p')}"
        except Exception:
            next_action = "Attend viewing"
    elif status_val == "viewing_done":
        next_action = "Decide: apply or pass"
    elif status_val == "applied":
        next_action = "Awaiting landlord decision"
    elif last:
        if last["direction"] == "in":
            next_action = "Reply"
        elif last["direction"] == "out":
            next_action = "Awaiting their reply"
    # We're awaiting their reply if the chronologically-last message was outbound.
    awaiting_their_reply = bool(last and last["direction"] == "out")
    return {
        "status": s["status"] if s else None,
        "status_note": s["status_note"] if s else None,
        "viewing_at": s["viewing_at"] if s else None,
        "last_ts": last["ts"] if last else None,
        # Headline = latest INBOUND message — what we need to act on.
        # The chronologically-last outbound (if any) is summarized via
        # awaiting_their_reply rather than shown as the headline.
        "last_direction": headline["direction"] if headline else None,
        "last_sender": headline["sender"] if headline else None,
        "last_summary": headline_summary,
        "awaiting_their_reply": awaiting_their_reply,
        "interaction_count": len(rows),
        "next_action": next_action,
    }


def status_for(conn: sqlite3.Connection, listing_key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM listing_status WHERE listing_key=?", (listing_key,)
    ).fetchone()


def set_status(
    conn: sqlite3.Connection,
    listing_key: str,
    status: str,
    note: str | None = None,
    viewing_at: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO listing_status (listing_key, status, status_note, viewing_at, updated_at)
           VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(listing_key) DO UPDATE SET
             status=excluded.status,
             status_note=COALESCE(excluded.status_note, listing_status.status_note),
             viewing_at=COALESCE(excluded.viewing_at, listing_status.viewing_at),
             updated_at=CURRENT_TIMESTAMP""",
        (listing_key, status, note, viewing_at),
    )
    conn.commit()
