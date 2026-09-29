"""
Looks up Telegram's "Similar Channels" recommendations for a channel --
the same list shown in the Telegram app when you open/join a channel.
Handy for discovering more Cambodia real estate groups to add to
config.yaml's `groups` list, starting from one you already know is good.

Usage:
    python find_similar_channels.py <channel_username> [<channel_username> ...]

Example:
    python find_similar_channels.py diamond_property
"""

import asyncio
import sys

from telethon import TelegramClient
from telethon.tl.functions.channels import GetChannelRecommendationsRequest

from config import load_config


async def similar_channels(client, channel_username):
    entity = await client.get_entity(channel_username)
    result = await client(GetChannelRecommendationsRequest(channel=entity))
    return result.chats


async def main(channel_usernames):
    cfg = load_config()
    tg = cfg["telegram"]
    client = TelegramClient(tg["session_name"], tg["api_id"], tg["api_hash"])
    await client.start(phone=tg["phone"])

    seen = {}
    for source in channel_usernames:
        print(f"\nChannels similar to '{source}':")
        try:
            chats = await similar_channels(client, source)
        except Exception as e:
            print(f"  couldn't look this one up: {e}")
            continue
        if not chats:
            print("  (none returned)")
        for chat in chats:
            username = getattr(chat, "username", None)
            count = getattr(chat, "participants_count", None)
            label = f"@{username}" if username else "(no public username)"
            print(f"  {label:35} {count or '?':>8} members  {chat.title}")
            if username:
                seen[username] = chat.title

    await client.disconnect()

    if seen:
        print(f"\n{len(seen)} distinct channel(s) found across all lookups. To add one to")
        print("config.yaml, add its username (without @) to the `groups:` list, then")
        print("make sure the account has joined it in the Telegram app first, and run:")
        print("  python3 listener.py backfill")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("Usage: python find_similar_channels.py <channel_username> [<channel_username> ...]")
    asyncio.run(main(sys.argv[1:]))
