"""
Telegram listener for the Cambodia listings tracker.

What it does:
  1. Logs into Telegram using YOUR account (via Telethon) -- first run
     will ask for your phone's login code interactively, then caches a
     session file so future runs don't need that again.
  2. For each group in config.yaml, backfills recent history (once)
     and then can run continuously, printing/storing new messages as
     they arrive.
  3. Runs every message through extract.parse_message(), keeps the
     ones that look like listings, downloads photos, and writes to
     listings.db (see db.py).

Usage:
    python listener.py backfill     # one-time pull of recent history for all groups
    python listener.py listen       # stay running, store new messages as they arrive
    python listener.py backfill listen   # do both, one after another

Run this as a long-lived process (e.g. under `tmux`, `screen`, `nohup`,
or a systemd/launchd service) if you want it to keep collecting new
listings while you're not watching it -- it does nothing while it's
not running, it doesn't schedule itself.
"""

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telethon import TelegramClient, events

import db
import extract
from config import load_config

PHOTOS_DIR = Path(__file__).parent / "photos"


async def _sender_display_name(message):
    """Who actually posted the message in the group (distinct from any
    phone/handle mentioned in the ad text itself, which extract.py
    captures separately as `contact`)."""
    try:
        sender = await message.get_sender()
    except Exception:
        return None
    if sender is None:
        return None
    if getattr(sender, "username", None):
        return f"@{sender.username}"
    name = " ".join(filter(None, [getattr(sender, "first_name", None), getattr(sender, "last_name", None)]))
    return name or None


async def store_message(conn, cfg, group_label, message):
    text = message.message or ""
    parsed = extract.parse_message(text)
    if not extract.looks_like_listing(text, parsed):
        return False

    photo_paths = []
    if cfg["settings"].get("download_photos") and message.photo:
        PHOTOS_DIR.mkdir(exist_ok=True)
        dest = PHOTOS_DIR / f"{group_label}_{message.id}.jpg"
        try:
            await message.download_media(file=str(dest))
            photo_paths.append(dest.name)  # dashboard.py serves these from /photos/<name>
        except Exception as e:
            print(f"  (photo download failed for {group_label}/{message.id}: {e})")

    posted_at = message.date.astimezone(timezone.utc).isoformat() if message.date else None
    link = f"https://t.me/{group_label}/{message.id}" if not group_label.startswith("+") else None
    posted_by = await _sender_display_name(message)

    listing = {
        "source_type": "telegram",
        "source_name": group_label,
        "message_id": message.id,
        "message_link": link,
        "posted_at": posted_at,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "raw_text": text,
        "photo_paths": photo_paths,
        "posted_by": posted_by,
        **parsed,
    }
    db.insert_listing(conn, listing)
    return True


async def backfill(client, cfg):
    days = cfg["settings"].get("backfill_days", 14)
    since = datetime.now(timezone.utc) - timedelta(days=days)

    with db.get_conn() as conn:
        for group in cfg["groups"]:
            print(f"Backfilling '{group}' (last {days} days)...")
            entity = await client.get_entity(group)
            kept = 0
            seen = 0
            async for message in client.iter_messages(entity, offset_date=None):
                if message.date and message.date < since:
                    break
                seen += 1
                if message.message:
                    if await store_message(conn, cfg, group, message):
                        kept += 1
            print(f"  scanned {seen} messages, kept {kept} as listing candidates")


async def listen(client, cfg):
    group_labels = set(cfg["groups"])
    entities = {g: await client.get_entity(g) for g in group_labels}
    label_by_id = {e.id: label for label, e in entities.items()}

    @client.on(events.NewMessage(chats=list(entities.values())))
    async def handler(event):
        label = label_by_id.get(event.chat_id, str(event.chat_id))
        with db.get_conn() as conn:
            kept = await store_message(conn, cfg, label, event.message)
        if kept:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] new listing from {label}: "
                  f"{(event.message.message or '')[:80]!r}")

    print("Listening for new messages... (Ctrl+C to stop)")
    await client.run_until_disconnected()


async def main():
    cfg = load_config()
    db.init_db()

    modes = set(sys.argv[1:]) or {"backfill", "listen"}
    if not modes & {"backfill", "listen"}:
        sys.exit("Usage: python listener.py [backfill] [listen]")

    tg = cfg["telegram"]
    client = TelegramClient(tg["session_name"], tg["api_id"], tg["api_hash"])
    await client.start(phone=tg["phone"])

    if "backfill" in modes:
        await backfill(client, cfg)
    if "listen" in modes:
        await listen(client, cfg)

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
