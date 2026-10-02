"""
Scrapes land listings from aps.com.kh into listings.db, alongside the
Telegram ones. Same downstream pipeline (sync.py -> publish_static.py).

APS pages carry an "Open in google map" link, but it is maps.google.com/
?q=<lon>,<lat> -- longitude first, rounded to 2 decimals (~1 km) -- so it
is NOT a real pin (source_maps_link stays empty). It IS a per-listing
coordinate though, so after un-swapping and checking it falls inside the
listing's province it becomes a blue (approximate) pin (approx_coords).
Failing that, a title naming BOTH a commune (khum) and district (srok)
is geocoded the same way as a Telegram post's sangkat + khan; anything
else is kept but gets no pin (see db._PUBLISH_GATE_SQL).

Rules applied: land only; updated within --days (default 183 = ~6 months);
only the target provinces below; one row per property (newest "Updated"
wins among apparent duplicates).

Usage:
    python3 scrape_aps.py             # dry run: parse + summarize, write nothing
    python3 scrape_aps.py --commit    # insert into listings.db, download photos
    python3 scrape_aps.py --days 90 --max-pages 2
"""

import argparse
import html
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import db
import extract
import geocode

BASE = "https://aps.com.kh"
CATEGORY = BASE + "/property/record_type_land/"
SOURCE_NAME = "aps.com.kh"
USER_AGENT = "cambodia-listings-tracker/1.0 (personal/family project)"
DELAY_SECONDS = 1.0
PHOTOS_DIR = Path(__file__).parent / "photos"

PROVINCES = {
    "phnom penh": "Phnom Penh", "kandal": "Kandal", "takeo": "Takeo",
    "kampong speu": "Kampong Speu", "kampot": "Kampot", "kep": "Kep",
    "sihanoukville": "Sihanoukville", "preah sihanouk": "Sihanoukville",
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
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

_last_request = 0.0


def fetch(url):
    global _last_request
    wait = DELAY_SECONDS - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.monotonic()
    return requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)


def listing_urls(max_pages):
    urls = {}
    for page in range(1, max_pages + 1):
        url = CATEGORY if page == 1 else f"{CATEGORY}page/{page}/"
        resp = fetch(url)
        if resp.status_code != 200:
            break
        found = re.findall(r"https://aps\.com\.kh/properties/[^\"'\s<>]+/", resp.text)
        new = [u for u in dict.fromkeys(found) if u not in urls]
        if not new:
            break
        for u in new:
            urls[u] = page
        print(f"  category page {page}: {len(new)} listings")
    return list(urls)


def visible_text(page_html):
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", page_html, flags=re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def parse_date(text):
    m = re.search(r"Updated:\s*(\d{1,2})\s+([A-Za-z]{3})\w*\s+(\d{4})", text)
    if not m or m.group(2).lower() not in MONTHS:
        return None
    return datetime(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1)), tzinfo=timezone.utc)


def parse_size(headline):
    m = re.search(r"([\d,]+(?:\.\d+)?)\s*(hectares?|ha\b|sq\.?\s*m|sqm)", headline, re.I)
    if not m:
        return None
    unit = "ha" if m.group(2).lower().startswith("h") else "sqm"
    return f"{m.group(1)} {unit}"


def canonical_province(raw):
    low = raw.lower()
    for key, canonical in PROVINCES.items():
        if key in low:
            return canonical
    return None


def parse_site_coordinate(page_html):
    """APS's "Open in google map" link is maps.google.com/?q=<lon>,<lat>
    -- longitude first, 2 decimals (~1 km) -- so Google can't use it as
    written. Returns (lat, lon) un-swapped, or None."""
    m = re.search(r"maps\.google\.com/\?q=(-?\d+\.\d+),(-?\d+\.\d+)", page_html)
    if not m:
        return None
    a, b = float(m.group(1)), float(m.group(2))
    if 102 <= a <= 108 and 9 <= b <= 15:
        return b, a
    if 9 <= a <= 15 and 102 <= b <= 108:
        return a, b
    return None


def distance_km(lat1, lon1, lat2, lon2):
    from math import asin, cos, radians, sin, sqrt
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    h = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 12742 * asin(sqrt(h))


def coordinate_plausible(coord, province):
    if not coord or province not in PROVINCE_CENTERS:
        return False
    clat, clon, radius = PROVINCE_CENTERS[province]
    return distance_km(coord[0], coord[1], clat, clon) <= radius


def main_gallery_photo(page_html):
    """APS serves photos from an S3 bucket, one folder per property. The
    page's own gallery is the folder with the most images; each related-
    property card at the bottom only contributes one from its own folder.
    Ties go to whichever appears first (the main content comes first)."""
    found = list(dict.fromkeys(re.findall(
        r"https://cbrekh\.s3\.amazonaws\.com/([^/\"'\s]+)/([^\"'\s<>]+\.(?:jpe?g|png|webp))", page_html)))
    if not found:
        return None
    counts, first = {}, {}
    for i, (folder, _) in enumerate(found):
        counts[folder] = counts.get(folder, 0) + 1
        first.setdefault(folder, i)
    best = max(counts, key=lambda f: (counts[f], -first[f]))
    name = next(n for f, n in found if f == best)
    return f"https://cbrekh.s3.amazonaws.com/{best}/{name}"


def parse_listing(url, page_html):
    title_m = re.search(r"<title>(.*?)</title>", page_html, re.S)
    if not title_m:
        return None
    title = html.unescape(title_m.group(1)).strip()
    post_id = re.search(r"postid-(\d+)", page_html)
    if not post_id:
        return None
    title = re.sub(r"\s*-\s*APS Cambodia\s*\d*\s*$", "", title)
    segments = [s.strip() for s in title.split("|")]
    headline, rest = segments[0], segments[1:]
    if not rest or "land" not in headline.lower():
        return None
    province = canonical_province(rest[-1])
    places = [p for p in rest[:-1] if p and p.lower() != (province or "").lower()]

    text = visible_text(page_html)
    updated = parse_date(text)
    price_m = re.search(r"Price:\s*([\d,]+(?:\.\d+)?)\s*USD(/sqm)?", text)
    price_value = float(price_m.group(1).replace(",", "")) if price_m else None
    price_raw = price_m.group(0) if price_m else None

    desc = ""
    start = text.find(headline[:30])
    end = text.find("Related Properties")
    if start != -1:
        desc = text[start:end if end > start else start + 1500]
    desc = desc[:1500]
    raw_text = f"{headline} | {', '.join(places + [province or ''])}".strip(" ,|")
    if price_raw:
        raw_text += f"\n{price_raw}"  # price_raw already starts with "Price:"
    raw_text += f"\n{desc}"

    commune = places[0] if places else None
    district = places[1] if len(places) >= 2 else None
    phone = extract.PHONE_RE.search(text)
    site_coord = parse_site_coordinate(page_html)

    return {
        "post_id": int(post_id.group(1)), "url": url, "headline": headline,
        "province": province, "commune": commune, "district": district,
        "updated": updated, "price_value": price_value, "price_raw": price_raw,
        "size_text": parse_size(headline), "raw_text": raw_text,
        "photo_url": main_gallery_photo(page_html),
        "site_coord": site_coord,
        "site_coord_ok": coordinate_plausible(site_coord, province),
        "contact": phone.group(0) if phone else None,
        "listing_kind": extract.extract_listing_kind(headline),
    }


def dedupe_keep_newest(items):
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


def download_photo(it):
    if not it["photo_url"]:
        return []
    PHOTOS_DIR.mkdir(exist_ok=True)
    name = f"{SOURCE_NAME}_{it['post_id']}.jpg"
    dest = PHOTOS_DIR / name
    if dest.exists():
        return [name]
    try:
        resp = fetch(it["photo_url"])
        if resp.status_code == 200 and resp.content:
            dest.write_bytes(resp.content)
            return [name]
    except requests.RequestException as e:
        print(f"  (photo failed for {it['post_id']}: {e})")
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="write to listings.db (default: dry run)")
    ap.add_argument("--days", type=int, default=183)
    ap.add_argument("--max-pages", type=int, default=30)
    args = ap.parse_args()

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    print(f"APS land listings updated since {cutoff.date()} (last {args.days} days)")
    urls = listing_urls(args.max_pages)
    print(f"{len(urls)} listing pages found; fetching each...")

    parsed, skipped = [], {"unparsed": 0, "too old": 0, "other province": 0}
    unparsed_urls = []
    for i, url in enumerate(urls, 1):
        resp = fetch(url)
        it = parse_listing(url, resp.text) if resp.status_code == 200 else None
        if not it:
            skipped["unparsed"] += 1
            unparsed_urls.append(f"{resp.status_code} {url}")
        elif not it["updated"] or it["updated"] < cutoff:
            skipped["too old"] += 1
        elif not it["province"]:
            skipped["other province"] += 1
        else:
            parsed.append(it)

    kept = dedupe_keep_newest(parsed)
    both = [it for it in kept if it["commune"] and it["district"]]
    print(f"\nparsed {len(parsed)} in scope, {len(parsed) - len(kept)} duplicate(s) dropped -> {len(kept)} listings")
    print(f"skipped: {skipped}")
    for u in unparsed_urls:
        print(f"  unparsed: {u}")
    site_ok = [it for it in kept if it["site_coord_ok"]]
    rejected = [it for it in kept if it["site_coord"] and not it["site_coord_ok"]]
    pinned = [it for it in kept if it["site_coord_ok"] or (it["commune"] and it["district"])]
    print(f"with a usable site coordinate (blue pin, ~1 km): {len(site_ok)}")
    print(f"site coordinate rejected as outside its province: {len(rejected)}")
    for it in rejected:
        print(f"  rejected: {it['headline'][:40]} | {it['province']} | {it['site_coord']}")
    print(f"with commune AND district (blue pin even without a coordinate): {len(both)}")
    print(f"total that will get a pin: {len(pinned)} of {len(kept)}")
    by_prov = {}
    for it in kept:
        by_prov[it["province"]] = by_prov.get(it["province"], 0) + 1
    print("by province:", dict(sorted(by_prov.items(), key=lambda kv: -kv[1])))
    for it in kept[:12]:
        print(f"  {it['updated'].date()} | {it['headline'][:45]:45} | {it['commune']} / {it['district']} / {it['province']} | {it['price_raw']}")

    if not args.commit:
        print("\n(dry run -- nothing written. Re-run with --commit to insert.)")
        return

    db.init_db()
    inserted = 0
    with db.get_conn() as conn:
        for it in kept:
            listing = {
                "source_type": "web", "source_name": SOURCE_NAME, "message_id": it["post_id"],
                "message_link": it["url"], "posted_at": it["updated"].isoformat(),
                "fetched_at": datetime.now(timezone.utc).isoformat(), "raw_text": it["raw_text"],
                "price_value": it["price_value"], "price_currency": "USD" if it["price_value"] else None,
                "price_raw": it["price_raw"], "location": it["commune"] or it["province"],
                "khan": it["district"] if (it["commune"] and it["district"]) else None,
                "sangkat": it["commune"] if (it["commune"] and it["district"]) else None,
                "property_type": "land", "bedrooms": None, "size_text": it["size_text"],
                "listing_kind": it["listing_kind"], "contact": it["contact"],
                "posted_by": "APS Cambodia", "photo_paths": download_photo(it),
                "dedup_hash": extract.make_dedup_hash(it["price_value"], it["commune"], "land", it["raw_text"]),
                "source_maps_link": None,
            }
            before = conn.total_changes
            db.insert_listing(conn, listing)
            if conn.total_changes > before:
                inserted += 1
            # Pin placement runs on every pass (not just new rows) so
            # re-running picks up rule changes. The site's own ~1 km
            # coordinate wins over a place-name geocode.
            row = conn.execute(
                "SELECT id FROM listings WHERE source_name=? AND message_id=?",
                (SOURCE_NAME, it["post_id"])).fetchone()
            if it["site_coord_ok"]:
                lat, lon = it["site_coord"]
                db.set_site_coordinate(conn, row["id"], lat, lon, geocode.maps_link(lat, lon))
            elif it["commune"] and it["district"]:
                coords = geocode_for(conn, it)
                if coords:
                    db.set_geocode_result(conn, row["id"], coords[0], coords[1], geocode.maps_link(*coords))
    print(f"\ninserted {inserted} new listing(s) ({len(kept) - inserted} already in the database)")


if __name__ == "__main__":
    main()
