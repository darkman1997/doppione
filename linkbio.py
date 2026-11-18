#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Manda "Link in bio" nel gruppo indicato ogni tot secondi, col suo account.

Variabili: TG_API_ID, TG_API_HASH, TG_SESSION, CHAT (id del gruppo),
TESTO, OGNI (secondi, default 1800), DURATA (secondi, default 20700 = 5h45m)
"""
import asyncio, os, sys, time
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError

API_ID = int(os.environ.get("TG_API_ID") or 0)
API_HASH = (os.environ.get("TG_API_HASH") or "").strip()
SESSION = (os.environ.get("TG_SESSION") or "").strip()
CHAT = int(os.environ.get("CHAT") or 0)
TESTO = os.environ.get("TESTO") or "🔗 Link in bio"
OGNI = int(os.environ.get("OGNI") or 1800)
DURATA = int(os.environ.get("DURATA") or 20700)


async def main() -> int:
    fine = time.time() + DURATA
    async with TelegramClient(StringSession(SESSION), API_ID, API_HASH) as c:
        ent = await c.get_entity(CHAT)
        print("gruppo:", getattr(ent, "title", CHAT))
        while time.time() < fine:
            try:
                await c.send_message(ent, TESTO)
                print("mandato", time.strftime("%H:%M:%S", time.gmtime()), flush=True)
            except FloodWaitError as e:
                print("Telegram chiede di aspettare", e.seconds, "s", flush=True)
                await asyncio.sleep(e.seconds + 5)
                continue
            except Exception as e:
                print("non riuscito:", type(e).__name__, str(e)[:80], flush=True)
                return 1
            await asyncio.sleep(OGNI)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
