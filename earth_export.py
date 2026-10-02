"""
Exports a Google Earth-ready .kmz snapshot of every visible, qualifying
listing (see db.listings_for_earth_export / db._PUBLISH_GATE_SQL). Each
pin embeds the listing's first photo (if one was downloaded) and its
full text in the popup. Pins are colored by precision (see db.pin_info):
green for a real poster-provided link, blue for a sangkat+khan guess --
the latter is only accurate to "somewhere in this sangkat", not the
actual building.

Open the resulting file in Google Earth Pro (double-click it) or upload
it via earth.google.com -> Projects -> Import KML file.

Run with:
    python earth_export.py
Regenerates the file from scratch each time -- it's a snapshot, not an
incremental sync like the Sheet/notify pipeline.
"""

import json
import sys
from pathlib import Path

import simplekml

import db

PHOTOS_DIR = Path(__file__).parent / "photos"
DEFAULT_OUTPUT = Path(__file__).parent / "cambodia_listings.kmz"

# Google's standard KML paddle icons -- reliable, no hosting of our own.
ICON_BY_TIER = {
    "real_link": "http://maps.google.com/mapfiles/kml/paddle/grn-blank.png",
    "sangkat_khan": "http://maps.google.com/mapfiles/kml/paddle/blu-blank.png",
}


def build_description(row):
    parts = []
    if row["price_value"]:
        parts.append(f"${row['price_value']:,.0f}")
    if row["size_text"]:
        parts.append(row["size_text"])
    if row["bedrooms"]:
        parts.append(f"{row['bedrooms']} bed")
    if row["listing_kind"]:
        parts.append(row["listing_kind"])
    headline = " · ".join(parts)
    lines = [headline] if headline else []
    lines.append(row["raw_text"] or "")
    if row["contact"]:
        lines.append(f"Contact: {row['contact']}")
    if row["posted_by"]:
        lines.append(f"Posted by: {row['posted_by']}")
    lines.append(f"Source: {row['source_name']} ({(row['posted_at'] or row['fetched_at'])[:10]})")
    tier, link = db.pin_info(row)
    if link:
        detail = {"real_link": "", "site_coordinate": " (approximate, ~1 km)"}.get(
            db.pin_basis(row), " (approximate, sangkat-level)")
        label = "Open in Google Maps" + detail
        lines.append(f'<a href="{link}">{label}</a>')
    return "<br/>".join(l for l in lines if l)


def export_kmz(conn, output_path=DEFAULT_OUTPUT):
    kml = simplekml.Kml()
    rows = db.listings_for_earth_export(conn)
    for row in rows:
        name = f"${row['price_value']:,.0f}" if row["price_value"] else (row["location"] or "Listing")
        pnt = kml.newpoint(name=name, coords=[(row["lon"], row["lat"])])
        tier, _ = db.pin_info(row)
        pnt.style.iconstyle.icon.href = ICON_BY_TIER.get(tier, ICON_BY_TIER["real_link"])
        desc_html = build_description(row)

        photo_paths = json.loads(row["photo_paths"] or "[]")
        if photo_paths:
            local_photo = PHOTOS_DIR / photo_paths[0]
            if local_photo.exists():
                kmz_relative = f"files/{photo_paths[0]}"
                kml.addfile(str(local_photo))
                desc_html = f'<img src="{kmz_relative}" width="320"/><br/>{desc_html}'

        pnt.description = f"<![CDATA[{desc_html}]]>"

    kml.savekmz(str(output_path))
    return len(rows)


if __name__ == "__main__":
    db.init_db()
    with db.get_conn() as conn:
        count = export_kmz(conn)
    print(f"Exported {count} pins to {DEFAULT_OUTPUT}")
    if count == 0:
        print("(Nothing geocoded yet -- run `python sync.py` first.)")
        sys.exit(0)
