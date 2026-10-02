"""
Scrapes land listings from pointerasia.com into listings.db, alongside the
Telegram ones. (Pointer also feeds khpropertyhub.com, which sits behind a
Cloudflare challenge -- the same property IDs appear here.)

Listing URLs come from Pointer's own sitemap; each page's server-rendered
HTML embeds a structured record with the price, updatedAt date, commune/
district/province names, land area, and a coordinate with a ~150 m
boundary circle (a deliberately blurred "preview" location). That
coordinate becomes an approximate (blue) pin once it's checked against the
listing's province. robots.txt disallows /api/ and /_next/, which this
never touches -- it only reads the public listing pages.

Rules applied: land only; updated within --days (default 183 = ~6 months);
only the target provinces; one row per property (newest "updatedAt" wins
among apparent duplicates).

Usage:
    python3 scrape_pointer.py              # dry run: parse + summarize
    python3 scrape_pointer.py --commit     # insert into listings.db + photos
    python3 scrape_pointer.py --limit 20   # only fetch the first 20 pages
"""

import argparse
import html
import json
import re
import statistics
from datetime import datetime, timedelta, timezone

import db
from webscrape import (
    canonical_province, coordinate_plausible, dedupe_keep_newest, distance_km,
    fetch, has_structures, store,
)

BASE = "https://pointerasia.com"
SOURCE_NAME = "pointerasia.com"


def land_urls():
    resp = fetch(f"{BASE}/properties-sitemap.xml")
    entries = re.findall(r"<loc>([^<]+)</loc>\s*<lastmod>([^<]+)</lastmod>", resp.text)
    urls = []
    for loc, lastmod in entries:
        slug = loc.rsplit("/", 1)[-1]
        if not slug.startswith("land-"):
            continue
        if not canonical_province(slug.replace("-", " ")):
            continue  # slug names a province we don't collect (e.g. battambang)
        urls.append((lastmod, loc))
    urls.sort(reverse=True)
    return [u for _, u in urls]


def flat_payload(page_html):
    """The page's Next.js streamed data, decoded and joined."""
    out = []
    for chunk in re.findall(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', page_html):
        try:
            out.append(json.loads('"' + chunk + '"'))
        except ValueError:
            continue
    return "".join(out)


def parse_size(value):
    m = re.search(r"([\d,]+(?:\.\d+)?)\s*(m²|m2|sqm|ha\b|hectares?)", value or "", re.I)
    if not m:
        return None
    return f"{m.group(1)} {'ha' if m.group(2).lower().startswith('h') else 'sqm'}"


def parse_listing(url, page_html):
    flat = flat_payload(page_html)
    i = flat.find('"originalPrice"')
    if i == -1:
        return None
    window = flat[max(0, i - 9000):i + 3500]

    head = re.search(
        r'"originalPrice":(\d+(?:\.\d+)?),"visibility":"[^"]*","updatedAt":"([\d-]+)","createdAt":"([\d-]+)","active":(true|false)',
        window)
    names = re.search(r'"districtName":"([^"]*)","communeName":"([^"]*)","provinceName":"([^"]*)","isArchived":(true|false)', window)
    ptype = re.search(r'"propertyType":\{"id":"\d+","name":"([^"]+)"\}', window)
    if not (head and names and ptype):
        return None
    if ptype.group(1).lower() != "land" or head.group(4) != "true" or names.group(4) == "true":
        return None

    price = float(head.group(1)) or None
    updated = datetime.strptime(head.group(2), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    district, commune, province_raw = (g.strip() or None for g in names.group(1, 2, 3))
    province = canonical_province(province_raw)

    area = re.search(r'"code":"land_area","value":"([^"]+)"', window)
    action = re.search(r'"actionType":\{"isSelling":(true|false),"isRenting":(true|false)', window)
    kind = "rent" if (action and action.group(2) == "true" and action.group(1) == "false") else "sale"

    coord, radius_m = None, None
    loc = re.search(
        r'"previewLocation":\{"position":\{"type":"Point","coordinates":\[(-?[\d.]+),(-?[\d.]+)\]\},"boundary":\{"type":"Polygon","coordinates":\[\[\[(-?[\d.]+),(-?[\d.]+)\]',
        flat)
    if loc:
        lon, lat = float(loc.group(1)), float(loc.group(2))
        coord = (lat, lon)
        radius_m = distance_km(lat, lon, float(loc.group(4)), float(loc.group(3))) * 1000

    ld = re.search(r'<script type="application/ld\+json">(\{"@context":"https://schema.org","@type":\["Residence".*?\})</script>', page_html, re.S)
    title, desc = None, ""
    if ld:
        try:
            d = json.loads(ld.group(1))
            title, desc = d.get("name"), re.sub(r"\s+", " ", d.get("description") or "").strip()
        except ValueError:
            pass
    if not title:
        t = re.search(r"<title>(.*?)</title>", page_html, re.S)
        title = re.sub(r"\s*\|\s*Pointer\s*$", "", html.unescape(t.group(1)).strip()) if t else url
    if has_structures(title):
        return None  # land with structures on it isn't a plain land listing
    og = re.search(r'<meta property="og:image" content="([^"]+)"', page_html)

    place = ", ".join(p for p in (commune, district, province_raw) if p)
    raw_text = f"{title} | {place}"
    if price:
        raw_text += f"\nPrice: ${price:,.0f}"
    if area:
        raw_text += f"\nLand area: {area.group(1)}"
    raw_text += f"\n{desc[:1500]}"

    return {
        "post_id": int(re.search(r"-(\d+)$", url).group(1)), "url": url, "headline": title,
        "province": province, "commune": commune, "district": district, "updated": updated,
        "price_value": price, "price_raw": f"${price:,.0f}" if price else None,
        "size_text": parse_size(area.group(1)) if area else None, "raw_text": raw_text,
        "photo_url": og.group(1) if og else None, "contact": None, "listing_kind": kind,
        "site_coord": coord, "site_coord_ok": coordinate_plausible(coord, province),
        "radius_m": radius_m,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="write to listings.db (default: dry run)")
    ap.add_argument("--days", type=int, default=183)
    ap.add_argument("--limit", type=int, default=0, help="only fetch the first N pages (testing)")
    args = ap.parse_args()

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    print(f"Pointer land listings updated since {cutoff.date()} (last {args.days} days)")
    urls = land_urls()
    if args.limit:
        urls = urls[: args.limit]
    print(f"{len(urls)} land pages in the sitemap for target provinces; fetching each...")

    parsed, skipped, failed = [], {"unparsed/archived": 0, "too old": 0, "other province": 0}, []
    for n, url in enumerate(urls, 1):
        try:
            resp = fetch(url)
            it = parse_listing(url, resp.text) if resp.status_code == 200 else None
        except Exception as e:  # one bad page shouldn't sink the run
            it, _ = None, failed.append(f"{url}: {e}")
        if not it:
            skipped["unparsed/archived"] += 1
        elif it["updated"] < cutoff:
            skipped["too old"] += 1
        elif not it["province"]:
            skipped["other province"] += 1
        else:
            parsed.append(it)
        if n % 25 == 0:
            print(f"  ...{n}/{len(urls)} fetched, {len(parsed)} in scope so far", flush=True)

    kept = dedupe_keep_newest(parsed)
    ok = [it for it in kept if it["site_coord_ok"]]
    rejected = [it for it in kept if it["site_coord"] and not it["site_coord_ok"]]
    radii = [it["radius_m"] for it in kept if it.get("radius_m")]
    print(f"\nparsed {len(parsed)} in scope, {len(parsed) - len(kept)} duplicate(s) dropped -> {len(kept)} listings")
    print(f"skipped: {skipped}; fetch errors: {len(failed)}")
    print(f"with a usable coordinate (blue pin): {len(ok)}; rejected as outside province: {len(rejected)}")
    for it in rejected[:5]:
        print(f"  rejected: {it['headline'][:40]} | {it['province']} | {it['site_coord']}")
    if radii:
        print(f"boundary radius around the coordinate: min {min(radii):.0f} m, median {statistics.median(radii):.0f} m, max {max(radii):.0f} m")
    both = sum(1 for it in kept if it["commune"] and it["district"])
    print(f"with both commune and district named: {both} of {len(kept)}")
    by_prov = {}
    for it in kept:
        by_prov[it["province"]] = by_prov.get(it["province"], 0) + 1
    print("by province:", dict(sorted(by_prov.items(), key=lambda kv: -kv[1])))
    print("by month updated:", dict(sorted({m: sum(1 for it in kept if it['updated'].strftime('%Y-%m') == m) for m in {it['updated'].strftime('%Y-%m') for it in kept}}.items())))
    for it in kept[:8]:
        print(f"  {it['updated'].date()} | {it['headline'][:40]:40} | {it['commune']} / {it['district']} / {it['province']} | {it['price_raw']} | {it['size_text']}")

    if not args.commit:
        print("\n(dry run -- nothing written. Re-run with --commit to insert.)")
        return

    db.init_db()
    inserted = 0
    with db.get_conn() as conn:
        for it in kept:
            if store(conn, SOURCE_NAME, it, "Pointer"):
                inserted += 1
    print(f"\ninserted {inserted} new listing(s) ({len(kept) - inserted} already in the database)")


if __name__ == "__main__":
    main()
