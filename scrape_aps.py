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
from datetime import datetime, timedelta, timezone

import db
import extract
from webscrape import (
    canonical_province, coordinate_plausible, dedupe_keep_newest, fetch,
    has_structures, store,
)

BASE = "https://aps.com.kh"
CATEGORY = BASE + "/property/record_type_land/"
SOURCE_NAME = "aps.com.kh"
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


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
    if not rest or "land" not in headline.lower() or has_structures(headline):
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
            if store(conn, SOURCE_NAME, it, "APS Cambodia"):
                inserted += 1
    print(f"\ninserted {inserted} new listing(s) ({len(kept) - inserted} already in the database)")


if __name__ == "__main__":
    main()
