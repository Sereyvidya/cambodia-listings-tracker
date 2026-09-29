"""
Exports a static, shareable snapshot of the qualifying listings (visible,
with a REAL poster-provided map link -- the same set that goes to
Sheets/Telegram/Earth, see db.listings_pending_sheet_sync) into docs/,
ready for GitHub Pages. No backend involved: docs/index.html and
docs/map.html are plain static pages that fetch docs/listings.json and
filter it client-side in the browser.

Run with:
    python3 publish_static.py
Then commit and push docs/ to publish the update -- see README.md's
"Sharing a live link" section for the one-time GitHub Pages setup.
"""

import json
import shutil
from pathlib import Path

import db

ROOT = Path(__file__).parent
PHOTOS_DIR = ROOT / "photos"
DOCS_DIR = ROOT / "docs"
DOCS_PHOTOS_DIR = DOCS_DIR / "photos"


def export():
    db.init_db()
    with db.get_conn() as conn:
        rows = db.query_listings(conn, has_source_map="yes", limit=5000)

    DOCS_PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
    listings = []
    for row in rows:
        d = dict(row)
        photo_paths = json.loads(d.pop("photo_paths") or "[]")
        copied = []
        for name in photo_paths:
            src = PHOTOS_DIR / name
            if src.exists():
                shutil.copy2(src, DOCS_PHOTOS_DIR / name)
                copied.append(name)
        d["photo_paths"] = copied
        # The published site should link to the poster's own real link,
        # not our derived (and now redundant) coordinate-search one.
        d["maps_link"] = d["source_maps_link"]
        listings.append(d)

    (DOCS_DIR / "listings.json").write_text(json.dumps(listings))

    # Remove photos left over from listings that no longer qualify (e.g.
    # excluded as a duplicate/reused map link since the last publish) --
    # otherwise docs/photos/ only ever grows.
    still_needed = {name for l in listings for name in l["photo_paths"]}
    removed = 0
    for existing in DOCS_PHOTOS_DIR.iterdir():
        if existing.name not in still_needed:
            existing.unlink()
            removed += 1

    print(f"Exported {len(listings)} listings and their photos to {DOCS_DIR} ({removed} stale photo(s) removed)")


if __name__ == "__main__":
    export()
