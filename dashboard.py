"""
Local web dashboard for browsing collected listings.

Run with:
    python dashboard.py
Then open http://127.0.0.1:5000 in a browser.

Reads from the same listings.db that listener.py writes to -- run them
side by side (listener collecting, dashboard for browsing) or just
dashboard.py alone to look at whatever's been collected so far.
"""

import json
from pathlib import Path

from flask import Flask, render_template, request, redirect, url_for, send_from_directory

import db

app = Flask(__name__)
PHOTOS_DIR = Path(__file__).parent / "photos"


@app.route("/")
def index():
    db.init_db()  # harmless no-op if it already exists; lets dashboard run standalone
    with db.get_conn() as conn:
        listings = db.query_listings(
            conn,
            min_price=request.args.get("min_price", type=float),
            max_price=request.args.get("max_price", type=float),
            location=request.args.get("location") or None,
            property_type=request.args.get("property_type") or None,
            listing_kind=request.args.get("listing_kind") or None,
            source_name=request.args.get("source_name") or None,
            search_text=request.args.get("q") or None,
            has_map=request.args.get("has_map") or None,
            has_source_map=request.args.get("has_source_map") or None,
            has_pin=request.args.get("has_pin") or None,
        )
        locations = db.distinct_values(conn, "location")
        property_types = db.distinct_values(conn, "property_type")
        sources = db.distinct_values(conn, "source_name")

        rows = list(listings)
        listings = []
        for row in rows:
            tier, link = db.pin_info(row)
            l = dict(row)
            l["photo_paths"] = json.loads(l["photo_paths"] or "[]")
            l["pin_tier"] = tier
            l["pin_link"] = link
            listings.append(l)

    return render_template(
        "index.html",
        listings=listings,
        locations=locations,
        property_types=property_types,
        sources=sources,
        filters=request.args,
        count=len(listings),
    )


@app.route("/map")
def map_view():
    db.init_db()
    with db.get_conn() as conn:
        listings = db.query_listings(
            conn,
            min_price=request.args.get("min_price", type=float),
            max_price=request.args.get("max_price", type=float),
            location=request.args.get("location") or None,
            property_type=request.args.get("property_type") or None,
            listing_kind=request.args.get("listing_kind") or None,
            source_name=request.args.get("source_name") or None,
            search_text=request.args.get("q") or None,
            has_pin="yes",  # only listings that would actually be published get a pin here
            limit=2000,
        )
        locations = db.distinct_values(conn, "location")
        property_types = db.distinct_values(conn, "property_type")
        sources = db.distinct_values(conn, "source_name")

        pin_tier_filter = request.args.get("pin_tier") or None
        rows = []
        for row in listings:
            tier, link = db.pin_info(row)
            if pin_tier_filter and tier != pin_tier_filter:
                continue
            d = dict(row)
            d["photo_paths"] = json.loads(d["photo_paths"] or "[]")
            d["maps_link"] = link
            d["pin_tier"] = tier
            rows.append(d)

    return render_template(
        "map.html",
        listings=rows,
        locations=locations,
        property_types=property_types,
        sources=sources,
        filters=request.args,
        count=len(rows),
    )


@app.route("/hide/<int:listing_id>", methods=["POST"])
def hide(listing_id):
    with db.get_conn() as conn:
        conn.execute("UPDATE listings SET hidden = 1 WHERE id = ?", (listing_id,))
    return redirect(request.referrer or url_for("index"))


@app.route("/photos/<path:filename>")
def photo(filename):
    return send_from_directory(PHOTOS_DIR, filename)


if __name__ == "__main__":
    db.init_db()
    app.run(debug=True, port=5000)
