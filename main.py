#!/usr/bin/env python3
"""
GR0UT — Global Map battle notifier.

Scanne la Carte Globale (World of Tanks, cluster EU) via l'API publique
Wargaming et poste un message Discord ~1h avant chaque bataille impliquant
le clan, avec la map jouée et l'heure de début.

Aucune dépendance lourde : requests + stdlib.
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

# --- Configuration (via variables d'environnement / secrets) ---------------

APP_ID = os.environ.get("WG_APP_ID", "").strip()
CLAN_ID = int(os.environ.get("WG_CLAN_ID", "500165786"))          # GR0UT par défaut
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

# Rôle Discord à ping à H-1 (id du rôle). Vide -> @here.
CW_ROLE_ID = os.environ.get("CW_ROLE_ID", "").strip()

API_BASE = os.environ.get("WG_API_BASE", "https://api.worldoftanks.eu")
# L'API renvoie les heures de bataille en UTC (heure naïve) ; on les convertit
# pour l'affichage. Ne pas confondre les deux (sinon décalage de 1-2h).
API_TZ = ZoneInfo(os.environ.get("API_TZ_NAME", "UTC"))
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Paris"))

# Fenêtre de notification : on prévient quand la bataille démarre dans
# [LEAD_MIN ; LEAD_MAX] minutes. Défaut ≈ 1h avant.
LEAD_MIN = int(os.environ.get("LEAD_MIN", "45"))
LEAD_MAX = int(os.environ.get("LEAD_MAX", "75"))

STATE_FILE = os.environ.get("STATE_FILE", "state.json")
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "gr0ut-globalmap-bot/1.0"})

# CDN images de maps Wargaming. Le nom de fichier vaut l'arena_id pour la
# plupart des maps ; quelques-unes utilisent un autre nom -> overrides ci-dessous.
MAP_CDN = os.environ.get("MAP_CDN", "https://eu-wotp.wgcdn.co/dcont/fb/image")
MAP_IMAGE_OVERRIDES = {
    "34_redshire": f"{MAP_CDN}/redshire.png",
    "23_westfeld": f"{MAP_CDN}/westfield.png",
}
_image_cache = {}


def map_image_url(arena_id):
    """URL d'image de la map, vérifiée en ligne. None si indisponible."""
    if not arena_id:
        return None
    if arena_id in _image_cache:
        return _image_cache[arena_id]
    url = MAP_IMAGE_OVERRIDES.get(arena_id, f"{MAP_CDN}/{arena_id}.png")
    resolved = None
    try:
        r = SESSION.get(url, timeout=10, stream=True)
        if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
            resolved = url
        r.close()
    except requests.RequestException:
        resolved = None
    _image_cache[arena_id] = resolved
    return resolved


# --- Petits helpers API ------------------------------------------------------

TRANSIENT = {"SOURCE_NOT_AVAILABLE", "REQUEST_LIMIT_EXCEEDED"}


def api_get(path, _retries=3, **params):
    """Appel GET à l'API WG, avec retries sur erreurs transitoires."""
    params["application_id"] = APP_ID
    url = f"{API_BASE}/{path.strip('/')}/"
    last = None
    for attempt in range(_retries):
        try:
            resp = SESSION.get(url, params=params, timeout=20)
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("status") == "ok":
                return payload["data"]
            err = payload.get("error") or {}
            last = RuntimeError(f"API error on {path}: {err}")
            if err.get("message") not in TRANSIENT:
                raise last
        except requests.RequestException as exc:
            last = exc
        time.sleep(2 * (attempt + 1))
    raise last


def active_eu_fronts():
    """Liste des front_id actifs sur le cluster EU."""
    fronts = api_get("wot/globalmap/fronts")
    return [f["front_id"] for f in fronts if f.get("is_active")]


def iter_front_provinces(front_id, page_size=100):
    """Itère toutes les provinces d'un front (pagination)."""
    page = 1
    while True:
        data = api_get(
            "wot/globalmap/provinces",
            front_id=front_id,
            limit=page_size,
            page_no=page,
        )
        if not data:
            break
        yield from data
        if len(data) < page_size:
            break
        page += 1


def resolve_clan_tags(clan_ids):
    """Résout des clan_id -> tag (une seule requête, par lots de 100)."""
    tags = {}
    ids = [c for c in {int(c) for c in clan_ids if c} if c]
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        data = api_get(
            "wgn/clans/info",
            clan_id=",".join(map(str, chunk)),
            fields="tag,name",
        )
        for cid, info in (data or {}).items():
            if info:
                tags[int(cid)] = info.get("tag") or info.get("name") or cid
    return tags


# --- Logique métier ----------------------------------------------------------

# Type de bataille renvoyé par clanbattles -> libellé.
ROLE_FROM_TYPE = {"attack": "Attaque", "defense": "Défense"}


def province_arenas(battles_raw):
    """{province_id: (arena_id, arena_name)} pour les provinces des batailles."""
    by_front = {}
    for b in battles_raw:
        by_front.setdefault(b["front_id"], set()).add(b["province_id"])
    arena = {}
    for front_id, pids in by_front.items():
        data = api_get("wot/globalmap/provinces", front_id=front_id,
                       province_id=",".join(pids), limit=100)
        for p in (data or []):
            arena[p["province_id"]] = (p.get("arena_id"), p.get("arena_name"))
    return arena


def collect_upcoming_battles(clan_id):
    """
    Batailles RÉELLEMENT programmées par le clan (division posée), via
    l'endpoint clanbattles. On ignore donc les provinces où le clan est
    seulement "impliqué" sans division (ex. défense non défendue).
    """
    raw = api_get("wot/globalmap/clanbattles", clan_id=clan_id)
    if not raw:
        return []
    arena = province_arenas(raw)
    battles = []
    for b in raw:
        aid, aname = arena.get(b["province_id"], (None, None))
        battles.append({
            "front_id": b.get("front_id"),
            "province_id": b.get("province_id"),
            "province_name": b.get("province_name"),
            "arena_id": aid,
            "arena_name": aname or b.get("province_name"),
            # clanbattles fournit un timestamp unix -> aucun souci de fuseau
            "start": datetime.fromtimestamp(b["time"], TZ),
            "role": ROLE_FROM_TYPE.get(b.get("type"), "Bataille"),
            "opponents": [b["competitor_id"]] if b.get("competitor_id") else [],
        })
    return battles


# --- Provinces possédées (gains / pertes) ------------------------------------

def owned_provinces(clan_id):
    """{province_id: {name, arena}} des provinces actuellement détenues."""
    owned = {}
    for front_id in active_eu_fronts():
        data = api_get("wot/globalmap/clanprovinces",
                       clan_id=clan_id, front_id=front_id)
        for p in (data.get(str(clan_id)) or []):
            owned[p["province_id"]] = {
                "name": p.get("province_name"),
                "arena": p.get("arena_name"),
            }
    return owned


def check_province_changes(state):
    """Compare les provinces détenues au snapshot précédent, annonce gains/pertes."""
    current = owned_provinces(CLAN_ID)
    prev = state.get("owned")
    state["owned"] = current

    if prev is None:
        print("provinces: snapshot initial enregistré.")
        return True  # à persister, pas d'annonce au 1er passage

    prev_ids, cur_ids = set(prev), set(current)
    gained, lost = cur_ids - prev_ids, prev_ids - cur_ids

    for pid in sorted(gained):
        p = current[pid]
        _send({"content": f"🏆 **GR0UT a pris {p['name']}** ({p['arena']}) ! 🎉"})
    for pid in sorted(lost):
        p = prev[pid]
        _send({"content": f"💔 GR0UT a perdu **{p['name']}** ({p.get('arena')})."})

    if gained or lost:
        print(f"provinces: +{len(gained)} / -{len(lost)}.")
    return bool(gained or lost)


# --- État (anti-doublon) -----------------------------------------------------

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"notified": []}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)


# --- Discord -----------------------------------------------------------------

ROLE_EMOJI = {"Attaque": "⚔️", "Défense": "🛡️", "Débarquement": "🪂"}
ROLE_COLOR = {"Attaque": 0xE74C3C, "Défense": 0x3498DB, "Débarquement": 0x9B59B6}

JOURS_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS_FR = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
           "août", "septembre", "octobre", "novembre", "décembre"]


def date_fr(dt):
    """'samedi 4 juillet 2026 à 19h15'."""
    return (f"{JOURS_FR[dt.weekday()]} {dt.day} {MOIS_FR[dt.month - 1]} "
            f"{dt.year} à {dt:%Hh%M}")


def build_embed(battle, tags):
    """Un embed par bataille : titre = map, vignette = image de la map."""
    emoji = ROLE_EMOJI.get(battle["role"], "•")
    opp = " / ".join(tags.get(int(o), str(o)) for o in battle["opponents"]) or "—"
    ts = int(battle["start"].timestamp())  # pour les timestamps dynamiques Discord
    embed = {
        "title": f"{emoji} {battle['arena_name']}",
        "description": (
            f"**{battle['province_name']}** — {battle['role']} vs **{opp}**\n"
            f"🗓️ **{date_fr(battle['start'])}**\n"
            f"⏳ <t:{ts}:R>"
        ),
        "color": ROLE_COLOR.get(battle["role"], 0x2ECC71),
        "footer": {"text": "GR0UT • Carte Globale • données API Wargaming"},
    }
    img = map_image_url(battle["arena_id"])
    if img:
        embed["thumbnail"] = {"url": img}
    return embed


def _send(body):
    if DRY_RUN or not WEBHOOK_URL:
        print("[DRY-RUN] Discord payload:")
        print(json.dumps(body, ensure_ascii=False, indent=2))
        return
    resp = SESSION.post(WEBHOOK_URL, json=body, timeout=20)
    resp.raise_for_status()


def post_discord(slot_start, battles, tags):
    """Poste les batailles d'un créneau (un embed par bataille, max 10/message)."""
    heure = slot_start.strftime("%Hh%M")
    embeds = [build_embed(b, tags)
              for b in sorted(battles, key=lambda x: x["arena_name"] or "")]

    if CW_ROLE_ID:
        ping = f"<@&{CW_ROLE_ID}>"
        mentions = {"roles": [CW_ROLE_ID]}
    else:
        ping = "@here"
        mentions = {"parse": ["everyone"]}
    content = f"{ping} 🎯 **{len(battles)} bataille(s)** ce soir à **{heure}** — présentez-vous !"

    # Discord limite à 10 embeds par message -> on découpe si besoin.
    for i in range(0, len(embeds), 10):
        _send({
            "content": content if i == 0 else "",
            "embeds": embeds[i:i + 10],
            "allowed_mentions": mentions if i == 0 else {"parse": []},
        })


# --- Entrée ------------------------------------------------------------------

def main():
    if not APP_ID:
        sys.exit("WG_APP_ID manquant (secret).")

    now = datetime.now(TZ)
    state = load_state()

    # La Carte Globale a des fenêtres d'indispo (SOURCE_NOT_AVAILABLE) : on ne
    # fait pas planter le run, on saute simplement ce cycle.
    try:
        # 1) Provinces gagnées / perdues (vérifié à chaque run).
        changed = check_province_changes(state)

        # 2) Notifications des batailles à venir (~1h avant).
        battles = collect_upcoming_battles(CLAN_ID)
    except RuntimeError as exc:
        print(f"[warn] Carte Globale indisponible, cycle ignoré : {exc}")
        return

    lo, hi = now + timedelta(minutes=LEAD_MIN), now + timedelta(minutes=LEAD_MAX)
    due = [b for b in battles if lo <= b["start"] <= hi]
    print(f"{now:%Y-%m-%d %H:%M %Z} — {len(battles)} bataille(s) GR0UT, "
          f"{len(due)} dans la fenêtre {LEAD_MIN}-{LEAD_MAX} min.")

    if due:
        notified = set(state.get("notified", []))
        tags = resolve_clan_tags({o for b in due for o in b["opponents"]})
        slots = {}
        for b in due:
            slots.setdefault(b["start"], []).append(b)
        for start, group in sorted(slots.items()):
            key = f"{start.date()}#{start:%H:%M}"
            if key in notified:
                continue
            post_discord(start, group, tags)
            notified.add(key)
            changed = True
            print(f"  → notifié créneau {key} ({len(group)} bataille(s)).")
        # On ne garde que les clés récentes (7 jours).
        cutoff = (now.date() - timedelta(days=7)).isoformat()
        state["notified"] = sorted(k for k in notified if k.split("#")[0] >= cutoff)

    if changed:
        save_state(state)


if __name__ == "__main__":
    main()
