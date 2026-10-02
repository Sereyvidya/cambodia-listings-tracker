"""
Posts newly-collected, geocoded listings into a Telegram group/channel
so your dad can check new listings right in Telegram, without opening
the dashboard or the Sheet.

Uses a SEPARATE Telethon session from the one listener.py uses to read
groups (config.yaml -> notify.session_name). Telegram accounts can be
logged into multiple sessions at once (same as phone + desktop + web),
so this just means one extra one-time login code the first time this
runs -- it avoids two processes fighting over the same local session
file, which Telethon's session storage doesn't handle well.

Only qualifying listings get sent (see db._PUBLISH_GATE_SQL), same rule
as the Sheet sync.
"""

from telethon import TelegramClient

import db


def format_listing_message(row):
    headline_bits = []
    if row["price_value"]:
        headline_bits.append(f"${row['price_value']:,.0f}")
    else:
        headline_bits.append("price n/a")
    if row["property_type"]:
        headline_bits.append(row["property_type"])
    if row["location"]:
        headline_bits.append(f"in {row['location']}")
    lines = [" ".join(headline_bits)]

    details = []
    if row["size_text"]:
        details.append(row["size_text"])
    if row["bedrooms"]:
        details.append(f"{row['bedrooms']} bed")
    if row["listing_kind"]:
        details.append(row["listing_kind"])
    if details:
        lines.append(" · ".join(details))

    lines.append("")
    lines.append((row["raw_text"] or "")[:400])
    lines.append("")

    if row["contact"]:
        lines.append(f"Contact: {row['contact']}")
    if row["posted_by"]:
        lines.append(f"Posted by: {row['posted_by']}")
    lines.append(f"Source: {row['source_name']}")
    tier, link = db.pin_info(row)
    note = {"real_link": "", "site_coordinate": " (approximate, from the listing site)"}.get(
        db.pin_basis(row), " (approximate, sangkat-level)")
    lines.append(f"Map: {link}{note}")
    return "\n".join(lines)


async def notify_new_listings(conn, cfg):
    notify_cfg = cfg["notify"]
    tg = cfg["telegram"]
    session_name = notify_cfg.get("session_name") or f"{tg['session_name']}_notify"
    target = notify_cfg["target_chat"]

    pending = db.listings_pending_notify(conn)
    if not pending:
        return 0

    client = TelegramClient(session_name, tg["api_id"], tg["api_hash"])
    await client.start(phone=tg["phone"])
    try:
        sent = 0
        for row in pending:
            await client.send_message(target, format_listing_message(row), link_preview=False)
            db.mark_notified(conn, row["id"])
            sent += 1
    finally:
        await client.disconnect()
    return sent
