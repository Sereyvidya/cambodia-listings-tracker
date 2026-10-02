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

# A listing is worth a pin in Sheets/Telegram/Earth/the published site
# when ANY of these holds:
#  - the poster included a real Google Maps link, and it isn't a
#    duplicate/reused-reference-point (see compute_duplicate_link_exclusions), or
#  - the post named BOTH its sangkat (commune) and khan (district) --
#    per your dad, a sangkat is precise enough to trust even with no
#    map link at all, but a khan alone is too coarse, or
#  - the source website supplied its own coordinate (approx_coords = 1,
#    see scrape_aps.py) -- rounded to ~1 km, so approximate, but at
#    least as precise as a sangkat centroid.
# Every branch needs lat/lon already resolved to actually place a pin.
_PUBLISH_GATE_SQL = """(
    (source_maps_link IS NOT NULL AND is_duplicate_link = 0)
    OR (khan IS NOT NULL AND sangkat IS NOT NULL)
    OR approx_coords = 1
) AND lat IS NOT NULL"""

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
    location TEXT,                      -- general display value: sangkat if known, else khan, else an informal area name
    khan TEXT,                          -- district, coarser than a sangkat
    sangkat TEXT,                       -- commune -- precise enough to pin even with no real map link, if paired with a khan
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
    is_duplicate_link INTEGER NOT NULL DEFAULT 0,  -- see db.compute_duplicate_link_exclusions()
    approx_coords INTEGER NOT NULL DEFAULT 0,      -- lat/lon came from the source site, rounded to ~1 km
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

-- One row per channel once its first backfill has completed, so
-- listener.py knows to use the long first-time window only once per
-- channel and the short ongoing window every run after that (see
-- listener.py:backfill).
CREATE TABLE IF NOT EXISTS backfilled_channels (
    source_name TEXT PRIMARY KEY,
    first_backfilled_at TEXT NOT NULL
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
    "is_duplicate_link": "INTEGER NOT NULL DEFAULT 0",
    "approx_coords": "INTEGER NOT NULL DEFAULT 0",
    "khan": "TEXT",
    "sangkat": "TEXT",
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
            price_raw, location, khan, sangkat, property_type, bedrooms, size_text,
            listing_kind, contact, posted_by, photo_paths, dedup_hash,
            source_maps_link
        ) VALUES (
            :source_type, :source_name, :message_id, :message_link,
            :posted_at, :fetched_at, :raw_text, :price_value, :price_currency,
            :price_raw, :location, :khan, :sangkat, :property_type, :bedrooms, :size_text,
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
    has_pin=None,
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
        clauses.append("source_maps_link IS NOT NULL AND is_duplicate_link = 0")
    elif has_source_map == "no":
        clauses.append("(source_maps_link IS NULL OR is_duplicate_link = 1)")
    if has_pin == "yes":
        clauses.append(_PUBLISH_GATE_SQL)
    elif has_pin == "no":
        clauses.append(f"NOT ({_PUBLISH_GATE_SQL})")

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT * FROM listings
        {where}
        ORDER BY COALESCE(posted_at, fetched_at) DESC
        LIMIT :limit
    """
    params["limit"] = limit
    return conn.execute(sql, params).fetchall()


def pin_info(row):
    """Given a listings row (qualifying per _PUBLISH_GATE_SQL or not),
    returns (tier, display_link):
      - "real_link": the poster's own Google Maps link -- precise.
      - "sangkat_khan": no real link, but approximate -- either the post
        named both its sangkat and khan (geocoded from that), or the
        source website gave its own ~1 km coordinate. This tier is the
        blue pin; see pin_basis() for which of the two it was.
      - None: doesn't qualify for a pin at all.
    display_link is whichever link is appropriate to show for that tier
    (None for the non-qualifying case)."""
    if row["source_maps_link"] and not row["is_duplicate_link"] and row["lat"] is not None:
        return "real_link", row["source_maps_link"]
    if (row["approx_coords"] or (row["khan"] and row["sangkat"])) and row["lat"] is not None:
        return "sangkat_khan", row["maps_link"]
    return None, None


def pin_basis(row):
    """How the pin was derived: "real_link", "site_coordinate" (the source
    website's own ~1 km coordinate), "sangkat_khan", or None. Used only to
    word the on-screen precision labels honestly; color/filtering use the
    coarser tier from pin_info()."""
    tier, _ = pin_info(row)
    if tier != "sangkat_khan":
        return tier
    return "site_coordinate" if row["approx_coords"] else "sangkat_khan"


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


def has_backfilled_before(conn, source_name):
    return conn.execute(
        "SELECT 1 FROM backfilled_channels WHERE source_name = ?", (source_name,)
    ).fetchone() is not None


def mark_backfilled(conn, source_name):
    """Records that source_name's first backfill has completed. Called
    after a channel's scan finishes (not before), so a run that dies
    partway through still gets the full first-time window on retry
    instead of being silently downgraded to the short ongoing one."""
    conn.execute(
        "INSERT OR IGNORE INTO backfilled_channels (source_name, first_backfilled_at) VALUES (?, ?)",
        (source_name, datetime.now(timezone.utc).isoformat()),
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


def set_site_coordinate(conn, listing_id, lat, lon, maps_link):
    """Records a coordinate the source website itself supplied (~1 km
    precision). Overrides any earlier place-name geocode for this row."""
    conn.execute(
        """UPDATE listings SET lat = ?, lon = ?, maps_link = ?, geocoded_at = ?, approx_coords = 1
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


def compute_duplicate_link_exclusions(conn):
    """Flags listings whose source_maps_link is shared with other visible
    listings, so Sheets/Telegram/Earth/the published site only ever show
    a trustworthy, one-per-property set of real map links. Two distinct
    patterns show up in practice, and they need opposite handling:

    - The SAME unit reposted over time, or cross-posted to another
      channel, with the same price each time -- a genuine duplicate.
      Keep only the most recently posted copy, flag the rest.
    - The SAME link reused across visibly DIFFERENT properties (very
      different prices under one link) -- some channels paste one fixed
      reference point (e.g. their office) into every post instead of a
      real per-listing pin. None of these are trustworthy as "this pin is
      where THIS property is", so the whole group gets flagged, not just
      the extras.

    Recomputed from scratch each call (it's cheap, and re-running sync.py
    after new backfills needs a full recheck, not an incremental one)."""
    conn.execute("UPDATE listings SET is_duplicate_link = 0")

    dup_links = conn.execute(
        """SELECT source_maps_link FROM listings
           WHERE hidden = 0 AND source_maps_link IS NOT NULL
           GROUP BY source_maps_link HAVING COUNT(*) > 1"""
    ).fetchall()

    reposts_collapsed = 0
    reused_link_groups = 0
    for row in dup_links:
        link = row["source_maps_link"]
        members = conn.execute(
            """SELECT id, price_value, posted_at, fetched_at FROM listings
               WHERE source_maps_link = ? AND hidden = 0""",
            (link,),
        ).fetchall()
        distinct_prices = {m["price_value"] for m in members if m["price_value"] is not None}

        if len(distinct_prices) <= 1:
            # Genuine repost/cross-post: keep the newest, flag the rest.
            newest = max(members, key=lambda m: m["posted_at"] or m["fetched_at"] or "")
            exclude_ids = [m["id"] for m in members if m["id"] != newest["id"]]
            reposts_collapsed += len(exclude_ids)
        else:
            # Reused reference point across different properties: none of
            # them get to claim this pin.
            exclude_ids = [m["id"] for m in members]
            reused_link_groups += 1

        conn.executemany(
            "UPDATE listings SET is_duplicate_link = 1 WHERE id = ?",
            [(i,) for i in exclude_ids],
        )

    return reposts_collapsed, reused_link_groups


def find_synced_duplicate(conn, dedup_hash, exclude_id):
    """id of an already-Sheet-synced listing sharing this dedup_hash, if any --
    used to avoid pushing the same cross-posted unit into the sheet twice."""
    row = conn.execute(
        "SELECT id FROM listings WHERE dedup_hash = ? AND id != ? AND sheet_synced_at IS NOT NULL LIMIT 1",
        (dedup_hash, exclude_id),
    ).fetchone()
    return row["id"] if row else None


def listings_pending_sheet_sync(conn):
    """Visible, qualifying listings (see _PUBLISH_GATE_SQL) not yet pushed
    to the Sheet."""
    return conn.execute(
        f"""SELECT * FROM listings
           WHERE hidden = 0 AND {_PUBLISH_GATE_SQL} AND sheet_synced_at IS NULL
           ORDER BY id"""
    ).fetchall()


def mark_sheet_synced(conn, listing_id):
    conn.execute(
        "UPDATE listings SET sheet_synced_at = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), listing_id),
    )


def listings_pending_notify(conn):
    """Visible, qualifying listings (see _PUBLISH_GATE_SQL) not yet sent
    to the Telegram notify group."""
    return conn.execute(
        f"""SELECT * FROM listings
           WHERE hidden = 0 AND {_PUBLISH_GATE_SQL} AND notified_at IS NULL
           ORDER BY id"""
    ).fetchall()


def mark_notified(conn, listing_id):
    conn.execute(
        "UPDATE listings SET notified_at = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), listing_id),
    )


def listings_for_earth_export(conn):
    """All visible, qualifying listings (see _PUBLISH_GATE_SQL) -- the
    KMZ export is a full snapshot, regenerated each run rather than
    tracked incrementally."""
    return conn.execute(
        f"SELECT * FROM listings WHERE hidden = 0 AND {_PUBLISH_GATE_SQL} ORDER BY id"
    ).fetchall()
