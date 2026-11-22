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
BENVENUTO_AUTO_ELIMINA_SEC = 360  # cancella il messaggio di benvenuto 6 minuti dopo l'invio
AVVISO_DEBOUNCE_SEC = 5  # aspetta qualche secondo per raggruppare raffiche di doppioni in un solo avviso
AVVISO_MAX_ATTESA_SEC = 15  # se la raffica continua, aggiorna comunque entro 15s dal primo doppione
AVVISO_AGGIORNA_MIN_SEC = 10  # intervallo minimo tra un edit e il successivo sullo stesso avviso
AVVISO_AUTO_ELIMINA_SEC = 120  # cancella l'avviso doppioni 2 minuti dopo l'ultimo aggiornamento


def tg(metodo: str, **campi) -> dict:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN mancante")
    dati = urllib.parse.urlencode({k: v for k, v in campi.items() if v is not None}).encode()
    req = urllib.request.Request(TG.format(TELEGRAM_TOKEN, metodo), data=dati)
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def carica() -> dict:
    s = {}
    if os.path.exists(STATO):
        try:
            with open(STATO, encoding="utf-8") as f:
                s = json.load(f)
        except (OSError, json.JSONDecodeError):
            s = {}
    s.setdefault("visti", {})
    s.setdefault("ultimo_update", 0)
    s.setdefault("cancellati", 0)
    s.setdefault("attivo", True)
    s.setdefault("inoltra_a", [])
    s.setdefault("in_attesa", {})
    s.setdefault("regola", {"video": 10, "minuti": 10})
    s.setdefault("chats", {})
    s.setdefault("esclusi", {})
    s.pop("testi_servizio", None)
    s.pop("pulizia", None)
    s.setdefault("cancella_servizio", True)
    s.setdefault("benvenuti_da_cancellare", [])
    s.setdefault("avvisi_doppioni", {})
    s.setdefault("coda_cancellazioni", [])
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


_CANCELLA_RIPRESA = 0.0
_AVVISO_RIPRESA = 0.0
CANCELLA_MAX_LOTE = 25


def _accoda_cancellazione(chat_id, mid: int, s: dict) -> None:
    if not chat_id or not mid:
        return
    coda = s.setdefault("coda_cancellazioni", [])
    if any(str(v.get("c")) == str(chat_id) and int(v.get("m") or 0) == int(mid) for v in coda):
        return
    coda.append({"c": chat_id, "m": int(mid)})
    if len(coda) > 2000:
        del coda[:len(coda) - 2000]


def _elimina_o_accoda(chat_id, mid: int | None, s: dict) -> bool:
    """Prova a cancellare subito; se Telegram frena (429/rete), accoda senza bloccare."""
    global _CANCELLA_RIPRESA
    if not chat_id or not mid:
        return False
    if time.time() < _CANCELLA_RIPRESA:
        _accoda_cancellazione(chat_id, int(mid), s)
        return True
    try:
        tg("deleteMessage", chat_id=chat_id, message_id=int(mid))
        return True
    except urllib.error.HTTPError as e:
        corpo = {}
        try:
            corpo = json.loads(e.read() or b"{}")
        except Exception:
            pass
        if e.code == 429:
            attesa = int(((corpo.get("parameters") or {}).get("retry_after")) or 10)
            _CANCELLA_RIPRESA = time.time() + max(2, min(attesa, 60))
            _accoda_cancellazione(chat_id, int(mid), s)
            print("troppe cancellazioni, accodo msg {} (pausa {} s)".format(mid, attesa))
            return True
        print("non riesco a cancellare: {} {}".format(e.code, corpo.get("description", "")))
        return False
    except (urllib.error.URLError, OSError) as e:
        _accoda_cancellazione(chat_id, int(mid), s)
        print("rete ballerina mentre cancellavo (accodato {}): {}".format(mid, e))
        return True


def processa_cancellazioni(s: dict) -> None:
    """Svuota a blocchi eventuali cancellazioni rimandate da un 429."""
    global _CANCELLA_RIPRESA
    if time.time() < _CANCELLA_RIPRESA:
        return
    coda = s.get("coda_cancellazioni") or []
    if not coda:
        return
    fatti = 0
    while coda and fatti < CANCELLA_MAX_LOTE:
        voce = coda[0]
        try:
            tg("deleteMessage", chat_id=voce["c"], message_id=int(voce["m"]))
            coda.pop(0)
            fatti += 1
        except urllib.error.HTTPError as e:
            corpo = {}
            try:
                corpo = json.loads(e.read() or b"{}")
            except Exception:
                pass
            if e.code == 429:
                attesa = int(((corpo.get("parameters") or {}).get("retry_after")) or 10)
                _CANCELLA_RIPRESA = time.time() + max(2, min(attesa, 60))
                print("cancellazioni in coda: pausa {} s (restano {})".format(attesa, len(coda)))
                return
            coda.pop(0)
        except (urllib.error.URLError, OSError) as e:
            print("rete ballerina in processa_cancellazioni: {}".format(e))
            return


def _testo_avviso_doppione(voce: dict, s: dict) -> str:
    chi = str(voce.get("chi") or "")
    dati = (s.get("in_attesa") or {}).get(chi) or {}
    obiettivo = int((s.get("regola") or {}).get("video", 10))
    mancano = max(1, obiettivo - int(dati.get("video", 0)))
    scartati = max(1, int(voce.get("scartati", 1)))
    chi_tag = menzione(voce.get("u") or {"id": chi})
    coda_mancano = (
        "devi mandare ancora 1 video."
        if mancano == 1
        else "te ne mancano ancora {}.".format(mancano)
    )
    if scartati == 1:
        return "{} il video era doppione e l'ho eliminato: {}".format(chi_tag, coda_mancano)
    return "{} ho eliminato {} video doppioni: {}".format(chi_tag, scartati, coda_mancano)


def _invia_avviso_non_bloccante(testo: str, chat_id) -> tuple[int | None, int]:
    """Invia senza bloccare il ciclo; se Telegram frena (429), restituisce retry_after."""
    try:
        ris = tg("sendMessage", chat_id=chat_id, text=testo, parse_mode="HTML")
        mid = (ris.get("result") or {}).get("message_id")
        return (mid if isinstance(mid, int) else None), 0
    except urllib.error.HTTPError as e:
        corpo = {}
        try:
            corpo = json.loads(e.read() or b"{}")
        except Exception:
            pass
        if e.code == 429:
            attesa = int(((corpo.get("parameters") or {}).get("retry_after")) or 10)
            return None, max(3, min(attesa, 60))
        print("avviso doppione non inviato: {} {}".format(e.code, corpo.get("description", "")))
        return None, 0
    except (urllib.error.URLError, OSError) as e:
        print("avviso doppione: rete ballerina ({})".format(e))
        return None, 5


def _modifica_avviso(testo: str, chat_id, mid: int) -> str:
    try:
        tg("editMessageText", chat_id=chat_id, message_id=int(mid), text=testo, parse_mode="HTML")
        return "ok"
    except urllib.error.HTTPError as e:
        corpo = {}
        try:
            corpo = json.loads(e.read() or b"{}")
        except Exception:
            pass
        desc = (corpo.get("description") or "").lower()
        if e.code == 429:
            attesa = int(((corpo.get("parameters") or {}).get("retry_after")) or 10)
            return "429:{}".format(max(3, min(attesa, 60)))
        if "message is not modified" in desc:
            return "not_modified"
        return "error"
    except (urllib.error.URLError, OSError):
        return "retry"


def avvisa_doppione(msg: dict, s: dict, chat_id) -> None:
    """Scala il conteggio e raggruppa gli avvisi doppioni per utente invece di spammare la chat."""
    u = msg.get("from") or {}
    chi = str(u.get("id") or "")
    if chi not in s.get("in_attesa", {}):
        return
    dati = s["in_attesa"][chi]
    dati["video"] = max(0, int(dati.get("video", 0)) - 1)
    adesso = time.time()
    chiave = "{}:{}".format(chat_id, chi)
    avvisi = s.setdefault("avvisi_doppioni", {})
    voce = avvisi.get(chiave)
    u_min = {
        "id": u.get("id"),
        "first_name": u.get("first_name"),
        "username": u.get("username"),
    }
    if not voce:
        voce = {
            "chat": chat_id,
            "chi": chi,
            "u": u_min,
            "scartati": 0,
            "primo_dup": adesso,
            "ultimo_dup": adesso,
            "mid": None,
            "ultimo_invio": 0.0,
            "inviati_scartati": 0,
            "scadenza_del": 0.0,
        }
        avvisi[chiave] = voce
    else:
        voce["u"] = u_min
    voce["scartati"] = int(voce.get("scartati", 0)) + 1
    if not voce.get("primo_dup"):
        voce["primo_dup"] = adesso
    voce["ultimo_dup"] = adesso


def processa_avvisi_doppioni(s: dict) -> None:
    """Invia/aggiorna un solo messaggio riepilogativo per utente e lo cancella dopo 2 minuti."""
    global _AVVISO_RIPRESA
    avvisi = s.get("avvisi_doppioni") or {}
    if not avvisi:
        return
    adesso = time.time()
    for chiave, voce in list(avvisi.items()):
        chat_id = voce.get("chat")
        chi = str(voce.get("chi") or "")
        mid = voce.get("mid")

        if chi not in (s.get("in_attesa") or {}):
            if mid:
                _elimina_o_accoda(chat_id, int(mid), s)
            avvisi.pop(chiave, None)
            continue

        scartati = int(voce.get("scartati", 0))
        inviati = int(voce.get("inviati_scartati", 0))
        scadenza = float(voce.get("scadenza_del") or 0)

        if mid and scadenza and adesso >= scadenza and scartati <= inviati:
            _elimina_o_accoda(chat_id, int(mid), s)
            avvisi.pop(chiave, None)
            print("avviso doppioni {} rimosso dopo 2 min in {}".format(mid, chat_id))
            continue

        if scartati <= inviati:
            continue

        pausa_ok = adesso - float(voce.get("ultimo_dup") or 0) >= AVVISO_DEBOUNCE_SEC
        attesa_max_ok = float(voce.get("primo_dup") or 0) > 0 and (
            adesso - float(voce.get("primo_dup") or 0) >= AVVISO_MAX_ATTESA_SEC
        )
        if not (pausa_ok or attesa_max_ok):
            continue
        if adesso < _AVVISO_RIPRESA:
            continue

        testo = _testo_avviso_doppione(voce, s)
        if mid:
            if adesso - float(voce.get("ultimo_invio") or 0) < AVVISO_AGGIORNA_MIN_SEC:
                continue
            esito = _modifica_avviso(testo, chat_id, int(mid))
            if esito.startswith("429:"):
                _AVVISO_RIPRESA = adesso + int(esito.split(":", 1)[1])
                continue
            if esito in ("ok", "not_modified"):
                voce["ultimo_invio"] = adesso
                voce["inviati_scartati"] = scartati
                voce["scadenza_del"] = adesso + AVVISO_AUTO_ELIMINA_SEC
                voce["primo_dup"] = 0.0
                print("avviso doppioni {} aggiornato per {} ({} scartati)".format(mid, chi, scartati))
                continue
            if esito == "retry":
                continue
            voce["mid"] = None

        nuovo_mid, retry_after = _invia_avviso_non_bloccante(testo, chat_id)
        if retry_after:
            _AVVISO_RIPRESA = adesso + retry_after
            continue
        if nuovo_mid:
            voce["mid"] = nuovo_mid
            voce["ultimo_invio"] = adesso
            voce["inviati_scartati"] = scartati
            voce["scadenza_del"] = adesso + AVVISO_AUTO_ELIMINA_SEC
            voce["primo_dup"] = 0.0
            print("avviso doppioni {} inviato per {} ({} scartati)".format(nuovo_mid, chi, scartati))
        elif not mid:
            # evito loop infiniti se non posso scrivere in chat
            voce["inviati_scartati"] = scartati
            voce["primo_dup"] = 0.0
            avvisi.pop(chiave, None)


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
        # primo esemplare: lo tengo in memoria e lo metto in coda inoltro (non bloccante)
        for k in ks:
            s["visti"][k] = {"d": date.today().isoformat(), "m": msg.get("message_id"), "c": chat_id}
        u_id = str((msg.get("from") or {}).get("id") or "")
        if u_id in s.get("in_attesa", {}):
            obiettivo = int((s.get("regola") or {}).get("video", 10))
            if int(s["in_attesa"][u_id].get("video", 0)) >= obiettivo:
                s["in_attesa"].pop(u_id, None)
                print("utente {} ha completato i {} video ed e' salvo".format(u_id, obiettivo))
        accoda_inoltro(msg, s, chat_id)
        return
    # doppione: cancello questo messaggio (o lo accodo se Telegram frena) e tengo il primo
    if _elimina_o_accoda(chat_id, msg.get("message_id"), s):
        s["cancellati"] += 1
        print("doppione cancellato in chat {} (msg {})".format(chat_id, msg.get("message_id")))
        avvisa_doppione(msg, s, chat_id)


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


CAMPI_MEDIA = (
    "video", "photo", "animation", "video_note",
    "document", "audio", "voice", "sticker",
    "paid_media", "story",
)

CAMPI_SERVIZIO = (
    "new_chat_members", "left_chat_member", "new_chat_title",
    "new_chat_photo", "delete_chat_photo", "group_chat_created",
    "supergroup_chat_created", "channel_chat_created",
    "message_auto_delete_timer_changed", "migrate_to_chat_id",
    "migrate_from_chat_id", "pinned_message", "invoice",
    "successful_payment", "users_shared", "chat_shared",
    "connected_website", "write_access_allowed",
    "proximity_alert_triggered", "boost_added",
    "chat_background_set", "forum_topic_created",
    "forum_topic_edited", "forum_topic_closed",
    "forum_topic_reopened", "general_forum_topic_hidden",
    "general_forum_topic_unhidden", "giveaway_created",
    "giveaway_started", "giveaway_completed",
    "video_chat_scheduled", "video_chat_started",
    "video_chat_ended", "video_chat_participants_invited",
    "web_app_data",
)


def ha_media(msg: dict) -> bool:
    return any(msg.get(k) for k in CAMPI_MEDIA)


def e_servizio(msg: dict) -> bool:
    if ha_media(msg):
        return False
    return any(msg.get(k) is not None for k in CAMPI_SERVIZIO)


def _invia(testo: str, chat_id) -> int | None:
    """Manda un messaggio e, se Telegram frena, aspetta e riprova una volta."""
    for tentativo in (1, 2):
        try:
            ris = tg("sendMessage", chat_id=chat_id, text=testo, parse_mode="HTML")
            mid = (ris.get("result") or {}).get("message_id")
            return mid if isinstance(mid, int) else None
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
            return None
        except (urllib.error.URLError, OSError) as e:
            print("benvenuto: rete ballerina ({})".format(e))
            return None
    return None


def benvenuto(msg: dict, s: dict) -> None:
    """Saluta i nuovi arrivati in italiano e avvia il conto alla rovescia."""
    esclusi = s.get("esclusi") or {}
    tutti_nuovi = [u for u in (msg.get("new_chat_members") or []) if not u.get("is_bot")]
    for u in tutti_nuovi:
        uid = u.get("id")
        if uid and u.get("username"):
            s.setdefault("nomi", {})[str(uid)] = (u.get("username") or "").lower()
    nuovi = [u for u in tutti_nuovi if str(u.get("id")) not in esclusi]
    if not nuovi:
        return
    chat_id = (msg.get("chat") or {}).get("id")
    video = int(s["regola"]["video"])
    minuti = int(s["regola"]["minuti"])
    t_ingresso = float(msg.get("date") or time.time())
    for u in nuovi:
        s["in_attesa"][str(u.get("id"))] = {"t": t_ingresso, "video": 0, "chat": chat_id}

    if len(nuovi) == 1:
        u = nuovi[0]
        mid_benv = _invia(
            "Benvenuto {}! 🎬 Se non mandi {} video entro {} minuti ti caccio fuori!"
            .format(menzione(u), video, minuti),
            chat_id,
        )
        if mid_benv:
            s.setdefault("benvenuti_da_cancellare", []).append({
                "chat": chat_id, "mid": mid_benv, "t": time.time(),
            })
        print("benvenuto a {}".format(u.get("id")))
        return

    # entrati in tanti: un messaggio ogni 20 persone, per non farsi bloccare da Telegram
    for i in range(0, len(nuovi), 20):
        gruppo = nuovi[i:i + 20]
        nomi = ", ".join(menzione(u) for u in gruppo)
        mid_benv = _invia(
            "Benvenuti {}! 🎬 Se non mandate {} video entro {} minuti vi caccio fuori!"
            .format(nomi, video, minuti),
            chat_id,
        )
        if mid_benv:
            s.setdefault("benvenuti_da_cancellare", []).append({
                "chat": chat_id, "mid": mid_benv, "t": time.time(),
            })
        time.sleep(1)
        print("benvenuto a {} nuovi".format(len(gruppo)))


_BENVENUTI_RIPRESA = 0.0


def cancella_benvenuti_scaduti(s: dict) -> None:
    """Cancella i messaggi di benvenuto trascorsi 6 minuti (360 s) dall'invio."""
    global _BENVENUTI_RIPRESA
    if time.time() < _BENVENUTI_RIPRESA:
        return
    coda = s.get("benvenuti_da_cancellare") or []
    if not coda:
        return
    adesso = time.time()
    rimasti = []
    modificato = False
    for idx, voce in enumerate(coda):
        t_invio = float(voce.get("t") or 0)
        if adesso - t_invio < BENVENUTO_AUTO_ELIMINA_SEC:
            rimasti.append(voce)
            continue
        chat_id = voce.get("chat")
        mid = voce.get("mid")
        if not chat_id or not mid:
            modificato = True
            continue
        try:
            tg("deleteMessage", chat_id=chat_id, message_id=int(mid))
            print("benvenuto {} cancellato dopo 6 min in {}".format(mid, chat_id))
            modificato = True
        except urllib.error.HTTPError as e:
            corpo = {}
            try:
                corpo = json.loads(e.read() or b"{}")
            except Exception:
                pass
            if e.code == 429:
                attesa = int(((corpo.get("parameters") or {}).get("retry_after")) or 10)
                _BENVENUTI_RIPRESA = time.time() + max(5, min(attesa, 60))
                rimasti.extend(coda[idx:])
                modificato = True
                break
            print("benvenuto {} non cancellato ({}): {}".format(mid, e.code, corpo.get("description", "")))
            modificato = True
        except (urllib.error.URLError, OSError) as e:
            print("rete ballerina mentre cancellavo benvenuto {}: {}".format(mid, e))
            rimasti.extend(coda[idx:])
            modificato = True
            break
    if modificato:
        s["benvenuti_da_cancellare"] = rimasti


def conta_video(msg: dict, s: dict) -> None:
    chi = str((msg.get("from") or {}).get("id") or "")
    if chi not in s["in_attesa"]:
        return
    if not video_del_messaggio(msg):
        return
    dati = s["in_attesa"][chi]
    dati["video"] = int(dati.get("video", 0)) + 1
    dati["ultimo_msg"] = float(msg.get("date") or time.time())


def controlla_scadenze(s: dict) -> None:
    """Caccia chi non ha mandato abbastanza video nel tempo previsto."""
    adesso = time.time()
    video = int(s["regola"]["video"])
    minuti = int(s["regola"]["minuti"])
    for chi, dati in list(s["in_attesa"].items()):
        contati = int(dati.get("video", 0))
        if contati >= video:
            s["in_attesa"].pop(chi, None)
            continue
        if adesso - float(dati.get("t", 0)) < minuti * 60 + 30:
            continue
        chat_id = dati.get("chat")
        try:
            tg("banChatMember", chat_id=chat_id, user_id=int(chi))
            s.setdefault("bannati", {})[chi] = {"il": date.today().isoformat(), "chat": chat_id}
            print("cacciato {} ({}/{} video): non puo rientrare".format(chi, contati, video))
        except urllib.error.HTTPError as e:
            print("non riesco a cacciare {}: {}".format(chi, e.code))
        s["in_attesa"].pop(chi, None)


_INOLTRO_RIPRESA = 0.0
INOLTRO_MAX_LOTE = 5


def accoda_inoltro(msg: dict, s: dict, origine) -> None:
    """Mette in coda il video per inoltrarlo a blocchi senza bloccare la lettura dei messaggi."""
    mid = msg.get("message_id")
    if not mid:
        return
    coda = s.setdefault("coda_inoltro", [])
    for destinazione in list(s.get("inoltra_a") or []):
        if str(destinazione) != str(origine):
            coda.append({"d": destinazione, "f": origine, "m": mid})
    if len(coda) > 2000:
        del coda[:len(coda) - 2000]


def processa_inoltri(s: dict) -> None:
    """Inoltra al massimo INOLTRO_MAX_LOTE video per giro, cosi getUpdates resta reattivo."""
    global _INOLTRO_RIPRESA
    if time.time() < _INOLTRO_RIPRESA:
        return
    coda = s.get("coda_inoltro") or []
    if not coda:
        return
    fatti = 0
    while coda and fatti < INOLTRO_MAX_LOTE:
        voce = coda[0]
        try:
            tg("copyMessage", chat_id=voce["d"], from_chat_id=voce["f"], message_id=voce["m"])
            coda.pop(0)
            fatti += 1
            print("inoltrato a {}".format(voce["d"]))
            time.sleep(PAUSA_INOLTRO)
        except urllib.error.HTTPError as e:
            corpo = {}
            try:
                corpo = json.loads(e.read() or b"{}")
            except Exception:
                pass
            if e.code == 429:
                attesa = int(((corpo.get("parameters") or {}).get("retry_after")) or 15)
                _INOLTRO_RIPRESA = time.time() + max(5, min(attesa, 90))
                print("troppi inoltri, riprendo tra {} s (restano {})".format(attesa, len(coda)))
                return
            print("inoltro fallito verso {}: {} {}".format(voce["d"], e.code, corpo.get("description", "")))
            coda.pop(0)
        except (urllib.error.URLError, OSError) as e:
            print("rete ballerina mentre inoltravo: {}".format(e))
            return


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


COMANDI_MENU = [
    {"command": "menu", "description": "Apri il pannello comandi a bottoni"},
    {"command": "stato", "description": "Doppioni cancellati, memoria e stato"},
    {"command": "attiva", "description": "Attiva il controllo anti-doppioni"},
    {"command": "disattiva", "description": "Metti in pausa anti-doppioni"},
    {"command": "foto", "description": "Attiva o spegni cancellazione foto"},
    {"command": "regola", "description": "Vedi o cambia regola video/minuti"},
    {"command": "escludi", "description": "Escludi utente dal conteggio video"},
    {"command": "bannati", "description": "Lista utenti bloccati"},
    {"command": "riammetti", "description": "Sblocca utente (rispondi o @nome/id)"},
    {"command": "inoltra", "description": "Copia qui i video degli altri gruppi"},
    {"command": "stopinoltra", "description": "Ferma inoltro video in questa chat"},
    {"command": "recupera", "description": "Ricopia qui i video gia visti"},
    {"command": "annulla", "description": "Ferma il recupero in corso"},
    {"command": "qui", "description": "Controlla ID e permessi del bot"},
    {"command": "aiuto", "description": "Guida ai comandi"},
]


def registra_comandi() -> None:
    try:
        tg("setMyCommands", commands=json.dumps(COMANDI_MENU, ensure_ascii=False))
    except Exception as e:
        print("setMyCommands: {}".format(e))


def tastiera_privata() -> dict:
    return {
        "keyboard": [
            [{"text": "🎛 Menu bottoni"}, {"text": "📊 Stato"}],
            [{"text": "⚡ Attiva / Disattiva"}, {"text": "🖼 Foto sì / no"}],
            [{"text": "🚫 Bannati"}, {"text": "❓ Aiuto"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def _testo_menu(s: dict) -> str:
    regola = s.get("regola") or {"video": 10, "minuti": 10}
    foto_on = s.get("cancella_foto", True)
    servizio_on = s.get("cancella_servizio", True)
    return (
        "<b>🎛 Pannello Anti-doppioni</b>\n"
        "Stato: <b>{}</b> · Foto non-admin: <b>{}</b> · Servizio: <b>{}</b>\n"
        "Regola nuovi: <b>{} video / {} min</b> · Doppioni tolti: <b>{}</b>"
    ).format(
        "attivo ✅" if s.get("attivo", True) else "spento ⏸",
        "cancellate ✅" if foto_on else "consentite ⏸",
        "cancellati ✅" if servizio_on else "visibili ⏸",
        regola.get("video", 10),
        regola.get("minuti", 10),
        s.get("cancellati", 0),
    )


def _markup_menu(s: dict) -> dict:
    attivo = s.get("attivo", True)
    foto_on = s.get("cancella_foto", True)
    servizio_on = s.get("cancella_servizio", True)
    regola = s.get("regola") or {"video": 10, "minuti": 10}
    bannati_n = len(s.get("bannati") or {})
    return {
        "inline_keyboard": [
            [
                {"text": "📊 Stato completo", "callback_data": "cb:stato"},
                {"text": "🔍 Permessi chat", "callback_data": "cb:qui"},
            ],
            [
                {"text": "⏸ Metti in pausa" if attivo else "▶️ Attiva bot", "callback_data": "cb:toggle"},
                {"text": "🖼 Foto: ON" if foto_on else "🖼 Foto: OFF", "callback_data": "cb:foto"},
            ],
            [
                {"text": "🧹 Servizio: ON" if servizio_on else "🧹 Servizio: OFF", "callback_data": "cb:servizio"},
                {"text": "📏 Regola ({}v/{}m)".format(regola.get("video", 10), regola.get("minuti", 10)), "callback_data": "cb:regola"},
            ],
            [
                {"text": "🚫 Bannati ({})".format(bannati_n), "callback_data": "cb:bannati"},
                {"text": "📤 Inoltra qui", "callback_data": "cb:inoltra"},
            ],
            [
                {"text": "🛑 Stop inoltro", "callback_data": "cb:stopinoltra"},
                {"text": "🔄 Recupera video", "callback_data": "cb:recupera"},
            ],
            [
                {"text": "⏹ Annulla recupero", "callback_data": "cb:annulla"},
            ],
        ]
    }


def invia_menu(chat_id, s: dict, message_id: int | None = None) -> None:
    testo = _testo_menu(s)
    markup = json.dumps(_markup_menu(s))
    if message_id:
        try:
            tg("editMessageText", chat_id=chat_id, message_id=message_id,
               text=testo, parse_mode="HTML", reply_markup=markup)
            return
        except Exception:
            pass
    tg("sendMessage", chat_id=chat_id, text=testo, parse_mode="HTML", reply_markup=markup)


def _e_admin(chat: dict, user_id, s: dict) -> bool:
    tipo = (chat or {}).get("type") or ""
    if tipo == "private":
        return True
    cid = str((chat or {}).get("id") or "")
    admins = (s.get("admin") or {}).get(cid)
    if not admins and cid:
        try:
            ris = tg("getChatAdministrators", chat_id=int(cid))
            admins = [
                (m.get("user") or {}).get("id") for m in ris.get("result", [])
                if (m.get("user") or {}).get("id")
            ]
            s.setdefault("admin", {})[cid] = admins
        except Exception:
            return True
    return not admins or user_id in admins


def gestisci_callback(cq: dict, s: dict) -> None:
    dati = cq.get("data") or ""
    msg = cq.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    mid = msg.get("message_id")
    uid = (cq.get("from") or {}).get("id")
    if not chat_id or not dati.startswith("cb:"):
        return
    if not _e_admin(chat, uid, s):
        try:
            tg("answerCallbackQuery", callback_query_id=cq.get("id"),
               text="Solo gli amministratori possono usare i bottoni del bot.", show_alert="true")
        except Exception:
            pass
        return
    try:
        tg("answerCallbackQuery", callback_query_id=cq.get("id"))
    except Exception:
        pass

    azione = dati[3:]
    if azione == "menu":
        invia_menu(chat_id, s, mid)
    elif azione == "toggle":
        s["attivo"] = not s.get("attivo", True)
        salva(s)
        invia_menu(chat_id, s, mid)
    elif azione == "foto":
        s["cancella_foto"] = not s.get("cancella_foto", True)
        salva(s)
        invia_menu(chat_id, s, mid)
    elif azione == "servizio":
        s["cancella_servizio"] = not s.get("cancella_servizio", True)
        salva(s)
        invia_menu(chat_id, s, mid)
    elif azione == "stato":
        tg("sendMessage", chat_id=chat_id,
           text="Doppioni cancellati: {}\nFoto cancellate: {}\nVideo in memoria: {}\nAttivo: {}\nGruppi in cui inoltro: {}".format(
               s["cancellati"], s.get("foto_cancellate", 0), len(s["visti"]),
               "sì ✅" if s["attivo"] else "no ⏸", len(s.get("inoltra_a") or [])))
    elif azione == "qui":
        comandi({"chat": chat, "text": "/qui"}, s)
    elif azione == "regola":
        regola = s.get("regola") or {"video": 10, "minuti": 10}
        tg("sendMessage", chat_id=chat_id,
           text="Regola attuale: {} video entro {} minuti.\nPer cambiarla scrivi: /regola <video> <minuti> (es. /regola 10 10)".format(
               regola.get("video", 10), regola.get("minuti", 10)))
    elif azione == "bannati":
        comandi({"chat": chat, "text": "/bannati"}, s)
    elif azione == "inoltra":
        comandi({"chat": chat, "text": "/inoltra"}, s)
    elif azione == "stopinoltra":
        comandi({"chat": chat, "text": "/stopinoltra"}, s)
    elif azione == "recupera":
        comandi({"chat": chat, "text": "/recupera"}, s)
    elif azione == "annulla":
        comandi({"chat": chat, "text": "/annulla"}, s)


def comandi(msg: dict, s: dict) -> None:
    testo = (msg.get("text") or "").strip()
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    is_privata = chat.get("type") == "private"
    low = testo.lower()

    if is_privata and not testo.startswith("/"):
        if "menu bottoni" in low:
            testo = "/menu"
        elif low.endswith("stato"):
            testo = "/stato"
        elif "attiva / disattiva" in low:
            testo = "/disattiva" if s.get("attivo", True) else "/attiva"
        elif "foto sì / no" in low or "foto si / no" in low:
            testo = "/foto"
        elif "bannati" in low:
            testo = "/bannati"
        elif low.endswith("aiuto"):
            testo = "/aiuto"

    if not testo.startswith("/"):
        return
    cmd = testo.split()[0].split("@")[0].lower()
    if cmd == "/menu":
        if is_privata:
            tg("sendMessage", chat_id=chat_id, text="Tastiera rapida aggiornata 👇",
               reply_markup=json.dumps(tastiera_privata()))
        invia_menu(chat_id, s)
        return
    if cmd in ("/start", "/aiuto", "/help"):
        registra_comandi()
        campi = {
            "chat_id": chat_id,
            "text": ("<b>Anti-doppioni</b>\nTengo il primo video che arriva e cancello le copie.\n\n"
                     "/menu — pannello comandi a bottoni\n"
                     "/stato — quanti doppioni ho cancellato\n"
                     "/foto — attiva/spegni cancellazione foto\n"
                     "/regola — cambia video/minuti per nuovi arrivati\n"
                     "/escludi — escludi un utente dal conteggio video\n"
                     "/bannati e /riammetti — gestisci utenti bloccati\n"
                     "/inoltra e /stopinoltra — gestisci inoltro video\n"
                     "/recupera e /annulla — ricopia i video già visti\n"
                     "/attiva e /disattiva — accendo o spengo\n"
                     "/qui — verifica permessi admin\n\n"
                     "Mi servono i permessi di amministratore con \'elimina messaggi\'."),
            "parse_mode": "HTML",
        }
        if is_privata:
            campi["reply_markup"] = json.dumps(tastiera_privata())
        else:
            campi["reply_markup"] = json.dumps(_markup_menu(s))
        tg("sendMessage", **campi)
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
    elif cmd in ("/escludi", "/esenta", "/salta", "/vip"):
        if not _e_admin(chat, (msg.get("from") or {}).get("id"), s):
            tg("sendMessage", chat_id=chat_id, text="Solo gli amministratori possono usare /escludi.")
            return
        pezzi = testo.split()
        uid = None
        label = ""
        if len(pezzi) > 1:
            solo = pezzi[1].lstrip("@").lower()
            if solo.isdigit():
                uid = int(solo)
                label = (s.get("nomi") or {}).get(str(uid), "")
            elif solo:
                label = solo
                for k, v in (s.get("nomi") or {}).items():
                    if v == solo:
                        uid = int(k)
                        break
        if uid is None:
            risposta = msg.get("reply_to_message") or {}
            u_rep = risposta.get("from") or {}
            uid = u_rep.get("id")
            if u_rep.get("username"):
                label = u_rep["username"].lower()
                s.setdefault("nomi", {})[str(uid)] = label
        if not uid:
            tg("sendMessage", chat_id=chat_id,
               text="Scrivimi /escludi rispondendo a un suo messaggio, oppure /escludi @nome o /escludi id.")
        else:
            s.setdefault("esclusi", {})[str(uid)] = label
            era_in_attesa = s.get("in_attesa", {}).pop(str(uid), None) is not None
            salva(s)
            extra = " (rimosso anche dal timer attuale)" if era_in_attesa else ""
            chi = "@" + label if label else str(uid)
            nota_ban = " — Nota: risulta nei bannati, usa /riammetti {} per sbloccarlo.".format(uid) if str(uid) in (s.get("bannati") or {}) else ""
            tg("sendMessage", chat_id=chat_id,
               text="🛡 {} escluso dal conteggio dei video ✅{}{}".format(chi, extra, nota_ban))
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


def cancella_servizio(msg: dict, s: dict) -> None:
    """Cancella subito i messaggi di servizio di Telegram (es. entrato/uscito/fissato)."""
    if not s.get("cancella_servizio", True):
        return
    chat = msg.get("chat") or {}
    if chat.get("type") not in ("group", "supergroup"):
        return
    if not e_servizio(msg):
        return
    chat_id = chat.get("id")
    mid = msg.get("message_id")
    if not chat_id or not mid:
        return
    try:
        tg("deleteMessage", chat_id=chat_id, message_id=mid)
        print("servizio cancellato in {} (msg {})".format(chat_id, mid))
    except urllib.error.HTTPError as e:
        print("servizio non cancellato: {}".format(e.code))
    except (urllib.error.URLError, OSError) as e:
        print("rete ballerina mentre cancellavo servizio: {}".format(e))


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
    registra_comandi()
    aggiorna_admin(s)
    while time.time() < fine:
        adesso = time.time()
        benv_vicino = any(
            adesso - float(v.get("t") or 0) >= BENVENUTO_AUTO_ELIMINA_SEC - 20
            for v in (s.get("benvenuti_da_cancellare") or [])
        )
        avviso_vicino = any(
            int(v.get("scartati", 0)) > int(v.get("inviati_scartati", 0))
            or (v.get("mid") and float(v.get("scadenza_del") or 0) - adesso <= 20)
            for v in (s.get("avvisi_doppioni") or {}).values()
        )
        attesa_poll = 2 if (
            s.get("coda_inoltro")
            or s.get("coda_cancellazioni")
            or s.get("recupero")
            or benv_vicino
            or avviso_vicino
        ) else 20
        try:
            ris = tg("getUpdates", offset=s["ultimo_update"] or None, timeout=attesa_poll)
        except (urllib.error.URLError, RuntimeError) as e:
            print("getUpdates: {}".format(e))
            time.sleep(5)
            continue
        for upd in ris.get("result", []):
            s["ultimo_update"] = upd["update_id"] + 1
            cq = upd.get("callback_query")
            if cq:
                try:
                    gestisci_callback(cq, s)
                except Exception as e:
                    print("callback saltato: {}".format(e))
                continue
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
                    cancella_servizio(msg, s)
                    conta_video(msg, s)
                    if s["attivo"]:
                        gestisci(msg, s)
                    comandi(msg, s)
                except Exception as e:  # un messaggio non deve fermare l'ascolto
                    print("salto un messaggio: {}".format(e))
        if len(ris.get("result", [])) < 50:
            controlla_scadenze(s)
        cancella_benvenuti_scaduti(s)
        processa_cancellazioni(s)
        processa_avvisi_doppioni(s)
        processa_inoltri(s)
        recupero(s)
        salva(s)
        time.sleep(0.5 if s.get("coda_inoltro") else 1)
    cancella_benvenuti_scaduti(s)
    processa_cancellazioni(s)
    processa_avvisi_doppioni(s)


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
