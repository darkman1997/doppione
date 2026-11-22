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


BENVENUTI_TRADOTTI = {
    "it": (
        "Benvenuto {nomi}! 🎬 Se non mandi {video} video entro {minuti} minuti ti caccio fuori!",
        "Benvenuti {nomi}! 🎬 Se non mandate {video} video entro {minuti} minuti vi caccio fuori!",
    ),
    "en": (
        "Welcome {nomi}! 🎬 If you don't send {video} videos within {minuti} minutes I will kick you out!",
        "Welcome {nomi}! 🎬 If you don't send {video} videos within {minuti} minutes I will kick you all out!",
    ),
    "es": (
        "¡Bienvenido {nomi}! 🎬 Si no envías {video} videos en {minuti} minutos te expulso del grupo!",
        "¡Bienvenidos {nomi}! 🎬 Si no envían {video} videos en {minuti} minutos los expulso del grupo!",
    ),
    "fr": (
        "Bienvenue {nomi} ! 🎬 Si tu n'envoies pas {video} vidéos dans les {minuti} minutes, je te bannis !",
        "Bienvenue {nomi} ! 🎬 Si vous n'envoyez pas {video} vidéos dans les {minuti} minutes, je vous bannis !",
    ),
    "de": (
        "Willkommen {nomi}! 🎬 Wenn du innerhalb von {minuti} Minuten nicht {video} Videos schickst, werfe ich dich raus!",
        "Willkommen {nomi}! 🎬 Wenn ihr innerhalb von {minuti} Minuten nicht {video} Videos schickt, werfe ich euch raus!",
    ),
    "pt": (
        "Bem-vindo {nomi}! 🎬 Se você não enviar {video} vídeos em até {minuti} minutos, vou te expulsar!",
        "Bem-vindos {nomi}! 🎬 Se vocês não enviarem {video} vídeos em até {minuti} minutos, vou expulsar vocês!",
    ),
    "ro": (
        "Bine ai venit {nomi}! 🎬 Dacă nu trimiți {video} videoclipuri în {minuti} minute, te dau afară!",
        "Bine ați venit {nomi}! 🎬 Dacă nu trimiteți {video} videoclipuri în {minuti} minute, vă dau afară!",
    ),
    "ru": (
        "Добро пожаловать, {nomi}! 🎬 Если не отправишь {video} видео в течение {minuti} минут, я тебя выгоню!",
        "Добро пожаловать, {nomi}! 🎬 Если не отправите {video} видео в течение {minuti} минут, я вас выгоню!",
    ),
    "uk": (
        "Ласкаво просимо, {nomi}! 🎬 Якщо не надішлеш {video} відео протягом {minuti} хвилин, я тебе вижену!",
        "Ласкаво просимо, {nomi}! 🎬 Якщо не надішлете {video} відео протягом {minuti} хвилин, я вас вижену!",
    ),
    "ar": (
        "أهلاً بك {nomi}! 🎬 إذا لم ترسل {video} فيديو خلال {minuti} دقيقة سأقوم بطردك!",
        "أهلاً بكم {nomi}! 🎬 إذا لم ترسلوا {video} فيديو خلال {minuti} دقيقة سأقوم بطردكم!",
    ),
    "tr": (
        "Hoş geldin {nomi}! 🎬 {minuti} dakika içinde {video} video göndermezsen seni gruptan atarım!",
        "Hoş geldiniz {nomi}! 🎬 {minuti} dakika içinde {video} video göndermezseniz sizi gruptan atarım!",
    ),
    "pl": (
        "Witaj {nomi}! 🎬 Jeśli nie wyślesz {video} filmów w ciągu {minuti} minut, wyrzucę cię z grupy!",
        "Witajcie {nomi}! 🎬 Jeśli nie wyślecie {video} filmów w ciągu {minuti} minut, wyrzucę was z grupy!",
    ),
    "nl": (
        "Welkom {nomi}! 🎬 Als je binnen {minuti} minuten geen {video} video's stuurt, zet ik je eruit!",
        "Welkom {nomi}! 🎬 Als jullie binnen {minuti} minuten geen {video} video's sturen, zet ik jullie eruit!",
    ),
    "sq": (
        "Mirë se erdhe {nomi}! 🎬 Nëse nuk dërgon {video} video brenda {minuti} minutash do të të nxjerr jashtë!",
        "Mirë se erdhët {nomi}! 🎬 Nëse nuk dërgoni {video} video brenda {minuti} minutash do t'ju nxjerr jashtë!",
    ),
    "el": (
        "Καλώς ήρθες {nomi}! 🎬 Αν δεν στείλεις {video} βίντεο μέσα σε {minuti} λεπτά θα σε διώξω!",
        "Καλώς ήρθατε {nomi}! 🎬 Αν δεν στείλετε {video} βίντεο μέσα σε {minuti} λεπτά θα σας διώξω!",
    ),
    "zh": (
        "欢迎 {nomi}！🎬 如果你在 {minuti} 分钟内不发送 {video} 个视频，我就会把你踢出群！",
        "欢迎 {nomi}！🎬 如果你们在 {minuti} 分钟内不发送 {video} 个视频，我就会把你们踢出群！",
    ),
    "ja": (
        "ようこそ {nomi}！🎬 {minuti}分以内に{video}本の動画を送らないと退出させます！",
        "ようこそ {nomi}！🎬 {minuti}分以内に{video}本の動画を送らないと退出させます！",
    ),
    "ko": (
        "환영합니다 {nomi}! 🎬 {minuti}분 안에 동영상 {video}개를 보내지 않으면 강퇴됩니다!",
        "환영합니다 {nomi}! 🎬 {minuti}분 안에 동영상 {video}개를 보내지 않으면 강퇴됩니다!",
    ),
    "hi": (
        "स्वागत है {nomi}! 🎬 अगर आप {minuti} मिनट में {video} वीडियो नहीं भेजते हैं तो मैं आपको बाहर निकाल दूंगा!",
        "स्वागत है {nomi}! 🎬 अगर आप {minuti} मिनट में {video} वीडियो नहीं भेजते हैं तो मैं आप सभी को बाहर निकाल दूंगा!",
    ),
    "id": (
        "Selamat datang {nomi}! 🎬 Jika kamu tidak mengirim {video} video dalam {minuti} menit, kamu akan dikeluarkan!",
        "Selamat datang {nomi}! 🎬 Jika kalian tidak mengirim {video} video dalam {minuti} menit, kalian akan dikeluarkan!",
    ),
    "fa": (
        "خوش آمدی {nomi}! 🎬 اگر تا {minuti} دقیقه {video} ویدیو نفرستی اخراج می‌شوی!",
        "خوش آمدید {nomi}! 🎬 اگر تا {minuti} دقیقه {video} ویدیو نفرستید اخراج می‌شوید!",
    ),
    "bg": (
        "Добре дошъл {nomi}! 🎬 Ако не изпратиш {video} видеа до {minuti} минути, ще те изгоня!",
        "Добре дошли {nomi}! 🎬 Ако не изпратите {video} видеа до {minuti} минути, ще ви изгоня!",
    ),
    "cs": (
        "Vítej {nomi}! 🎬 Pokud do {minuti} minut nepošleš {video} videí, vyhodím tě!",
        "Vítejte {nomi}! 🎬 Pokud do {minuti} minut nepošlete {video} videí, vyhodím vás!",
    ),
    "hu": (
        "Üdv {nomi}! 🎬 Ha {minuti} percen belül nem küldesz {video} videót, kirúglak!",
        "Üdv {nomi}! 🎬 Ha {minuti} percen belül nem küldtök {video} videót, kirúglak titeket!",
    ),
    "sv": (
        "Välkommen {nomi}! 🎬 Om du inte skickar {video} videor inom {minuti} minuter slänger jag ut dig!",
        "Välkomna {nomi}! 🎬 Om ni inte skickar {video} videor inom {minuti} minuter slänger jag ut er!",
    ),
}
def lingua_utente(u: dict) -> str:
    codice = (u.get("language_code") or "").strip().lower()
    if codice:
        base = codice.replace("_", "-").split("-")[0]
        if base in BENVENUTI_TRADOTTI:
            return base
    return "en"


def messaggio_benvenuto(lang: str, nomi: str, video: int, minuti: int, plurale: bool) -> str:
    idx = 1 if plurale else 0
    modelli = BENVENUTI_TRADOTTI.get(lang) or BENVENUTI_TRADOTTI["en"]
    return modelli[idx].format(nomi=nomi, video=video, minuti=minuti)


def benvenuto(msg: dict, s: dict) -> None:
    """Saluta i nuovi arrivati nella loro lingua e avvia il conto alla rovescia."""
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
    per_lingua: dict[str, list[dict]] = {}
    for u in nuovi:
        lang = lingua_utente(u)
        s["in_attesa"][str(u.get("id"))] = {"t": t_ingresso, "video": 0, "chat": chat_id, "lang": lang}
        per_lingua.setdefault(lang, []).append(u)

    for lang, utenti_lang in per_lingua.items():
        if len(utenti_lang) == 1:
            u = utenti_lang[0]
            _invia(messaggio_benvenuto(lang, menzione(u), video, minuti, plurale=False), chat_id)
            print("benvenuto a {} (lingua {})".format(u.get("id"), lang))
            continue
        # entrati in tanti con la stessa lingua: un messaggio ogni 20 persone
        for i in range(0, len(utenti_lang), 20):
            gruppo = utenti_lang[i:i + 20]
            nomi = ", ".join(menzione(u) for u in gruppo)
            _invia(messaggio_benvenuto(lang, nomi, video, minuti, plurale=True), chat_id)
            time.sleep(1)
            print("benvenuto a {} nuovi (lingua {})".format(len(gruppo), lang))


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
    return (
        "<b>🎛 Pannello Anti-doppioni</b>\n"
        "Stato: <b>{}</b> · Foto non-admin: <b>{}</b>\n"
        "Regola nuovi: <b>{} video / {} min</b> · Doppioni tolti: <b>{}</b>"
    ).format(
        "attivo ✅" if s.get("attivo", True) else "spento ⏸",
        "cancellate ✅" if foto_on else "consentite ⏸",
        regola.get("video", 10),
        regola.get("minuti", 10),
        s.get("cancellati", 0),
    )


def _markup_menu(s: dict) -> dict:
    attivo = s.get("attivo", True)
    foto_on = s.get("cancella_foto", True)
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
                {"text": "📏 Regola ({}v/{}m)".format(regola.get("video", 10), regola.get("minuti", 10)), "callback_data": "cb:regola"},
                {"text": "🚫 Bannati ({})".format(bannati_n), "callback_data": "cb:bannati"},
            ],
            [
                {"text": "📤 Inoltra qui", "callback_data": "cb:inoltra"},
                {"text": "🛑 Stop inoltro", "callback_data": "cb:stopinoltra"},
            ],
            [
                {"text": "🔄 Recupera video", "callback_data": "cb:recupera"},
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
        try:
            ris = tg("getUpdates", offset=s["ultimo_update"] or None, timeout=20)
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
                    conta_video(msg, s)
                    if s["attivo"]:
                        gestisci(msg, s)
                    comandi(msg, s)
                except Exception as e:  # un messaggio non deve fermare l'ascolto
                    print("salto un messaggio: {}".format(e))
        if len(ris.get("result", [])) < 50:
            controlla_scadenze(s)
        processa_inoltri(s)
        recupero(s)
        salva(s)
        time.sleep(0.5 if s.get("coda_inoltro") else 1)


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
