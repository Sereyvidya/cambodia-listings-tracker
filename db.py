"""
SQLite storage layer for the Cambodia listings tracker.

One table, kept deliberately simple: every message that looks like a
listing gets one row. Dedup across sources is approximate (see
extract.py:dedup_hash) rather than exact, because the same unit gets
reposted with slightly different wording all the time.
"""

import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path(__file__).parent / "listings.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL DEFAULT 'telegram',
    source_name TEXT NOT NULL,          -- group/channel username or title
    message_id INTEGER NOT NULL,
    message_link TEXT,                  -- t.me/<group>/<id> when public
    posted_at TEXT,                     -- ISO 8601, from the message itself
    fetched_at TEXT NOT NULL,           -- ISO 8601, when we pulled it
    raw_text TEXT,
    price_value REAL,                   -- normalized to USD where possible
    price_currency TEXT,                -- 'USD', 'KHR', or NULL if unparsed
    price_raw TEXT,                     -- the substring we parsed it from
    location TEXT,
    property_type TEXT,                 -- apartment/condo/villa/land/house/room/office/other
    bedrooms INTEGER,
    size_text TEXT,                     -- normalized floor/plot size, e.g. '85 sqm' or '5x20m'
    listing_kind TEXT,                  -- 'sale' | 'rent' | NULL if unclear
    contact TEXT,                       -- phone/handle mentioned in the ad text itself
    posted_by TEXT,                     -- who posted the message in the group
    photo_paths TEXT,                   -- JSON list of local file paths
    dedup_hash TEXT,
    lat REAL,                           -- latitude: precise if source_maps_link resolved, else our own neighborhood-level guess
    lon REAL,                           -- longitude, same caveat
    maps_link TEXT,                     -- generic coordinate-search Maps link built from lat/lon, once we have any
    geocoded_at TEXT,                   -- when a geocode attempt was made (success or not)
    source_maps_link TEXT,              -- the REAL Google Maps link the poster themselves included, if any --
                                         -- this (not maps_link) is what "has a map pin" means to Sheets/Telegram/Earth/the published site
    source_map_resolved_at TEXT,        -- when we tried resolving source_maps_link to coordinates (success or fail)
    sheet_synced_at TEXT,               -- when this row was pushed to Google Sheets
    notified_at TEXT,                   -- when a Telegram notification was sent for this row
    hidden INTEGER NOT NULL DEFAULT 0,  -- lets the dashboard let a user hide junk
    UNIQUE(source_name, message_id)
);

CREATE INDEX IF NOT EXISTS idx_listings_posted_at ON listings(posted_at);
CREATE INDEX IF NOT EXISTS idx_listings_dedup_hash ON listings(dedup_hash);
CREATE INDEX IF NOT EXISTS idx_listings_location ON listings(location);

-- One row per distinct location string we've tried to geocode, so the
-- free geocoding service only ever sees each neighborhood name once.
CREATE TABLE IF NOT EXISTS geocode_cache (
    location TEXT PRIMARY KEY,
    lat REAL,                           -- NULL means "looked up, unresolvable"
    lon REAL,
    resolved_at TEXT NOT NULL
);
"""

# Columns added after the table's first release. init_db() adds any that
# are missing from an existing listings.db so upgrades don't lose data.
_MIGRATED_COLUMNS = {
    "size_text": "TEXT",
    "posted_by": "TEXT",
    "lat": "REAL",
    "lon": "REAL",
    "maps_link": "TEXT",
    "geocoded_at": "TEXT",
    "sheet_synced_at": "TEXT",
    "notified_at": "TEXT",
    "source_maps_link": "TEXT",
    "source_map_resolved_at": "TEXT",
}


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        existing = {row[1] for row in conn.execute("PRAGMA table_info(listings)")}
        for name, coltype in _MIGRATED_COLUMNS.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE listings ADD COLUMN {name} {coltype}")


def insert_listing(conn, listing: dict):
    """Insert one listing dict. Silently ignores exact duplicates
    (same source_name + message_id) via INSERT OR IGNORE."""
    photo_paths = json.dumps(listing.get("photo_paths") or [])
    conn.execute(
        """
        INSERT OR IGNORE INTO listings (
            source_type, source_name, message_id, message_link,
            posted_at, fetched_at, raw_text, price_value, price_currency,
            price_raw, location, property_type, bedrooms, size_text,
            listing_kind, contact, posted_by, photo_paths, dedup_hash,
            source_maps_link
        ) VALUES (
            :source_type, :source_name, :message_id, :message_link,
            :posted_at, :fetched_at, :raw_text, :price_value, :price_currency,
            :price_raw, :location, :property_type, :bedrooms, :size_text,
            :listing_kind, :contact, :posted_by, :photo_paths, :dedup_hash,
            :source_maps_link
        )
        """,
        {**listing, "photo_paths": photo_paths},
    )


def query_listings(
    conn,
    min_price=None,
    max_price=None,
    location=None,
    property_type=None,
    listing_kind=None,
    source_name=None,
    search_text=None,
    has_map=None,
    has_source_map=None,
    include_hidden=False,
    limit=500,
):
    clauses = []
    params = {}
    if not include_hidden:
        clauses.append("hidden = 0")
    if min_price is not None:
        clauses.append("price_value >= :min_price")
        params["min_price"] = min_price
    if max_price is not None:
        clauses.append("price_value <= :max_price")
        params["max_price"] = max_price
    if location:
        clauses.append("location LIKE :location")
        params["location"] = f"%{location}%"
    if property_type:
        clauses.append("property_type = :property_type")
        params["property_type"] = property_type
    if listing_kind:
        clauses.append("listing_kind = :listing_kind")
        params["listing_kind"] = listing_kind
    if source_name:
        clauses.append("source_name = :source_name")
        params["source_name"] = source_name
    if search_text:
        clauses.append("raw_text LIKE :search_text")
        params["search_text"] = f"%{search_text}%"
    if has_map == "yes":
        clauses.append("maps_link IS NOT NULL")
    elif has_map == "no":
        clauses.append("maps_link IS NULL")
    if has_source_map == "yes":
        clauses.append("source_maps_link IS NOT NULL")
    elif has_source_map == "no":
        clauses.append("source_maps_link IS NULL")

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT * FROM listings
        {where}
        ORDER BY COALESCE(posted_at, fetched_at) DESC
        LIMIT :limit
    """
    params["limit"] = limit
    return conn.execute(sql, params).fetchall()


def distinct_values(conn, column):
    assert column in ("location", "property_type", "source_name", "listing_kind")
    rows = conn.execute(
        f"SELECT DISTINCT {column} FROM listings WHERE {column} IS NOT NULL ORDER BY {column}"
    ).fetchall()
    return [r[0] for r in rows]


# --- geocode cache -----------------------------------------------------

def get_cached_geocode(conn, location):
    """(lat, lon) if this location string has been looked up before (lat/lon
    are None if it was tried and came back unresolvable); None if it has
    never been looked up at all."""
    row = conn.execute(
        "SELECT lat, lon FROM geocode_cache WHERE location = ?", (location,)
    ).fetchone()
    return None if row is None else (row["lat"], row["lon"])


def set_cached_geocode(conn, location, lat, lon):
    conn.execute(
        "INSERT OR REPLACE INTO geocode_cache (location, lat, lon, resolved_at) VALUES (?, ?, ?, ?)",
        (location, lat, lon, datetime.now(timezone.utc).isoformat()),
    )


# --- sync pipeline (geocoding / Sheets / Telegram notify) --------------

def listings_needing_geocode(conn):
    """Visible listings with a location we haven't tried to geocode yet."""
    return conn.execute(
        "SELECT * FROM listings WHERE hidden = 0 AND location IS NOT NULL AND geocoded_at IS NULL"
    ).fetchall()


def set_geocode_result(conn, listing_id, lat, lon, maps_link):
    conn.execute(
        """UPDATE listings SET lat = ?, lon = ?, maps_link = ?, geocoded_at = ?
           WHERE id = ?""",
        (lat, lon, maps_link, datetime.now(timezone.utc).isoformat(), listing_id),
    )


def listings_needing_source_map_resolve(conn):
    """Visible listings with a real poster-provided map link we haven't
    tried resolving to coordinates yet (see geocode.resolve_source_map_link)."""
    return conn.execute(
        """SELECT * FROM listings
           WHERE hidden = 0 AND source_maps_link IS NOT NULL AND source_map_resolved_at IS NULL"""
    ).fetchall()


def mark_source_map_resolved(conn, listing_id):
    conn.execute(
        "UPDATE listings SET source_map_resolved_at = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), listing_id),
    )


def find_synced_duplicate(conn, dedup_hash, exclude_id):
    """id of an already-Sheet-synced listing sharing this dedup_hash, if any --
    used to avoid pushing the same cross-posted unit into the sheet twice."""
    row = conn.execute(
        "SELECT id FROM listings WHERE dedup_hash = ? AND id != ? AND sheet_synced_at IS NOT NULL LIMIT 1",
        (dedup_hash, exclude_id),
    ).fetchone()
    return row["id"] if row else None


def listings_pending_sheet_sync(conn):
    """Visible listings with a REAL poster-provided map link, not yet
    pushed to the Sheet -- dad only wants listings the poster themselves
    pinned a location for, not our own neighborhood-level guess."""
    return conn.execute(
        """SELECT * FROM listings
           WHERE hidden = 0 AND source_maps_link IS NOT NULL AND sheet_synced_at IS NULL
           ORDER BY id"""
    ).fetchall()


def mark_sheet_synced(conn, listing_id):
    conn.execute(
        "UPDATE listings SET sheet_synced_at = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), listing_id),
    )


def listings_pending_notify(conn):
    """Visible listings with a real poster-provided map link, not yet sent
    to the Telegram notify group."""
    return conn.execute(
        """SELECT * FROM listings
           WHERE hidden = 0 AND source_maps_link IS NOT NULL AND notified_at IS NULL
           ORDER BY id"""
    ).fetchall()


def mark_notified(conn, listing_id):
    conn.execute(
        "UPDATE listings SET notified_at = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), listing_id),
    )


def listings_for_earth_export(conn):
    """All visible listings with a real poster-provided map link that's
    been resolved to coordinates -- the KMZ export is a full snapshot,
    regenerated each run rather than tracked incrementally. (Requires
    lat/lon too, not just source_maps_link, since sync.py resolves those
    in a separate step -- a listing can briefly have one without the
    other between runs.)"""
    return conn.execute(
        """SELECT * FROM listings
           WHERE hidden = 0 AND source_maps_link IS NOT NULL AND lat IS NOT NULL
           ORDER BY id"""
    ).fetchall()
