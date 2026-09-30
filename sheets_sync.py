"""
Pushes new, geocoded listings into a Google Sheet so your dad can browse
them without running the local dashboard.

Auth is via a Google service account (not your personal Google login):
see README.md's "Google Sheets setup" section for how to create one.
config.yaml -> google_sheets.service_account_file points at the JSON key
file, and google_sheets.spreadsheet_id at the target sheet (which must
be shared with the service account's email address as an Editor).

Only qualifying listings get pushed (see db._PUBLISH_GATE_SQL): either a
REAL Google Maps link the poster themselves included, or -- per your
dad -- a post naming both its sangkat and khan, which is precise enough
to trust even with no map link at all. Cross-posted duplicates (same
extract.make_dedup_hash) are pushed once; later duplicates are marked
synced without adding a second row.
"""

from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

import db

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

HEADERS = [
    "Description", "Price (USD)", "Size", "Location", "Type", "Sale/Rent",
    "Bedrooms", "Contact", "Posted By", "Source", "Posted/Fetched",
    "Google Maps Link", "Pin Precision",
]


def get_worksheet(cfg):
    sheets_cfg = cfg["google_sheets"]
    sa_path = Path(__file__).parent / sheets_cfg["service_account_file"]
    if not sa_path.exists():
        raise SystemExit(
            f"{sa_path} not found. See README.md's Google Sheets setup section."
        )
    creds = Credentials.from_service_account_file(str(sa_path), scopes=SCOPES)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheets_cfg["spreadsheet_id"])
    return sh.sheet1


def ensure_header(ws):
    if not ws.row_values(1):
        ws.append_row(HEADERS)


def row_for_listing(row):
    posted = (row["posted_at"] or row["fetched_at"] or "")[:16].replace("T", " ")
    tier, link = db.pin_info(row)
    precision = "Exact (from post)" if tier == "real_link" else "Approximate (sangkat-level)"
    return [
        row["raw_text"] or "",
        f"{row['price_value']:,.0f}" if row["price_value"] else "",
        row["size_text"] or "",
        row["location"] or "",
        row["property_type"] or "",
        row["listing_kind"] or "",
        row["bedrooms"] or "",
        row["contact"] or "",
        row["posted_by"] or "",
        row["source_name"] or "",
        posted,
        link or "",
        precision,
    ]


def sync_new_listings(conn, cfg):
    ws = get_worksheet(cfg)
    ensure_header(ws)

    pushed = 0
    skipped_dupes = 0
    for row in db.listings_pending_sheet_sync(conn):
        dup_id = db.find_synced_duplicate(conn, row["dedup_hash"], row["id"])
        if dup_id is not None:
            skipped_dupes += 1
        else:
            ws.append_row(row_for_listing(row), value_input_option="USER_ENTERED")
            pushed += 1
        db.mark_sheet_synced(conn, row["id"])
    return pushed, skipped_dupes
