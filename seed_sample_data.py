"""
Loads a handful of fake listings into listings.db so you can try the
dashboard immediately, before wiring up real Telegram credentials.

Run:
    python seed_sample_data.py
    python dashboard.py

Safe to run multiple times (INSERT OR IGNORE on the same message_id/
source_name pair just no-ops).
"""

from datetime import datetime, timedelta, timezone

import db
import extract

SAMPLE_MESSAGES = [
    ("demo_bkk1_condos", 1, "For rent: 2 bedroom condo in BKK1, $650/month, "
     "pool + gym, close to Aeon Mall. Available now! Contact 012 345 678", "@agent_dara"),
    ("demo_bkk1_condos", 2, "URGENT SALE - Villa in Chroy Changvar, 4 bed, "
     "river view, $250,000. @agent_sokha", "@agent_sokha"),
    ("demo_phnompenh_land", 3, "Land for sale near Sen Sok, 5x20m, "
     "$45,000. Good for building. Call 097 888 1234", "Sokha Land Co"),
    ("demo_phnompenh_land", 4, "Studio apartment for rent in Toul Kork, "
     "$280/month, furnished, 1 bed. 010 222 333", "Vanna P."),
    ("demo_bkk1_condos", 5, "House for rent Siem Reap, 3 bedroom, "
     "1,200,000 riel per month, near Pub Street.", "@agent_dara"),
    ("demo_phnompenh_land", 6, "thanks bro!", "Random User"),  # should get filtered out
]


def main():
    db.init_db()
    with db.get_conn() as conn:
        kept = 0
        for source_name, message_id, text, posted_by in SAMPLE_MESSAGES:
            parsed = extract.parse_message(text)
            if not extract.looks_like_listing(text, parsed):
                print(f"  skipped (not listing-like): {text[:40]!r}")
                continue
            listing = {
                "source_type": "telegram",
                "source_name": source_name,
                "message_id": message_id,
                "message_link": f"https://t.me/{source_name}/{message_id}",
                "posted_at": (datetime.now(timezone.utc) - timedelta(days=message_id)).isoformat(),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "raw_text": text,
                "photo_paths": [],
                "posted_by": posted_by,
                **parsed,
            }
            db.insert_listing(conn, listing)
            kept += 1
        print(f"Seeded {kept} sample listings into listings.db")


if __name__ == "__main__":
    main()
