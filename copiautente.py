#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
copiautente.py — copia tutti i video di un gruppo Telegram in un altro,
passando dal TUO account (non da un bot), cosi vede anche lo storico.

Variabili d'ambiente
  TG_API_ID        obbligatoria  api_id da my.telegram.org
  TG_API_HASH      obbligatoria  api_hash da my.telegram.org
  TG_SESSION       obbligatoria  stringa di sessione (una riga)
  SORGENTE         obbligatoria  id del gruppo da cui copiare  (es. -1003933737714)
  DESTINAZIONE     obbligatoria  id del gruppo dove copiare    (es. -5358964797)
  QUANTI           facoltativa   quanti video copiare per giro (default 300)
  STATO            facoltativa   file di stato (default copia-stato.json)

Uso
  python copiautente.py           copia un blocco e salva il punto
  python copiautente.py --stato   dice a che punto e
  python copiautente.py --azzera  riparte da capo
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from telethon import TelegramClient
from telethon.errors import FloodWaitError, ChatForwardsRestrictedError
from telethon.sessions import StringSession
from telethon.tl.types import InputMessagesFilterVideo

API_ID = int(os.environ.get("TG_API_ID") or 0)
API_HASH = (os.environ.get("TG_API_HASH") or "").strip()
SESSION = (os.environ.get("TG_SESSION") or "").strip()
SORGENTE = int(os.environ.get("SORGENTE") or 0)
DESTINAZIONE = int(os.environ.get("DESTINAZIONE") or 0)
QUANTI = int(os.environ.get("QUANTI") or 300)
STATO = os.environ.get("STATO") or "copia-stato.json"
PAUSA = float(os.environ.get("PAUSA") or 2.5)


def carica() -> dict:
    if os.path.exists(STATO):
        try:
            with open(STATO, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    return {"ultimo": 0, "copiati": 0, "saltati": 0, "doppi": 0}


def salva(s: dict) -> None:
    with open(STATO, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)


async def copia(s: dict) -> int:
    copiati = 0
    async with TelegramClient(StringSession(SESSION), API_ID, API_HASH) as client:
        me = await client.get_me()
        print("connesso come {}".format(getattr(me, "first_name", "") or me.id))
        # cosa c'e gia nel gruppo di destinazione: niente copie doppie
        presenti = set()
        async for m in client.iter_messages(DESTINAZIONE, limit=None):
            doc = getattr(getattr(m, "media", None), "document", None)
            if doc is not None:
                presenti.add(doc.id)
        print("gia presenti nella destinazione: {}".format(len(presenti)))

        # dal piu vecchio al piu nuovo, ripartendo da dove eravamo
        async for msg in client.iter_messages(
                SORGENTE, filter=InputMessagesFilterVideo, reverse=True,
                min_id=int(s.get("ultimo") or 0)):
            if copiati >= QUANTI:
                break
            doc = getattr(getattr(msg, "media", None), "document", None)
            if doc is not None and doc.id in presenti:
                s["doppi"] = s.get("doppi", 0) + 1
                s["ultimo"] = msg.id
                salva(s)
                continue
            while True:
                try:
                    await client.forward_messages(DESTINAZIONE, msg)
                    copiati += 1
                    s["copiati"] = s.get("copiati", 0) + 1
                    s["ultimo"] = msg.id
                    print("copiato msg {} ({}/{})".format(msg.id, copiati, QUANTI))
                    break
                except FloodWaitError as e:
                    attesa = int(getattr(e, "seconds", 30)) + 5
                    print("Telegram chiede di aspettare {} s".format(attesa))
                    await asyncio.sleep(attesa)
                except ChatForwardsRestrictedError:
                    print("msg {} non inoltrrabile (contenuto protetto), salto".format(msg.id))
                    s["saltati"] = s.get("saltati", 0) + 1
                    s["ultimo"] = msg.id
                    break
                except Exception as e:
                    print("msg {} saltato: {}".format(msg.id, type(e).__name__))
                    s["saltati"] = s.get("saltati", 0) + 1
                    s["ultimo"] = msg.id
                    break
            salva(s)
            await asyncio.sleep(PAUSA)
    salva(s)
    print("giro finito: {} copiati, punto {}".format(copiati, s.get("ultimo")))
    return copiati


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--stato", action="store_true")
    p.add_argument("--azzera", action="store_true")
    a = p.parse_args(argv)
    s = carica()
    if a.stato:
        print("ultimo messaggio copiato: {}".format(s.get("ultimo")))
        print("copiati in totale: {} (saltati {}, gia presenti {})".format(s.get("copiati"), s.get("saltati", 0), s.get("doppi", 0)))
        return 0
    if a.azzera:
        salva({"ultimo": 0, "copiati": 0, "saltati": 0})
        print("riparto da capo")
        return 0
    if not (API_ID and API_HASH and SESSION and SORGENTE and DESTINAZIONE):
        print("mancano i dati di accesso o gli id dei gruppi")
        return 2
    asyncio.run(copia(s))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
