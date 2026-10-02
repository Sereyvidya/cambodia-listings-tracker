"""
Pieces shared by the per-website scrapers (scrape_aps.py, scrape_pointer.py,
scrape_ips.py): polite fetching, province matching, coordinate sanity
checks, photo download, duplicate collapsing, and one store() that turns a
parsed listing into a listings.db row and places its pin.

A parsed listing is a dict with: post_id, url, headline, province,
commune, district, updated (aware datetime), price_value, price_raw,
size_text, raw_text, photo_url, contact, listing_kind, site_coord
((lat, lon) or None), site_coord_ok (bool) -- and optionally
property_type, bedrooms.
"""

import re
import time
from datetime import datetime, timezone
from math import asin, cos, radians, sin, sqrt
from pathlib import Path

import requests

import db
import extract
import geocode

USER_AGENT = "cambodia-listings-tracker/1.0 (personal/family project)"
DELAY_SECONDS = 1.0
PHOTOS_DIR = Path(__file__).parent / "photos"

# The target provinces for land listings, keyed by lowercase substrings.
PROVINCES = {
    "phnom penh": "Phnom Penh", "kandal": "Kandal", "takeo": "Takeo",
    "kampong speu": "Kampong Speu", "kampot": "Kampot", "kep": "Kep",
    "sihanoukville": "Sihanoukville", "preah sihanouk": "Sihanoukville",
    "sihanouk": "Sihanoukville",
    "kampong chhnang": "Kampong Chhnang", "kampong chnang": "Kampong Chhnang",
    "siem reap": "Siem Reap", "mondulkiri": "Mondulkiri", "mondul kiri": "Mondulkiri",
}
# Rough province centers (lat, lon) and the farthest a listing can plausibly
# be from them, in km. Only used to reject site coordinates that are
# obviously wrong -- one APS "Kep" listing, for instance, carries a
# coordinate ~130 km away near Kien Svay in Kandal.
PROVINCE_CENTERS = {
    "Phnom Penh": (11.56, 104.92, 25), "Kandal": (11.30, 105.00, 60),
    "Takeo": (10.99, 104.78, 55), "Kampong Speu": (11.45, 104.52, 85),
    "Kampot": (10.62, 104.18, 55), "Kep": (10.48, 104.32, 20),
    "Sihanoukville": (10.75, 103.75, 55), "Kampong Chhnang": (12.25, 104.67, 85),
    "Siem Reap": (13.40, 104.00, 110), "Mondulkiri": (12.45, 107.20, 110),
}

_last_request = 0.0


def fetch(url):
    """GET with a descriptive User-Agent, at most one request per second
    across every scraper in this process."""
    global _last_request
    wait = DELAY_SECONDS - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.monotonic()
    return requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)


# Titles that say there's a building on the land ("Land and Building",
# "Land with House and Warehouse", "Land and Warehouse"). Dad asked for
# plain land, so these are excluded; "Land with Title" and the like are not.
_STRUCTURE = r"(?:house|houses|building|buildings|warehouse|factory|villa|shophouse)"
HAS_STRUCTURES = re.compile(
    rf"\bland\s+(?:and|&)\s+(?:\d[\d,.]*\s*(?:sq\s?m|sqm|m2)\s+)?{_STRUCTURE}\b"
    rf"|\bwith\s+(?:\d+\s+|a\s+|an\s+)?(?:\w+\s+)?{_STRUCTURE}\b", re.I)


def has_structures(title):
    return bool(HAS_STRUCTURES.search(title or ""))


def canonical_province(raw):
    low = (raw or "").lower()
    for key, canonical in PROVINCES.items():
        if key in low:
            return canonical
    return None


def distance_km(lat1, lon1, lat2, lon2):
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    h = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 12742 * asin(sqrt(h))


def coordinate_plausible(coord, province):
    if not coord or province not in PROVINCE_CENTERS:
        return False
    clat, clon, radius = PROVINCE_CENTERS[province]
    return distance_km(coord[0], coord[1], clat, clon) <= radius


def dedupe_keep_newest(items):
    """One row per apparent property: same price, size, commune and
    province is treated as a repost; the newest "updated" wins."""
    best = {}
    for it in items:
        key = (it["price_value"], it["size_text"], it["commune"], it["province"])
        if key not in best or (it["updated"] and (not best[key]["updated"] or it["updated"] > best[key]["updated"])):
            best[key] = it
    return list(best.values())


def geocode_for(conn, it):
    """Commune + district + province, falling back to commune + province.
    "X Island" is also tried as "Koh X" -- Nominatim knows the island under
    its Khmer-derived name ("Koh Norea") but not the English one."""
    queries = [", ".join(p for p in (it["commune"], it["district"], it["province"]) if p)]
    if it["commune"] and it["commune"].lower().endswith(" island"):
        koh = "Koh " + it["commune"][: -len(" island")]
        queries += [", ".join(p for p in (koh, it["province"]) if p)]
    queries.append(", ".join(p for p in (it["commune"], it["province"]) if p))
    for query in queries:
        cached = db.get_cached_geocode(conn, query)
        if cached is None:
            coords = geocode.geocode(query)
            db.set_cached_geocode(conn, query, *(coords if coords else (None, None)))
        else:
            coords = cached if cached[0] is not None else None
        if coords:
            return coords
    return None


def download_photo(source_name, post_id, photo_url):
    if not photo_url:
        return []
    PHOTOS_DIR.mkdir(exist_ok=True)
    name = f"{source_name}_{post_id}.jpg"
    dest = PHOTOS_DIR / name
    if dest.exists():
        return [name]
    try:
        resp = fetch(photo_url)
        if resp.status_code == 200 and resp.content:
            dest.write_bytes(resp.content)
            return [name]
    except requests.RequestException as e:
        print(f"  (photo failed for {post_id}: {e})")
    return []


def store(conn, source_name, it, posted_by):
    """Insert one parsed listing (ignored if already stored), then place its
    pin. Pin placement runs on every pass, not just for new rows, so
    re-running picks up rule changes. A coordinate the site supplied
    (blurred or rounded by the site, so an approximate blue pin) wins over
    a place-name geocode. Returns True if the row was newly inserted."""
    has_both = bool(it["commune"] and it["district"])
    listing = {
        "source_type": "web", "source_name": source_name, "message_id": it["post_id"],
        "message_link": it["url"], "posted_at": it["updated"].isoformat(),
        "fetched_at": datetime.now(timezone.utc).isoformat(), "raw_text": it["raw_text"],
        "price_value": it["price_value"], "price_currency": "USD" if it["price_value"] else None,
        "price_raw": it["price_raw"], "location": it["commune"] or it["province"],
        "khan": it["district"] if has_both else None,
        "sangkat": it["commune"] if has_both else None,
        "property_type": it.get("property_type", "land"), "bedrooms": it.get("bedrooms"),
        "size_text": it["size_text"], "listing_kind": it["listing_kind"], "contact": it["contact"],
        "posted_by": posted_by, "photo_paths": download_photo(source_name, it["post_id"], it["photo_url"]),
        "dedup_hash": extract.make_dedup_hash(it["price_value"], it["commune"], "land", it["raw_text"]),
        "source_maps_link": None,
    }
    before = conn.total_changes
    db.insert_listing(conn, listing)
    inserted = conn.total_changes > before

    row = conn.execute(
        "SELECT id FROM listings WHERE source_name=? AND message_id=?",
        (source_name, it["post_id"])).fetchone()
    if it["site_coord_ok"]:
        lat, lon = it["site_coord"]
        db.set_site_coordinate(conn, row["id"], lat, lon, geocode.maps_link(lat, lon))
    elif has_both:
        coords = geocode_for(conn, it)
        if coords:
            db.set_geocode_result(conn, row["id"], coords[0], coords[1], geocode.maps_link(*coords))
    return inserted
