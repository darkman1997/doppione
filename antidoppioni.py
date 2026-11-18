#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
antidoppioni.py — Bot Telegram che cancella i video doppioni in un gruppo.

Regola: tiene il primo che arriva, cancella quelli uguali che arrivano dopo.
Riconosce il doppione da:
  1. file_unique_id (stesso file, anche inoltrato);
  2. nome file + dimensione + durata (stessa cosa ricaricata);
Solo libreria standard.

Variabili d'ambiente
  TELEGRAM_TOKEN   obbligatoria  token del bot
  STATO            facoltativa   default doppioni-stato.json
  SECONDI_ASCOLTO  facoltativa   default 420 (quanto resta in ascolto prima di uscire)

Uso
  python antidoppioni.py --loop     ascolta per SECONDI_ASCOLTO secondi e processa
  python antidoppioni.py --stato    stampa il riepilogo
  python antidoppioni.py --pulisci  dimentica i doppioni in memoria (non cancella nulla)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime

TELEGRAM_TOKEN = (os.environ.get("TELEGRAM_TOKEN") or "").strip()
STATO = (os.environ.get("STATO") or "doppioni-stato.json").strip()
SECONDI_ASCOLTO = int(os.environ.get("SECONDI_ASCOLTO") or 420)
TG = "https://api.telegram.org/bot{}/{}"
TTL_GIORNI = 60
ARRETRATO_MAX = 600  # secondi: messaggi piu vecchi vengono ignorati (niente ondate di arretrato)
RECUPERO_MAX_GIRO = 90  # quanti video vecchi ricopiare al massimo in un giro
PAUSA_INOLTRO = 3.0  # secondi tra una copia e l'altra (Telegram: max ~20 messaggi/minuto per gruppo)


def tg(metodo: str, **campi) -> dict:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN mancante")
    dati = urllib.parse.urlencode({k: v for k, v in campi.items() if v is not None}).encode()
    req = urllib.request.Request(TG.format(TELEGRAM_TOKEN, metodo), data=dati)
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def carica() -> dict:
    if not os.path.exists(STATO):
        return {"visti": {}, "ultimo_update": 0, "cancellati": 0, "attivo": True}
    try:
        with open(STATO, encoding="utf-8") as f:
            s = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"visti": {}, "ultimo_update": 0, "cancellati": 0, "attivo": True}
    s.setdefault("visti", {})
    s.setdefault("ultimo_update", 0)
    s.setdefault("cancellati", 0)
    s.setdefault("attivo", True)
    s.setdefault("inoltra_a", [])
    s.setdefault("in_attesa", {})
    s.setdefault("regola", {"video": 10, "minuti": 10})
    s.setdefault("chats", {})
    return s


def salva(s: dict) -> None:
    limite = date.today().toordinal() - TTL_GIORNI
    for k in list(s["visti"]):
        try:
            if date.fromisoformat(s["visti"][k]["d"]).toordinal() < limite:
                s["visti"].pop(k, None)
        except (KeyError, ValueError):
            s["visti"].pop(k, None)
    with open(STATO, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1, sort_keys=True)


# --------------------------------------------------------------------------
# video
# --------------------------------------------------------------------------

def video_del_messaggio(msg: dict) -> dict | None:
    """Restituisce i dati del video contenuto nel messaggio, se c'è."""
    for campo in ("video", "animation", "video_note"):
        if msg.get(campo):
            v = msg[campo]
            return {
                "unico": v.get("file_unique_id") or "",
                "nome": v.get("file_name") or msg.get("caption") or "",
                "dim": v.get("file_size") or v.get("file_size_bytes") or 0,
                "durata": v.get("duration") or 0,
            }
    doc = msg.get("document") or {}
    mime = (doc.get("mime_type") or "").lower()
    if doc and (mime.startswith("video/") or (doc.get("file_name") or "").lower().endswith(
            (".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v"))):
        return {
            "unico": doc.get("file_unique_id") or "",
            "nome": doc.get("file_name") or "",
            "dim": doc.get("file_size") or 0,
            "durata": 0,
        }
    return None


def chiavi(v: dict) -> list[str]:
    """Chiavi con cui riconoscere lo stesso video."""
    k = []
    if v["unico"]:
        k.append("u:" + v["unico"])
    nome = "".join(ch for ch in (v["nome"] or "").lower() if ch.isalnum())
    if nome and v["dim"]:
        k.append("n:{}|{}|{}".format(nome, v["dim"], v["durata"]))
    return k


def e_arretrato(msg: dict) -> bool:
    """True se il messaggio e piu vecchio della soglia: non va toccato."""
    return time.time() - float(msg.get("date") or 0) > ARRETRATO_MAX


def avvisa_doppione(msg: dict, s: dict, chat_id) -> None:
    """Se chi lo ha mandato sta facendo il conto dei 10 video, lo avvisa e scala il conteggio."""
    u = msg.get("from") or {}
    chi = str(u.get("id") or "")
    if chi not in s.get("in_attesa", {}):
        return
    dati = s["in_attesa"][chi]
    dati["video"] = max(0, int(dati.get("video", 0)) - 1)
    mancano = max(1, int(s["regola"]["video"]) - int(dati.get("video", 0)))
    testo = ("{} il video era doppione e l'ho eliminato: te ne mancano ancora {}."
             .format(menzione(u), mancano))
    if mancano == 1:
        testo = ("{} il video era doppione e l'ho eliminato: devi mandare ancora 1 video."
                 .format(menzione(u)))
    _invia(testo, chat_id)
    print("avvisato {} del doppione".format(chi))


def gestisci(msg: dict, s: dict) -> None:
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if not chat_id or e_arretrato(msg):
        return
    v = video_del_messaggio(msg)
    if not v:
        return
    ks = chiavi(v)
    if not ks:
        return
    doppio = next((k for k in ks if k in s["visti"]), None)
    if doppio is None:
        # primo esemplare: lo tengo in memoria e lo inoltro dove richiesto
        for k in ks:
            s["visti"][k] = {"d": date.today().isoformat(), "m": msg.get("message_id"), "c": chat_id}
        inoltra(msg, s, chat_id)
        return
    # doppione: cancello questo messaggio e tengo il primo
    try:
        tg("deleteMessage", chat_id=chat_id, message_id=msg.get("message_id"))
        s["cancellati"] += 1
        print("doppione cancellato in chat {} (msg {})".format(chat_id, msg.get("message_id")))
        avvisa_doppione(msg, s, chat_id)
    except urllib.error.HTTPError as e:
        dettaglio = ""
        try:
            dettaglio = json.loads(e.read() or b"{}").get("description", "")
        except Exception:
            pass
        print("non riesco a cancellare: {} {}".format(e.code, dettaglio))
    except (urllib.error.URLError, OSError) as e:
        print("rete ballerina mentre cancellavo: {}".format(e))


def ricorda_nome(msg: dict, s: dict) -> None:
    """Tiene da parte id e nome utente di chi scrive, per poterlo riammettere dopo."""
    u = msg.get("from") or {}
    uid = u.get("id")
    nome = u.get("username")
    if not uid:
        return
    nomi = s.setdefault("nomi", {})
    nomi[str(uid)] = (nome or "").lower()
    if len(nomi) > 3000:
        for k in list(nomi)[:500]:
            nomi.pop(k, None)


def annota_chat(msg: dict, s: dict) -> None:
    c = msg.get("chat") or {}
    if c.get("id"):
        s["chats"][str(c["id"])] = {"titolo": c.get("title") or c.get("first_name") or "", "tipo": c.get("type")}


def nome_utente(u: dict) -> str:
    nome = u.get("first_name") or u.get("username") or "amico"
    return nome


def menzione(u: dict) -> str:
    """Tag che funziona anche se l'utente non ha username."""
    from html import escape
    return '<a href="tg://user?id={}">{}</a>'.format(u.get("id"), escape(nome_utente(u)))


def _invia(testo: str, chat_id) -> None:
    """Manda un messaggio e, se Telegram frena, aspetta e riprova una volta."""
    for tentativo in (1, 2):
        try:
            tg("sendMessage", chat_id=chat_id, text=testo, parse_mode="HTML")
            return
        except urllib.error.HTTPError as e:
            corpo = {}
            try:
                corpo = json.loads(e.read() or b"{}")
            except Exception:
                pass
            if e.code == 429 and tentativo == 1:
                attesa = int(((corpo.get("parameters") or {}).get("retry_after")) or 10)
                attesa = max(2, min(attesa, 60))
                print("benvenuto: aspetto {} s".format(attesa))
                time.sleep(attesa)
                continue
            print("benvenuto non inviato: {} {}".format(e.code, corpo.get("description", "")))
            return
        except (urllib.error.URLError, OSError) as e:
            print("benvenuto: rete ballerina ({})".format(e))
            return


def benvenuto(msg: dict, s: dict) -> None:
    """Saluta i nuovi arrivati e avvia il conto alla rovescia (uno o tanti)."""
    nuovi = [u for u in (msg.get("new_chat_members") or []) if not u.get("is_bot")]
    if not nuovi:
        return
    chat_id = (msg.get("chat") or {}).get("id")
    video = int(s["regola"]["video"])
    minuti = int(s["regola"]["minuti"])
    for u in nuovi:
        s["in_attesa"][str(u.get("id"))] = {"t": time.time(), "video": 0, "chat": chat_id}

    if len(nuovi) == 1:
        _invia("Benvenuto {}! 🎬 Se non mandi {} video entro {} minuti ti caccio fuori!"
               .format(menzione(nuovi[0]), video, minuti), chat_id)
        print("benvenuto a {}".format(nuovi[0].get("id")))
        return

    # entrati in tanti: un messaggio ogni 20 persone, per non farsi bloccare da Telegram
    for i in range(0, len(nuovi), 20):
        gruppo = nuovi[i:i + 20]
        nomi = ", ".join(menzione(u) for u in gruppo)
        _invia("Benvenuti {}! 🎬 Se non mandate {} video entro {} minuti vi caccio fuori!"
               .format(nomi, video, minuti), chat_id)
        time.sleep(1)
        print("benvenuto a {} nuovi".format(len(gruppo)))


def conta_video(msg: dict, s: dict) -> None:
    chi = str((msg.get("from") or {}).get("id") or "")
    if chi not in s["in_attesa"]:
        return
    if video_del_messaggio(msg):
        s["in_attesa"][chi]["video"] = s["in_attesa"][chi].get("video", 0) + 1


def controlla_scadenze(s: dict) -> None:
    """Caccia chi non ha mandato abbastanza video nel tempo previsto."""
    adesso = time.time()
    video = int(s["regola"]["video"])
    minuti = int(s["regola"]["minuti"])
    for chi, dati in list(s["in_attesa"].items()):
        if adesso - float(dati.get("t", 0)) < minuti * 60:
            continue
        if int(dati.get("video", 0)) >= video:
            s["in_attesa"].pop(chi, None)
            continue
        chat_id = dati.get("chat")
        try:
            tg("banChatMember", chat_id=chat_id, user_id=int(chi))
            s.setdefault("bannati", {})[chi] = {"il": date.today().isoformat(), "chat": chat_id}
            print("cacciato {} e bloccato: non puo rientrare".format(chi))
        except urllib.error.HTTPError as e:
            print("non riesco a cacciare {}: {}".format(chi, e.code))
        s["in_attesa"].pop(chi, None)


def inoltra(msg: dict, s: dict, origine) -> None:
    """Copia il video nei gruppi indicati (senza ricaricare il file)."""
    for destinazione in list(s.get("inoltra_a") or []):
        if str(destinazione) == str(origine):
            continue
        try:
            tg("copyMessage", chat_id=destinazione, from_chat_id=origine,
               message_id=msg.get("message_id"))
            print("inoltrato a {}".format(destinazione))
            time.sleep(PAUSA_INOLTRO)
            continue
        except urllib.error.HTTPError as e:
            corpo = {}
            try:
                corpo = json.loads(e.read() or b"{}")
            except Exception:
                pass
            if e.code == 429:
                attesa = int(((corpo.get("parameters") or {}).get("retry_after")) or 10)
                attesa = max(3, min(attesa, 60))
                print("troppi inoltri, aspetto {} s".format(attesa))
                time.sleep(attesa)
                try:
                    tg("copyMessage", chat_id=destinazione, from_chat_id=origine,
                       message_id=msg.get("message_id"))
                    print("inoltrato a {} (dopo attesa)".format(destinazione))
                    time.sleep(PAUSA_INOLTRO)
                    continue
                except urllib.error.HTTPError as e2:
                    print("inoltro fallito verso {}: {} (dopo attesa)".format(destinazione, e2.code))
                    continue
            print("inoltro fallito verso {}: {} {}".format(
                destinazione, e.code, corpo.get("description", "")))
        except (urllib.error.URLError, OSError) as e:
            print("rete ballerina mentre inoltravo: {}".format(e))
            time.sleep(5)


_COPIATI_GIRO = 0
_RIPRESA = 0.0  # quando il recupero puo riprovare dopo un 429


def avvia_recupero(msg: dict, s: dict) -> None:
    """Mette in coda i video gia visti in questa chat per ricopiarli nelle destinazioni."""
    chat_id = (msg.get("chat") or {}).get("id")
    ids = sorted({int(v["m"]) for v in s["visti"].values()
                  if v.get("c") == chat_id and isinstance(v.get("m"), int)})
    if not ids:
        tg("sendMessage", chat_id=chat_id,
           text="Non ho video in memoria per questa chat: non posso recuperarli.")
        return
    s["recupero"] = {"chat": chat_id, "coda": ids, "pos": 0, "copiati": 0}
    salva(s)
    tg("sendMessage", chat_id=chat_id,
       text="Recupero avviato: {} video da ricopiare, a piccoli blocchi. "
            "Scrivi /annulla per fermarmi.".format(len(ids)))


def recupero(s: dict) -> None:
    """Ricopia a blocchi i video in coda (rispetta i limiti di Telegram)."""
    global _COPIATI_GIRO, _RIPRESA
    if time.time() < _RIPRESA:
        return
    r = s.get("recupero")
    if not r:
        return
    destinazioni = list(s.get("inoltra_a") or [])
    if not destinazioni:
        return
    coda = r.get("coda") or []
    while r["pos"] < len(coda) and _COPIATI_GIRO < RECUPERO_MAX_GIRO:
        mid = coda[r["pos"]]
        for dest in destinazioni:
            try:
                tg("copyMessage", chat_id=dest, from_chat_id=r["chat"], message_id=mid)
                r["copiati"] = r.get("copiati", 0) + 1
                _COPIATI_GIRO += 1
                print("recuperato msg {} -> {}".format(mid, dest))
                time.sleep(PAUSA_INOLTRO)
            except urllib.error.HTTPError as e:
                corpo = {}
                try:
                    corpo = json.loads(e.read() or b"{}")
                except Exception:
                    pass
                if e.code == 429:
                    attesa = int(((corpo.get("parameters") or {}).get("retry_after")) or 20)
                    _RIPRESA = time.time() + max(5, min(attesa, 120))
                    print("recupero: troppi invii, riprendo tra {} s".format(attesa))
                    salva(s)
                    return
                print("recupero: salto msg {} ({})".format(mid, e.code))
        r["pos"] += 1
    if r["pos"] >= len(coda):
        print("recupero finito: {} video".format(r.get("copiati", 0)))
        s.pop("recupero", None)
    salva(s)


def comandi(msg: dict, s: dict) -> None:
    testo = (msg.get("text") or "").strip()
    if not testo.startswith("/"):
        return
    chat_id = (msg.get("chat") or {}).get("id")
    cmd = testo.split()[0].split("@")[0].lower()
    if cmd in ("/start", "/aiuto", "/help"):
        tg("sendMessage", chat_id=chat_id,
           text=("<b>Anti-doppioni</b>\nTengo il primo video che arriva e cancello le copie.\n\n"
                 "/stato — quanti doppioni ho cancellato\n"
                 "/inoltra — copia qui i video nuovi degli altri gruppi\n"
                 "/stopinoltra — smetti di copiarli qui\n"
                 "/recupera — ricopia i video gia visti in questa chat\n"
                 "/annulla — ferma il recupero\n"
                 "/attiva e /disattiva — accendo o spengo\n\n"
                 "Mi servono i permessi di amministratore con \'elimina messaggi\'."),
           parse_mode="HTML")
    elif cmd == "/stato":
        tg("sendMessage", chat_id=chat_id,
           text="Doppioni cancellati: {}\nVideo in memoria: {}\nAttivo: {}\nGruppi in cui inoltro: {}".format(
               s["cancellati"], len(s["visti"]), "sì" if s["attivo"] else "no",
               len(s.get("inoltra_a") or [])))
    elif cmd in ("/qui", "/chat"):
        c = msg.get("chat") or {}
        info = tg("getChat", chat_id=c.get("id"))
        me = tg("getMe")
        mio = (me.get("result") or {}).get("id")
        stato = ""
        try:
            membro = tg("getChatMember", chat_id=c.get("id"), user_id=mio)
            r = (membro.get("result") or {})
            stato = r.get("status") or ""
            if stato == "administrator":
                stato += " (cancella: {}; banna: {})".format(
                    "sì" if r.get("can_delete_messages") else "no",
                    "sì" if r.get("can_restrict_members") else "no")
        except urllib.error.HTTPError as e:
            stato = "non riesco a leggere i miei permessi ({})".format(e.code)
        titolo = (info.get("result") or {}).get("title") or ""
        tg("sendMessage", chat_id=c.get("id"),
           text="Chat: {}\nTitolo: {}\nIo sono: {}".format(c.get("id"), titolo, stato))
    elif cmd in ("/regola", "/regole"):
        pezzi = testo.split()
        if len(pezzi) >= 3 and pezzi[1].isdigit() and pezzi[2].isdigit():
            s["regola"] = {"video": int(pezzi[1]), "minuti": int(pezzi[2])}
            tg("sendMessage", chat_id=chat_id,
               text="Regola aggiornata: {} video entro {} minuti.".format(pezzi[1], pezzi[2]))
        else:
            tg("sendMessage", chat_id=chat_id,
               text="Regola attuale: {} video entro {} minuti.\nPer cambiarla: /regola 10 10".format(
                   s["regola"]["video"], s["regola"]["minuti"]))
    elif cmd in ("/foto", "/nofoto"):
        s["cancella_foto"] = not s.get("cancella_foto", True)
        salva(s)
        tg("sendMessage", chat_id=chat_id,
           text="Cancellazione foto: " + ("attiva ✅" if s["cancella_foto"] else "spenta ⏸"))
    elif cmd in ("/bannati", "/bloccati"):
        b = s.get("bannati") or {}
        if not b:
            tg("sendMessage", chat_id=chat_id, text="Nessuno bloccato.")
        else:
            righe = ["{} — dal {}".format(k, v.get("il", "?")) for k, v in list(b.items())[:40]]
            tg("sendMessage", chat_id=chat_id,
               text="Bloccati:\n" + "\n".join(righe) +
                    "\n\nPer riammettere: /riammetti id")
    elif cmd in ("/riammetti", "/sblocca"):
        pezzi = testo.split()
        uid = None
        if len(pezzi) > 1:
            solo = pezzi[1].lstrip("@").lower()
            if solo.isdigit():
                uid = int(solo)
            elif solo:
                for k, v in (s.get("nomi") or {}).items():
                    if v == solo:
                        uid = int(k)
                        break
        if uid is None:
            risposta = msg.get("reply_to_message") or {}
            uid = (risposta.get("from") or {}).get("id")
        if not uid:
            tg("sendMessage", chat_id=chat_id,
               text="Scrivimi /riammetti come risposta a un suo messaggio, "
                    "oppure /riammetti @nome o /riammetti id.")
        else:
            try:
                tg("unbanChatMember", chat_id=chat_id, user_id=int(uid), only_if_banned="true")
                (s.get("bannati") or {}).pop(str(uid), None)
                salva(s)
                tg("sendMessage", chat_id=chat_id,
                   text="Riammesso: {} può rientrare.".format(uid))
            except urllib.error.HTTPError as e:
                tg("sendMessage", chat_id=chat_id,
                   text="Non riesco a riammettere ({}): mi serve il permesso di bannare.".format(e.code))
    elif cmd in ("/recupera", "/ricopia"):
        avvia_recupero(msg, s)
    elif cmd in ("/annulla", "/stoprecupero"):
        if s.get("recupero"):
            s.pop("recupero", None)
            salva(s)
            tg("sendMessage", chat_id=chat_id, text="Recupero fermato.")
        else:
            tg("sendMessage", chat_id=chat_id, text="Nessun recupero in corso.")
    elif cmd == "/inoltra":
        if chat_id not in s["inoltra_a"]:
            s["inoltra_a"].append(chat_id)
        tg("sendMessage", chat_id=chat_id,
           text="Da qui in poi copio qui i video nuovi che arrivano negli altri gruppi ✅")
    elif cmd in ("/stopinoltra", "/noninoltrare"):
        if chat_id in s["inoltra_a"]:
            s["inoltra_a"].remove(chat_id)
        tg("sendMessage", chat_id=chat_id, text="Va bene, non ti copio più niente qui.")
    elif cmd in ("/attiva", "/disattiva"):
        s["attivo"] = cmd == "/attiva"
        tg("sendMessage", chat_id=chat_id, text="Ora sono " + ("attivo ✅" if s["attivo"] else "spento ⏸"))


def aggiorna_admin(s: dict) -> None:
    """Ricorda chi sono gli amministratori di ogni gruppo (le loro foto non si toccano)."""
    for chat in list((s.get("chats") or {}).keys()):
        try:
            ris = tg("getChatAdministrators", chat_id=int(chat))
            s.setdefault("admin", {})[chat] = [
                (m.get("user") or {}).get("id") for m in ris.get("result", [])
                if (m.get("user") or {}).get("id")
            ]
        except Exception as e:
            print("admin di {} non letti: {}".format(chat, e))


def cancella_foto(msg: dict, s: dict) -> None:
    """Cancella le foto mandate dai membri semplici (non dagli amministratori)."""
    if not s.get("cancella_foto", True) or not msg.get("photo"):
        return
    chat_id = (msg.get("chat") or {}).get("id")
    chi = (msg.get("from") or {}).get("id")
    if not chat_id or not chi:
        return
    if chi in (s.get("admin", {}).get(str(chat_id)) or []):
        return
    try:
        tg("deleteMessage", chat_id=chat_id, message_id=msg.get("message_id"))
        s["foto_cancellate"] = s.get("foto_cancellate", 0) + 1
        print("foto cancellata in {} (msg {})".format(chat_id, msg.get("message_id")))
    except urllib.error.HTTPError as e:
        print("foto non cancellata: {} {}".format(e.code, ""))
    except (urllib.error.URLError, OSError) as e:
        print("rete ballerina mentre cancellavo la foto: {}".format(e))


def ciclo(s: dict, secondi: int) -> None:
    global _COPIATI_GIRO
    _COPIATI_GIRO = 0
    fine = time.time() + secondi
    if not s.get("ultimo_update"):
        # primo avvio: salta tutto quello che si e accumulato, parti da adesso
        try:
            ris = tg("getUpdates", offset=-1, limit=1, timeout=0)
            for upd in ris.get("result", []):
                s["ultimo_update"] = upd["update_id"] + 1
            print("arretrato saltato, riparto da update {}".format(s["ultimo_update"]))
        except Exception as e:
            print("salto arretrato non riuscito: {}".format(e))
    aggiorna_admin(s)
    while time.time() < fine:
        try:
            ris = tg("getUpdates", offset=s["ultimo_update"] or None, timeout=20)
        except (urllib.error.URLError, RuntimeError) as e:
            print("getUpdates: {}".format(e))
            time.sleep(5)
            continue
        for upd in ris.get("result", []):
            s["ultimo_update"] = upd["update_id"] + 1
            msg = upd.get("message") or upd.get("channel_post") or {}
            if msg and e_arretrato(msg):
                # messaggio vecchio (arretrato): non lo cancello, non lo inoltro, non do il benvenuto
                continue
            if msg:
                try:
                    annota_chat(msg, s)
                    ricorda_nome(msg, s)
                    cancella_foto(msg, s)
                    benvenuto(msg, s)
                    conta_video(msg, s)
                    if s["attivo"]:
                        gestisci(msg, s)
                    comandi(msg, s)
                except Exception as e:  # un messaggio non deve fermare l'ascolto
                    print("salto un messaggio: {}".format(e))
        controlla_scadenze(s)
        recupero(s)
        salva(s)
        time.sleep(1)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Anti-doppioni per gruppi Telegram")
    p.add_argument("--loop", action="store_true", help="ascolta e cancella (default)")
    p.add_argument("--stato", action="store_true", help="riepilogo")
    p.add_argument("--pulisci", action="store_true", help="svuota la memoria dei video visti")
    a = p.parse_args(argv)

    s = carica()
    if a.stato:
        print("doppioni cancellati: {}".format(s["cancellati"]))
        print("video in memoria: {}".format(len(s["visti"])))
        print("attivo: {}".format(s["attivo"]))
        return 0
    if a.pulisci:
        s["visti"] = {}
        controlla_scadenze(s)
        recupero(s)
        salva(s)
        time.sleep(1)
        print("memoria svuotata")
        return 0
    ciclo(s, SECONDI_ASCOLTO)
    salva(s)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
