"""
Scrapes land listings from ips-cambodia.com into listings.db, alongside the
Telegram ones.

Listing URLs come from the site's sitemaps (its robots.txt disallows the
?page= / ?listing_page= pagination, which this never uses), narrowed to the
target provinces by sitemap name and to land by URL slug. Each page gives
a real datePublished/dateModified in its JSON-LD, the commune/district/
province, a price, and a per-listing coordinate that the site renders as a
"property radius map" -- so it is treated as approximate (a blue pin) once
checked against the listing's province, like APS and Pointer.

One catch: most IPS listings carry the same bulk datePublished (a site
migration), so their real age is unknowable. By default those are NOT
imported (the "latest posts" rule can't be verified for them); pass
--include-undated to import them anyway. A date shared by --bulk-threshold
or more fetched listings (default 25) counts as a bulk date.

Rules applied: land only; updated within --days (default 183 = ~6 months);
only the target provinces; one row per property (newest date wins among
apparent duplicates). Re-runs skip pages already stored unless the
sitemap's lastmod is newer than when they were fetched.

Usage:
    python3 scrape_ips.py                    # dry run: parse + summarize
    python3 scrape_ips.py --commit           # insert into listings.db + photos
    python3 scrape_ips.py --limit 40         # only fetch the first 40 pages
    python3 scrape_ips.py --commit --include-undated
"""

import argparse
import html
import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

import db
from webscrape import (
    canonical_province, coordinate_plausible, dedupe_keep_newest, fetch,
    has_structures, store,
)

BASE = "https://ips-cambodia.com"
SOURCE_NAME = "ips-cambodia.com"
LAND = re.compile(r"(^|-)land(-|$)")
NOT_LAND = re.compile(r"bedroom|villa|condo|apartment|townhouse|shophouse|penthouse|studio|flat|-with-|building|warehouse|factory")


def iso(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc) if text else None


def candidate_urls():
    index = fetch(f"{BASE}/sitemap_index.xml").text
    areas = [u for u in re.findall(r"<loc>([^<]+)</loc>", index)
             if "/properties-" in u and canonical_province(u.rsplit("/", 1)[-1].replace("-", " "))]
    print(f"  {len(areas)} area sitemaps in target provinces")
    found = []
    for area in areas:
        for loc, lastmod in re.findall(r"<loc>([^<]+)</loc>\s*<lastmod>([^<]+)</lastmod>", fetch(area).text):
            parts = loc.split("/")
            if len(parts) < 6 or parts[3] not in ("buy", "rent"):
                continue
            slug = parts[-2] if loc.endswith("/") else parts[-1]
            if LAND.search(slug) and not NOT_LAND.search(slug):
                found.append((lastmod, loc))
    found.sort(reverse=True)
    return list(dict.fromkeys(u for _, u in found)), dict((u, lm) for lm, u in found)


def parse_size(title):
    m = re.search(r"([\d,]+(?:\.\d+)?)\s*(hectares?|ha\b|sq\.?\s*m|sqm)", title, re.I)
    if not m:
        return None
    return f"{m.group(1)} {'ha' if m.group(2).lower().startswith('h') else 'sqm'}"


def parse_listing(url, page_html):
    slug = [p for p in url.split("/") if p][-1]
    post_id = re.match(r"(\d+)-", slug)
    t = re.search(r"<title>(.*?)</title>", page_html, re.S)
    if not (post_id and t):
        return None
    title = re.sub(r"\s*\|\s*IPS Cambodia\s*$", "", html.unescape(t.group(1)).strip())
    if has_structures(title):
        return None

    published = re.search(r'"datePublished":"([^"]+)"', page_html)
    modified = re.search(r'"dateModified":"([^"]+)"', page_html)
    published = iso(published.group(1)) if published else None
    modified = iso(modified.group(1)) if modified else None
    if not (published or modified):
        return None

    text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"<(script|style)[^>]*>.*?</\1>", "", page_html, flags=re.S))))
    loc = re.search(r"Location ([^,<]{2,60}), ([^,<]{2,60}), ([A-Za-z ]{3,30}?) View map", text)
    if loc:
        commune, district, province_raw = (g.strip() for g in loc.groups())
    else:
        # Pages for the "other provinces" have no commune/district block: the
        # commune is the last place name in the title before the province.
        commune, district = None, None
        tail = re.split(r"\b(?:for sale|for rent)\s*[\u2013\u2014-]\s*", title, flags=re.I)[-1]
        parts = [p.strip() for p in tail.split(",") if p.strip()]
        province_raw = next((p for p in reversed(parts) if canonical_province(p)), "") or url.split("/")[4].replace("-", " ")
        names = [p for p in parts if not canonical_province(p)]
        commune = names[-1] if names else None
    province = canonical_province(province_raw)

    price = None
    offer = re.search(r'"@type":"Offer".{0,700}?"price":"?(\d+(?:\.\d+)?)"?', page_html, re.S)
    per_sqm = re.search(r"\$\s?([\d,]+(?:\.\d+)?)\s*/\s*sqm", text)
    if offer:
        price, price_raw = float(offer.group(1)), f"${float(offer.group(1)):,.0f}"
    elif per_sqm:
        price, price_raw = float(per_sqm.group(1).replace(",", "")), per_sqm.group(0)
    else:
        price_raw = None

    ptype = re.search(r"Property Type (\w[\w ]*?) (?:Floor Area|Dimensions|Land Size|Bedrooms|Bathrooms)", text)
    if ptype and "land" not in ptype.group(1).lower():
        return None

    lat, lng = re.search(r'data-lat="(-?\d+\.\d+)"', page_html), re.search(r'data-lng="(-?\d+\.\d+)"', page_html)
    coord = (float(lat.group(1)), float(lng.group(1))) if (lat and lng) else None
    img = re.search(r'"image":\s*\[\s*"([^"]+)"', page_html)
    desc = re.search(r"Property Description (.*?)(?: Features | Property Features| Share this property| Request Info| Get in touch|$)", text)

    place = ", ".join(p for p in (commune, district, province_raw) if p)
    raw_text = f"{title} | {place}"
    if price_raw:
        raw_text += f"\nPrice: {price_raw}" + (f" ({per_sqm.group(0)})" if (offer and per_sqm) else "")
    raw_text += f"\n{(desc.group(1) if desc else '')[:1500]}"

    return {
        "post_id": int(post_id.group(1)), "url": url, "headline": title, "province": province,
        "commune": commune or None, "district": district or None,
        "updated": modified or published, "published": published, "modified": modified,
        "price_value": price, "price_raw": price_raw, "size_text": parse_size(title),
        "raw_text": raw_text, "photo_url": img.group(1) if img else None, "contact": None,
        "listing_kind": "rent" if "/rent/" in url else "sale",
        "site_coord": coord, "site_coord_ok": coordinate_plausible(coord, province),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="write to listings.db (default: dry run)")
    ap.add_argument("--days", type=int, default=183)
    ap.add_argument("--limit", type=int, default=0, help="only fetch the first N pages (testing)")
    ap.add_argument("--include-undated", action="store_true", help="also import listings that only have a bulk-import date")
    ap.add_argument("--bulk-threshold", type=int, default=25)
    args = ap.parse_args()

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    print(f"IPS land listings dated since {cutoff.date()} (last {args.days} days)")
    urls, lastmods = candidate_urls()

    db.init_db()
    with db.get_conn() as conn:
        stored = {r["message_link"]: r["fetched_at"] for r in conn.execute(
            "SELECT message_link, fetched_at FROM listings WHERE source_name = ?", (SOURCE_NAME,))}
    todo = [u for u in urls if u not in stored or lastmods[u] > stored[u]]
    print(f"{len(urls)} land pages in target provinces; {len(urls) - len(todo)} already stored and unchanged")
    # A site migration stamped most listings with the same few dates; those
    # dates say nothing about when a listing was really posted, so skip them
    # before fetching (the pages carry the same dates).
    per_day = Counter(lastmods[u][:10] for u in todo)
    bulk_days = {d for d, c in per_day.items() if c >= args.bulk_threshold}
    if bulk_days and not args.include_undated:
        before = len(todo)
        todo = [u for u in todo if lastmods[u][:10] not in bulk_days]
        print(f"skipping {before - len(todo)} page(s) whose date is a bulk-import date ({min(bulk_days)}..{max(bulk_days)}, {len(bulk_days)} days); use --include-undated to fetch them")
    if args.limit:
        todo = todo[: args.limit]
    print(f"fetching {len(todo)} page(s)...")

    parsed, skipped, failed = [], {"unparsed": 0, "too old": 0, "other province": 0}, []
    for n, url in enumerate(todo, 1):
        try:
            resp = fetch(url)
            it = parse_listing(url, resp.text) if resp.status_code == 200 else None
        except Exception as e:  # one bad page shouldn't sink the run
            it, _ = None, failed.append(f"{url}: {e}")
        if not it:
            skipped["unparsed"] += 1
        elif it["updated"] < cutoff:
            skipped["too old"] += 1
        elif not it["province"]:
            skipped["other province"] += 1
        else:
            parsed.append(it)
        if n % 25 == 0:
            print(f"  ...{n}/{len(todo)} fetched, {len(parsed)} in scope so far", flush=True)

    day = lambda d: d.date().isoformat()
    counts = Counter(day(it["updated"]) for it in parsed)
    bulk = {d for d, c in counts.items() if c >= args.bulk_threshold}
    for it in parsed:
        it["undated"] = day(it["updated"]) in bulk
    genuine = [it for it in parsed if not it["undated"]]
    undated = [it for it in parsed if it["undated"]]
    pool = parsed if args.include_undated else genuine
    kept = dedupe_keep_newest(pool)

    ok = [it for it in kept if it["site_coord_ok"]]
    rejected = [it for it in kept if it["site_coord"] and not it["site_coord_ok"]]
    both = [it for it in kept if it["commune"] and it["district"]]
    print(f"\nparsed {len(parsed)} in scope: {len(genuine)} with a real date, {len(undated)} carrying only a bulk date {sorted(bulk)}")
    print(f"importing {'ALL' if args.include_undated else 'real-dated only'}: {len(pool)} -> {len(pool) - len(kept)} duplicate(s) dropped -> {len(kept)} listings")
    print(f"skipped: {skipped}; fetch errors: {len(failed)}")
    print(f"with a usable coordinate (blue pin): {len(ok)}; rejected as outside province: {len(rejected)}")
    for it in rejected[:5]:
        print(f"  rejected: {it['headline'][:45]} | {it['province']} | {it['site_coord']}")
    print(f"with both commune and district named: {len(both)} of {len(kept)}")
    print("top dates:", counts.most_common(6))
    by_prov = Counter(it["province"] for it in kept)
    print("by province:", dict(by_prov.most_common()))
    for it in kept[:8]:
        print(f"  {it['updated'].date()} | {it['headline'][:42]:42} | {it['commune']} / {it['district']} / {it['province']} | {it['price_raw']}")

    if not args.commit:
        print("\n(dry run -- nothing written. Re-run with --commit to insert.)")
        return

    inserted = 0
    with db.get_conn() as conn:
        for it in kept:
            if store(conn, SOURCE_NAME, it, "IPS Cambodia"):
                inserted += 1
    print(f"\ninserted {inserted} new listing(s) ({len(kept) - inserted} already in the database)")


if __name__ == "__main__":
    main()
