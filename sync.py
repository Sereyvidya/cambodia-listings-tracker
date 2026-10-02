"""
Publishes what listener.py has collected: geocodes locations, resolves
any real map links posters included to precise coordinates, pushes
qualifying listings to Google Sheets, notifies your Telegram group, and
refreshes the Google Earth export -- each step independently on/off in
config.yaml.

Run this whenever you want to publish what's been collected so far:
    python sync.py

It's a one-shot script (like seed_sample_data.py), not a long-running
process -- run it by hand, or on a schedule yourself (cron/launchd) if
you want it automatic. Safe to run repeatedly: every step only acts on
rows it hasn't already processed.

Order matters: geocoding/resolving/dedup must happen before Sheets sync
/ notify / Earth export, since all three only touch listings with a
source_maps_link -- a REAL Google Maps link the poster themselves
included, not our own neighborhood-level guess (see extract.py's
extract_map_link and db.py's column comments) -- that also isn't shared
with a repost or a different property (see
db.compute_duplicate_link_exclusions).
"""

import asyncio

import db
import geocode
from config import load_config


def geocode_pending(conn, cfg):
    region_hint = cfg.get("geocoding", {}).get("region_hint", "Cambodia")
    pending = db.listings_needing_geocode(conn)
    resolved = 0
    for row in pending:
        cached = db.get_cached_geocode(conn, row["location"])
        if cached is None:
            coords = geocode.geocode(row["location"], region_hint=region_hint)
            db.set_cached_geocode(conn, row["location"], *(coords if coords else (None, None)))
        else:
            coords = cached if cached[0] is not None else None

        if coords:
            lat, lon = coords
            db.set_geocode_result(conn, row["id"], lat, lon, geocode.maps_link(lat, lon))
            resolved += 1
        else:
            db.set_geocode_result(conn, row["id"], None, None, None)
    return len(pending), resolved


def resolve_source_map_links(conn):
    """For listings where the poster included a real Google Maps link,
    follow it to get precise coordinates (overwriting the neighborhood-
    level guess from geocode_pending, if any) -- this doesn't change
    whether a listing qualifies as having a map link (that's just whether
    source_maps_link is set at all), only how accurate its pin is."""
    pending = db.listings_needing_source_map_resolve(conn)
    resolved = 0
    for row in pending:
        coords = geocode.resolve_source_map_link(row["source_maps_link"])
        if coords:
            lat, lon = coords
            db.set_geocode_result(conn, row["id"], lat, lon, geocode.maps_link(lat, lon))
            resolved += 1
        db.mark_source_map_resolved(conn, row["id"], ok=bool(coords))
    return len(pending), resolved


def main():
    cfg = load_config()
    db.init_db()

    with db.get_conn() as conn:
        checked, resolved = geocode_pending(conn, cfg)
        print(f"Geocoding: checked {checked} new location(s), resolved {resolved}")

        src_checked, src_resolved = resolve_source_map_links(conn)
        print(f"Source map links: resolved {src_resolved}/{src_checked} to precise coordinates")

        reposts, reused_groups = db.compute_duplicate_link_exclusions(conn)
        print(f"Duplicate map links: collapsed {reposts} repost(s), excluded {reused_groups} reused-link group(s)")

        if cfg.get("google_sheets", {}).get("enabled"):
            import sheets_sync
            pushed, dupes = sheets_sync.sync_new_listings(conn, cfg)
            print(f"Google Sheets: pushed {pushed} row(s), {dupes} skipped as duplicates")
        else:
            print("Google Sheets: disabled (google_sheets.enabled: false in config.yaml)")

        if cfg.get("notify", {}).get("enabled"):
            import telegram_notify
            sent = asyncio.run(telegram_notify.notify_new_listings(conn, cfg))
            print(f"Telegram notify: sent {sent} message(s)")
        else:
            print("Telegram notify: disabled (notify.enabled: false in config.yaml)")

        if cfg.get("earth_export", {}).get("enabled", True):
            import earth_export
            count = earth_export.export_kmz(conn)
            print(f"Google Earth export: wrote {count} pin(s) to {earth_export.DEFAULT_OUTPUT}")


if __name__ == "__main__":
    main()
